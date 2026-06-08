#!/usr/bin/env bash
# register.sh — register THIS tmux session with the local phone-home relay so the
# phone can route dictation to it and be redirected to its viewer.
#
# Run from inside the tmux pane you want to register (or pass --pane/--socket).
# Posts to the loopback /v1/register endpoint (never exposed off-loopback).
#
# Config (env, overridable by flags):
#   PHONE_HOME_PORT             relay port (default 8765)
#   PHONE_HOME_REGISTER_SECRET  secret the relay's /register requires (if set)
#   PHONE_HOME_VIEWER_TEMPLATE  viewer_url template; {session_id} is substituted
#                               (default 'claude://code/{session_id}')
#
# Flags: --label L  --viewer URL  --session-id ID  --pane %N  --socket PATH
#        --repo R   --force
#
# Generic: works for any tmux session; nothing here is amp-agent-specific.
set -euo pipefail

PORT="${PHONE_HOME_PORT:-8765}"
SECRET="${PHONE_HOME_REGISTER_SECRET:-}"
VIEWER_TEMPLATE="${PHONE_HOME_VIEWER_TEMPLATE:-claude://code/{session_id}}"
API="v1"   # versioned API path; bump when the relay's format changes

label="" viewer="" session_id="" pane="" socket="" repo="" force=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) label="$2"; shift 2 ;;
    --viewer) viewer="$2"; shift 2 ;;
    --session-id) session_id="$2"; shift 2 ;;
    --pane) pane="$2"; shift 2 ;;
    --socket) socket="$2"; shift 2 ;;
    --repo) repo="$2"; shift 2 ;;
    --force) force="true"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "register.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

# Resolve tmux pane + socket from the current environment if not given.
if [[ -z "$pane" ]]; then
  pane="${TMUX_PANE:-}"
  [[ -z "$pane" ]] && pane="$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)"
fi
if [[ -z "$socket" ]]; then
  socket="$(tmux display-message -p '#{socket_path}' 2>/dev/null || true)"
fi
[[ -n "$pane" && -n "$socket" ]] || { echo "register.sh: not in tmux (need --pane + --socket)" >&2; exit 2; }

# Default label = repo/dir basename of the pane's cwd (or $PWD).
if [[ -z "$label" ]]; then
  pane_path="$(tmux display-message -p -t "$pane" '#{pane_current_path}' 2>/dev/null || echo "$PWD")"
  label="$(basename "$pane_path")"
fi

# Build viewer_url from the template if not given explicitly.
if [[ -z "$viewer" ]]; then
  [[ -n "$session_id" ]] || { echo "register.sh: need --viewer or --session-id" >&2; exit 2; }
  viewer="${VIEWER_TEMPLATE/\{session_id\}/$session_id}"
fi

# JSON body (versioned: v=1). python3 builds it safely (no shell-quoting hazards).
body="$(PH_LABEL="$label" PH_SOCK="$socket" PH_PANE="$pane" PH_VIEWER="$viewer" \
        PH_REPO="$repo" PH_SECRET="$SECRET" PH_FORCE="$force" python3 - <<'PY'
import json, os
d = {"v": 1,
     "label": os.environ["PH_LABEL"],
     "tmux_socket": os.environ["PH_SOCK"],
     "pane_id": os.environ["PH_PANE"],
     "viewer_url": os.environ["PH_VIEWER"],
     "repo": os.environ.get("PH_REPO", "")}
if os.environ.get("PH_SECRET"):
    d["register_secret"] = os.environ["PH_SECRET"]
if os.environ.get("PH_FORCE"):
    d["force"] = True
print(json.dumps(d))
PY
)"

curl -fsS -X POST "http://127.0.0.1:${PORT}/${API}/register" \
     -H "Content-Type: application/json" -d "$body"
echo
