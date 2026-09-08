"""
스크리닝 대상 유니버스 산출.

- 국내: pykrx로 KOSPI200 구성종목 (지수 구성종목 자체가 이미 대형주 위주 필터).
- 미국: S&P500(Wikipedia) + Nasdaq100(slickcharts, Wikipedia 문서에 표가 빠져서 대체 소스 사용).
  이 소스가 막히면 nasdaq.com 등 다른 소스로 교체 필요.

각 종목에 시가총액 기반 "신뢰도 등급"(지수/초대형주 > 대형주 > 중형주 > 소형주)을 매기고,
config.universe.exclude_small_cap=True면 소형주는 유니버스에서 제외한다.
"""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from config import CredibilityTierConfig, ScreenerConfig

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stock_screener"}

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URL = "https://www.slickcharts.com/nasdaq100"


def _read_html_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _normalize_us_ticker(ticker: str) -> str:
    # yfinance는 '.'을 '-'로 표기 (예: BRK.B -> BRK-B)
    return ticker.strip().upper().replace(".", "-")


def fetch_us_universe() -> list[dict]:
    """S&P500 + Nasdaq100 구성종목. {ticker, name, market, index_member} 리스트."""
    rows: dict[str, dict] = {}

    try:
        sp500 = _read_html_tables(SP500_WIKI_URL)[0]
        sym_col = "Symbol" if "Symbol" in sp500.columns else sp500.columns[0]
        name_col = "Security" if "Security" in sp500.columns else sp500.columns[1]
        for _, r in sp500.iterrows():
            t = _normalize_us_ticker(str(r[sym_col]))
            rows[t] = {
                "ticker": t,
                "name": str(r[name_col]),
                "market": "US",
                "index_member": {"S&P500"},
            }
    except Exception:
        logger.exception("S&P500 목록 로드 실패")

    try:
        nd_tables = _read_html_tables(NASDAQ100_URL)
        nd100 = next(t for t in nd_tables if "Symbol" in t.columns)
        name_col = "Company" if "Company" in nd100.columns else nd100.columns[1]
        for _, r in nd100.iterrows():
            t = _normalize_us_ticker(str(r["Symbol"]))
            if t in rows:
                rows[t]["index_member"].add("Nasdaq100")
            else:
                rows[t] = {
                    "ticker": t,
                    "name": str(r[name_col]),
                    "market": "US",
                    "index_member": {"Nasdaq100"},
                }
    except Exception:
        logger.exception("Nasdaq100 목록 로드 실패 (slickcharts 접속 확인 필요)")

    if not rows:
        raise RuntimeError("미국 유니버스를 하나도 가져오지 못했습니다 (소스 파싱 실패)")

    return sorted(rows.values(), key=lambda r: r["ticker"])


def fetch_kr_universe(index_name: str = "KOSPI200", fallback_top_n: int = 200) -> list[dict]:
    """
    KOSPI200 구성종목.

    1차: pykrx.stock.get_index_portfolio_deposit_file(지수코드). KOSPI200 지수 코드는 "1028".
         *** 이 엔드포인트는 KRX_ID/KRX_PW 환경변수(mykrx.co.kr 로그인) 없이는 빈 응답만
         돌려주는 것이 확인됐다 (2026-09) — 로그인 자격증명을 설정하면 정상 동작한다. ***
    2차(폴백): 로그인 없이 접근 가능한 FinanceDataReader 상장목록에서 KOSPI 시가총액 상위
         fallback_top_n개를 근사 유니버스로 사용한다. 정확한 KOSPI200 구성종목은 아니지만,
         "지수/대형주 위주"라는 목적에는 부합한다. 이 경우 index_member에 "근사" 표시를 남긴다.
    """
    from pykrx import stock

    index_ticker_map = {
        "KOSPI200": "1028",
        "KOSPI": "1001",
        "KOSDAQ150": "2203",
    }
    index_ticker = index_ticker_map.get(index_name)
    if index_ticker is None:
        raise ValueError(f"지원하지 않는 지수명: {index_name} (지원: {list(index_ticker_map)})")

    tickers: list[str] = []
    try:
        tickers = stock.get_index_portfolio_deposit_file(index_ticker) or []
    except Exception:
        logger.warning("pykrx 지수 구성종목 조회 실패", exc_info=True)

    if tickers:
        rows = []
        for t in tickers:
            try:
                name = stock.get_market_ticker_name(t)
            except Exception:
                name = t
            rows.append({"ticker": t, "name": name, "market": "KR", "index_member": {index_name}})
        return rows

    logger.warning(
        "%s 구성종목을 pykrx로 가져오지 못했습니다 (KRX_ID/KRX_PW 로그인 필요할 수 있음). "
        "FinanceDataReader 시가총액 상위 %d개로 대체합니다.",
        index_name, fallback_top_n,
    )
    return _fallback_kr_universe_by_marcap(index_name, fallback_top_n)


def _fallback_kr_universe_by_marcap(index_name: str, top_n: int) -> list[dict]:
    from data_fetcher import fetch_kr_listing_table

    table = fetch_kr_listing_table()
    kospi = table[table["Market"] == "KOSPI"].copy()
    kospi = kospi.sort_values("Marcap", ascending=False).head(top_n)

    rows = []
    for code, r in kospi.iterrows():
        rows.append({
            "ticker": str(code),
            "name": str(r["Name"]),
            "market": "KR",
            "index_member": {f"{index_name}(근사:시총상위)"},
        })
    if not rows:
        raise RuntimeError(f"{index_name} 구성종목을 가져오지 못했습니다 (pykrx/FDR 모두 실패)")
    return rows


def classify_credibility_tier(
    market_cap: float | None,
    market: str,
    is_index_member: bool,
    cfg: CredibilityTierConfig,
) -> str:
    """
    신뢰도 등급: 지수 > 대형주 > 중형주 > 소형주.
    - 지수 구성종목 자체는 이미 이 프로그램의 유니버스 진입 조건이므로, 그중에서도
      시총 최상위(mega_cap 컷 이상)는 "지수/초대형주"로 별도 표기해 신뢰도를 더 높게 본다.
    """
    if market_cap is None or market_cap <= 0:
        return "중형주" if is_index_member else "소형주"

    mega = cfg.krw_mega_cap if market == "KR" else cfg.usd_mega_cap
    large = cfg.krw_large_cap if market == "KR" else cfg.usd_large_cap
    mid = cfg.krw_mid_cap if market == "KR" else cfg.usd_mid_cap

    if market_cap >= mega:
        return "지수/초대형주"
    if market_cap >= large:
        return "대형주"
    if market_cap >= mid:
        return "중형주"
    return "소형주"


def build_universe(cfg: ScreenerConfig, markets: tuple[str, ...] = ("KR", "US")) -> list[dict]:
    """마켓별 유니버스를 합쳐서 반환. 시총/신뢰도 등급은 아직 채우지 않음(price.py에서 채움)."""
    combined: list[dict] = []
    if "KR" in markets:
        try:
            combined.extend(fetch_kr_universe(cfg.universe.kr_index))
        except Exception:
            logger.exception("국내 유니버스 로드 실패")
    if "US" in markets:
        try:
            combined.extend(fetch_us_universe())
        except Exception:
            logger.exception("미국 유니버스 로드 실패")
    if not combined:
        raise RuntimeError("유니버스를 하나도 만들지 못했습니다 (KR/US 모두 실패)")
    return combined
