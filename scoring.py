"""
종합 스코어링.

핵심 철학(요청서 반영): 단일 지표는 노이즈가 많으므로, 스토캐스틱 슬로우 과매도 +
윌리엄스 %R 과매도가 "동시에" 뜰 때만 핵심 신호("대바닥 후보")로 인정한다. 이 AND 조건이
성립하지 않으면 다른 보조 지표가 아무리 좋아도 점수를 매기지 않는다(보조 지표는 AND
신호가 이미 성립한 종목의 순위를 매기는 데만 쓰인다). 과매수 쪽(대칭적으로 "고점 경계")도
같은 방식으로 별도 판정하되, 이 프로그램의 주 목적이 반등 후보 스크리닝이므로 점수화하지
않고 플래그만 남긴다.

이동평균 골든/데드크로스는 의도적으로 이 스코어링에 포함하지 않는다.
"""
from __future__ import annotations

import logging

import pandas as pd

from config import ScreenerConfig
from indicators.custom_volatility import compute_custom_volatility
from indicators.stddev_volatility import detect_squeeze_expansion
from indicators.stochastic import compute_stochastic_slow
from indicators.supply_demand import compute_buy_streak
from indicators.volume import compute_volume_signal
from indicators.williams_r import compute_williams_r
from universe import classify_credibility_tier

logger = logging.getLogger(__name__)


def _min_bars_required(cfg: ScreenerConfig) -> int:
    return max(
        cfg.stochastic.k_period + cfg.stochastic.slowing_period + cfg.stochastic.d_period,
        cfg.williams_r.period,
        cfg.custom_volatility.period,
        cfg.stddev_volatility.window + cfg.stddev_volatility.local_min_neighbors * 2 + 1,
        cfg.volume.recovery_lookback_days + 2,
    ) + 5


def _stoch_williams_signals(df: pd.DataFrame, cfg: ScreenerConfig) -> dict:
    """스토캐스틱 슬로우 + 윌리엄스 %R 최신값과 과매도·과매수 판정. df는 core_timeframe에
    해당하는 봉(일봉 또는 주봉)이면 된다 — 이 함수 자체는 어느 쪽이든 상관없다."""
    stoch = compute_stochastic_slow(
        df, cfg.stochastic.k_period, cfg.stochastic.slowing_period, cfg.stochastic.d_period
    )
    wr = compute_williams_r(df, cfg.williams_r.period)

    k, d = stoch["slow_k"].iloc[-1], stoch["slow_d"].iloc[-1]
    wr_v = wr.iloc[-1]
    if pd.isna(k) or pd.isna(d) or pd.isna(wr_v):
        return {"valid": False}

    return {
        "valid": True,
        "slow_k": float(k),
        "slow_d": float(d),
        "williams_r": float(wr_v),
        "stoch_oversold": bool(k <= cfg.stochastic.oversold and d <= cfg.stochastic.oversold),
        "stoch_overbought": bool(k >= cfg.stochastic.overbought and d >= cfg.stochastic.overbought),
        "wr_oversold": bool(wr_v <= cfg.williams_r.oversold),
        "wr_overbought": bool(wr_v >= cfg.williams_r.overbought),
    }


def _custom_vol_signal(df_daily: pd.DataFrame, cfg: ScreenerConfig) -> dict:
    """자체 변동성 지표. 요청서에 기간이 '19일'로 명시돼 있어 core_timeframe과 무관하게
    항상 일봉으로 계산한다 (주봉 모드에서 19주가 되어버리면 요청 스펙과 달라진다)."""
    custom = compute_custom_volatility(df_daily, cfg.custom_volatility.period)
    v = custom.iloc[-1]
    if pd.isna(v):
        return {"valid": False, "value": None, "oversold": False, "overbought": False}
    return {
        "valid": True,
        "value": float(v),
        "oversold": bool(v <= cfg.custom_volatility.oversold),
        "overbought": bool(v >= cfg.custom_volatility.overbought),
    }


