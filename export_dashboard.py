"""
HTML 대시보드 시각화용 데이터 export.

main.py의 스크리닝 로직(run)을 그대로 재사용하되, KR/US 유니버스 크기를 각각 따로 정해서
균형 있게 섞고, 점수 상위 종목들은 최근 지표 시계열(종가/스토캐/윌리엄스%R)까지 추가로 뽑아
JSON 하나로 저장한다. 이 JSON을 별도의 정적 HTML(artifact)에 그대로 임베드해서 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from config import load_config
from data_fetcher import fetch_ohlcv, to_weekly
from indicators.stochastic import compute_stochastic_slow
from indicators.williams_r import compute_williams_r
from main import run
from universe import build_universe

logger = logging.getLogger(__name__)

OUT_FILE = Path(__file__).parent / "output" / "dashboard_data.json"


def build_balanced_universe(cfg, kr_limit: int = 200, us_limit: int = 150) -> list[dict]:
    kr_rows = build_universe(cfg, markets=("KR",))[:kr_limit]
    us_rows = build_universe(cfg, markets=("US",))[:us_limit]
    return kr_rows + us_rows


def build_series(ticker: str, market: str, cfg, lookback: int = 90) -> list[dict]:
    """cfg.core_timeframe과 같은 봉으로 시계열을 뽑는다 — 실제 AND 신호를 판정한 봉과
    대시보드 차트에 그려지는 봉이 다르면(예: 신호는 주봉 기준인데 차트는 일봉으로 그려서
    그 순간 %K/%D가 20 밑이 아닌 것처럼 보이는 등) 오해를 부른다."""
    df = fetch_ohlcv(ticker, market, days=cfg.universe.history_days)
    if df.empty or len(df) < 20:
        return []
    if cfg.core_timeframe == "weekly":
        df = to_weekly(df)
        if df.empty or len(df) < 20:
            return []
    stoch = compute_stochastic_slow(
        df, cfg.stochastic.k_period, cfg.stochastic.slowing_period, cfg.stochastic.d_period
    )
    wr = compute_williams_r(df, cfg.williams_r.period)

    out = []
    for date in df.tail(lookback).index:
        k, d, w = stoch.loc[date, "slow_k"], stoch.loc[date, "slow_d"], wr.loc[date]
        out.append({
            "date": date.strftime("%Y-%m-%d"),
            "close": round(float(df.loc[date, "Close"]), 2),
            "slow_k": None if pd.isna(k) else round(float(k), 1),
            "slow_d": None if pd.isna(d) else round(float(d), 1),
            "williams_r": None if pd.isna(w) else round(float(w), 1),
        })
    return out


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("__main__", "main", "universe", "data_fetcher", "scoring", "output", "export_dashboard"):
        logging.getLogger(name).setLevel(logging.INFO)

    cfg = load_config()
    rows = build_balanced_universe(cfg)
    logger.info("균형 유니버스 %d개 (KR/US) 확보, 스크리닝 시작", len(rows))

    results = run(markets=None, limit=None, cache_hours=24.0, cfg=cfg, universe_rows=rows)
    logger.info("스크리닝 완료: %d개 종목", len(results))

    results_sorted = sorted(results, key=lambda r: r["score"], reverse=True)
    top_rebound = [r for r in results_sorted if r["is_rebound_candidate"]][:15]
    top_warning = [r for r in results_sorted if r["is_top_warning"]][:5]

    series = {}
    for r in top_rebound + top_warning:
        key = f"{r['market']}:{r['ticker']}"
        series[key] = build_series(r["ticker"], r["market"], cfg)
    logger.info("시계열 %d개 종목 추가 수집", len(series))

    for r in results:
        r["last_date"] = str(r["last_date"])[:10]

    summary = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "core_timeframe": cfg.core_timeframe,
        "total": len(results),
        "rebound_count": sum(1 for r in results if r["is_rebound_candidate"]),
        "warning_count": sum(1 for r in results if r["is_top_warning"]),
        "kr_count": sum(1 for r in results if r["market"] == "KR"),
        "us_count": sum(1 for r in results if r["market"] == "US"),
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps({"summary": summary, "results": results, "series": series}, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("저장 완료: %s", OUT_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
