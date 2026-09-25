"""차분 수집 진행 상태 — Streamlit UI가 별도 프로세스(fetch_data.py) 진행을 읽습니다."""
from __future__ import annotations

import json
import os
import time
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
    current_step: int | None = None,
    total_steps: int | None = None,
) -> None:
    ratio_f = float(max(0.0, min(1.0, ratio)))
    prev = read_update_status()
    payload: dict[str, Any] = {
        # 수집 모드·시작 시각·프로세스 — 진행 중 갱신에도 유지 (UI 모드 표시·경과시간·새로고침 복구용)
        "mode": prev.get("mode", ""),
        "started_at": prev.get("started_at", time.time()),
        "pid": prev.get("pid") or os.getpid(),
        "ratio": ratio_f,
        "percent": int(round(ratio_f * 100)),
        "message": str(message),
        "running": bool(running and not done),
        "done": bool(done),
        "error": error,
    }
    if current_step is not None:
        payload["current_step"] = int(current_step)
    if total_steps is not None:
        payload["total_steps"] = int(total_steps)
    UPDATE_STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def reset_update_status(
    message: str = "최근 2개월 누락 데이터를 확인하고 수집 중입니다...",
    *,
    mode: str = "",
    pid: int | None = None,
) -> None:
    """수집 시작 — 모드·시작 시각·프로세스 ID 기록."""
    UPDATE_STATUS_FILE.write_text(
        json.dumps(
            {"mode": mode, "started_at": time.time(), "pid": pid or os.getpid()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_update_status(0.0, message, running=True, done=False, error=None)


def finish_update_status(*, error: str | None = None) -> None:
    mode = read_update_status().get("mode") or "매매·전월세 업데이트"
    if error:
        write_update_status(1.0, f"오류: {error}", running=False, done=True, error=error)
    else:
        write_update_status(1.0, f"{mode} 완료", running=False, done=True)
    # 완료 시각 — 수집 종료 시 열려 있던 화면이 없어도 다음 접속에서 캐시 갱신 판단용
    data = read_update_status()
    data["finished_at"] = time.time()
    UPDATE_STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
