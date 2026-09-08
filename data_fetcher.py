"""
OHLCV / 시가총액 / (국내 한정) 수급 데이터 수집.

- 국내(KR) 일봉 OHLCV: pykrx (로그인 없이 동작 확인됨)
- 국내 시가총액 / 지수 구성종목 폴백: FinanceDataReader

*** 중요: pykrx KRX_ID/KRX_PW 로그인 이슈 (2026-09 확인) ***
pykrx의 get_market_cap_by_date, get_index_portfolio_deposit_file,
get_market_trading_value_by_date(외국인 순매수) 등 "종목 횡단면(cross-sectional)" 계열
엔드포인트는 KRX_ID/KRX_PW 환경변수(mykrx.co.kr 계정 로그인)가 없으면 빈 응답만 돌려준다.
반면 get_market_ohlcv_by_date(일봉 시세)는 로그인 없이 정상 동작한다.
그래서 이 모듈은:
  - 시가총액/지수 구성종목: 로그인이 필요 없는 FinanceDataReader의 전체 KRX 상장목록으로 대체
    (KOSPI200 정확한 구성종목이 아니라 "KOSPI 시가총액 상위 N종목" 근사치가 된다 — universe.py 참고)
  - 외국인 순매수(수급): 대체 무료 소스가 마땅치 않아, 실패 시 그냥 None을 반환하고
    해당 항목 없이 점수를 계산한다(요청서에서도 이 항목은 "선택"으로 명시됨).
    KRX_ID/KRX_PW를 환경변수로 설정하면 pykrx가 정상 동작해 이 기능도 살아난다.
- 미국(US): yfinance

수백 종목을 매번 새로 받으면 느리고 API에도 부담이 크므로, ./cache/ohlcv/ 아래에
종목별 CSV로 캐시하고, 캐시가 `cache_max_age_hours`보다 오래됐을 때만 재다운로드한다.
당일 실시간성이 중요한 프로그램이 아니라 스윙 스크리닝용이므로 기본 24시간 캐시면 충분하다.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# pykrx는 KRX_ID/KRX_PW를 os.environ에서 직접 읽는다. .env 파일(있으면)을 여기서 미리
# 로드해둬야 이 모듈 아래의 모든 pykrx 호출이 로그인된 상태로 동작한다.
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache" / "ohlcv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

META_CACHE_DIR = Path(__file__).parent / "cache" / "meta"
META_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_KR_LISTING_CACHE_FILE = META_CACHE_DIR / "kr_listing.csv"

# 외국인 순매수 엔드포인트가 (KRX 로그인 미설정 등으로) 막혀 있으면, 종목마다 매번
# 실패하는 네트워크 호출을 반복하지 않도록 프로세스 내에서 한 번만 시도하고 기억해둔다.
_foreign_data_available: bool | None = None

# KR 상장목록은 종목마다 필요하지만 내용은 하루에 한 번만 바뀐다. 프로세스 안에서 한 번만
# 만들어 재사용하고, 다운로드가 실패했다는 사실도 기억해 수백 번 재시도하지 않는다.
_kr_listing_table: pd.DataFrame | None = None
_kr_listing_download_failed = False

_MAX_RETRIES = 3
_RETRY_DELAY_SEC = 2

_STD_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _cache_path(market: str, ticker: str) -> Path:
    safe = ticker.replace("/", "_")
    return CACHE_DIR / f"{market}_{safe}.csv"


def _load_cache(market: str, ticker: str, max_age_hours: float) -> pd.DataFrame | None:
    path = _cache_path(market, ticker)
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df if not df.empty else None
    except Exception:
        logger.warning("캐시 파일 손상, 재다운로드: %s", path)
        return None


def _save_cache(market: str, ticker: str, df: pd.DataFrame) -> None:
    try:
        df.to_csv(_cache_path(market, ticker))
    except Exception:
        logger.warning("캐시 저장 실패 (무시하고 진행): %s/%s", market, ticker)


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance는 단일 티커 다운로드도 (Price, Ticker) MultiIndex 컬럼을 반환하는 버전이 있다."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def _fetch_ohlcv_us_raw(ticker: str, days: int) -> pd.DataFrame:
    import yfinance as yf

    period_days = max(days, 30)
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            df = yf.download(
                tickers=ticker,
                period=f"{period_days}d",
                interval="1d",
                auto_adjust=False,
                progress=False,
            )
            if df is not None and not df.empty:
                df = _flatten_yf_columns(df)
                return df[_STD_COLS].dropna(subset=["Close", "High", "Low"])
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("yfinance 다운로드 실패 %s (attempt %d/%d): %s", ticker, attempt, _MAX_RETRIES, exc)
        time.sleep(_RETRY_DELAY_SEC)
    if last_exc:
        raise last_exc
    return pd.DataFrame(columns=_STD_COLS)


def _fetch_ohlcv_kr_raw(ticker: str, days: int) -> pd.DataFrame:
    # 토스증권 Open API가 1순위. 공식 REST라 막힐 일이 없고 수정주가까지 적용된다.
    # 자격증명이 없거나 실패하면 아래 pykrx 경로로 그대로 떨어진다.
    import kr_source

    toss_df = kr_source.fetch_ohlcv(ticker, days)
    if toss_df is not None and not toss_df.empty:
        return toss_df[_STD_COLS].dropna(subset=["Close", "High", "Low"])

    from pykrx import stock

    todate = dt.date.today()
    fromdate = todate - dt.timedelta(days=int(days * 1.6) + 10)  # 주말/휴장일 감안 여유
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            df = stock.get_market_ohlcv_by_date(
                fromdate.strftime("%Y%m%d"), todate.strftime("%Y%m%d"), ticker
            )
            if df is not None and not df.empty:
                df = df.rename(
                    columns={"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}
                )
                return df[_STD_COLS].dropna(subset=["Close", "High", "Low"])
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("pykrx OHLCV 실패 %s (attempt %d/%d): %s", ticker, attempt, _MAX_RETRIES, exc)
        time.sleep(_RETRY_DELAY_SEC)
    if last_exc:
        raise last_exc
    return pd.DataFrame(columns=_STD_COLS)


def fetch_ohlcv(ticker: str, market: str, days: int = 730, cache_max_age_hours: float = 24.0) -> pd.DataFrame:
    """일봉 OHLCV. market은 'KR' 또는 'US'."""
    cached = _load_cache(market, ticker, cache_max_age_hours)
    if cached is not None:
        return cached

    if market == "KR":
        df = _fetch_ohlcv_kr_raw(ticker, days)
    elif market == "US":
        df = _fetch_ohlcv_us_raw(ticker, days)
    else:
        raise ValueError(f"알 수 없는 market: {market}")

    if not df.empty:
        _save_cache(market, ticker, df)
    return df


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 -> 주봉 리샘플 (금요일 마감 기준). 강의에서 '주봉이 더 신뢰도 높다'고 한 부분에 사용."""
    if df.empty:
        return df
    weekly = df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return weekly.dropna(subset=["Close"])


