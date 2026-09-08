"""
토스증권 Open API 클라이언트.

pykrx는 KRX 웹사이트를 스크래핑하는 비공식 경로라 언제 막혀도 이상하지 않고, 실제로
해외 IP에서는 차단된다. 이 모듈은 공식 REST API(https://openapi.tossinvest.com)로
같은 데이터를 받아온다. 스펙은 서버가 직접 배포하는 OpenAPI 문서를 기준으로 작성했다:
    https://openapi.tossinvest.com/openapi-docs/latest/openapi.json  (v1.2.15)

pykrx 대비 얻는 것:
  - 종목별 투자자별 매매동향이 일별 시계열로, 기관 7개 세부 분류까지 나온다.
  - 캔들에 수정주가(adjusted) 옵션이 있다.
  - 초당 20회(캔들)까지 허용돼 pykrx보다 훨씬 빠르다.

pykrx로만 되던 것(이 API에 없는 것):
  - 종목별 PER/EPS 시계열, 지수 PER  -> 밸류에이션 밴드는 다른 방식으로 대체해야 한다.
  - 지수 구성종목(KOSPI200)          -> 전체 종목 + 시가총액 상위로 근사한다.

자격증명은 .env에 둔다 (토스증권 WTS > 설정 > Open API에서 발급, 호출 IP 등록 필요):
    TOSS_CLIENT_ID=...
    TOSS_CLIENT_SECRET=...
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.tossinvest.com"

# 스펙의 Rate Limits 표 (클라이언트 × 그룹 단위 초당 요청 수).
# 한도는 사전 공지 없이 조정될 수 있어 응답 헤더가 정답이지만, 여기서 미리 지켜 429를 줄인다.
RATE_LIMITS = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "STOCK_ALL": 1,
    "STOCK_TRADING_TREND": 10,
    "MARKET_INFO": 3,
    "MARKET_DATA": 15,
    "MARKET_DATA_CHART": 20,
}

MAX_CANDLES_PER_PAGE = 200      # 스펙상 count 최대값
MAX_TRADING_PER_PAGE = 100

_TOKEN_SAFETY_SEC = 60          # 만료 직전에 미리 갱신


class TossAPIError(RuntimeError):
    pass


class _RateLimiter:
    """그룹별 초당 요청 수 제한. 마지막 1초 안의 호출 수를 세고 넘치면 그만큼만 잔다."""

    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, group: str) -> None:
        limit = RATE_LIMITS.get(group)
        if not limit:
            return
        with self._lock:
            calls = self._calls.setdefault(group, deque())
            now = time.monotonic()
            while calls and now - calls[0] >= 1.0:
                calls.popleft()
            if len(calls) >= limit:
                sleep_for = 1.0 - (now - calls[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while calls and now - calls[0] >= 1.0:
                    calls.popleft()
            calls.append(time.monotonic())


class TossClient:
    """토스증권 Open API 클라이언트. 토큰 발급/갱신과 레이트리밋을 알아서 처리한다."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise TossAPIError(
                "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 없습니다. "
                "토스증권 WTS > 설정 > Open API에서 발급받아 .env에 넣으세요."
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._limiter = _RateLimiter()
        self._token: str | None = None
        self._token_expires_at = 0.0

    # ------------------------------------------------------------------ 인증
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - _TOKEN_SAFETY_SEC:
            return self._token

        self._limiter.acquire("AUTH")
        resp = self._session.post(
            f"{BASE_URL}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise TossAPIError(f"토큰 발급 실패 ({resp.status_code}): {resp.text[:200]}")
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise TossAPIError(f"토큰 응답에 access_token이 없습니다: {payload}")
        self._token = token
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        logger.info("토스 액세스 토큰 발급 (만료 %.0f초 후)", payload.get("expires_in", 3600))
        return token

    # ------------------------------------------------------------------ 요청
    def _get(self, path: str, group: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last_error = None
        for attempt in range(self.max_retries):
            self._limiter.acquire(group)
            try:
                resp = self._session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {self._access_token()}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                # 토큰 만료로 보고 한 번 더 발급받아 재시도한다.
                self._token = None
                last_error = TossAPIError(f"401 인증 실패: {resp.text[:200]}")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("%s %s -> %d, %.1f초 후 재시도", path, params.get("symbol", ""), resp.status_code, wait)
                time.sleep(wait)
                last_error = TossAPIError(f"{resp.status_code}: {resp.text[:200]}")
                continue
            raise TossAPIError(f"{path} 실패 ({resp.status_code}): {resp.text[:300]}")

        raise TossAPIError(f"{path} 재시도 소진: {last_error}")

    # ------------------------------------------------------------ 종목 정보
    def get_stocks(self, symbols: list[str]) -> list[dict]:
        """종목 기본 정보. symbols는 콤마로 묶어 한 번에 보낸다."""
        if not symbols:
            return []
        data = self._get("/api/v1/stocks", "STOCK", {"symbols": ",".join(symbols)})
        return data.get("result") or []

    def get_all_stocks(
        self, market: str, security_type: str | None = "STOCK", common_share: bool | None = True
    ) -> list[dict]:
        """마켓별 전체 종목. market: KOSPI / KOSDAQ / NYSE / NASDAQ / AMEX 등."""
        data = self._get(
            "/api/v1/stocks/all",
            "STOCK_ALL",
            {
                "market": market,
                "status": "ACTIVE",
                "securityType": security_type,
                "commonShare": None if common_share is None else str(common_share).lower(),
            },
        )
        return data.get("result") or []

    # ------------------------------------------------------------------ 시세
    def get_candles(
        self, symbol: str, interval: str = "1d", count: int = 500, adjusted: bool = True
    ) -> list[dict]:
        """캔들 목록을 오래된 순으로 돌려준다.

        API는 한 번에 최대 200봉을 최신순으로 주고 nextBefore로 페이지를 잇는다.
        여기서는 필요한 개수를 다 모은 뒤 시간 오름차순으로 뒤집어 반환한다.
        """
        collected: list[dict] = []
        before: str | None = None
        while len(collected) < count:
            page_size = min(MAX_CANDLES_PER_PAGE, count - len(collected))
            data = self._get(
                "/api/v1/candles",
                "MARKET_DATA_CHART",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "count": page_size,
                    "before": before,
                    "adjusted": str(adjusted).lower(),
                },
            )
            result = data.get("result") or {}
            candles = result.get("candles") or []
            if not candles:
                break
            collected.extend(candles)
            before = result.get("nextBefore")
            if not before:
                break
        collected.reverse()  # 최신순 -> 오래된 순
        return collected

    def get_investor_trading(self, symbol: str, days: int = 60) -> list[dict]:
        """투자자별 매매동향(일별)을 오래된 순으로. 국내 종목만 지원한다."""
        collected: list[dict] = []
        until: str | None = None
        while len(collected) < days:
            page_size = min(MAX_TRADING_PER_PAGE, days - len(collected))
            data = self._get(
                f"/api/v1/stocks/{symbol}/investor-trading",
                "STOCK_TRADING_TREND",
                {"count": page_size, "until": until},
            )
            result = data.get("result") or {}
            records = result.get("records") or []
            if not records:
                break
            collected.extend(records)
            until = result.get("nextUntil")
            if not until:
                break
        collected.reverse()
        return collected


# ---------------------------------------------------------------- 변환 헬퍼
def candles_to_ohlcv(candles: list[dict]) -> pd.DataFrame:
    """캔들 목록을 기존 파이프라인이 쓰는 OHLCV 데이터프레임으로.

    응답의 수치는 전부 문자열이라(정밀도 보존 목적) 여기서 숫자로 바꾼다.
    인덱스는 날짜(tz 제거) 오름차순 — data_fetcher가 쓰는 형식과 같다.
    """
    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    df = pd.DataFrame(candles)
    index = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.assign(_idx=index.dt.tz_convert("Asia/Seoul").dt.tz_localize(None).dt.normalize())
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(df["openPrice"], errors="coerce"),
            "High": pd.to_numeric(df["highPrice"], errors="coerce"),
            "Low": pd.to_numeric(df["lowPrice"], errors="coerce"),
            "Close": pd.to_numeric(df["closePrice"], errors="coerce"),
            "Volume": pd.to_numeric(df["volume"], errors="coerce"),
        }
    )
    out.index = df["_idx"]
    out.index.name = "date"
    return out[~out.index.duplicated(keep="last")].sort_index().dropna(subset=["Close"])


