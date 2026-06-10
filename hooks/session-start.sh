#!/usr/bin/env bash
# phone-home SessionStart hook — registers this Claude session with the local
# relay so the phone can route dictation to it, with the correct viewer URL.
#
# The viewer must be the remote-control SESSION URL Claude prints at
# `--remote-control` start (https://claude.ai/code/session_XXX) — there is no
# `claude://code/<id>` deep-link scheme, and the local session UUID (from the
# hook JSON) is NOT the remote-control id. That URL lands in the session's
# transcript (.jsonl) once remote-control connects, which is shortly AFTER
# SessionStart — so this hook polls the transcript briefly for it.
#
# No-ops cleanly when not in tmux or the relay isn't up. Wire it ASYNC in
# settings.json (so the poll doesn't block the session):
#   {"type":"command","command":"bash ~/.claude/phone-home/hooks/session-start.sh 2>/dev/null || true","async":true}
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ -n "${TMUX:-}" ]] || exit 0   # only tmux sessions are routable

sid="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || true)"
[[ -n "$sid" ]] || exit 0

# /register requires the relay's register-secret (install.sh stores it in the
# Keychain); supply it so the hook's registration is accepted.
export PHONE_HOME_REGISTER_SECRET="${PHONE_HOME_REGISTER_SECRET:-$(security find-generic-password -a "$USER" -s phone-home-register-secret -w 2>/dev/null || true)}"

# Extract the remote-control URL from this session's transcript (.jsonl). The
# local session UUID names the transcript file; the URL appears inside it.
find_url() {
  local f
  f="$(ls -1 "$HOME/.claude/projects/"*/"$sid.jsonl" 2>/dev/null | head -1)"
  [[ -n "$f" ]] || return 1
  grep -ohE 'https://claude\.ai/code/session_[A-Za-z0-9]+' "$f" 2>/dev/null | tail -1
}

# Poll briefly — the URL is emitted after remote-control connects (post-start).
url=""
for _ in $(seq 1 "${PHONE_HOME_HOOK_RETRIES:-20}"); do
  url="$(find_url || true)"
  [[ -n "$url" ]] && break
  sleep "${PHONE_HOME_HOOK_INTERVAL:-2}"
done

if [[ -n "$url" ]]; then
  bash "$HERE/../register.sh" --viewer "$url" 2>/dev/null || true
else
  # No remote-control URL found (session may not be --remote-control, or it never
  # appeared): register with the generic Code tab so the redirect at least opens
  # the app's session list (pick by name). Injection works regardless of viewer.
  bash "$HERE/../register.sh" --viewer "https://claude.ai/code" 2>/dev/null || true
fi
exit 0
