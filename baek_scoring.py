"""
백찬규 프레임 채점.

기존 scoring.py(스토캐스틱 + 윌리엄스 %R 교차확인)와는 완전히 독립된 별도 스크리너다.
기존 로직은 일절 건드리지 않는다.

    주가 = 기업의 미래 이익 (분자) / [국채금리 + 위험 프리미엄] (분모)

분모(macro.py)는 점수가 아니라 곱수로 적용한다. 분모가 팽창하는 국면에서는 분자가 아무리
좋아도 주가가 눌리기 때문이다. 분자 쪽은 다섯 축으로 나눠 채점한다.

  1. 매출 증가율      명목성장률(실질 2.4% + 물가 3% ≒ 하이싱글) 대비. IT는 그 2배가 기준.
  2. 증가율의 기울기  절대 금액이 아니라 각도(미분). 둔화가 시작되면 실적이 좋아도 빠진다.
  3. 마진 개선        원가를 판가에 전가 못 하면 영업이익률이 훼손된다(마진 스퀴즈).
  4. 밸류에이션 밴드  자기 PER 밴드 안에서 어디에 있는가. 국가 간 PER 비교는 하지 않는다.
  5. ROE 듀퐁 축      레짐마다 개선 가능한 축이 다르다(고금리=매출 / 디스인플레=마진 / 인하=레버리지).

그리고 무엇보다, 매출과 영업이익이 동반 감소하는 종목은 아무리 싸 보여도 후보에서 제외한다.
"35만원 하던 종목이 25만원 됐다고 싸다며 매수했다가 1만7천원까지 폭락"하는 절대가격 함정을
막는 게 이 스크리너의 존재 이유다.
"""
from __future__ import annotations

import logging
import statistics

from config import BaekConfig

logger = logging.getLogger(__name__)

# 레짐별로 ROE 3분해 중 어느 축이 개선될 수 있는지. 백 센터장의 2023~2025년 복기 그대로다.
REGIME_AXIS = {
    "risk_off": ("revenue", "고금리·고물가 — 레버리지도 마진도 막힌다. 남는 건 매출액 성장(2023년형)."),
    "neutral": ("margin", "물가·금리 진정 — 마진이 개선될 수 있는 구간(2024년형)."),
    "risk_on": ("leverage", "금리 인하 — 타인자본 조달이 쉬워진다. 레버리지 활용 국면(2025년형)."),
}

VERDICT_CANDIDATE = "후보"
VERDICT_WATCH = "관찰"
VERDICT_EXCLUDED = "제외"


def _ramp(value: float | None, low: float, high: float) -> float:
    """low에서 0, high에서 1이 되는 선형 램프. 범위 밖은 잘라낸다."""
    if value is None or high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def build_market_context(fundamentals: list[dict], cfg: BaekConfig) -> dict:
    """시장별 PER 중앙값. 미국은 과거 PER 시계열을 무료로 못 구해 자기 밴드 백분위를 낼 수
    없으므로, 대신 같은 시장 구성종목의 PER 중앙값 대비로 평가한다."""
    context: dict[str, dict] = {}
    for market in ("KR", "US"):
        pers = [f["per"] for f in fundamentals if f and f.get("market") == market and f.get("per")]
        context[market] = {
            "per_median": round(statistics.median(pers), 2) if len(pers) >= 5 else None,
            "per_sample": len(pers),
        }
    return context


def _score_revenue_growth(fund: dict, cfg: BaekConfig) -> tuple[float, str]:
    """명목성장률 기준선 대비 매출 증가율. IT는 요구치가 2배."""
    fc = cfg.fundamental
    w = cfg.weights.revenue_growth
    yoy = fund.get("revenue_yoy")
    if yoy is None:
        return 0.0, "매출 증가율 데이터 없음"

    is_it = (fund.get("sector") or "") in fc.it_sectors
    required = fc.nominal_growth_high * (fc.it_growth_multiplier if is_it else 1.0)
    cheer = fc.nominal_growth_high * (fc.it_cheer_multiplier if is_it else fc.it_growth_multiplier)

    if yoy <= 0:
        return 0.0, f"매출 역성장 {yoy:.1f}%"
    # 0 ~ 요구치 구간에서 배점의 60%까지, 요구치 ~ 환호선 구간에서 나머지 40%.
    if yoy <= required:
        score = w * 0.6 * _ramp(yoy, 0.0, required)
        note = f"매출 {yoy:.1f}% — 기준선 {required:.1f}% 미달"
    else:
        score = w * (0.6 + 0.4 * _ramp(yoy, required, cheer))
        note = f"매출 {yoy:.1f}% — 기준선 {required:.1f}% 통과"
    return score, note


