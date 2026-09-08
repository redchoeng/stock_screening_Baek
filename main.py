"""
기술적 지표 기반 과매도 반등 후보 스크리너 — 실행 진입점.

사용 예:
    python main.py                              # KR+US 전체 유니버스 스크리닝
    python main.py --markets KR                 # 국내만
    python main.py --markets US --limit 50       # 미국만, 티커 50개로 축소(테스트용)
    python main.py --no-charts                   # 차트 생성 생략(속도 우선)
    python main.py --top-n-charts 5               # 점수 상위 5종목만 차트 생성 (기본값)

*** 반드시 읽어야 할 주의사항 (요청서 5번 반영) ***
- 이 프로그램의 점수는 "현재 과매도/과매수 상태를 여러 지표로 교차 확인"한 결과이며,
  미래 수익을 보장하지 않는다. 매매 판단의 유일한 근거로 쓰지 말 것.
- 스토캐스틱 슬로우 + 윌리엄스 %R이 동시에 과매도일 때만 핵심 신호로 인정한다(AND 조건).
  단일 지표만 뜨는 경우는 애초에 반등 후보 목록에 들어오지 않는다.
- 이동평균 골든/데드크로스는 이 프로그램의 신호 판정에 전혀 관여하지 않는다.
- 지수/대형주일수록 신뢰도가 높다 — 결과의 credibility_tier 컬럼을 반드시 함께 볼 것.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import load_config
from data_fetcher import fetch_investor_net_buy_series, fetch_market_cap, fetch_ohlcv, to_weekly
from output import save_results
from scoring import score_ticker
from universe import build_universe, classify_credibility_tier
from visualize import plot_ticker

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
CHART_DIR = OUTPUT_DIR / "charts"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="기술적 지표 기반 과매도 반등 후보 스크리너")
    p.add_argument("--markets", default="KR,US", help="스크리닝할 시장 (콤마 구분, 예: KR,US)")
    p.add_argument("--limit", type=int, default=None, help="테스트용 티커 수 제한")
    p.add_argument("--no-charts", action="store_true", help="차트 생성 생략")
    p.add_argument("--top-n-charts", type=int, default=10, help="점수 상위 몇 종목까지 차트 생성할지")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR), help="결과 저장 디렉터리")
    p.add_argument("--cache-hours", type=float, default=24.0, help="OHLCV 캐시 유효 시간(시간 단위)")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")
    return p.parse_args()


def run(
    markets: list[str] | None,
    limit: int | None,
    cache_hours: float,
    cfg,
    universe_rows: list[dict] | None = None,
) -> list[dict]:
    """universe_rows를 직접 넘기면 그걸 그대로 쓴다 (예: export_dashboard.py처럼 시장별로
    개수를 따로 조절한 유니버스를 쓰고 싶을 때). 안 넘기면 markets/limit으로 새로 만든다."""
    if universe_rows is None:
        universe_rows = build_universe(cfg, markets=tuple(markets))
        if limit:
            universe_rows = universe_rows[:limit]
    logger.info("유니버스 %d개 종목 스크리닝 시작 (markets=%s)", len(universe_rows), markets)

    results: list[dict] = []
    for i, row in enumerate(universe_rows, start=1):
        ticker, name, market = row["ticker"], row["name"], row["market"]
        if i % 25 == 0 or i == len(universe_rows):
            logger.info("진행률 %d/%d (%s)", i, len(universe_rows), ticker)

        try:
            df_daily = fetch_ohlcv(ticker, market, days=cfg.universe.history_days, cache_max_age_hours=cache_hours)
            if df_daily.empty:
                continue

            market_cap = fetch_market_cap(ticker, market)

            # 소형주 제외는 유니버스 단계에서 미리 걸러야 불필요한 지표 계산을 안 한다
            if cfg.universe.exclude_small_cap:
                tier_preview = classify_credibility_tier(
                    market_cap, market, is_index_member=True, cfg=cfg.credibility
                )
                if tier_preview == cfg.universe.small_cap_exclusion_tier:
                    continue

            # 리샘플일 뿐 네트워크 호출이 아니라 항상 계산해둔다 (core_timeframe이 어느 쪽이든 필요).
            df_weekly = to_weekly(df_daily)

            investor_series = None
            if market == "KR" and cfg.supply_demand.enabled:
                investor_series = fetch_investor_net_buy_series(ticker, days=cfg.supply_demand.strong_streak_days + 15)

            result = score_ticker(
                ticker=ticker,
                name=name,
                market=market,
                df_daily=df_daily,
                cfg=cfg,
                df_weekly=df_weekly,
                market_cap=market_cap,
                investor_series=investor_series,
                index_member=row.get("index_member"),
            )
            if result:
                results.append(result)
        except Exception:
            logger.warning("스코어링 실패, 건너뜀: %s (%s)", ticker, name, exc_info=True)
            continue

    return results


def main() -> int:
    # Windows 콘솔 기본 인코딩(cp949)에서는 한글 종목명이 콘솔 출력 시 깨진다.
    # 저장되는 CSV/Excel 파일 자체는 항상 UTF-8이라 무관하지만, 콘솔 가독성을 위해 시도한다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # pykrx는 내부적으로 logging.info(args, kwargs) 형태로 잘못 호출하는 버그가 있어(현재 배포 버전
    # 확인됨), 루트 로거가 INFO 이상이면 API가 막혀 있을 때마다 "--- Logging error ---" 트레이스백이
    # 출력된다. 루트는 WARNING으로 낮추고, 이 프로젝트 모듈 로거들만 원하는 레벨로 다시 올려서
    # 우리 진행 로그는 그대로 보이게 한다.
    logging.getLogger().setLevel(logging.WARNING)
    app_level = logging.DEBUG if args.verbose else logging.INFO
    for name in ("__main__", "main", "universe", "data_fetcher", "scoring", "output", "visualize", "config"):
        logging.getLogger(name).setLevel(app_level)

    cfg = load_config()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]

    results = run(markets, args.limit, args.cache_hours, cfg)
    if not results:
        logger.warning("스코어링된 종목이 없습니다 (데이터 수집 실패 여부 확인)")
        return 1

    out_dir = Path(args.output_dir)
    save_results(results, out_dir)

    rebound = sorted(
        (r for r in results if r["is_rebound_candidate"]), key=lambda r: r["score"], reverse=True
    )
    logger.info(
        "반등 후보 %d개 / 고점 경계 %d개 / 전체 스크리닝 %d개",
        len(rebound), sum(1 for r in results if r["is_top_warning"]), len(results),
    )

    if not args.no_charts and rebound:
        chart_dir = out_dir / "charts"
        for r in rebound[: args.top_n_charts]:
            try:
                df_daily = fetch_ohlcv(r["ticker"], r["market"], days=cfg.universe.history_days, cache_max_age_hours=args.cache_hours)
                # 실제 반등 후보 판정에 쓰인 봉과 같은 걸 그려야 차트와 신호가 일치한다.
                if cfg.core_timeframe == "weekly":
                    chart_df, tf_label = to_weekly(df_daily), "주봉"
                else:
                    chart_df, tf_label = df_daily, "일봉"
                path = plot_ticker(chart_df, r["ticker"], r["name"], cfg, chart_dir, timeframe_label=tf_label)
                if path:
                    logger.info("차트 저장: %s", path)
            except Exception:
                logger.warning("차트 생성 실패: %s", r["ticker"], exc_info=True)

    print("\n=== 반등 후보 상위 종목 ===")
    for r in rebound[:20]:
        print(
            f"[{r['credibility_tier']}] {r['market']} {r['ticker']} {r['name']}  "
            f"score={r['score']}  K={r['slow_k']:.1f} D={r['slow_d']:.1f} %R={r['williams_r']:.1f}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
