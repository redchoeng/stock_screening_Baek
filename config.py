"""
전역 설정.

이 파일의 dataclass 기본값을 직접 수정하거나, 프로젝트 루트에 screener_config.json을
두면 거기 있는 키만 오버라이드된다 (부분 오버라이드 가능, 나머지는 기본값 유지).

*** 중요 (강의 핵심 반영) ***
- 이 프로그램의 신호는 "현재 과매도/과매수 상태를 여러 지표로 교차 확인"한 결과이며,
  미래 가격을 예측하지 않는다.
- 단일 지표 신호는 노이즈로 간주하고 기본적으로 사용하지 않는다. 스토캐스틱+윌리엄스 %R이
  동시에 과매도/과매수일 때만 핵심 신호로 취급한다 (scoring.py 참고).
- 이동평균 골든크로스/데드크로스는 단독으로 신호를 발생시키지 않는다. 이 프로그램은
  아예 골든/데드크로스를 신호로 채점하지 않는다("속는 차트" 사례).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_OVERRIDE_FILE = Path(__file__).parent / "screener_config.json"


@dataclass
class StochasticConfig:
    """스토캐스틱 슬로우. raw %K(k_period) -> slow %K(slowing_period 평활) -> %D(d_period 평활)."""
    k_period: int = 15
    slowing_period: int = 5
    d_period: int = 5
    oversold: float = 20.0
    overbought: float = 80.0


@dataclass
class WilliamsRConfig:
    """윌리엄스 %R. 값 범위 -100~0."""
    period: int = 15
    oversold: float = -80.0
    overbought: float = -20.0


@dataclass
class CustomVolatilityConfig:
    """
    강의에서 소개한 자체 변동성 지표.

    구현 노트: "당일 종가 기준, 스토캐스틱/윌리엄스와 유사하되 계산 기준이 다름"이라는
    설명을 반영해, 고가/저가가 아닌 '종가의 n일 최고/최저'를 기준으로 윌리엄스 %R과
    같은 형태의 식을 적용했다:

        CustomVol = (최근 n일 종가 최고 - 당일 종가) / (최근 n일 종가 최고 - 최근 n일 종가 최저) * -100

    강의 원본 수식과 정확히 일치하지 않을 수 있으니, 실제 자료와 비교해 다르면
    indicators/custom_volatility.py의 compute_custom_volatility()만 교체하면 된다.
    """
    period: int = 19  # 기본 세팅보다 긴 기간 -> 신호 빈도 감소
    oversold: float = -80.0
    overbought: float = -20.0
    include_in_core_and_signal: bool = False  # True면 스토캐+윌리엄스 AND 조건에 이것도 포함


@dataclass
class StdDevVolatilityConfig:
    """이동평균 대비 표준편차 값 자체를 시계열로 추적, 국소 최소 후 재확대를 탐지."""
    window: int = 20             # 표준편차 계산 롤링 윈도우 (볼린저밴드와 동일 관례상 20)
    lookback_days: int = 60      # 국소 최소치를 탐색할 구간
    local_min_neighbors: int = 5  # 좌우 n일보다 낮아야 국소 최소로 인정
    expansion_pct: float = 0.30  # 국소 최소 대비 현재 표준편차가 이 비율 이상 커지면 "재확대"
    contraction_pct: float = 0.20  # 국소 최소가 그 직전 고점 대비 이 비율 이상 줄었어야 "수축"으로 인정
    recency_days: int = 10        # 국소 최소 시점이 최근 이 기간 이내여야 유효 (오래된 수축은 무시)


@dataclass
class VolumeConfig:
    """52주 평균 거래량 대비 배율, 시가총액 규모별 임계값 분리."""
    avg_period_days: int = 252
    large_cap_multiplier: float = 2.5      # 대형주: 2~3배
    small_cap_multiplier: float = 5.0      # 중소형주: 5배
    # 국내(KRW) 대형주 컷: 시총 3000억원
    krw_large_cap_threshold: float = 300_000_000_000
    # 미국(USD) 대형주 컷: 시총 100억달러 (원화 3000억원과 1:1 대응은 아니며, 시장 규모 차이를
    # 반영해 별도로 설정. 필요하면 조정)
    usd_large_cap_threshold: float = 10_000_000_000
    recovery_check_enabled: bool = True    # "감소 후 회복" 옵션 체크
    recovery_lookback_days: int = 10
    recovery_pickup_ratio: float = 1.2     # 거래량 저점 대비 이 배율 이상 늘어야 "회복"
    recovery_price_near_low_pct: float = 1.05  # 종가가 최근 저점의 이 배율 이내여야 "저점 부근"


@dataclass
class SupplyDemandConfig:
    """외국인/기관 연속 순매수 (국내 종목 전용, 해외 종목은 자동 skip).
    둘 다 계산하고, 더 강한(streak이 긴) 쪽으로 가점한다 — 요청서 2-7이 데이터 자체는
    "외국인/기관"이라 했으므로 둘 다 반영하되, 점수는 이중으로 주지 않는다."""
    enabled: bool = True
    min_streak_days: int = 10     # 이 이상 연속 순매수부터 가점 시작
    strong_streak_days: int = 20  # 이 이상이면 만점


@dataclass
class ScoringWeights:
    """0~100점 스케일로 정규화되는 가중치. 필요 시 자유롭게 조정."""
    stoch_williams_and_oversold: float = 40.0   # 핵심 AND 신호 (과매도)
    custom_vol_bonus: float = 10.0              # 자체 변동성 지표까지 겹치면 추가 가점
    stddev_squeeze_expansion: float = 25.0
    volume_surge_positive: float = 20.0
    foreign_continuous_buy: float = 15.0        # 국내 종목만 해당 (해외는 이 항목 제외하고 재정규화)
    weekly_confirmation_bonus: float = 10.0     # 주봉에서도 같은 과매도 AND 신호가 나오면 가점


@dataclass
class CredibilityTierConfig:
    """신뢰도 등급: 지수 > 대형주 > 중형주 > 소형주. 시가총액 기준 (통화별 컷)."""
    krw_mega_cap: float = 10_000_000_000_000   # 10조원 이상 -> 지수급 초대형주
    krw_large_cap: float = 300_000_000_000     # 3000억원 이상 -> 대형주
    krw_mid_cap: float = 100_000_000_000       # 1000억원 이상 -> 중형주
    # 미만 -> 소형주

    usd_mega_cap: float = 200_000_000_000      # 2000억달러 이상
    usd_large_cap: float = 10_000_000_000      # 100억달러 이상
    usd_mid_cap: float = 2_000_000_000         # 20억달러 이상


@dataclass
class UniverseConfig:
    kr_index: str = "KOSPI200"       # pykrx 지수 구성종목
    us_indices: tuple = ("SP500", "NASDAQ100")
    exclude_small_cap: bool = True    # 스몰캡(소형주) 자동 제외 여부 (신뢰도 낮음)
    small_cap_exclusion_tier: str = "소형주"  # 이 등급은 제외 (exclude_small_cap=True일 때)
    history_days: int = 730           # 최소 1~2년치 (여유 있게 730일 = 2년)


@dataclass
class MacroConfig:
    """백찬규 프레임의 '분모' — 주가 = 이익 / (무위험이자율 + 위험프리미엄).

    기존 반등 스크리너는 가격 오실레이터만 보므로 이 축이 아예 없다. 여기서 판정한 레짐은
    종목 점수에 곱수로 적용된다(baek_scoring.py). 임계값은 전부 방송 시점 스냅샷이므로
    하드코딩하지 않고 여기에 두고 바꿔 쓴다.
    """
    ust10y_symbol: str = "^TNX"    # 미 10년물 국채 금리 (%)
    wti_symbol: str = "CL=F"       # WTI 유가 (달러)
    vix_symbol: str = "^VIX"

    ust10y_risk_off: float = 4.5   # "미 10년물 4.5% 돌파가 핵심 하방 변수"
    ust10y_risk_on: float = 4.0
    wti_warning: float = 85.0      # "유가 85~100불 도달 여부"
    wti_risk_off: float = 100.0

    # "유가가 10% 상승하면 미국 CPI는 0.15~0.3%p 상승"
    oil_to_cpi_low: float = 0.15
    oil_to_cpi_high: float = 0.30
    oil_move_window_days: int = 21   # 유가 변동률을 볼 구간 (약 1개월)

    # 레짐별 점수 곱수. 분모가 팽창 중이면 아무리 분자가 좋아도 주가는 눌린다.
    multiplier_risk_off: float = 0.70
    multiplier_neutral: float = 0.90
    multiplier_risk_on: float = 1.00

    cache_hours: float = 6.0


@dataclass
class FundamentalConfig:
    """백찬규 프레임의 '분자' — 이익/매출/마진/ROE. 절대가격 함정 방어의 핵심."""
    # "명목 성장률 = 실질 2.4% + 물가 3% ≒ 5.5~7% 하이싱글"이 어닝 서프라이즈 기준선
    nominal_growth_base: float = 5.5
    nominal_growth_high: float = 8.0
    # "IT 기업은 명목 성장률의 2배 이상(최소 10%)을 내야 기립박수, 3배면 환호"
    it_growth_multiplier: float = 2.0
    it_cheer_multiplier: float = 3.0
    it_sectors: tuple = ("Technology", "Communication Services")

    per_band_years: int = 5          # PER 밴드 백분위 산출 구간
    # 국내 PER 값 자체는 시가총액 ÷ 순이익(TTM)으로 계산한다 — 미국 yfinance trailingPE와
    # 같은 기준이라 두 시장을 나란히 볼 수 있다. pykrx PER 시계열은 '직전 확정 연간 EPS'
    # 기준이라 기준이 달라 점수에는 쓰지 않고 참고용 5년 밴드로만 붙인다.
    # 이 참고 밴드 하나에 국내 종목당 12초쯤 든다. 콜드런을 빨리 끝내야 하면 False로 끄면 된다
    # (캐시가 살아 있는 평소 실행에는 영향이 없다).
    fetch_pykrx_per_band: bool = True
    min_quarters: int = 5            # 매출 YoY 계산에 필요한 최소 분기 수
    cache_hours: float = 168.0       # 재무는 분기 단위로만 바뀐다 -> 1주일


@dataclass
class BaekWeights:
    """백 프레임 스크리너 배점 (합계 100). 매크로 레짐은 점수가 아니라 곱수로 적용된다."""
    revenue_growth: float = 30.0        # 분자 핵심: 명목성장률 대비 매출 증가율
    growth_acceleration: float = 20.0   # "애널리스트는 절대금액이 아니라 기울기를 본다"(미분)
    margin_improvement: float = 15.0    # 마진 스퀴즈 방어
    valuation_band: float = 15.0        # 자기 PER 밴드 내 위치
    roe_regime_axis: float = 20.0       # 레짐별 듀퐁 주도 축


@dataclass
class BaekConfig:
    macro: MacroConfig = field(default_factory=MacroConfig)
    fundamental: FundamentalConfig = field(default_factory=FundamentalConfig)
    weights: BaekWeights = field(default_factory=BaekWeights)
    # 매출과 영업이익이 동반 감소 추세면 아무리 과매도여도 후보에서 제외한다.
    # "35만원 하던 종목이 25만원 됐다고 싸다며 샀다가 1만7천원까지 폭락" — 절대가격 함정.
    exclude_deteriorating_earnings: bool = True
    # 후보/관찰 판정은 매크로 곱수를 적용하기 전의 '종목 자체 점수'로 한다.
    # 분모(레짐)는 특정 종목의 실격 사유가 아니라 시장 전체에 걸리는 경고이므로,
    # 곱수는 순위와 표시 점수에만 반영하고 판정선은 종목 품질로 긋는다.
    candidate_score: float = 75.0
    watch_score: float = 40.0
    kr_limit: int = 100    # 종목당 yfinance 호출이 ~2.6초라 유니버스를 따로 제한한다
    us_limit: int = 80


@dataclass
class ScreenerConfig:
    stochastic: StochasticConfig = field(default_factory=StochasticConfig)
    williams_r: WilliamsRConfig = field(default_factory=WilliamsRConfig)
    custom_volatility: CustomVolatilityConfig = field(default_factory=CustomVolatilityConfig)
    stddev_volatility: StdDevVolatilityConfig = field(default_factory=StdDevVolatilityConfig)
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    supply_demand: SupplyDemandConfig = field(default_factory=SupplyDemandConfig)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    credibility: CredibilityTierConfig = field(default_factory=CredibilityTierConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    # 백찬규 프레임 스크리너 전용 설정. 기존 반등 스크리너 동작에는 일절 영향을 주지 않는다.
    baek: BaekConfig = field(default_factory=BaekConfig)
    # 핵심 AND 신호(스토캐+윌리엄스)를 어느 봉 기준으로 판정할지. 강의에서 "주봉이 더 신뢰도
    # 높다"고 명시했으므로 기본값은 weekly. "daily"로 바꾸면 일봉이 핵심 게이트가 되고 주봉은
    # 반대로 보조 확인(가점)이 된다. 자체 변동성/표준편차 스퀴즈/거래량은 요청서 원문이
    # "당일/최근 N일"이라 명시했으므로 core_timeframe과 무관하게 항상 일봉으로 계산한다.
    core_timeframe: str = "weekly"  # "daily" 또는 "weekly"


def _apply_overrides(obj, overrides: dict):
    for key, value in overrides.items():
        if not hasattr(obj, key):
            logger.warning("알 수 없는 설정 키 무시: %s", key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_overrides(current, value)
        else:
            setattr(obj, key, value)


def load_config(override_file: Path | None = None) -> ScreenerConfig:
    """기본값 + screener_config.json(있으면) 부분 오버라이드로 최종 설정을 만든다."""
    cfg = ScreenerConfig()
    path = override_file or CONFIG_OVERRIDE_FILE
    if path.exists():
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
            _apply_overrides(cfg, overrides)
            logger.info("설정 오버라이드 적용: %s", path)
        except Exception:
            logger.exception("설정 오버라이드 파일 파싱 실패, 기본값 사용: %s", path)
    return cfg


def config_to_dict(cfg: ScreenerConfig) -> dict:
    return asdict(cfg)


if __name__ == "__main__":
    # 현재 유효 설정을 확인하거나, screener_config.json 템플릿을 만들 때 사용:
    #   python config.py > screener_config.json
    import sys
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(config_to_dict(load_config()), indent=2, ensure_ascii=False))
