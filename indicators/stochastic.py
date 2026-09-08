"""
스토캐스틱 슬로우(Slow Stochastic).

raw %K(k_period 룩백) -> slow %K(slowing_period 평활) -> %D(d_period 평활).
기본 세팅(5,3,3)이 아니라 강의 세팅(15,5,5)을 기본값으로 쓴다 (config.StochasticConfig).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_stochastic_slow(
    df: pd.DataFrame,
    k_period: int = 15,
    slowing_period: int = 5,
    d_period: int = 5,
) -> pd.DataFrame:
    """반환: columns = ['slow_k', 'slow_d'], index는 df와 동일."""
    low_n = df["Low"].rolling(k_period).min()
    high_n = df["High"].rolling(k_period).max()
    # pd.NA를 쓰면 일부 pandas 버전에서 Series가 object dtype이 돼 rolling()이
    # "No numeric types to aggregate"로 깨진다 -> np.nan(float) 사용.
    denom = (high_n - low_n).replace(0, np.nan)

    raw_k = (df["Close"] - low_n) / denom * 100
    slow_k = raw_k.rolling(slowing_period).mean()
    slow_d = slow_k.rolling(d_period).mean()

    return pd.DataFrame({"slow_k": slow_k, "slow_d": slow_d}, index=df.index)


def is_oversold(stoch: pd.DataFrame, threshold: float, use: str = "slow_k") -> pd.Series:
    return stoch[use] <= threshold


def is_overbought(stoch: pd.DataFrame, threshold: float, use: str = "slow_k") -> pd.Series:
    return stoch[use] >= threshold