def investor_trading_to_series(records: list[dict]) -> dict[str, pd.Series]:
    """투자자별 매매동향을 {"foreign": Series, "institution": Series} 순매수 시계열로.

    기존 pykrx 경로는 순매수 '거래대금'이었고 이쪽은 '거래량(주)'이다. 연속 순매수 일수를
    세는 데는 부호만 쓰므로 판정 결과는 같지만, 값의 단위가 다르다는 점은 알고 있어야 한다.
    """
    if not records:
        return {}
    rows = {"foreign": {}, "institution": {}}
    for r in records:
        date = pd.to_datetime(r.get("date"), errors="coerce")
        if pd.isna(date):
            continue
        for key, field in (("foreign", "foreigner"), ("institution", "institution")):
            block = r.get(field) or {}
            value = block.get("netBuyVolume")
            if value is not None:
                rows[key][date] = pd.to_numeric(value, errors="coerce")
    return {
        key: pd.Series(values).sort_index()
        for key, values in rows.items()
        if values
    }


def probe() -> None:
    """자격증명과 엔드포인트가 살아 있는지 빠르게 확인한다."""
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    client = TossClient()
    info = client.get_stocks(["005930", "000660"])
    for s in info:
        print(f"  종목: {s.get('symbol')} {s.get('name')} 발행주식수={s.get('sharesOutstanding')} 시장={s.get('market')}")

    ohlcv = candles_to_ohlcv(client.get_candles("005930", count=10))
    print(f"  캔들 {len(ohlcv)}행\n{ohlcv.tail(3)}")

    series = investor_trading_to_series(client.get_investor_trading("005930", days=10))
    for key, s in series.items():
        print(f"  수급 {key}: {len(s)}일, 최근 3일 {list(s.tail(3).values)}")

    kospi = client.get_all_stocks("KOSPI")
    print(f"  KOSPI 전체 종목: {len(kospi)}개")


if __name__ == "__main__":
    probe()
