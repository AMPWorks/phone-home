#!/usr/bin/env bash
# deregister.sh — remove this tmux session from the phone-home relay.
# Run from inside the pane, or pass --pane/--label. Loopback /v1/deregister.
#
# Config: PHONE_HOME_PORT (default 8765).
# Flags:  --pane %N   --label L
set -euo pipefail

PORT="${PHONE_HOME_PORT:-8765}"
API="v1"
pane="" label=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pane) pane="$2"; shift 2 ;;
    --label) label="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "deregister.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$pane" && -z "$label" ]]; then
  pane="${TMUX_PANE:-}"
  [[ -z "$pane" ]] && pane="$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)"
fi
[[ -n "$pane" || -n "$label" ]] || { echo "deregister.sh: need --pane or --label" >&2; exit 2; }

body="$(PH_PANE="$pane" PH_LABEL="$label" python3 - <<'PY'
import json, os
d = {}
if os.environ.get("PH_PANE"):  d["pane_id"] = os.environ["PH_PANE"]
if os.environ.get("PH_LABEL"): d["label"] = os.environ["PH_LABEL"]
print(json.dumps(d))
PY
)"

# Best-effort: a missing relay or already-gone entry is not an error here.
curl -fsS -X POST "http://127.0.0.1:${PORT}/${API}/deregister" \
     -H "Content-Type: application/json" -d "$body" 2>/dev/null || true
echo
