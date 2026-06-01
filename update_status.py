"""차분 수집 진행 상태 — Streamlit UI가 별도 프로세스(fetch_data.py) 진행을 읽습니다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
UPDATE_STATUS_FILE = BASE_DIR / "data_update_status.json"
UPDATE_LOG_FILE = BASE_DIR / "data_update.log"


def read_update_status() -> dict[str, Any]:
    if not UPDATE_STATUS_FILE.exists():
        return {}
    try:
        return json.loads(UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_update_status(
    ratio: float,
    message: str,
    *,
    running: bool = True,
    done: bool = False,
    error: str | None = None,
) -> None:
    payload = {
        "ratio": float(max(0.0, min(1.0, ratio))),
        "message": str(message),
        "running": bool(running and not done),
        "done": bool(done),
        "error": error,
    }
    UPDATE_STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def reset_update_status(
    message: str = "최근 1개월 누락 데이터를 확인하고 수집 중입니다...",
) -> None:
    write_update_status(0.0, message, running=True, done=False, error=None)


def finish_update_status(*, error: str | None = None) -> None:
    if error:
        write_update_status(1.0, f"오류: {error}", running=False, done=True, error=error)
    else:
        write_update_status(1.0, "매매·전월세 업데이트 완료", running=False, done=True)
