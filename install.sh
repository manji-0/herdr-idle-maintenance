#!/bin/sh
# Install the Herdr idle maintenance watcher for Claude Code and Cursor.
set -eu

LABEL="dev.herdr.idle-maintenance"
LIB_DIR="$HOME/.local/lib/herdr-idle-maintenance"
DATA_DIR="$HOME/.local/share/herdr-idle-maintenance"
STATE_DIR="$DATA_DIR/state"
SUMMARY_DIR="$DATA_DIR/claude-summaries"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"

IDLE_SECONDS="${HERDR_IDLE_SECONDS:-1800}"
INTERVAL_SECONDS="${HERDR_IDLE_CHECK_INTERVAL_SECONDS:-60}"
SKIP_HOOKS=0

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

info() { printf '%s\n' "$1"; }
warn() { printf 'warning: %s\n' "$1" >&2; }
die() { printf 'error: %s\n' "$1" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./install.sh [options]

Options:
  --idle-seconds <n>   Idle threshold before a summary is requested (default: 1800)
  --interval <n>       launchd polling interval in seconds (default: 60)
  --no-hooks           Install the watcher only; do not touch agent settings
  -h, --help           Show this help

Environment:
  HERDR_BIN                          Path to the herdr binary
  HERDR_IDLE_SECONDS                 Same as --idle-seconds
  HERDR_IDLE_CHECK_INTERVAL_SECONDS  Same as --interval
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --idle-seconds) [ $# -ge 2 ] || die "--idle-seconds needs a value"; IDLE_SECONDS="$2"; shift 2 ;;
    --interval) [ $# -ge 2 ] || die "--interval needs a value"; INTERVAL_SECONDS="$2"; shift 2 ;;
    --no-hooks) SKIP_HOOKS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$IDLE_SECONDS" in ''|*[!0-9]*) die "--idle-seconds must be a positive integer" ;; esac
case "$INTERVAL_SECONDS" in ''|*[!0-9]*) die "--interval must be a positive integer" ;; esac
[ "$IDLE_SECONDS" -gt 0 ] || die "--idle-seconds must be greater than 0"
[ "$INTERVAL_SECONDS" -gt 0 ] || die "--interval must be greater than 0"

[ "$(uname -s)" = "Darwin" ] || die "this installer targets macOS launchd (got $(uname -s))"

if [ -n "${HERDR_BIN:-}" ]; then
  herdr_bin="$HERDR_BIN"
elif command -v herdr >/dev/null 2>&1; then
  herdr_bin=$(command -v herdr)
else
  herdr_bin="$HOME/.local/bin/herdr"
fi
[ -x "$herdr_bin" ] || die "herdr not found at $herdr_bin; install Herdr or set HERDR_BIN"

if [ -x /usr/bin/python3 ]; then
  python_bin=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  python_bin=$(command -v python3)
else
  die "python3 not found"
fi

if command -v jq >/dev/null 2>&1; then
  jq_bin=$(command -v jq)
else
  jq_bin=""
fi

# --- watcher ---------------------------------------------------------------

mkdir -p "$LIB_DIR" "$STATE_DIR" "$SUMMARY_DIR" "$LAUNCH_AGENTS_DIR"
for script in record-stop.py run-maintenance.py; do
  [ -f "$SCRIPT_DIR/lib/$script" ] || die "missing $SCRIPT_DIR/lib/$script"
  cp "$SCRIPT_DIR/lib/$script" "$LIB_DIR/$script"
  chmod 755 "$LIB_DIR/$script"
done
info "installed scripts into $LIB_DIR"

sed \
  -e "s|@PYTHON@|$python_bin|g" \
  -e "s|@SCRIPT@|$LIB_DIR/run-maintenance.py|g" \
  -e "s|@HERDR_BIN@|$herdr_bin|g" \
  -e "s|@IDLE_SECONDS@|$IDLE_SECONDS|g" \
  -e "s|@INTERVAL@|$INTERVAL_SECONDS|g" \
  -e "s|@LOG@|$DATA_DIR/launchd.log|g" \
  -e "s|@ERR_LOG@|$DATA_DIR/launchd.err.log|g" \
  "$SCRIPT_DIR/launchd/$LABEL.plist.template" > "$PLIST"
