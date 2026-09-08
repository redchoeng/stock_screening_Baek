"""
거래량 분석.

- 52주(avg_period_days) 평균 거래량 대비 최근 거래량 배율.
- 대형주는 2~3배(large_cap_multiplier), 중소형주는 5배(small_cap_multiplier) 기준으로
  '급증' 판정 임계값을 다르게 적용 (시가총액 기준, 통화별 컷은 config.VolumeConfig 참고).
- 방향성 결합: 상승 중 급증=긍정, 하락 중 급증=경고.
- (선택) 하락 후 반등 시 거래량이 '감소 -> 회복'으로 전환되는지 체크.
"""
from __future__ import annotations

import pandas as pd


def compute_volume_signal(
    df: pd.DataFrame,
    market_cap: float | None,
    market: str,
    avg_period_days: int = 252,
    large_cap_multiplier: float = 2.5,
    small_cap_multiplier: float = 5.0,
    krw_large_cap_threshold: float = 300_000_000_000,
    usd_large_cap_threshold: float = 10_000_000_000,
    recovery_check_enabled: bool = True,
    recovery_lookback_days: int = 10,
    recovery_pickup_ratio: float = 1.2,
    recovery_price_near_low_pct: float = 1.05,
) -> dict:
    if df.empty or len(df) < 5:
        return {"is_surge": False, "reason": "insufficient_data"}

    min_periods = max(20, avg_period_days // 4)
    avg_vol = df["Volume"].rolling(avg_period_days, min_periods=min_periods).mean().iloc[-1]
    latest_vol = float(df["Volume"].iloc[-1])

    if pd.isna(avg_vol) or avg_vol <= 0:
        return {"is_surge": False, "reason": "no_avg_volume"}

    ratio = latest_vol / float(avg_vol)

    threshold_cap = krw_large_cap_threshold if market == "KR" else usd_large_cap_threshold
    is_large_cap = market_cap is not None and market_cap >= threshold_cap
    applied_multiplier = large_cap_multiplier if is_large_cap else small_cap_multiplier
    is_surge = ratio >= applied_multiplier

    price_change = float(df["Close"].iloc[-1] - df["Close"].iloc[-2]) if len(df) >= 2 else 0.0
    direction = "up" if price_change > 0 else ("down" if price_change < 0 else "flat")

    positive_signal = is_surge and direction == "up"
    warning_signal = is_surge and direction == "down"

    recovery_signal = False
    if recovery_check_enabled and len(df) > recovery_lookback_days + 1:
        recent = df.iloc[-recovery_lookback_days:]
        min_pos = int(recent["Volume"].values.argmin())
        if min_pos < len(recent) - 1:  # 최저점이 바로 오늘이 아니라 이미 지나간 저점이어야 '회복'
            vol_at_min = float(recent["Volume"].iloc[min_pos])
            vol_recovering = vol_at_min > 0 and latest_vol > vol_at_min * recovery_pickup_ratio
            price_recent_low = float(recent["Close"].min())
            price_near_low = (
                price_recent_low > 0
                and float(df["Close"].iloc[-1]) <= price_recent_low * recovery_price_near_low_pct
            )
            recovery_signal = vol_recovering and price_near_low

    return {
        "avg_volume": float(avg_vol),
        "latest_volume": latest_vol,
        "ratio": ratio,
        "is_large_cap": is_large_cap,
        "applied_multiplier": applied_multiplier,
        "is_surge": is_surge,
        "direction": direction,
        "positive_signal": positive_signal,
        "warning_signal": warning_signal,
        "recovery_signal": recovery_signal,
    }
