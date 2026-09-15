#!/bin/sh
# Remove the Herdr idle maintenance watcher and its agent hooks.
set -eu

LABEL="dev.herdr.idle-maintenance"
LIB_DIR="$HOME/.local/lib/herdr-idle-maintenance"
DATA_DIR="$HOME/.local/share/herdr-idle-maintenance"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PURGE=0

info() { printf '%s\n' "$1"; }
warn() { printf 'warning: %s\n' "$1" >&2; }
die() { printf 'error: %s\n' "$1" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./uninstall.sh [--purge]

  --purge   Also delete recorded state and generated summaries
            (~/.local/share/herdr-idle-maintenance)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

uid=$(id -u)
launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
info "unloaded launchd agent $LABEL"

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  info "removed $PLIST"
fi

if command -v jq >/dev/null 2>&1; then
  jq_bin=$(command -v jq)
else
  jq_bin=""
fi

apply_jq() {
  target="$1"
  program="$2"
  shift 2
  [ -f "$target" ] || return 0

  cp -p "$target" "$target.bak.$(date +%Y%m%d%H%M%S)"
  tmp="$target.tmp.$$"
  if "$jq_bin" "$@" "$program" "$target" > "$tmp"; then
    mv "$tmp" "$target"
    info "cleaned hook entry from $target"
  else
    rm -f "$tmp"
    warn "failed to update $target; edit it by hand"
  fi
}

if [ -z "$jq_bin" ]; then
  warn "jq not found; remove the record-stop.py entries from ~/.claude/settings.json and ~/.cursor/hooks.json by hand"
else
  apply_jq "$HOME/.claude/settings.json" '
    .hooks.Stop = [ (.hooks.Stop // [])[]
      | .hooks = [ (.hooks // [])[] | select(((.command // "") | contains($marker)) | not) ]
      | select((.hooks | length) > 0)
    ]
    | if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end
    | if (.hooks | length) == 0 then del(.hooks) else . end
    | if (.permissions.allow? != null)
      then .permissions.allow = [ .permissions.allow[] | select((. | contains($summary_dir)) | not) ]
      else . end
  ' --arg marker "record-stop.py claude" --arg summary_dir "herdr-idle-maintenance/claude-summaries"

  apply_jq "$HOME/.cursor/hooks.json" '
    .hooks.stop = [ (.hooks.stop // [])[] | select(((.command // "") | contains($marker)) | not) ]
    | if (.hooks.stop | length) == 0 then del(.hooks.stop) else . end
  ' --arg marker "record-stop.py cursor"
fi

if [ -d "$LIB_DIR" ]; then
  rm -rf "$LIB_DIR"
  info "removed $LIB_DIR"
fi

if [ "$PURGE" -eq 1 ]; then
  if [ -d "$DATA_DIR" ]; then
    rm -rf "$DATA_DIR"
    info "removed $DATA_DIR"
  fi
else
  info "kept state and summaries in $DATA_DIR (use --purge to delete)"
fi

info "Done."
