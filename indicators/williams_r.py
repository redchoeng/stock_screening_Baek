"""
윌리엄스 %R. 값 범위 -100(과매도 극단) ~ 0(과매수 극단).

%R = (n일 최고가 - 당일 종가) / (n일 최고가 - n일 최저가) * -100
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_williams_r(df: pd.DataFrame, period: int = 15) -> pd.Series:
    high_n = df["High"].rolling(period).max()
    low_n = df["Low"].rolling(period).min()
    denom = (high_n - low_n).replace(0, np.nan)  # np.nan 사용 이유: stochastic.py 주석 참고
    wr = (high_n - df["Close"]) / denom * -100
    wr.name = "williams_r"
    return wr


def is_oversold(wr: pd.Series, threshold: float) -> pd.Series:
    return wr <= threshold


def is_overbought(wr: pd.Series, threshold: float) -> pd.Series:
    return wr >= threshold
