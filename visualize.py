"""
종목별 차트 시각화: 캔들 + 이동평균 + 스토캐스틱/윌리엄스%R + 거래량 서브플롯.

mplfinance를 사용한다 (requirements.txt 참고). 골든/데드크로스는 그림에 마킹하지 않는다
(강의에서 단독 신호로 쓰지 말라고 한 부분을 시각화에서도 강조하지 않기 위함) — 이동평균선
자체는 추세 참고용으로만 그린다.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from config import ScreenerConfig
from indicators.stochastic import compute_stochastic_slow
from indicators.williams_r import compute_williams_r

logger = logging.getLogger(__name__)

# 종목명이 한글이면 기본 폰트(DejaVu Sans)에 글리프가 없어 차트 제목이 네모(□)로 깨진다.
# mplfinance는 style을 적용할 때 자체 rcParams 컨텍스트를 쓰기 때문에 matplotlib.rcParams를
# 전역으로 바꿔도 무시된다 -> mpf.make_mpf_style(rc=...)로 폰트를 style에 직접 넣어야 한다.
_KOREAN_FONT_RC = {"font.family": "Malgun Gothic", "axes.unicode_minus": False}


def plot_ticker(
    df: pd.DataFrame,
    ticker: str,
    name: str,
    cfg: ScreenerConfig,
    out_dir: Path,
    ma_periods: tuple[int, ...] = (20, 60),
    lookback_periods: int = 180,
    timeframe_label: str = "일봉",
) -> Path | None:
    """df는 일봉 또는 주봉 OHLCV — 어느 쪽을 넘기든 그대로 그린다.
    timeframe_label은 제목/스토캐스틱 축 라벨에만 쓰인다(과매도 판정 자체는 스코어링 단계에서
    이미 cfg.core_timeframe으로 끝난 뒤이므로 여기선 표시용)."""
    try:
        import mplfinance as mpf
    except ImportError:
        logger.warning("mplfinance 미설치 -> 차트 생략 (pip install mplfinance)")
        return None

    if df.empty or len(df) < max(ma_periods) + 5:
        return None

    plot_df = df.tail(lookback_periods + max(ma_periods)).copy()

    stoch = compute_stochastic_slow(
        plot_df, cfg.stochastic.k_period, cfg.stochastic.slowing_period, cfg.stochastic.d_period
    )
    wr = compute_williams_r(plot_df, cfg.williams_r.period)

    plot_df = plot_df.tail(lookback_periods)
    stoch = stoch.reindex(plot_df.index)
    wr = wr.reindex(plot_df.index)

    mavs = [p for p in ma_periods if p < len(plot_df)]

    addplots = [
        mpf.make_addplot(stoch["slow_k"], panel=2, ylabel=f"Stoch({timeframe_label})", color="tab:blue"),
        mpf.make_addplot(stoch["slow_d"], panel=2, color="tab:orange"),
        mpf.make_addplot(
            pd.Series(cfg.stochastic.oversold, index=plot_df.index), panel=2, color="gray", linestyle="--"
        ),
        mpf.make_addplot(
            pd.Series(cfg.stochastic.overbought, index=plot_df.index), panel=2, color="gray", linestyle="--"
        ),
        mpf.make_addplot(wr, panel=3, ylabel="Williams %R", color="tab:green"),
        mpf.make_addplot(
            pd.Series(cfg.williams_r.oversold, index=plot_df.index), panel=3, color="gray", linestyle="--"
        ),
        mpf.make_addplot(
            pd.Series(cfg.williams_r.overbought, index=plot_df.index), panel=3, color="gray", linestyle="--"
        ),
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_ticker = ticker.replace("/", "_")
    out_path = out_dir / f"{safe_ticker}_{name}.png".replace(" ", "_")

    try:
        korean_style = mpf.make_mpf_style(base_mpf_style="yahoo", rc=_KOREAN_FONT_RC)
        mpf.plot(
            plot_df,
            type="candle",
            style=korean_style,
            mav=tuple(mavs) if mavs else None,
            volume=True,
            addplot=addplots,
            panel_ratios=(3, 1, 1, 1),
            title=f"{name} ({ticker}) - {timeframe_label}",
            savefig=dict(fname=str(out_path), dpi=120, bbox_inches="tight"),
        )
        return out_path
    except Exception:
        logger.warning("차트 생성 실패: %s (%s)", ticker, name, exc_info=True)
        return None
