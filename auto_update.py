"""
사이트 데이터 자동 갱신.

두 스크리너를 차례로 돌리고, docs/ 아래 JSON이 실제로 바뀌었을 때만 커밋·푸시한다.
.github/workflows/pages.yml이 푸시를 받아 배포하므로 이 스크립트 하나가 끝나면 사이트가
최신이 된다.

GitHub Actions cron이 아니라 로컬에서 도는 이유:
  - pykrx가 KRX 서버에 붙는데 해외 러너 IP는 차단·제한될 수 있다. KOSPI200 구성종목·수급·
    PER 시계열이 전부 KRX 의존이라 막히면 국내 데이터가 통째로 빈다.
  - 로컬에는 이미 cache/와 .env 자격증명이 있어 훨씬 빠르고, 비밀번호를 밖으로 올릴 필요도 없다.

Windows 작업 스케줄러 등록은 register_task.ps1 참고.

사용법:
    python auto_update.py                # 갱신 후 변경분 커밋·푸시
    python auto_update.py --no-push      # 갱신만 (커밋·푸시 안 함)
    python auto_update.py --only baek    # 한쪽만 갱신
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "auto_update.log"
TRACKED_OUTPUTS = ("docs/data.json", "docs/baek_data.json")

STEPS = {
    "rebound": ("반등 신호 스크리너", "export_dashboard.py"),
    "baek": ("백 프레임 스크리너", "export_baek.py"),
}

logger = logging.getLogger("auto_update")


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    if verbose:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        logger.addHandler(stream)


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=check,
    )


def run_step(key: str, timeout_min: int) -> bool:
    """export 스크립트 하나를 돌린다. 성공 여부만 돌려주고 예외를 밖으로 내지 않는다 —
    한쪽이 실패해도 다른 쪽은 갱신되어야 한다."""
    label, script = STEPS[key]
    logger.info("[%s] 시작: %s", label, script)

    env = dict(os.environ)
    # 자식 프로세스가 윈도우 기본 코드페이지(cp949)에서 한글 로그를 찍다 죽지 않도록.
    env["PYTHONIOENCODING"] = "utf-8"

    started = dt.datetime.now()
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout_min * 60,
        )
    except subprocess.TimeoutExpired:
        logger.error("[%s] %d분 초과로 중단", label, timeout_min)
        return False

    elapsed = (dt.datetime.now() - started).total_seconds() / 60
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    if proc.returncode != 0:
        logger.error("[%s] 실패 (exit %d, %.1f분): %s", label, proc.returncode, elapsed, " | ".join(tail))
        return False

    logger.info("[%s] 완료 (%.1f분): %s", label, elapsed, " | ".join(tail))
    return True


def changed_outputs() -> list[str]:
    """docs/ 아래 결과 JSON 중 실제로 내용이 바뀐 것들."""
    result = run_git("status", "--porcelain", "--", *TRACKED_OUTPUTS)
    changed = []
    for line in result.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if path:
            changed.append(path)
    return changed


def commit_and_push(changed: list[str]) -> bool:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    message = f"데이터 자동 갱신 {stamp}\n\n" + "\n".join(f"- {p}" for p in changed)
    try:
        run_git("add", "--", *TRACKED_OUTPUTS)
        run_git("commit", "-m", message)
        logger.info("커밋 완료: %s", ", ".join(changed))
    except subprocess.CalledProcessError as exc:
        logger.error("커밋 실패: %s", (exc.stderr or exc.stdout or "").strip())
        return False

    try:
        run_git("push", "origin", "HEAD")
        logger.info("푸시 완료 — Pages가 배포를 이어받는다")
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("푸시 실패 (커밋은 로컬에 남아 있다): %s", (exc.stderr or exc.stdout or "").strip())
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="스크리너 데이터 자동 갱신")
    parser.add_argument("--no-push", action="store_true", help="갱신만 하고 커밋·푸시하지 않는다")
    parser.add_argument("--only", choices=sorted(STEPS), help="한쪽 스크리너만 갱신")
    parser.add_argument("--timeout-min", type=int, default=90, help="스크립트별 제한 시간 (기본 90분)")
    parser.add_argument("--quiet", action="store_true", help="콘솔 출력 없이 로그 파일만")
    args = parser.parse_args()

    setup_logging(verbose=not args.quiet)
    logger.info("=" * 60)
    logger.info("자동 갱신 시작 (only=%s, push=%s)", args.only or "전체", not args.no_push)

    # 갱신 전에 작업트리가 깨끗한지 본다. 손으로 고치던 게 섞여 들어가면 안 된다.
    dirty = run_git("status", "--porcelain").stdout.strip()
    if dirty:
        others = [l for l in dirty.splitlines() if l[3:].strip().strip('"') not in TRACKED_OUTPUTS]
        if others:
            logger.warning("결과 JSON 말고도 변경된 파일이 있다 — 그 파일들은 커밋하지 않는다:\n%s", "\n".join(others))

    keys = [args.only] if args.only else list(STEPS)
    results = {k: run_step(k, args.timeout_min) for k in keys}

    changed = changed_outputs()
    if not changed:
        logger.info("결과 JSON에 변경 없음 — 커밋할 것이 없다")
    elif args.no_push:
        logger.info("--no-push: 변경만 남긴다 (%s)", ", ".join(changed))
    else:
        commit_and_push(changed)

    failed = [STEPS[k][0] for k, ok in results.items() if not ok]
    if failed:
        logger.error("실패한 단계: %s", ", ".join(failed))
        return 1
    logger.info("자동 갱신 정상 종료")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
