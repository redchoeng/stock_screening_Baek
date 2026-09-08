"""결과 저장 (CSV/Excel). scoring.score_ticker()가 반환한 dict 리스트를 입력으로 받는다."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RESULT_COLUMNS = [
    "ticker", "name", "market", "credibility_tier", "index_member",
    "score", "is_rebound_candidate", "is_top_warning", "core_timeframe", "secondary_confirmed",
    "last_date", "last_close", "market_cap",
    "slow_k", "slow_d", "williams_r", "custom_volatility",
    "core_oversold_and_signal", "core_overbought_and_signal", "custom_vol_bonus_earned",
    "stddev_squeeze_signal", "stddev_days_since_local_min",
    "volume_ratio", "volume_direction", "volume_positive_signal", "volume_warning_signal",
    "volume_recovery_signal",
    "foreign_buy_streak_days", "foreign_buy_meaningful",
    "institution_buy_streak_days", "institution_buy_meaningful",
]


def results_to_dataframe(results: list[dict]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    df = pd.DataFrame(results)
    cols = [c for c in RESULT_COLUMNS if c in df.columns] + [c for c in df.columns if c not in RESULT_COLUMNS]
    return df[cols]


def save_results(
    results: list[dict],
    out_dir: Path,
    run_tag: str | None = None,
    formats: tuple[str, ...] = ("csv", "xlsx"),
) -> dict[str, Path]:
    """
    전체 결과, 반등 후보(과매도 AND 신호, 점수 내림차순), 고점 경계(과매수 AND 신호)를
    각각 저장한다. 신뢰도 등급(지수/초대형주 > 대형주 > 중형주 > 소형주) 컬럼을 포함한다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = run_tag or dt.date.today().strftime("%Y%m%d")

    df_all = results_to_dataframe(results)
    df_rebound = df_all[df_all.get("is_rebound_candidate", False) == True].sort_values(  # noqa: E712
        "score", ascending=False
    )
    df_warning = df_all[df_all.get("is_top_warning", False) == True]  # noqa: E712

    written: dict[str, Path] = {}
    datasets = {"all_screened": df_all, "rebound_candidates": df_rebound, "top_warnings": df_warning}

    for label, df in datasets.items():
        if "csv" in formats:
            path = out_dir / f"{label}_{tag}.csv"
            df.to_csv(path, index=False, encoding="utf-8-sig")  # BOM 포함 -> 엑셀에서 한글 깨짐 방지
            written[f"{label}_csv"] = path
        if "xlsx" in formats:
            path = out_dir / f"{label}_{tag}.xlsx"
            try:
                df.to_excel(path, index=False)
                written[f"{label}_xlsx"] = path
            except Exception:
                logger.warning("엑셀 저장 실패 (openpyxl 설치 확인): %s", path, exc_info=True)

    logger.info(
        "결과 저장 완료: 전체 %d개, 반등 후보 %d개, 고점 경계 %d개 -> %s",
        len(df_all), len(df_rebound), len(df_warning), out_dir,
    )
    return written
