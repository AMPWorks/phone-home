#!/usr/bin/env bash
# phone-home SessionEnd / Stop hook — deregisters this session from the relay.
# Best-effort (won't fire on kill -9; the relay's liveness sweep covers that).
# Wire it ASYNC in settings.json (SessionEnd, or Stop if SessionEnd is absent):
#   {"type":"command","command":"bash ~/.claude/phone-home/hooks/session-end.sh 2>/dev/null || true","async":true}
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -n "${TMUX:-}" ]] || exit 0
bash "$HERE/../deregister.sh" 2>/dev/null || true
exit 0
