"""
기술적 지표 계산 모듈 모음.

각 함수는 OHLCV DataFrame(컬럼: Open/High/Low/Close/Volume, DatetimeIndex 오름차순)을 받아
pandas Series 또는 DataFrame을 반환하는 순수 함수다. 특정 종목/시장에 대한 부수효과나
네트워크 호출은 하지 않는다 (그건 data_fetcher.py 책임).
"""
from indicators.stochastic import compute_stochastic_slow
from indicators.williams_r import compute_williams_r
from indicators.custom_volatility import compute_custom_volatility
from indicators.stddev_volatility import compute_stddev_series, detect_squeeze_expansion
from indicators.volume import compute_volume_signal
from indicators.supply_demand import compute_buy_streak

__all__ = [
    "compute_stochastic_slow",
    "compute_williams_r",
    "compute_custom_volatility",
    "compute_stddev_series",
    "detect_squeeze_expansion",
    "compute_volume_signal",
    "compute_buy_streak",
]
