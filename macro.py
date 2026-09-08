"""
매크로 레짐 판별 — 백찬규 프레임의 '분모'.

    주가 = 기업의 미래 이익 (분자) / [국채금리 + 위험 프리미엄] (분모)

기존 반등 스크리너(scoring.py)는 가격 오실레이터만 보기 때문에 이 축이 통째로 빠져 있다.
분모가 팽창하는 국면에서는 분자(실적)가 아무리 좋아도 주가가 눌리므로, 여기서 판정한
레짐을 종목 점수에 곱수로 적용한다.

판정 재료 (전부 yfinance, 로그인 불필요):
  - 미 10년물 국채 금리(^TNX): 4.5% 돌파가 핵심 하방 변수
  - WTI 유가(CL=F): 85~100달러 구간이 경계, 100달러 이상이면 위험
  - VIX: 참고용(레짐 판정에는 쓰지 않고 표시만)

유가 -> 물가 파급은 "유가 10% 상승 시 미국 CPI +0.15~0.3%p" 계수를 그대로 적용해
예상 CPI 기여분을 밴드로 계산한다. 이 값은 예측이 아니라 '지금 유가 움직임이 물가에
어느 정도 부담인지'를 같은 단위로 환산해 보여주는 참고치다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import pandas as pd

from config import MacroConfig

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "cache" / "meta" / "macro.json"

REGIME_LABELS = {
    "risk_off": "분모 팽창 (위험)",
    "neutral": "분모 횡보 (중립)",
    "risk_on": "분모 진정 (양호)",
}


def _fetch_close_series(symbol: str, period: str = "1y") -> pd.Series | None:
    """yfinance 종가 시계열. 실패하면 None (매크로 없이도 스크리너는 돌아가야 한다)."""
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period)
        if hist is None or hist.empty or "Close" not in hist:
            logger.warning("매크로 시계열 비어 있음: %s", symbol)
            return None
        return hist["Close"].dropna()
    except Exception:
        logger.warning("매크로 시계열 수집 실패: %s", symbol, exc_info=True)
        return None


def _level_and_change(series: pd.Series | None, window_days: int) -> tuple[float | None, float | None, str | None]:
    """(최신값, window_days 전 대비 변화율%, 최신 날짜)."""
    if series is None or series.empty:
        return None, None, None
    last = float(series.iloc[-1])
    last_date = series.index[-1].strftime("%Y-%m-%d")
    if len(series) <= window_days:
        return last, None, last_date
    prev = float(series.iloc[-window_days - 1])
    change_pct = None if prev == 0 else (last / prev - 1) * 100
    return last, change_pct, last_date


def _classify(us10y: float | None, wti: float | None, wti_change_pct: float | None, cfg: MacroConfig) -> str:
    """레짐 판정. 금리와 유가 중 하나라도 위험선을 넘으면 risk_off로 본다 — 분모는
    두 요소가 더해지는 구조라 어느 한쪽만 튀어도 할인율이 커지기 때문이다."""
    if us10y is None and wti is None:
        return "neutral"

    rate_risk = us10y is not None and us10y >= cfg.ust10y_risk_off
    oil_risk = wti is not None and wti >= cfg.wti_risk_off
    # 경계 구간(85~100달러)이라도 상승 중이면 위험으로 본다.
    oil_rising_in_band = (
        wti is not None
        and wti >= cfg.wti_warning
        and wti_change_pct is not None
        and wti_change_pct > 0
    )
    if rate_risk or oil_risk or oil_rising_in_band:
        return "risk_off"

    rate_ok = us10y is not None and us10y < cfg.ust10y_risk_on
    oil_ok = wti is not None and wti < cfg.wti_warning
    if rate_ok and oil_ok:
        return "risk_on"
    return "neutral"


def _build_notes(us10y, wti, wti_change_pct, cpi_low, cpi_high, regime, cfg: MacroConfig) -> list[str]:
    notes = []
    if us10y is not None:
        if us10y >= cfg.ust10y_risk_off:
            notes.append(f"미 10년물 {us10y:.2f}% — 기준선 {cfg.ust10y_risk_off}% 위. 할인율 분모가 커진 상태.")
        elif us10y < cfg.ust10y_risk_on:
            notes.append(f"미 10년물 {us10y:.2f}% — {cfg.ust10y_risk_on}% 아래로 분모 부담이 낮다.")
        else:
            notes.append(f"미 10년물 {us10y:.2f}% — {cfg.ust10y_risk_on}~{cfg.ust10y_risk_off}% 경계 구간.")
    if wti is not None:
        if wti >= cfg.wti_risk_off:
            notes.append(f"WTI {wti:.1f}달러 — {cfg.wti_risk_off:.0f}달러 이상. 마진 스퀴즈 경계.")
        elif wti >= cfg.wti_warning:
            direction = "상승 중" if (wti_change_pct or 0) > 0 else "하락 중"
            notes.append(f"WTI {wti:.1f}달러 — {cfg.wti_warning:.0f}~{cfg.wti_risk_off:.0f}달러 경계 구간이며 {direction}.")
        else:
            notes.append(f"WTI {wti:.1f}달러 — {cfg.wti_warning:.0f}달러 아래로 물가 압력이 제한적.")
    if cpi_low is not None:
        notes.append(
            f"최근 {cfg.oil_move_window_days}거래일 유가 {wti_change_pct:+.1f}% → "
            f"CPI 기여 {cpi_low:+.2f}~{cpi_high:+.2f}%p 추정 (유가 10%당 {cfg.oil_to_cpi_low}~{cfg.oil_to_cpi_high}%p)."
        )
    if regime == "risk_off":
        notes.append("분모가 팽창하는 국면이므로 실적이 좋은 종목도 점수를 깎아 반영한다.")
    return notes


def fetch_macro(cfg: MacroConfig | None = None, use_cache: bool = True) -> dict:
    """현재 매크로 레짐. 네트워크가 막혀도 예외를 던지지 않고 unknown 레짐을 돌려준다."""
    cfg = cfg or MacroConfig()

    if use_cache and CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours <= cfg.cache_hours:
            try:
                cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                logger.info("매크로 캐시 사용 (%.1f시간 전)", age_hours)
                return cached
            except Exception:
                logger.warning("매크로 캐시 손상, 재수집")

    ust = _fetch_close_series(cfg.ust10y_symbol)
    oil = _fetch_close_series(cfg.wti_symbol)
    vix = _fetch_close_series(cfg.vix_symbol)

    us10y, us10y_chg, us10y_date = _level_and_change(ust, cfg.oil_move_window_days)
    wti, wti_chg, wti_date = _level_and_change(oil, cfg.oil_move_window_days)
    vix_level, _, _ = _level_and_change(vix, cfg.oil_move_window_days)

    cpi_low = cpi_high = None
    if wti_chg is not None:
        cpi_low = wti_chg / 10.0 * cfg.oil_to_cpi_low
        cpi_high = wti_chg / 10.0 * cfg.oil_to_cpi_high

    regime = _classify(us10y, wti, wti_chg, cfg)
    multiplier = {
        "risk_off": cfg.multiplier_risk_off,
        "neutral": cfg.multiplier_neutral,
        "risk_on": cfg.multiplier_risk_on,
    }[regime]

    result = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "regime": regime,
        "regime_label": REGIME_LABELS[regime],
        "multiplier": multiplier,
        "us10y": None if us10y is None else round(us10y, 2),
        "us10y_change_pct": None if us10y_chg is None else round(us10y_chg, 1),
        "us10y_date": us10y_date,
        "us10y_threshold": cfg.ust10y_risk_off,
        "wti": None if wti is None else round(wti, 2),
        "wti_change_pct": None if wti_chg is None else round(wti_chg, 1),
        "wti_date": wti_date,
        "wti_warning": cfg.wti_warning,
        "wti_risk_off": cfg.wti_risk_off,
        "vix": None if vix_level is None else round(vix_level, 2),
        "cpi_impact_low": None if cpi_low is None else round(cpi_low, 2),
        "cpi_impact_high": None if cpi_high is None else round(cpi_high, 2),
        "notes": _build_notes(us10y, wti, wti_chg, cpi_low, cpi_high, regime, cfg),
    }

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":
    import sys

    # 윈도우 콘솔 기본 코드페이지(cp949)에서 한글/em dash가 깨지지 않도록.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(fetch_macro(use_cache=False), ensure_ascii=False, indent=2))