def _score_acceleration(fund: dict, cfg: BaekConfig) -> tuple[float, str, bool]:
    """증가율의 기울기. 둔화 중이면 0점이고 별도로 경고 플래그를 세운다."""
    w = cfg.weights.growth_acceleration
    accel = fund.get("revenue_acceleration")
    if accel is None:
        return 0.0, "기울기 데이터 없음", False
    prev, now = fund.get("accel_yoy_prev"), fund.get("accel_yoy_now")
    pair = f" ({prev:.1f}% → {now:.1f}%)" if prev is not None and now is not None else ""
    if accel <= 0:
        return 0.0, f"증가율 둔화 {accel:+.1f}%p{pair}", True
    return w * _ramp(accel, 0.0, 10.0), f"증가율 가속 {accel:+.1f}%p{pair}", False


def _score_margin(fund: dict, cfg: BaekConfig) -> tuple[float, str, bool]:
    """영업이익률 개선폭. 원가를 판가에 못 넘기면 여기서 걸린다."""
    w = cfg.weights.margin_improvement
    delta = fund.get("op_margin_delta")
    margin = fund.get("op_margin")
    if delta is None:
        return 0.0, "마진 데이터 없음", False
    label = f"영업이익률 {margin:.1f}% ({delta:+.1f}%p)" if margin is not None else f"마진 {delta:+.1f}%p"
    if delta < 0:
        return 0.0, label + " — 마진 스퀴즈", True
    return w * _ramp(delta, 0.0, 3.0), label, False


def _score_valuation(fund: dict, market_ctx: dict, cfg: BaekConfig) -> tuple[float, str, bool]:
    """자기 PER 밴드 백분위(한국) 또는 시장 중앙값 대비(미국)."""
    w = cfg.weights.valuation_band
    per = fund.get("per")
    if per is None:
        return 0.0, "PER 없음 (적자 등)", False

    pct = fund.get("per_percentile")
    if pct is not None:
        # 밴드 하단일수록 높은 점수. 상위 80% 이상이면 비싸다고 본다.
        return w * (100.0 - pct) / 100.0, f"PER {per:.1f} — 5년 밴드 {pct:.0f}%", pct >= 80.0

    median = (market_ctx.get(fund.get("market")) or {}).get("per_median")
    if not median:
        return 0.0, f"PER {per:.1f} — 비교 기준 없음", False
    ratio = per / median
    # 시장 중앙값의 0.7배에서 만점, 1.3배에서 0점.
    return w * _ramp(-ratio, -1.3, -0.7), f"PER {per:.1f} — 시장 중앙값 {median:.1f}의 {ratio:.2f}배", ratio >= 1.3


def _score_regime_axis(fund: dict, regime: str, cfg: BaekConfig) -> tuple[float, str]:
    """레짐이 지목한 듀퐁 축 60% + 절대 ROE 수준 40%."""
    w = cfg.weights.roe_regime_axis
    axis, _ = REGIME_AXIS.get(regime, REGIME_AXIS["neutral"])

    roe = fund.get("roe")
    base = w * 0.4 * _ramp(roe, 0.0, 20.0)  # ROE 20%면 만점

    if axis == "revenue":
        metric, axis_score = fund.get("revenue_yoy"), _ramp(fund.get("revenue_yoy"), 0.0, 20.0)
        label = f"매출 축 {metric:.1f}%" if metric is not None else "매출 축 데이터 없음"
    elif axis == "margin":
        metric, axis_score = fund.get("net_margin"), _ramp(fund.get("net_margin"), 0.0, 15.0)
        label = f"순이익률 축 {metric:.1f}%" if metric is not None else "순이익률 축 데이터 없음"
    else:
        metric, axis_score = fund.get("leverage"), _ramp(fund.get("leverage"), 1.0, 3.0)
        label = f"레버리지 축 {metric:.2f}배" if metric is not None else "레버리지 축 데이터 없음"

    roe_label = f"ROE {roe:.1f}%" if roe is not None else "ROE 없음"
    return base + w * 0.6 * axis_score, f"{label} · {roe_label}"


