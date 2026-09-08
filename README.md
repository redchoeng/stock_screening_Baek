# 반등 신호 스크리너 (stock_screener)

스토캐스틱 슬로우와 윌리엄스 %R이 **동시에** 과매도일 때만 신호로 인정하는, 교차확인 기반 종목 스크리너.
국내(KOSPI200)와 미국(S&P500 / Nasdaq100) 종목을 대상으로 한다.

## 핵심 설계 원칙

- **단일 지표는 신호로 쓰지 않는다.** 스토캐스틱 + 윌리엄스 %R이 같은 시점에 함께 과매도여야 "반등 후보"로 인정한다.
  이 AND 조건이 성립하지 않으면 표준편차 스퀴즈·거래량·수급이 아무리 좋아도 **0점**이다.
- **판정 기준은 주봉(기본값).** 일봉은 보조 확인으로만 쓰고 가점을 준다. `config.core_timeframe`으로 바꿀 수 있다.
- **이동평균 골든/데드크로스는 신호로 쓰지 않는다.** 차트에 이동평균선은 그리지만 채점에는 일절 반영하지 않는다.
- 이 점수는 **현재 과매도/과매수 상태를 교차 확인한 결과**이며 미래 수익을 예측하지 않는다.

## 설치

```bash
pip install -r requirements.txt
```

`.env.example`을 `.env`로 복사하고 KRX 계정(mykrx.co.kr)을 채운다:

```
KRX_ID=your_id
KRX_PW=your_password
```

KRX 로그인이 없어도 동작하지만, 다음 기능이 제한된다:
- KOSPI200 정확한 구성종목 → FinanceDataReader 시가총액 상위 200종목 근사치로 대체
- 외국인/기관 수급 데이터 → 수집 불가, 해당 항목 없이 채점

## 사용법

```bash
# 국내 + 미국 전체 스크리닝
python main.py

# 국내만, 상위 30종목, 차트 생략
python main.py --markets KR --limit 30 --no-charts

# HTML 대시보드용 JSON 추출
python export_dashboard.py
```

결과는 `output/`에 CSV/Excel로, 차트는 `output/charts/`에 PNG로 저장된다.

## 계산하는 지표

| 지표 | 기본 설정 | 기준 봉 |
|---|---|---|
| 스토캐스틱 슬로우 | %K 15, 평활 5, %D 5 / 과매도 20 · 과매수 80 | core_timeframe (기본 주봉) |
| 윌리엄스 %R | 15 / 과매도 -80 · 과매수 -20 | core_timeframe (기본 주봉) |
| 자체 변동성 지표 | 19일 / -100~0 범위 | 항상 일봉 |
| 표준편차 스퀴즈 | 20일 표준편차의 국소 최소 후 재확대 | 항상 일봉 |
| 거래량 | 52주 평균 대비 대형주 2.5배 · 중소형주 5배 | 항상 일봉 |
| 외국인/기관 수급 | 10일 이상 연속 순매수 (20일이면 만점) | 국내 종목만 |

## 배점

| 항목 | 가중치 |
|---|---|
| 스토캐스틱 + 윌리엄스 AND 과매도 | 40 |
| 표준편차 스퀴즈 재확대 | 25 |
| 거래량 급증 (상승 방향) | 20 |
| 외국인/기관 연속 순매수 (국내만) | 15 |
| 자체 변동성 지표 동시 과매도 | 10 |
| 반대쪽 타임프레임에서도 확인 | 10 |

각 시장에서 획득 가능한 배점 합계로 나눠 0~100으로 정규화한다. 국내는 수급 항목이 있어 분모가
더 크므로, **점수는 같은 시장 안에서만 비교해야 한다.**

## 설정 변경

모든 기간·임계값·가중치는 `config.py`의 dataclass에 있다. 코드를 건드리지 않고 바꾸려면
프로젝트 루트에 `screener_config.json`을 두면 해당 키만 덮어쓴다:

```json
{
  "stochastic": { "k_period": 10, "oversold": 25 },
  "core_timeframe": "daily",
  "weights": { "volume_surge_positive": 30 }
}
```

현재 유효 설정 전체를 템플릿으로 뽑으려면:

```bash
python config.py > screener_config.json
```

## 구조

```
main.py               CLI 진입점, 유니버스 순회 + 결과 저장
config.py             모든 파라미터 (하드코딩 없음)
universe.py           KOSPI200 / S&P500 / Nasdaq100 종목 목록, 신뢰도 등급 분류
data_fetcher.py       OHLCV / 시가총액 / 수급 수집 + 디스크 캐시
indicators/           지표별 순수 계산 함수
scoring.py            AND 조건 판정 및 종합 점수
output.py             CSV / Excel 저장
visualize.py          종목별 캔들 + 지표 차트 PNG
export_dashboard.py   HTML 대시보드용 JSON 추출
```

## 주의

기술적 지표는 과거 가격의 통계적 요약일 뿐이며 미래를 예측하지 않는다.
이 프로그램의 결과를 매매 판단의 유일한 근거로 사용하지 말 것.
