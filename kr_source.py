"""
국내(KR) 데이터 공급자 — 토스증권 Open API 구현.

data_fetcher / universe 가 국내 데이터를 필요로 할 때 여기를 먼저 부르고, 실패하면 기존
pykrx 경로로 떨어진다. 모든 함수는 예외를 밖으로 내보내지 않고 None(또는 빈 값)을 돌려준다 —
데이터 소스 하나가 죽어도 스크리너 전체가 멈추면 안 되기 때문이다.

토스로 옮긴 것:
  - 일봉 OHLCV        /api/v1/candles (수정주가 적용)
  - 시가총액           /api/v1/stocks 의 발행주식수 × /api/v1/prices 의 현재가
  - 외국인/기관 수급    /api/v1/stocks/{symbol}/investor-trading
  - 국내 유니버스       /api/v1/stocks/all + 시가총액 상위

토스에 없어서 못 옮긴 것: 종목 PER/EPS 시계열, 지수 PER, KOSPI200 구성종목.
앞의 둘은 fundamentals.py가 순이익/발행주식수로 PER을 직접 계산해 메우고, 구성종목은
시가총액 상위 N개로 근사한다(기존 폴백과 같은 방식).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CAP_CACHE_FILE = Path(__file__).parent / "cache" / "meta" / "toss_kr_marketcap.csv"
CAP_CACHE_HOURS = 24.0
BATCH_SIZE = 200  # /stocks, /prices 모두 한 번에 200건까지

_client = None
_client_failed = False
_cap_table: pd.DataFrame | None = None


def get_client():
    """TossClient 싱글턴. 자격증명이 없거나 초기화에 실패하면 None (조용히 pykrx로 폴백)."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        from toss_api import TossClient

        _client = TossClient()
        logger.info("토스증권 Open API 사용")
    except Exception as exc:
        _client_failed = True
        logger.info("토스증권 API를 쓰지 않는다 (%s) — pykrx 경로로 진행", exc)
    return _client


def available() -> bool:
    return get_client() is not None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------- 시세
def fetch_ohlcv(ticker: str, days: int) -> pd.DataFrame | None:
    """일봉 OHLCV. days는 달력 일수이므로 거래일 기준으로 넉넉히 환산해 받는다."""
    client = get_client()
    if client is None:
        return None
    try:
        from toss_api import candles_to_ohlcv

        # 주말·공휴일을 감안해 달력 일수의 약 70%가 거래일. 여유를 둬 10봉 더 받는다.
        count = max(30, int(days * 0.7) + 10)
        df = candles_to_ohlcv(client.get_candles(ticker, interval="1d", count=count, adjusted=True))
        return df if not df.empty else None
    except Exception:
        logger.warning("토스 일봉 조회 실패: %s", ticker, exc_info=True)
        return None


