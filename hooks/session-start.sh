#!/usr/bin/env bash
# phone-home SessionStart hook — registers this Claude session with the local
# relay so the phone can route dictation to it. Reads the Claude Code hook JSON
# on stdin (for session_id), no-ops cleanly when not inside tmux or the relay
# isn't up. Wire it ASYNC in settings.json (append to existing SessionStart):
#   {"type":"command","command":"bash ~/.claude/phone-home/hooks/session-start.sh 2>/dev/null || true","async":true}
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ -n "${TMUX:-}" ]] || exit 0   # only tmux sessions are routable

sid="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || true)"
[[ -n "$sid" ]] || exit 0

# register.sh resolves pane/socket/label from this tmux env; viewer_url is built
# from PHONE_HOME_VIEWER_TEMPLATE (default claude://code/{session_id}).
bash "$HERE/../register.sh" --session-id "$sid" 2>/dev/null || true
exit 0
