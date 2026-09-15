#!/usr/bin/env python3
"""Run idle maintenance for Cursor and Claude Code agents hosted by Herdr."""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


IDLE_SECONDS = int(os.environ.get("HERDR_IDLE_SECONDS", "1800"))
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("HERDR_IDLE_COMMAND_TIMEOUT_SECONDS", "600"))
ROOT = Path.home() / ".local/share/herdr-idle-maintenance"
STATE_DIR = Path(os.environ.get("HERDR_IDLE_MAINTENANCE_STATE_DIR", ROOT / "state")).expanduser()
SUMMARY_DIR = Path(
    os.environ.get("HERDR_IDLE_MAINTENANCE_SUMMARY_DIR", ROOT / "claude-summaries")
).expanduser()
HERDR = Path(os.environ.get("HERDR_BIN", Path.home() / ".local/bin/herdr")).expanduser()


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


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


def strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def current_agent_matches(state: dict) -> bool:
    pane_id = state.get("pane_id")
    expected_agent = state.get("agent")
    if not isinstance(pane_id, str) or not isinstance(expected_agent, str):
        return False
    try:
        result = subprocess.run(
            [str(HERDR), "agent", "get", pane_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False

    values = set(strings(payload))
    if expected_agent not in values:
        return False
    if not ({"idle", "done"} & values):
        return False
    session_id = state.get("session_id")
    if isinstance(session_id, str) and session_id and session_id not in values:
        return False
    return True


def summary_path(state: dict) -> Path:
    raw_session_id = state.get("session_id") or state.get("pane_id") or "session"
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw_session_id)).strip("-")
    safe_session_id = safe_session_id[:80] or "session"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return SUMMARY_DIR / f"{safe_session_id}-{timestamp}.md"


def prompt_for(state: dict) -> str:
    if state["agent"] == "cursor":
        return "/summarize"

    path = summary_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    return (
        "Do not compact or clear this conversation. Create a comprehensive handoff summary "
        f"of the current session and write it to this exact absolute path:\n{path}\n\n"
        "The Markdown summary must preserve enough context for a fresh agent to continue: "
        "the user's goal, current state, important decisions and rationale, files changed, "
        "commands/tests and their results, unresolved problems, and concrete next steps. "
        "After verifying the file exists, your final response must contain only that absolute "
        "file path and no other text."
    )


def run_one(path: Path, now: float) -> None:
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(path)
        generation = state.get("generation")
        last_response_at = state.get("last_response_at")
        if (
            state.get("agent") not in ("cursor", "claude")
            or not isinstance(generation, int)
            or not isinstance(last_response_at, (int, float))
            or now - last_response_at < IDLE_SECONDS
            or state.get("handled_generation") == generation
            or state.get("maintenance_generation") is not None
        ):
            return

        # Claim this generation before checking/sending so overlapping launchd
        # invocations cannot submit duplicate maintenance prompts.
        state["handled_generation"] = generation
        state["maintenance_generation"] = generation
        state["maintenance_started_at"] = now
        atomic_write(path, state)

    if not current_agent_matches(state):
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            latest = load_json(path)
            if latest.get("generation") == generation:
                latest["maintenance_generation"] = None
                latest["handled_generation"] = None
                atomic_write(path, latest)
        return

    prompt = prompt_for(state)
    pane_id = state["pane_id"]
    log(f"sending {state['agent']} maintenance to pane {pane_id}")
    succeeded = False
    try:
        result = subprocess.run(
            [
                str(HERDR),
                "agent",
                "prompt",
                pane_id,
                prompt,
                "--wait",
                "--until",
                "idle",
                "--until",
                "done",
                "--timeout",
                str(COMMAND_TIMEOUT_SECONDS * 1000),
            ],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS + 15,
        )
        succeeded = result.returncode == 0
        if not succeeded:
            log(f"maintenance failed for pane {pane_id}: {result.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as error:
        log(f"maintenance failed for pane {pane_id}: {error}")

    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        latest = load_json(path)
        if latest.get("generation") != generation:
            return
        latest["maintenance_generation"] = None
        latest["maintenance_finished_at"] = time.time()
        if not succeeded:
            # Retry on the next launchd tick; Herdr rejects blocked/working agents.
            latest["handled_generation"] = None
        atomic_write(path, latest)


def main() -> int:
    if IDLE_SECONDS <= 0 or not HERDR.is_file():
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    global_lock_path = ROOT / "watcher.lock"
    with global_lock_path.open("a+", encoding="utf-8") as global_lock:
        try:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = time.time()
        for path in STATE_DIR.glob("*.json"):
            try:
                run_one(path, now)
            except Exception as error:
                log(f"unexpected error for {path.name}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