# ----------------------------------------------------------------- 시가총액
def _build_cap_table(markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """시장별 전체 보통주에 대해 (발행주식수 × 현재가)로 시가총액 표를 만든다.

    종목마다 따로 묻지 않고 200건씩 묶어 부르므로 800종목이 열 번 남짓의 호출로 끝난다.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("토스 클라이언트를 만들지 못했습니다")

    rows: dict[str, dict] = {}
    for market in markets:
        for item in client.get_all_stocks(market):
            symbol = item.get("symbol")
            if symbol:
                rows[symbol] = {"Code": symbol, "Name": item.get("name"), "Market": market}

    symbols = list(rows)
    for batch in _chunks(symbols, BATCH_SIZE):
        for info in client.get_stocks(batch):
            row = rows.get(info.get("symbol"))
            if row is not None:
                row["Shares"] = pd.to_numeric(info.get("sharesOutstanding"), errors="coerce")

    for batch in _chunks(symbols, BATCH_SIZE):
        data = client._get("/api/v1/prices", "MARKET_DATA", {"symbols": ",".join(batch)})
        for price in data.get("result") or []:
            row = rows.get(price.get("symbol"))
            if row is not None:
                row["Close"] = pd.to_numeric(price.get("lastPrice"), errors="coerce")

    table = pd.DataFrame(list(rows.values()))
    if table.empty:
        raise RuntimeError("토스에서 국내 종목을 하나도 받지 못했습니다")
    table["Marcap"] = table.get("Shares") * table.get("Close")
    table = table.dropna(subset=["Marcap"])
    table = table[table["Marcap"] > 0].sort_values("Marcap", ascending=False).reset_index(drop=True)
    return table


def cap_table(max_age_hours: float = CAP_CACHE_HOURS) -> pd.DataFrame | None:
    """시가총액 표(캐시 우선). 실패하면 None."""
    global _cap_table
    if _cap_table is not None:
        return _cap_table

    if CAP_CACHE_FILE.exists():
        age = (time.time() - CAP_CACHE_FILE.stat().st_mtime) / 3600
        if age <= max_age_hours:
            try:
                _cap_table = pd.read_csv(CAP_CACHE_FILE, dtype={"Code": str})
                return _cap_table
            except Exception:
                logger.warning("토스 시가총액 캐시 손상, 다시 만든다")

    try:
        table = _build_cap_table()
    except Exception:
        logger.warning("토스 시가총액 표 생성 실패", exc_info=True)
        if CAP_CACHE_FILE.exists():
            try:
                _cap_table = pd.read_csv(CAP_CACHE_FILE, dtype={"Code": str})
                logger.warning("만료된 시가총액 캐시를 대신 쓴다")
                return _cap_table
            except Exception:
                pass
        return None

    try:
        CAP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(CAP_CACHE_FILE, index=False)
    except Exception:
        logger.warning("토스 시가총액 캐시 저장 실패 (무시하고 진행)")
    _cap_table = table
    return table


def fetch_market_cap(ticker: str) -> float | None:
    table = cap_table()
    if table is None:
        return None
    hit = table[table["Code"] == ticker]
    if hit.empty:
        return None
    value = hit.iloc[0]["Marcap"]
    return float(value) if pd.notna(value) else None


def fetch_shares_outstanding(ticker: str) -> float | None:
    """발행주식수. PER을 직접 계산할 때 쓴다(토스는 PER을 주지 않는다)."""
    table = cap_table()
    if table is None or "Shares" not in table.columns:
        return None
    hit = table[table["Code"] == ticker]
    if hit.empty:
        return None
    value = hit.iloc[0]["Shares"]
    return float(value) if pd.notna(value) else None


# --------------------------------------------------------------------- 수급
def fetch_investor_series(ticker: str, days: int = 60) -> dict[str, pd.Series] | None:
    """외국인/기관 순매수 시계열. pykrx 경로는 거래대금이었고 이쪽은 거래량(주)이다 —
    연속 순매수 일수는 부호만 보므로 판정은 같다."""
    client = get_client()
    if client is None:
        return None
    try:
        from toss_api import investor_trading_to_series

        series = investor_trading_to_series(client.get_investor_trading(ticker, days=days))
        return series or None
    except Exception:
        logger.warning("토스 수급 조회 실패: %s", ticker, exc_info=True)
        return None


# ----------------------------------------------------------------- 유니버스
def fetch_universe(top_n: int = 200, market: str = "KOSPI") -> list[dict] | None:
    """시가총액 상위 N개 보통주. KOSPI200 정확한 구성종목은 토스가 주지 않으므로 근사다."""
    table = cap_table()
    if table is None:
        return None
    subset = table[table["Market"] == market].head(top_n)
    if subset.empty:
        return None
    return [
        {
            "ticker": str(r["Code"]),
            "name": str(r["Name"]),
            "market": "KR",
            "index_member": {f"{market}(토스 시총상위 근사)"},
        }
        for _, r in subset.iterrows()
    ]


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("토스 사용 가능:", available())
    table = cap_table()
    print(f"시가총액 표: {len(table) if table is not None else 0}종목")
    if table is not None:
        print(table.head(5).to_string())
    print("삼성전자 시총:", fetch_market_cap("005930"))
    ohlcv = fetch_ohlcv("005930", 400)
    print(f"일봉 {len(ohlcv) if ohlcv is not None else 0}행, 마지막 {ohlcv.index[-1].date() if ohlcv is not None else '-'}")
    uni = fetch_universe(5)
    print("유니버스 상위 5:", [(u["ticker"], u["name"]) for u in (uni or [])])
