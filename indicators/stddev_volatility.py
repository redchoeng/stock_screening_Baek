"""
표준편차 기반 변동성 측정.

볼린저밴드의 '밴드' 자체가 아니라, 종가의 롤링 표준편차 값을 하나의 시계열로 취급해
"줄어들다가(수축) 다시 커지는(재확대) 국소 최소치" 패턴을 찾는다. 이 패턴은 추세 전환이
임박했다는 신호로 플래그만 하며, 방향(상승/하락)은 알려주지 않는다 — 방향 판단은
스토캐스틱/윌리엄스 등 다른 지표와 함께 봐야 한다.

절대 표준편차 값은 종목마다 스케일이 달라 비교 불가능하므로, 항상 '국소 최소 대비 상대적
변화율'로 판단한다.
"""
from __future__ import annotations

import pandas as pd


def compute_stddev_series(df: pd.DataFrame, window: int = 20) -> pd.Series:
    std = df["Close"].rolling(window).std()
    std.name = "stddev"
    return std


def detect_squeeze_expansion(
    df: pd.DataFrame,
    window: int = 20,
    lookback_days: int = 60,
    local_min_neighbors: int = 5,
    expansion_pct: float = 0.30,
    contraction_pct: float = 0.20,
    recency_days: int = 10,
) -> dict:
    """
    최신 시점(df 마지막 행) 기준으로 '수축 후 재확대' 신호를 판정한다.

    반환 dict의 signal=True면:
      1) lookback_days 이내에 국소 최소치가 있었고 (local_min_neighbors일 좌우보다 낮음)
      2) 그 국소 최소치 직전 window일 고점 대비 contraction_pct 이상 수축했으며
      3) 현재 표준편차가 그 국소 최소치 대비 expansion_pct 이상 재확대됐다.
    """
    std = compute_stddev_series(df, window)
    n = len(std)
    min_required = window + local_min_neighbors * 2 + 1
    if n < min_required:
        return {"signal": False, "reason": "insufficient_data"}

    latest_idx = n - 1
    current_std = std.iloc[latest_idx]

    # 국소 최소로 확정하려면 좌우 local_min_neighbors일이 이미 알려져 있어야 하므로,
    # 탐색 구간의 끝은 latest_idx - local_min_neighbors까지만 허용한다.
    search_start = max(window, latest_idx - lookback_days, local_min_neighbors)
    search_end = latest_idx - local_min_neighbors

    candidates = []
    for idx in range(search_start, search_end + 1):
        window_vals = std.iloc[idx - local_min_neighbors: idx + local_min_neighbors + 1]
        if window_vals.isna().any():
            continue
        if std.iloc[idx] == window_vals.min():
            candidates.append(idx)

    if not candidates:
        return {"signal": False, "reason": "no_local_min_found"}

    local_min_idx = candidates[-1]  # 가장 최근 국소 최소
    local_min_val = float(std.iloc[local_min_idx])
    local_min_date = std.index[local_min_idx]
    days_since = latest_idx - local_min_idx

    if local_min_val <= 0 or pd.isna(local_min_val):
        return {"signal": False, "reason": "invalid_local_min"}

    pre_window = std.iloc[max(0, local_min_idx - window): local_min_idx]
    pre_peak = float(pre_window.max()) if not pre_window.empty and not pre_window.isna().all() else None
    contracted = pre_peak is not None and pre_peak > 0 and (pre_peak - local_min_val) / pre_peak >= contraction_pct

    expanded = (
        current_std is not None
        and not pd.isna(current_std)
        and (float(current_std) - local_min_val) / local_min_val >= expansion_pct
    )

    return {
        "signal": bool(contracted and expanded),
        "local_min_date": local_min_date,
        "local_min_value": local_min_val,
        "pre_peak_value": pre_peak,
        "current_value": float(current_std) if not pd.isna(current_std) else None,
        "days_since_local_min": int(days_since),
        "contracted": bool(contracted),
        "expanded": bool(expanded),
    }