info "wrote $PLIST"

uid=$(id -u)
launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/$uid" "$PLIST" 2>/dev/null; then
  launchctl load -w "$PLIST"
fi
info "loaded launchd agent $LABEL (every ${INTERVAL_SECONDS}s, idle threshold ${IDLE_SECONDS}s)"

# --- agent hooks -----------------------------------------------------------

apply_jq() {
  target="$1"
  program="$2"
  shift 2

  if [ -f "$target" ]; then
    backup="$target.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "$target" "$backup"
  else
    mkdir -p "$(dirname "$target")"
    printf '{}\n' > "$target"
    backup=""
  fi

  tmp="$target.tmp.$$"
  if "$jq_bin" "$@" "$program" "$target" > "$tmp"; then
    mv "$tmp" "$target"
  else
    rm -f "$tmp"
    die "failed to update $target${backup:+ (backup: $backup)}"
  fi
}

configure_claude() {
  target="$HOME/.claude/settings.json"
  cmd="$python_bin $LIB_DIR/record-stop.py claude"
  marker="record-stop.py claude"
  perm="Write($SUMMARY_DIR/**)"
  apply_jq "$target" '
    .hooks.Stop = (
      [ (.hooks.Stop // [])[]
        | .hooks = [ (.hooks // [])[] | select(((.command // "") | contains($marker)) | not) ]
        | select((.hooks | length) > 0)
      ]
      + [ { hooks: [ { type: "command", command: $cmd, timeout: 5 } ] } ]
    )
    | .permissions.allow = (
        if ([ (.permissions.allow // [])[] ] | index($perm))
        then (.permissions.allow // [])
        else ((.permissions.allow // []) + [ $perm ])
        end
      )
  ' --arg cmd "$cmd" --arg marker "$marker" --arg perm "$perm"
  info "configured Claude Code Stop hook in $target"
}

configure_cursor() {
  target="$HOME/.cursor/hooks.json"
  cmd="$python_bin $LIB_DIR/record-stop.py cursor"
  marker="record-stop.py cursor"
  apply_jq "$target" '
    .hooks.stop = (
      [ (.hooks.stop // [])[] | select(((.command // "") | contains($marker)) | not) ]
      + [ { command: $cmd, timeout: 5 } ]
    )
    | .version = (.version // 1)
  ' --arg cmd "$cmd" --arg marker "$marker"
  info "configured Cursor stop hook in $target"
}

print_manual_hooks() {
  cat <<MANUAL

jq was not found, so agent settings were left untouched. Add these by hand.

~/.claude/settings.json
  .hooks.Stop += [{"hooks": [{"type": "command",
    "command": "$python_bin $LIB_DIR/record-stop.py claude", "timeout": 5}]}]
  .permissions.allow += ["Write($SUMMARY_DIR/**)"]

~/.cursor/hooks.json
  .hooks.stop += [{"command": "$python_bin $LIB_DIR/record-stop.py cursor", "timeout": 5}]
MANUAL
}

if [ "$SKIP_HOOKS" -eq 1 ]; then
  info "skipped agent hook configuration (--no-hooks)"
elif [ -z "$jq_bin" ]; then
  print_manual_hooks
else
  configured=0
  if [ -d "$HOME/.claude" ]; then
    configure_claude
    configured=1
  else
    warn "~/.claude not found; skipped Claude Code"
  fi
  if [ -d "$HOME/.cursor" ]; then
    configure_cursor
    configured=1
  else
    warn "~/.cursor not found; skipped Cursor"
  fi
  [ "$configured" -eq 1 ] || warn "no agent settings were configured"
fi

cat <<DONE

Done. Restart any running Claude Code or Cursor session so it picks up the hook.
Summaries land in $SUMMARY_DIR
Watcher log: $DATA_DIR/launchd.log
DONE