def _combine_core_signal(sw: dict, custom: dict, cfg: ScreenerConfig) -> dict:
    """스토캐+윌리엄스(core_timeframe) + (옵션) 자체지표(항상 일봉)를 AND로 결합."""
    if cfg.custom_volatility.include_in_core_and_signal:
        core_oversold = sw["stoch_oversold"] and sw["wr_oversold"] and custom["oversold"]
        core_overbought = sw["stoch_overbought"] and sw["wr_overbought"] and custom["overbought"]
    else:
        core_oversold = sw["stoch_oversold"] and sw["wr_oversold"]
        core_overbought = sw["stoch_overbought"] and sw["wr_overbought"]

    custom_bonus_earned = (
        core_oversold and custom["oversold"] and not cfg.custom_volatility.include_in_core_and_signal
    )

    return {
        "valid": True,
        "slow_k": sw["slow_k"],
        "slow_d": sw["slow_d"],
        "williams_r": sw["williams_r"],
        "custom_volatility": custom["value"],
        "core_oversold": bool(core_oversold),
        "core_overbought": bool(core_overbought),
        "custom_bonus_earned": bool(custom_bonus_earned),
    }


def score_ticker(
    ticker: str,
    name: str,
    market: str,
    df_daily: pd.DataFrame,
    cfg: ScreenerConfig,
    df_weekly: pd.DataFrame | None = None,
    market_cap: float | None = None,
    investor_series: dict[str, pd.Series] | None = None,
    index_member: set | None = None,
) -> dict | None:
    """단일 종목 스코어링. 데이터가 부족하면 None을 반환한다(스킵).

    핵심 AND 신호(스토캐+윌리엄스)는 cfg.core_timeframe이 가리키는 봉(기본 weekly)으로
    판정한다. 반대쪽 타임프레임에서도 같은 신호가 나오면 secondary_confirmed로 가점만
    준다. 자체 변동성/표준편차 스퀴즈/거래량/수급은 요청서 원문이 "당일/최근 N일"이라
    명시했으므로 core_timeframe과 무관하게 항상 일봉(df_daily)으로 계산한다.
    """
    if df_daily is None or df_daily.empty or len(df_daily) < _min_bars_required(cfg):
        return None

    if cfg.core_timeframe == "weekly":
        core_df, secondary_df = df_weekly, df_daily
    else:
        core_df, secondary_df = df_daily, df_weekly

    if core_df is None or core_df.empty:
        return None
    core_sw = _stoch_williams_signals(core_df, cfg)
    if not core_sw["valid"]:
        return None
    custom = _custom_vol_signal(df_daily, cfg)
    core = _combine_core_signal(core_sw, custom, cfg)

    secondary_confirmed = False
    if secondary_df is not None and not secondary_df.empty:
        min_bars = cfg.stochastic.k_period + cfg.stochastic.slowing_period + cfg.stochastic.d_period
        if len(secondary_df) >= min_bars:
            secondary_sw = _stoch_williams_signals(secondary_df, cfg)
            secondary_confirmed = secondary_sw.get("valid", False) and secondary_sw.get("stoch_oversold", False) and secondary_sw.get("wr_oversold", False)

    stddev_result = detect_squeeze_expansion(
        df_daily,
        window=cfg.stddev_volatility.window,
        lookback_days=cfg.stddev_volatility.lookback_days,
        local_min_neighbors=cfg.stddev_volatility.local_min_neighbors,
        expansion_pct=cfg.stddev_volatility.expansion_pct,
        contraction_pct=cfg.stddev_volatility.contraction_pct,
        recency_days=cfg.stddev_volatility.recency_days,
    )

    volume_result = compute_volume_signal(
        df_daily,
        market_cap,
        market,
        avg_period_days=cfg.volume.avg_period_days,
        large_cap_multiplier=cfg.volume.large_cap_multiplier,
        small_cap_multiplier=cfg.volume.small_cap_multiplier,
        krw_large_cap_threshold=cfg.volume.krw_large_cap_threshold,
        usd_large_cap_threshold=cfg.volume.usd_large_cap_threshold,
        recovery_check_enabled=cfg.volume.recovery_check_enabled,
        recovery_lookback_days=cfg.volume.recovery_lookback_days,
        recovery_pickup_ratio=cfg.volume.recovery_pickup_ratio,
        recovery_price_near_low_pct=cfg.volume.recovery_price_near_low_pct,
    )

    # 외국인/기관 중 더 강한(streak이 긴) 쪽을 가점에 쓴다 — 요청서 2-7이 데이터 자체는
    # "외국인/기관"이라 명시했으니 둘 다 반영하되, 이중으로 점수를 주지는 않는다.
    foreign_result = None
    institution_result = None
    if market == "KR" and cfg.supply_demand.enabled and investor_series:
        if investor_series.get("foreign") is not None:
            foreign_result = compute_buy_streak(
                investor_series["foreign"], cfg.supply_demand.min_streak_days, cfg.supply_demand.strong_streak_days
            )
        if investor_series.get("institution") is not None:
            institution_result = compute_buy_streak(
                investor_series["institution"], cfg.supply_demand.min_streak_days, cfg.supply_demand.strong_streak_days
            )
    supply_candidates = [r for r in (foreign_result, institution_result) if r and r.get("is_meaningful")]
    supply_strength = max((r["strength"] for r in supply_candidates), default=0.0)
    supply_meaningful = bool(supply_candidates)

    w = cfg.weights
    max_possible = w.stoch_williams_and_oversold + w.custom_vol_bonus + w.stddev_squeeze_expansion + w.volume_surge_positive
    if market == "KR" and cfg.supply_demand.enabled:
        max_possible += w.foreign_continuous_buy
    max_possible += w.weekly_confirmation_bonus

    # 보조 지표(표준편차 스퀴즈/거래량/수급/주봉)는 스토캐+윌리엄스 AND 과매도가 이미 성립한
    # 종목의 순위를 매기는 데만 쓴다 — AND 신호가 없으면 다른 지표가 아무리 좋아도 0점이다.
    # (단일 지표만 뜨는 경우를 노이즈로 간주하고 신호에서 제외한다는 요청서 철학의 핵심.)
    score = 0.0
    if core["core_oversold"]:
        score += w.stoch_williams_and_oversold
        if core["custom_bonus_earned"]:
            score += w.custom_vol_bonus
        if stddev_result.get("signal"):
            score += w.stddev_squeeze_expansion
        if volume_result.get("positive_signal"):
            score += w.volume_surge_positive
        if supply_meaningful:
            score += w.foreign_continuous_buy * supply_strength
        if secondary_confirmed:
            score += w.weekly_confirmation_bonus

    normalized_score = round(score / max_possible * 100, 1) if max_possible > 0 else 0.0

    tier = classify_credibility_tier(
        market_cap, market, is_index_member=bool(index_member), cfg=cfg.credibility
    )

    last_close = float(df_daily["Close"].iloc[-1])
    last_date = df_daily.index[-1]

    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "index_member": ",".join(sorted(index_member)) if index_member else "",
        "credibility_tier": tier,
        "last_date": last_date,
        "last_close": last_close,
        "market_cap": market_cap,
        "slow_k": core["slow_k"],
        "slow_d": core["slow_d"],
        "williams_r": core["williams_r"],
        "custom_volatility": core["custom_volatility"],
        "core_oversold_and_signal": core["core_oversold"],
        "core_overbought_and_signal": core["core_overbought"],
        "custom_vol_bonus_earned": core["custom_bonus_earned"],
        "core_timeframe": cfg.core_timeframe,
        "secondary_confirmed": secondary_confirmed,
        "stddev_squeeze_signal": stddev_result.get("signal", False),
        "stddev_days_since_local_min": stddev_result.get("days_since_local_min"),
        "volume_ratio": volume_result.get("ratio"),
        "volume_direction": volume_result.get("direction"),
        "volume_positive_signal": volume_result.get("positive_signal", False),
        "volume_warning_signal": volume_result.get("warning_signal", False),
        "volume_recovery_signal": volume_result.get("recovery_signal", False),
        "foreign_buy_streak_days": foreign_result.get("streak_days") if foreign_result else None,
        "foreign_buy_meaningful": foreign_result.get("is_meaningful") if foreign_result else None,
        "institution_buy_streak_days": institution_result.get("streak_days") if institution_result else None,
        "institution_buy_meaningful": institution_result.get("is_meaningful") if institution_result else None,
        "score": normalized_score,
        "is_rebound_candidate": bool(core["core_oversold"]),
        "is_top_warning": bool(core["core_overbought"]),
    }