def score_stock(row: dict, fund: dict | None, macro: dict, market_ctx: dict, cfg: BaekConfig) -> dict:
    """단일 종목 백 프레임 채점. fund가 없으면 데이터 부족으로 제외한다."""
    base = {
        "ticker": row["ticker"],
        "name": row["name"],
        "market": row["market"],
        "index_member": sorted(row.get("index_member") or []),
        "regime": macro.get("regime"),
    }

    if not fund or not fund.get("has_growth_data"):
        return {
            **base,
            "score": 0.0,
            "score_raw": 0.0,
            "verdict": VERDICT_EXCLUDED,
            "exclude_reason": "재무 데이터 부족",
            "flags": [],
            "axes": {},
            "notes": ["매출 증가율을 계산할 재무 데이터를 확보하지 못했다."],
            "fundamentals": fund or {},
        }

    if cfg.exclude_deteriorating_earnings and fund.get("earnings_deteriorating"):
        return {
            **base,
            "score": 0.0,
            "score_raw": 0.0,
            "verdict": VERDICT_EXCLUDED,
            "exclude_reason": "매출·영업이익 동반 감소",
            "flags": ["deteriorating"],
            "axes": {},
            "notes": [
                f"매출 {fund['revenue_yoy']:+.1f}%, 영업이익 {fund['op_income_yoy']:+.1f}% — "
                "실적이 훼손된 종목은 아무리 눌려 있어도 후보로 올리지 않는다."
            ],
            "fundamentals": fund,
        }

    rev_score, rev_note = _score_revenue_growth(fund, cfg)
    accel_score, accel_note, peaked = _score_acceleration(fund, cfg)
    margin_score, margin_note, squeezed = _score_margin(fund, cfg)
    val_score, val_note, expensive = _score_valuation(fund, market_ctx, cfg)
    axis_score, axis_note = _score_regime_axis(fund, macro.get("regime", "neutral"), cfg)

    raw = rev_score + accel_score + margin_score + val_score + axis_score
    multiplier = macro.get("multiplier", 1.0)
    final = raw * multiplier

    flags = []
    if peaked:
        flags.append("momentum_peaked")
    if squeezed:
        flags.append("margin_squeeze")
    if expensive:
        flags.append("expensive")

    # 판정은 곱수 적용 '전' 점수로. 레짐이 나쁘다고 좋은 기업이 실격되는 건 아니다.
    if raw >= cfg.candidate_score:
        verdict = VERDICT_CANDIDATE
    elif raw >= cfg.watch_score:
        verdict = VERDICT_WATCH
    else:
        verdict = VERDICT_EXCLUDED

    return {
        **base,
        "score": round(final, 1),
        "score_raw": round(raw, 1),
        "multiplier": multiplier,
        "regime_caution": macro.get("regime") == "risk_off",
        "verdict": verdict,
        "exclude_reason": None,
        "flags": flags,
        "axes": {
            "revenue_growth": round(rev_score, 1),
            "growth_acceleration": round(accel_score, 1),
            "margin_improvement": round(margin_score, 1),
            "valuation_band": round(val_score, 1),
            "roe_regime_axis": round(axis_score, 1),
        },
        "notes": [rev_note, accel_note, margin_note, val_note, axis_note],
        "fundamentals": fund,
    }


def max_possible_score(cfg: BaekConfig) -> float:
    w = cfg.weights
    return (
        w.revenue_growth
        + w.growth_acceleration
        + w.margin_improvement
        + w.valuation_band
        + w.roe_regime_axis
    )
