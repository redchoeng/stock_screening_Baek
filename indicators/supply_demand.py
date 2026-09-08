"""
수급 분석 (국내 종목 전용). 외국인/기관 연속 순매수 일수를 계산한다.

10~20일 이상 연속 순매수는 장기 추세 전환 가능성의 보조 근거로 쓰인다 (가중치는 낮게,
scoring.py의 foreign_continuous_buy 참고 — 외국인/기관 중 더 강한 쪽 streak을 쓴다).
해외 종목은 이 항목 자체를 skip한다(scoring.py에서 해당 가중치를 제외하고 재정규화).
"""
from __future__ import annotations

import pandas as pd


def compute_buy_streak(
    net_buy_series: pd.Series | None,
    min_streak_days: int = 10,
    strong_streak_days: int = 20,
) -> dict:
    """외국인/기관 등 투자자 유형 하나의 순매수 시계열에서 최근 연속 순매수 일수를 센다."""
    if net_buy_series is None or net_buy_series.empty:
        return {"streak_days": 0, "is_meaningful": False, "strength": 0.0, "reason": "no_data"}

    streak = 0
    for v in reversed(net_buy_series.tolist()):
        if pd.isna(v):
            break
        if v > 0:
            streak += 1
        else:
            break

    is_meaningful = streak >= min_streak_days
    strength = min(1.0, streak / strong_streak_days) if is_meaningful and strong_streak_days > 0 else 0.0

    return {"streak_days": streak, "is_meaningful": is_meaningful, "strength": strength}
