"""
강의에서 소개한 자체 변동성 지표 (-100 ~ 0 범위).

*** 구현 노트 ***
요청서에는 "당일 종가 기준 계산식, 스토캐스틱/윌리엄스와 유사하되 계산 기준이 다름"이라고만
설명돼 있어 정확한 원 수식을 알 수 없다. 여기서는 다음과 같이 해석해 구현했다:

    고가/저가가 아니라 '종가'의 n일 최고/최저를 기준으로 윌리엄스 %R과 같은 형태를 적용

        CustomVol = (최근 n일 종가 최고 - 당일 종가) / (최근 n일 종가 최고 - 최근 n일 종가 최저) * -100

강의 원본과 다르면 이 함수만 교체하면 되고, scoring.py/config.py 등 나머지는 그대로 쓸 수 있다.
기본 기간은 스토캐스틱/윌리엄스(15)보다 긴 19일 (신호 빈도를 낮춰 노이즈 감소, 요청서 명시사항).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_custom_volatility(df: pd.DataFrame, period: int = 19) -> pd.Series:
    close_high_n = df["Close"].rolling(period).max()
    close_low_n = df["Close"].rolling(period).min()
    denom = (close_high_n - close_low_n).replace(0, np.nan)  # np.nan 사용 이유: stochastic.py 주석 참고
    val = (close_high_n - df["Close"]) / denom * -100
    val.name = "custom_volatility"
    return val


def is_oversold(val: pd.Series, threshold: float) -> pd.Series:
    return val <= threshold


def is_overbought(val: pd.Series, threshold: float) -> pd.Series:
    return val >= threshold
