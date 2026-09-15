#!/usr/bin/env python3
"""Record completed agent turns that originated inside a Herdr pane."""

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def state_dir() -> Path:
    override = os.environ.get("HERDR_IDLE_MAINTENANCE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local/share/herdr-idle-maintenance/state"


def state_path(pane_id: str) -> Path:
    digest = hashlib.sha256(pane_id.encode("utf-8")).hexdigest()[:32]
    return state_dir() / f"{digest}.json"


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("cursor", "claude"):
        return 0
    agent = sys.argv[1]

    # This is the hard boundary: sessions not launched in a Herdr pane are ignored.
    if os.environ.get("HERDR_ENV") != "1":
        return 0
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    socket_path = os.environ.get("HERDR_SOCKET_PATH", "")
    if not pane_id or not socket_path:
        return 0

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0

    if agent == "cursor":
        if event.get("status") not in (None, "completed"):
            return 0
    else:
        if event.get("hook_event_name") not in (None, "Stop"):
            return 0
        if event.get("agent_id"):
            return 0
        # A later Stop will fire when in-flight work has really settled.
        if event.get("background_tasks") or event.get("session_crons"):
            return 0

    path = state_path(pane_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(path)

        # The watcher marks its own prompt before sending it. Do not arm another
        # timer when that maintenance turn finishes.
        maintenance_generation = state.get("maintenance_generation")
        if maintenance_generation is not None:
            state["maintenance_generation"] = None
            state["maintenance_finished_at"] = time.time()
            atomic_write(path, state)
            return 0

        previous_generation = state.get("generation")
        generation = previous_generation + 1 if isinstance(previous_generation, int) else 1
        session_id = (
            event.get("session_id")
            or event.get("sessionId")
            or event.get("conversation_id")
            or event.get("conversationId")
        )
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()

        next_state = {
            "version": 1,
            "agent": agent,
            "pane_id": pane_id,
            "session_id": session_id if isinstance(session_id, str) else None,
            "cwd": cwd,
            "generation": generation,
            "handled_generation": None,
            "maintenance_generation": None,
            "last_response_at": time.time(),
        }
        atomic_write(path, next_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