def _read_kr_listing_cache() -> pd.DataFrame:
    return pd.read_csv(_KR_LISTING_CACHE_FILE, dtype={"Code": str}).set_index("Code")


def fetch_kr_listing_table(cache_max_age_hours: float = 24.0) -> pd.DataFrame:
    """
    FinanceDataReader 전체 KRX 상장목록 (Code 인덱스, Name/Market/Marcap 등 컬럼).
    pykrx의 시가총액/지수구성 엔드포인트가 KRX 로그인 없이는 막혀 있어, 그 대체로 쓴다.
    수백 종목을 개별 조회하는 대신 한 번에 통째로 받아서 로컬 캐시해 재사용한다.

    *** 견고성 (2026-09 확인) ***
    fdr.StockListing("KRX")가 HTTP 404를 내는 일이 있다(업스트림 소스 변경). 이 함수는
    종목마다 호출되므로, 그대로 두면 실패한 네트워크 요청을 수백 번 반복하면서 국내 종목의
    시가총액이 통째로 비어 신뢰도 등급과 거래량 임계값이 어긋난다. 그래서:
      - 결과를 프로세스 안에서 한 번만 만들고 재사용한다 (실패도 기억한다).
      - 다운로드가 실패하면 만료된 캐시라도 쓴다. 시가총액은 하루 이틀 묵어도 등급 판정에는
        충분하고, 값이 아예 없는 것보다 낫다.
    """
    global _kr_listing_table, _kr_listing_download_failed
    if _kr_listing_table is not None:
        return _kr_listing_table

    cache_exists = _KR_LISTING_CACHE_FILE.exists()
    age_hours = (
        (time.time() - _KR_LISTING_CACHE_FILE.stat().st_mtime) / 3600 if cache_exists else None
    )

    if cache_exists and age_hours <= cache_max_age_hours:
        try:
            _kr_listing_table = _read_kr_listing_cache()
            return _kr_listing_table
        except Exception:
            logger.warning("KR 상장목록 캐시 손상, 재다운로드")

    if not _kr_listing_download_failed:
        try:
            import FinanceDataReader as fdr

            df = fdr.StockListing("KRX")
            df["Code"] = df["Code"].astype(str).str.zfill(6)
            df = df.set_index("Code")
            try:
                df.to_csv(_KR_LISTING_CACHE_FILE)
            except Exception:
                logger.warning("KR 상장목록 캐시 저장 실패 (무시하고 진행)")
            _kr_listing_table = df
            return _kr_listing_table
        except Exception:
            _kr_listing_download_failed = True
            logger.warning("KR 상장목록 다운로드 실패 (이후 재시도하지 않는다)", exc_info=True)

    if cache_exists:
        try:
            _kr_listing_table = _read_kr_listing_cache()
            logger.warning(
                "KR 상장목록: 만료된 캐시(%.1f시간 전)를 대신 쓴다 — 시가총액이 최신이 아니다",
                age_hours,
            )
            return _kr_listing_table
        except Exception:
            logger.warning("KR 상장목록 캐시도 읽지 못했다", exc_info=True)

    raise RuntimeError("KR 상장목록을 다운로드도 캐시도 하지 못했습니다")


