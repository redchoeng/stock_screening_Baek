"""
백 프레임 스크리너 실행 + 대시보드용 JSON export.

기존 export_dashboard.py(반등 스크리너)와 완전히 분리된 파이프라인이다. 결과는
docs/baek_data.json으로 나가고, 사이트의 "백 프레임" 탭이 이걸 읽는다.

종목당 yfinance 호출이 2~3초라 유니버스를 config.baek.kr_limit / us_limit으로 제한한다.
재무는 분기 단위로만 바뀌므로 cache/fundamentals/ 아래 1주일 캐시가 걸려 있어서,
두 번째 실행부터는 거의 즉시 끝난다.

사용법:
    python export_baek.py                # 캐시 사용
    python export_baek.py --no-cache     # 재무 데이터 강제 재수집
    python export_baek.py --kr 50 --us 40
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import requests

from baek_scoring import build_market_context, max_possible_score, score_stock
from config import load_config
from fundamentals import fetch_fundamentals
from macro import fetch_macro
from universe import _normalize_us_ticker, build_universe

logger = logging.getLogger(__name__)

OUT_FILE = Path(__file__).parent / "output" / "baek_data.json"
SITE_FILE = Path(__file__).parent / "docs" / "baek_data.json"

_KR_LISTING_CACHE = Path(__file__).parent / "cache" / "meta" / "kr_listing.csv"
SP500_WEIGHT_URL = "https://www.slickcharts.com/sp500"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stock_screener"}


def _us_size_rank() -> dict[str, float]:
    """slickcharts S&P500 표는 시총 가중 비중 순이라 사실상 시총 랭킹이다."""
    resp = requests.get(SP500_WEIGHT_URL, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    table = next(t for t in tables if "Symbol" in t.columns)
    weight_col = next(c for c in table.columns if "weight" in str(c).lower())
    ranks: dict[str, float] = {}
    for _, r in table.iterrows():
        try:
            ranks[_normalize_us_ticker(str(r["Symbol"]))] = float(str(r[weight_col]).replace("%", ""))
        except (TypeError, ValueError):
            continue
    return ranks


def _kr_size_rank() -> dict[str, float]:
    """국내 시총 랭킹. 토스(발행주식수 × 현재가)를 우선 쓰고, 없으면 FDR 상장목록 캐시."""
    import kr_source

    table = kr_source.cap_table()
    if table is not None and "Marcap" in table.columns:
        return {str(c): float(m) for c, m in zip(table["Code"], table["Marcap"]) if pd.notna(m)}
    listing = pd.read_csv(_KR_LISTING_CACHE, dtype={"Code": str})
    return {str(c): float(m) for c, m in zip(listing["Code"], listing["Marcap"]) if pd.notna(m)}


def rank_by_size(rows: list[dict], market: str) -> list[dict]:
    """유니버스를 시가총액 큰 순으로 재정렬한다.

    universe.py의 미국 유니버스는 티커 알파벳순이라 앞에서 N개를 자르면 A~B로만 채워진다.
    백 프레임은 종목당 재무 호출이 비싸 유니버스를 잘라 쓸 수밖에 없으므로, 자르기 전에
    시총 순으로 세운다. 순위 소스를 못 얻으면 원래 순서를 그대로 둔다(중단하지 않는다).
    """
    try:
        ranks = _us_size_rank() if market == "US" else _kr_size_rank()
    except Exception:
        logger.warning("%s 시총 순위 소스를 못 얻어 원래 순서를 유지한다", market, exc_info=True)
        return rows
    if not ranks:
        return rows
    missing = sum(1 for r in rows if r["ticker"] not in ranks)
    if missing:
        logger.info("%s 시총 순위 누락 %d종목은 뒤로 밀린다", market, missing)
    return sorted(rows, key=lambda r: ranks.get(r["ticker"], -1.0), reverse=True)


def fetch_index_valuation() -> dict:
    """시장 레벨 밸류에이션. 국내는 pykrx 지수 PER 시계열로 자기 밴드 백분위까지 낸다.

    백 센터장이 인용한 '한국 8~10배', 'S&P 5년 19.9배 / 10년 18.9배'는 방송 시점 수치이므로
    코드에 박지 않고, 실제 지수 PER을 받아 밴드 안에서 지금 어디인지를 계산한다.
    """
    result: dict = {}
    try:
        from pykrx import stock

        end = pd.Timestamp.today()
        start = end - pd.DateOffset(years=5)
        df = stock.get_index_fundamental_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1028"  # KOSPI200
        )
        if df is not None and not df.empty and "PER" in df.columns:
            per = pd.to_numeric(df["PER"], errors="coerce")
            per = per[per > 0].dropna()
            if len(per) >= 60:
                now = float(per.iloc[-1])
                result["KR"] = {
                    "label": "KOSPI200",
                    "per": round(now, 2),
                    "percentile": round(float((per <= now).mean() * 100), 1),
                    "band_low": round(float(per.min()), 2),
                    "band_high": round(float(per.max()), 2),
                    "source": "pykrx 지수 PER 5년",
                }
    except Exception:
        # 토스증권 API는 지수 PER을 제공하지 않고, pykrx 경로는 KRX 로그인이 있어야 한다.
        # 없으면 지수 밴드 카드만 빠지고 시장 PER 중앙값은 그대로 나온다.
        logger.info("KOSPI200 지수 PER을 받지 못했다 (KRX 로그인 필요) — 지수 밴드는 생략한다")
    return result


def run_baek(cfg, kr_limit: int, us_limit: int, use_cache: bool = True) -> dict:
    macro = fetch_macro(cfg.baek.macro, use_cache=use_cache)
    logger.info("매크로 레짐: %s (곱수 %.2f)", macro["regime_label"], macro["multiplier"])

    rows = (
        rank_by_size(build_universe(cfg, markets=("KR",)), "KR")[:kr_limit]
        + rank_by_size(build_universe(cfg, markets=("US",)), "US")[:us_limit]
    )
    logger.info("백 프레임 유니버스 %d개 종목 (시총 상위 기준)", len(rows))

    fundamentals: list[dict | None] = []
    for i, row in enumerate(rows, start=1):
        if i % 20 == 0 or i == len(rows):
            logger.info("펀더멘털 수집 %d/%d", i, len(rows))
        fundamentals.append(
            fetch_fundamentals(row["ticker"], row["market"], cfg.baek.fundamental, use_cache=use_cache)
        )

    got = sum(1 for f in fundamentals if f and f.get("has_growth_data"))
    logger.info("재무 데이터 확보 %d/%d", got, len(rows))

    market_ctx = build_market_context([f for f in fundamentals if f], cfg.baek)
    results = [
        score_stock(row, fund, macro, market_ctx, cfg.baek)
        for row, fund in zip(rows, fundamentals)
    ]
    results.sort(key=lambda r: r["score"], reverse=True)

    summary = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(results),
        "kr_count": sum(1 for r in results if r["market"] == "KR"),
        "us_count": sum(1 for r in results if r["market"] == "US"),
        "candidate_count": sum(1 for r in results if r["verdict"] == "후보"),
        "watch_count": sum(1 for r in results if r["verdict"] == "관찰"),
        "excluded_deteriorating": sum(1 for r in results if r.get("exclude_reason") == "매출·영업이익 동반 감소"),
        "excluded_no_data": sum(1 for r in results if r.get("exclude_reason") == "재무 데이터 부족"),
        "momentum_peaked": sum(1 for r in results if "momentum_peaked" in r["flags"]),
        "margin_squeeze": sum(1 for r in results if "margin_squeeze" in r["flags"]),
        "max_score": max_possible_score(cfg.baek),
        # 판정선은 config에서 바뀔 수 있으므로 대시보드가 문자열로 박아 쓰지 않도록 함께 내린다.
        "candidate_score": cfg.baek.candidate_score,
        "watch_score": cfg.baek.watch_score,
    }

    return {
        "summary": summary,
        "macro": macro,
        "market_context": market_ctx,
        "index_valuation": fetch_index_valuation(),
        "weights": {
            "revenue_growth": cfg.baek.weights.revenue_growth,
            "growth_acceleration": cfg.baek.weights.growth_acceleration,
            "margin_improvement": cfg.baek.weights.margin_improvement,
            "valuation_band": cfg.baek.weights.valuation_band,
            "roe_regime_axis": cfg.baek.weights.roe_regime_axis,
        },
        "results": results,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="백찬규 프레임 스크리너")
    parser.add_argument("--kr", type=int, default=None, help="국내 종목 수 (기본 config.baek.kr_limit)")
    parser.add_argument("--us", type=int, default=None, help="미국 종목 수 (기본 config.baek.us_limit)")
    parser.add_argument("--no-cache", action="store_true", help="재무/매크로 캐시 무시하고 재수집")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("__main__", "export_baek", "macro", "fundamentals", "baek_scoring", "universe"):
        logging.getLogger(name).setLevel(logging.INFO)

    cfg = load_config()
    payload = run_baek(
        cfg,
        kr_limit=args.kr if args.kr is not None else cfg.baek.kr_limit,
        us_limit=args.us if args.us is not None else cfg.baek.us_limit,
        use_cache=not args.no_cache,
    )

    text = json.dumps(payload, ensure_ascii=False)
    for path in (OUT_FILE, SITE_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("저장 완료: %s", path)

    s = payload["summary"]
    logger.info(
        "레짐 %s | 후보 %d · 관찰 %d · 실적훼손 제외 %d · 데이터부족 %d (전체 %d)",
        payload["macro"]["regime_label"],
        s["candidate_count"], s["watch_count"],
        s["excluded_deteriorating"], s["excluded_no_data"], s["total"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
