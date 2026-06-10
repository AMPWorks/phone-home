#!/usr/bin/env bash
# Functional smoke: start the relay, register a throwaway tmux session via
# register.sh, confirm it appears in /v1/sessions, deregister, confirm it's gone.
# Needs: python3, tmux, curl. Run from the repo root: bash tests/smoke_register.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== syntax check =="
bash -n register.sh deregister.sh hooks/session-start.sh hooks/session-end.sh
python3 -m py_compile phone_home.py
echo "  ok"

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
REG="$(mktemp -t ph-reg.XXXXXX)"
SOCK="$(mktemp -u -t ph-sock.XXXXXX)"
cleanup() { kill "${RELAY:-0}" 2>/dev/null || true; tmux -S "$SOCK" kill-server 2>/dev/null || true; rm -f "$REG"; }
trap cleanup EXIT

echo "== start relay on :$PORT =="
PHONE_HOME_TOKEN=tok python3 phone_home.py --port "$PORT" --registry "$REG" --register-secret reg \
  >/dev/null 2>&1 &
RELAY=$!
for _ in $(seq 1 20); do curl -fsS "http://127.0.0.1:$PORT/v1/sessions?token=tok" >/dev/null 2>&1 && break; sleep 0.1; done

echo "== throwaway tmux session =="
tmux -S "$SOCK" new-session -d -s t 'sleep 120'
PANE="$(tmux -S "$SOCK" display-message -p -t t '#{pane_id}')"

echo "== register.sh =="
PHONE_HOME_PORT="$PORT" PHONE_HOME_REGISTER_SECRET=reg \
  bash register.sh --label smoketest --socket "$SOCK" --pane "$PANE" --viewer "claude://code/abc" --repo demo
curl -fsS "http://127.0.0.1:$PORT/v1/sessions?token=tok" | grep -q '"label": *"smoketest"' \
  && echo "  register OK" || { echo "  FAIL: not in /sessions"; exit 1; }

echo "== wrong register secret refused =="
if PHONE_HOME_PORT="$PORT" PHONE_HOME_REGISTER_SECRET=wrong \
     bash register.sh --label x --socket "$SOCK" --pane "$PANE" --viewer v 2>/dev/null; then
  echo "  FAIL: bad secret accepted"; exit 1
else echo "  refused OK"; fi

echo "== deregister.sh (socket-scoped) =="
PHONE_HOME_PORT="$PORT" bash deregister.sh --pane "$PANE" --socket "$SOCK"
if curl -fsS "http://127.0.0.1:$PORT/v1/sessions?token=tok" | grep -q smoketest; then
  echo "  FAIL: still registered"; exit 1
else echo "  deregister OK"; fi

echo "ALL SMOKE CHECKS PASSED"