def fetch_market_cap(ticker: str, market: str) -> float | None:
    """최신 시가총액. KR은 FinanceDataReader 상장목록(로그인 불필요), US는 yfinance."""
    try:
        if market == "KR":
            # 1순위: 토스 (발행주식수 × 현재가). 전 종목을 한 번에 받아 캐시하므로 종목당 비용이 없다.
            import kr_source

            cap = kr_source.fetch_market_cap(ticker)
            if cap:
                return cap

            try:
                table = fetch_kr_listing_table()
                if ticker in table.index:
                    val = table.loc[ticker, "Marcap"]
                    if pd.notna(val):
                        return float(val)
            except Exception:
                logger.warning("FDR 상장목록 기반 시가총액 조회 실패: %s", ticker, exc_info=True)

            # pykrx 폴백: KRX_ID/KRX_PW 로그인이 설정된 환경에서만 동작한다.
            try:
                from pykrx import stock

                today = dt.date.today().strftime("%Y%m%d")
                cap_df = stock.get_market_cap_by_date(today, today, ticker)
                if cap_df is not None and not cap_df.empty:
                    return float(cap_df["시가총액"].iloc[-1])
            except Exception:
                pass
            return None
        elif market == "US":
            import yfinance as yf

            info = yf.Ticker(ticker).fast_info
            cap = getattr(info, "market_cap", None)
            return float(cap) if cap else None
        else:
            raise ValueError(f"알 수 없는 market: {market}")
    except Exception:
        logger.warning("시가총액 조회 실패: %s/%s", market, ticker, exc_info=True)
        return None


def fetch_investor_net_buy_series(ticker: str, days: int = 40) -> dict[str, pd.Series] | None:
    """
    국내 종목 전용: 외국인/기관 순매수(거래대금 기준) 일별 시계열, 날짜 오름차순.
    반환: {"foreign": Series, "institution": Series} (기관 컬럼이 없는 pykrx 버전이면
    "institution" 키가 빠질 수 있다). 데이터 자체를 못 가져오면 None.

    이 엔드포인트가 KRX_ID/KRX_PW 로그인 없이 막혀 있는 게 확인되면(모듈 상단 설명 참고),
    이후 호출은 네트워크 요청 없이 바로 None을 반환한다 — 수백 종목마다 매번 실패하는
    요청을 반복하지 않기 위함. .env에 KRX_ID/KRX_PW를 채워두면 정상 동작한다.

    1순위는 토스증권 Open API다. 로그인 없이 공식 REST로 받을 수 있고 기관 세부 분류까지
    나온다. 다만 토스는 순매수 '거래량(주)'을, pykrx는 '거래대금'을 준다 — 연속 순매수
    일수는 부호만 보므로 판정 결과는 같지만 값의 단위가 다르다.
    """
    global _foreign_data_available

    import kr_source

    toss_series = kr_source.fetch_investor_series(ticker, days=max(days, 40))
    if toss_series:
        return toss_series

    if _foreign_data_available is False:
        return None

    try:
        from pykrx import stock

        todate = dt.date.today()
        fromdate = todate - dt.timedelta(days=int(days * 1.6) + 10)
        df = stock.get_market_trading_value_by_date(
            fromdate.strftime("%Y%m%d"), todate.strftime("%Y%m%d"), ticker, detail=False
        )
        if df is None or df.empty:
            if _foreign_data_available is None:
                logger.warning(
                    "외국인/기관 순매수 데이터 응답이 비어 있습니다. pykrx의 투자자별 매매동향 API는 "
                    "KRX_ID/KRX_PW 환경변수(mykrx.co.kr 로그인) 없이는 접근이 막혀 있을 수 있습니다. "
                    "이후 종목들은 수급 점수 없이 계산합니다."
                )
            _foreign_data_available = False
            return None

        foreign_col = next((c for c in ["외국인합계", "외국인", "외국인계"] if c in df.columns), None)
        institution_col = next((c for c in ["기관합계", "기관"] if c in df.columns), None)
        if foreign_col is None:
            logger.warning("외국인 순매수 컬럼을 찾지 못함 (columns=%s)", list(df.columns))
            _foreign_data_available = False
            return None

        _foreign_data_available = True
        result = {"foreign": df[foreign_col].sort_index()}
        if institution_col is not None:
            result["institution"] = df[institution_col].sort_index()
        return result
    except Exception:
        if _foreign_data_available is None:
            logger.warning("수급 데이터 조회 중 오류, 이후 요청은 자동 생략합니다: %s", ticker, exc_info=True)
        _foreign_data_available = False
        return None
