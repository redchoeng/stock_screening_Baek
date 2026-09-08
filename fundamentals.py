"""
종목별 펀더멘털 수집 — 백찬규 프레임의 '분자'와 밸류에이션.

기존 반등 스크리너는 가격/거래량 파생 지표만 보기 때문에, 실적이 훼손된 종목일수록
오실레이터가 더 깊이 과매도에 머무는 구조적 편향이 있다("35만원 하던 게 25만원 됐다고
싸다며 샀다가 1만7천원"). 여기서 매출/마진/ROE/PER을 붙여 그 함정을 걸러낸다.

데이터 소스 (DART API 키 없이 동작):
  - 손익계산서/재무상태표: yfinance (미국은 물론 한국 종목도 005930.KS 형태로 제공된다)
  - 한국 PER 시계열: pykrx get_market_fundamental_by_date — 자기 PER 밴드 백분위 산출용.
    yfinance는 한국 종목의 trailingPE를 None으로 주는 경우가 많아 pykrx로 보완한다.
  - 미국 PER: yfinance trailingPE / forwardPE. 과거 PER 시계열은 무료로 구하기 어려워
    밴드 백분위는 한국 종목만 산출된다(미국은 시장 중앙값 대비로 baek_scoring에서 평가).

산출값의 핵심은 절대 수준이 아니라 기울기다. 애널리스트는 실적의 절대 금액이 아니라
증가율의 각도(미분)를 보는 직업이고, 증가율이 둔화되기 시작하면 실적이 좋아도 주가는
빠진다(2024년 2분기 엔비디아).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from config import FundamentalConfig

# pykrx는 KRX_ID/KRX_PW를 os.environ에서 직접 읽는다. 이걸 빼먹으면 국내 PER 시계열이
# 조용히 빈 값으로 돌아와 밸류에이션 밴드가 통째로 사라진다 (data_fetcher.py와 같은 이유).
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache" / "fundamentals"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_KR_LISTING_CACHE = Path(__file__).parent / "cache" / "meta" / "kr_listing.csv"

_kr_market_map: dict[str, str] | None = None

# yfinance 재무제표 행 라벨은 종목마다 조금씩 다르다. 우선순위 순으로 찾는다.
_REVENUE_ROWS = ("Total Revenue", "Operating Revenue")
_OP_INCOME_ROWS = ("Operating Income", "Total Operating Income As Reported", "EBIT")
_NET_INCOME_ROWS = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Including Noncontrolling Interests",
)
_ASSETS_ROWS = ("Total Assets",)
_EQUITY_ROWS = ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest")


def _cache_path(market: str, ticker: str) -> Path:
    safe = ticker.replace("/", "_")
    return CACHE_DIR / f"{market}_{safe}.json"


def _load_cache(market: str, ticker: str, max_age_hours: float) -> dict | None:
    path = _cache_path(market, ticker)
    if not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) / 3600 > max_age_hours:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(market: str, ticker: str, data: dict) -> None:
    try:
        _cache_path(market, ticker).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("펀더멘털 캐시 저장 실패: %s", ticker, exc_info=True)


def kr_yf_symbol(ticker: str) -> str:
    """국내 6자리 코드를 yfinance 심볼로. KOSDAQ은 .KQ, 그 외는 .KS."""
    global _kr_market_map
    if _kr_market_map is None:
        _kr_market_map = {}
        try:
            listing = pd.read_csv(_KR_LISTING_CACHE, dtype={"Code": str})
            _kr_market_map = dict(zip(listing["Code"], listing["Market"]))
        except Exception:
            logger.warning("KR 상장목록 캐시를 못 읽어 전 종목 .KS로 가정한다")
    return f"{ticker}.KQ" if _kr_market_map.get(ticker) == "KOSDAQ" else f"{ticker}.KS"


def _pick_row(df: pd.DataFrame | None, candidates: tuple[str, ...]) -> pd.Series | None:
    """재무제표에서 원하는 항목 행을 라벨 우선순위로 찾아 오래된 순 시계열로 돌려준다."""
    if df is None or df.empty:
        return None
    for label in candidates:
        if label in df.index:
            series = df.loc[label]
            if isinstance(series, pd.DataFrame):  # 같은 라벨이 중복된 경우
                series = series.iloc[0]
            series = series.dropna()
            if not series.empty:
                return series.sort_index()  # 컬럼이 최신순이므로 오래된 순으로 뒤집는다
    return None


def _growth(curr: float | None, prev: float | None) -> float | None:
    """증가율(%). 분모가 0 이하면 증가율에 의미가 없으므로 None."""
    if curr is None or prev is None or prev <= 0:
        return None
    return (curr / prev - 1) * 100


def _yoy_pair(series: pd.Series | None, quarterly: bool) -> tuple[float | None, float | None]:
    """(최신 YoY 증가율, 그 직전 시점의 YoY 증가율).

    두 번째 값이 있어야 증가율의 기울기(가속/둔화)를 볼 수 있다. 분기 데이터는 계절성이
    있으므로 직전 분기가 아니라 1년 전 같은 분기와 비교한다.
    """
    if series is None:
        return None, None
    vals = [float(v) for v in series.values]
    lag = 4 if quarterly else 1
    latest = _growth(vals[-1], vals[-1 - lag]) if len(vals) >= lag + 1 else None
    prev = _growth(vals[-2], vals[-2 - lag]) if len(vals) >= lag + 2 else None
    return latest, prev


def _ttm(series: pd.Series | None, offset: int = 0) -> float | None:
    """최근 4분기 합계. offset=4면 그 직전 4분기."""
    if series is None:
        return None
    vals = [float(v) for v in series.values]
    end = len(vals) - offset
    if end < 4:
        return None
    return sum(vals[end - 4:end])


def _period_metrics(rev: pd.Series | None, op: pd.Series | None, quarterly: bool) -> dict:
    """한 가지 기준(분기 또는 연간) 안에서만 증가율/기울기/마진을 계산한다.

    분기 YoY와 연간 YoY를 섞어서 빼면 기울기가 아니라 잡음이 되므로, 가속도와 마진 변화는
    반드시 같은 기준 안에서 구한 값끼리만 비교한다.
    """
    rev_yoy, rev_yoy_prev = _yoy_pair(rev, quarterly)
    op_yoy, _ = _yoy_pair(op, quarterly)

    acceleration = None
    if rev_yoy is not None and rev_yoy_prev is not None:
        acceleration = rev_yoy - rev_yoy_prev  # %p. 양수면 증가율의 각도가 서는 중.

    if quarterly:
        rev_now, rev_before = _ttm(rev), _ttm(rev, offset=4)
        op_now, op_before = _ttm(op), _ttm(op, offset=4)
    else:
        rev_now = float(rev.iloc[-1]) if rev is not None and len(rev) else None
        rev_before = float(rev.iloc[-2]) if rev is not None and len(rev) >= 2 else None
        op_now = float(op.iloc[-1]) if op is not None and len(op) else None
        op_before = float(op.iloc[-2]) if op is not None and len(op) >= 2 else None

    op_margin = (op_now / rev_now * 100) if (op_now is not None and rev_now) else None
    op_margin_prev = (op_before / rev_before * 100) if (op_before is not None and rev_before) else None
    margin_delta = (op_margin - op_margin_prev) if (op_margin is not None and op_margin_prev is not None) else None

    return {
        "basis": "quarterly" if quarterly else "annual",
        "rev_yoy": rev_yoy,
        "rev_yoy_prev": rev_yoy_prev,
        "op_yoy": op_yoy,
        "acceleration": acceleration,
        "op_margin": op_margin,
        "op_margin_prev": op_margin_prev,
        "margin_delta": margin_delta,
    }


def _kr_per_series(ticker: str, years: int) -> pd.Series | None:
    """pykrx 일별 PER 시계열. KRX 로그인이 없으면 빈 값이 오므로 그때는 None."""
    try:
        from pykrx import stock

        end = pd.Timestamp.today()
        start = end - pd.DateOffset(years=years)
        df = stock.get_market_fundamental_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        if df is None or df.empty or "PER" not in df.columns:
            return None
        per = pd.to_numeric(df["PER"], errors="coerce")
        per = per[per > 0].dropna()  # 적자기업은 PER이 0으로 찍힌다 -> 밴드 계산에서 제외
        return per if len(per) >= 60 else None
    except Exception:
        logger.debug("pykrx PER 시계열 실패: %s", ticker, exc_info=True)
        return None


def fetch_fundamentals(
    ticker: str,
    market: str,
    cfg: FundamentalConfig | None = None,
    use_cache: bool = True,
) -> dict | None:
    """단일 종목 펀더멘털. 데이터를 아예 못 받으면 None."""
    cfg = cfg or FundamentalConfig()

    if use_cache:
        cached = _load_cache(market, ticker, cfg.cache_hours)
        if cached is not None:
            return cached

    symbol = kr_yf_symbol(ticker) if market == "KR" else ticker
    try:
        import yfinance as yf

        tk = yf.Ticker(symbol)
        info = tk.info or {}
        q_income = tk.quarterly_income_stmt
        a_income = tk.income_stmt
        a_balance = tk.balance_sheet
    except Exception:
        logger.debug("yfinance 펀더멘털 실패: %s", symbol, exc_info=True)
        return None

    q_rev = _pick_row(q_income, _REVENUE_ROWS)
    q_op = _pick_row(q_income, _OP_INCOME_ROWS)
    a_rev = _pick_row(a_income, _REVENUE_ROWS)
    a_op = _pick_row(a_income, _OP_INCOME_ROWS)
    a_net = _pick_row(a_income, _NET_INCOME_ROWS)
    a_assets = _pick_row(a_balance, _ASSETS_ROWS)
    a_equity = _pick_row(a_balance, _EQUITY_ROWS)

    # --- 매출/영업이익 증가율, 기울기, 마진 -------------------------------------
    # yfinance 분기 손익계산서는 보통 5분기치뿐이라 YoY는 나와도 '그 직전 YoY'가 안 나온다.
    # 증가율의 기울기는 백 프레임의 핵심(2024년 2분기 엔비디아)이라 반드시 살려야 하므로,
    # 분기로 못 구하면 연간 4개년으로 폴백한다. 단 두 기준을 섞지는 않는다.
    quarterly_m = _period_metrics(q_rev, q_op, quarterly=True)
    annual_m = _period_metrics(a_rev, a_op, quarterly=False)
    use_quarterly = q_rev is not None and len(q_rev) >= cfg.min_quarters
    primary, fallback = (quarterly_m, annual_m) if use_quarterly else (annual_m, quarterly_m)

    def _pick(key: str):
        value = primary.get(key)
        return value if value is not None else fallback.get(key)

    rev_yoy = _pick("rev_yoy")
    op_yoy = _pick("op_yoy")
    basis = primary["basis"] if primary.get("rev_yoy") is not None else fallback["basis"]

    # 가속도와 마진 변화는 계산된 기준을 통째로 가져와야 의미가 유지된다.
    accel_src = primary if primary.get("acceleration") is not None else fallback
    acceleration = accel_src.get("acceleration")
    acceleration_basis = accel_src["basis"] if acceleration is not None else None
    # 헤드라인 revenue_yoy는 분기, 가속도는 연간에서 나오는 경우가 흔하다. 이때 두 기준의
    # 값을 나란히 놓으면 "130.0 - 16.2 = -5.3"처럼 말이 안 되는 쌍이 보이므로, 가속도를
    # 만든 두 값은 반드시 그 기준 안에서 짝지어 따로 내보낸다.
    accel_yoy_now = accel_src.get("rev_yoy") if acceleration is not None else None
    accel_yoy_prev = accel_src.get("rev_yoy_prev") if acceleration is not None else None

    margin_src = primary if primary.get("margin_delta") is not None else fallback
    if margin_src.get("margin_delta") is None:
        margin_src = primary if primary.get("op_margin") is not None else fallback
    op_margin = margin_src.get("op_margin")
    op_margin_prev = margin_src.get("op_margin_prev")
    margin_delta = margin_src.get("margin_delta")
    margin_basis = margin_src["basis"] if op_margin is not None else None

    # --- ROE 듀퐁 3분해 (연간 기준) ---------------------------------------------
    net_margin = asset_turnover = leverage = roe = None
    if a_net is not None and a_rev is not None and a_assets is not None and a_equity is not None:
        try:
            net, rev = float(a_net.iloc[-1]), float(a_rev.iloc[-1])
            assets, equity = float(a_assets.iloc[-1]), float(a_equity.iloc[-1])
            if rev > 0 and assets > 0 and equity > 0:
                net_margin = net / rev * 100
                asset_turnover = rev / assets
                leverage = assets / equity
                roe = net_margin / 100 * asset_turnover * leverage * 100
        except Exception:
            pass

    # --- 밸류에이션 -------------------------------------------------------------
    per = info.get("trailingPE")
    forward_per = info.get("forwardPE")
    per_percentile = None
    if market == "KR":
        per_series = _kr_per_series(ticker, cfg.per_band_years)
        if per_series is not None and len(per_series):
            per = float(per_series.iloc[-1])
            per_percentile = float((per_series <= per).mean() * 100)
    per = float(per) if isinstance(per, (int, float)) and per and per > 0 else None
    forward_per = (
        float(forward_per) if isinstance(forward_per, (int, float)) and forward_per and forward_per > 0 else None
    )

    # --- 실적 훼손 게이트 --------------------------------------------------------
    deteriorating = bool(rev_yoy is not None and rev_yoy < 0 and op_yoy is not None and op_yoy < 0)

    data = {
        "ticker": ticker,
        "market": market,
        "yf_symbol": symbol,
        "sector": info.get("sector"),
        "basis": basis,
        "acceleration_basis": acceleration_basis,
        "margin_basis": margin_basis,
        "revenue_yoy": None if rev_yoy is None else round(rev_yoy, 1),
        "revenue_acceleration": None if acceleration is None else round(acceleration, 1),
        "accel_yoy_now": None if accel_yoy_now is None else round(accel_yoy_now, 1),
        "accel_yoy_prev": None if accel_yoy_prev is None else round(accel_yoy_prev, 1),
        "op_income_yoy": None if op_yoy is None else round(op_yoy, 1),
        "op_margin": None if op_margin is None else round(op_margin, 2),
        "op_margin_prev": None if op_margin_prev is None else round(op_margin_prev, 2),
        "op_margin_delta": None if margin_delta is None else round(margin_delta, 2),
        "net_margin": None if net_margin is None else round(net_margin, 2),
        "asset_turnover": None if asset_turnover is None else round(asset_turnover, 3),
        "leverage": None if leverage is None else round(leverage, 2),
        "roe": None if roe is None else round(roe, 2),
        "per": None if per is None else round(per, 2),
        "forward_per": None if forward_per is None else round(forward_per, 2),
        "per_percentile": None if per_percentile is None else round(per_percentile, 1),
        "earnings_deteriorating": deteriorating,
        "has_growth_data": rev_yoy is not None,
    }
    _save_cache(market, ticker, data)
    return data


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for tk, mk in (("005930", "KR"), ("000660", "KR"), ("NVDA", "US"), ("KO", "US")):
        print(json.dumps(fetch_fundamentals(tk, mk, use_cache=False), ensure_ascii=False, indent=1))
