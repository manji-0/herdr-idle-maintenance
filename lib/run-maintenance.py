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
CONFIG_DIR = Path(
    os.environ.get(
        "HERDR_IDLE_MAINTENANCE_CONFIG_DIR", Path.home() / ".config/herdr-idle-maintenance"
    )
).expanduser()
HERDR = Path(os.environ.get("HERDR_BIN", Path.home() / ".local/bin/herdr")).expanduser()

# Overridable per agent. Placeholders are substituted literally, so a template
# may contain braces of its own without breaking.
DEFAULT_PROMPTS = {
    "claude": (
        "Do not compact or clear this conversation. Create a comprehensive handoff summary "
        "of the current session and write it to this exact absolute path:\n"
        "{summary_path}\n\n"
        "The Markdown summary must preserve enough context for a fresh agent to continue: "
        "the user's goal, current state, important decisions and rationale, files changed, "
        "commands/tests and their results, unresolved problems, and concrete next steps. "
        "After verifying the file exists, your final response must contain only that absolute "
        "file path and no other text."
    ),
    "cursor": "/summarize",
}

USAGE = """Usage: run-maintenance.py [--print-prompt <agent>]

With no arguments, scan recorded sessions and ask any agent that has been idle
for HERDR_IDLE_SECONDS (default 1800) to write a handoff summary.

Options:
  --print-prompt <agent>  Print the prompt template in effect for "claude" or
                          "cursor", after applying any override, and exit.
  -h, --help              Show this help.

Customising the prompt, in order of precedence:
  1. HERDR_IDLE_MAINTENANCE_PROMPT_<AGENT>       inline template
  2. HERDR_IDLE_MAINTENANCE_PROMPT_<AGENT>_FILE  path to a template file
  3. <config dir>/prompt-<agent>.txt             default template file
  4. the built-in template

Start from the current template:
  run-maintenance.py --print-prompt claude \\
    > ~/.config/herdr-idle-maintenance/prompt-claude.txt

Placeholders: {summary_path} {summary_dir} {agent} {session_id} {pane_id}
              {cwd} {timestamp}
"""


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


def prompt_template(agent: str) -> str:
    """Resolve the template for an agent: inline env, file env, config dir, built-in."""
    env_prefix = f"HERDR_IDLE_MAINTENANCE_PROMPT_{agent.upper()}"

    inline = os.environ.get(env_prefix)
    if inline and inline.strip():
        return inline.strip()

    candidates = []
    override = os.environ.get(f"{env_prefix}_FILE")
    if override:
        candidates.append((Path(override).expanduser(), True))
    candidates.append((CONFIG_DIR / f"prompt-{agent}.txt", False))

    for candidate, explicit in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as error:
            # A missing file in the config dir is the normal case; stay quiet.
            if explicit:
                log(f"cannot read prompt file {candidate} ({error}); using the default")
            continue
        if text.strip():
            return text.strip()
        log(f"prompt file {candidate} is empty; using the default")

    return DEFAULT_PROMPTS[agent]


def render_prompt(template: str, state: dict, path: Path) -> str:
    values = {
        "summary_path": str(path),
        "summary_dir": str(SUMMARY_DIR),
        "agent": str(state.get("agent") or ""),
        "session_id": str(state.get("session_id") or ""),
        "pane_id": str(state.get("pane_id") or ""),
        "cwd": str(state.get("cwd") or ""),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def prompt_for(state: dict) -> str:
    template = prompt_template(state["agent"])
    path = summary_path(state)
    # Only the templates that actually name the output directory need it to exist.
    if "{summary_path}" in template or "{summary_dir}" in template:
        path.parent.mkdir(parents=True, exist_ok=True)
    return render_prompt(template, state, path)


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


def print_prompt(args: list) -> int:
    if len(args) != 1 or args[0] not in DEFAULT_PROMPTS:
        sys.stderr.write(f"--print-prompt needs one of: {', '.join(sorted(DEFAULT_PROMPTS))}\n")
        return 2
    sys.stdout.write(prompt_template(args[0]) + "\n")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args:
        if args[0] in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return 0
        if args[0] == "--print-prompt":
            return print_prompt(args[1:])
        sys.stderr.write(f"unknown option: {args[0]}\n\n{USAGE}")
        return 2

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
