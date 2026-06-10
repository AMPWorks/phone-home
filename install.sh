#!/usr/bin/env bash
# install.sh — idempotent installer for the phone-home relay (macOS / launchd).
# Deploys the relay + scripts, provisions secrets in the macOS Keychain, and
# installs + loads a per-user LaunchAgent. Re-running is always safe.
#
# Standalone-reproducible: nothing here assumes amp-agent or ampworksstudio. The
# Cloudflare tunnel + iOS Shortcut are separate steps (see cloudflare-setup.sh
# and README). Config via env:
#   PHONE_HOME_PREFIX   install dir (default ~/.claude/phone-home)
#   PHONE_HOME_PORT     relay port (default 8765)
#   PHONE_HOME_DEFAULT  default session id/label for /say when none given
#   PHONE_HOME_REPLAY_TTL  /say replay-nonce window seconds (default 0 = off for v1)
# Flags: --dry-run  --no-load
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

PREFIX="${PHONE_HOME_PREFIX:-$HOME/.claude/phone-home}"
PORT="${PHONE_HOME_PORT:-8765}"
DEFAULT_SESSION="${PHONE_HOME_DEFAULT:-}"
REPLAY_TTL="${PHONE_HOME_REPLAY_TTL:-0}"
LABEL="com.ampworksstudio.phone-home"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
KC_TOKEN="phone-home-token"          # Keychain service names
KC_REG="phone-home-register-secret"

DRY="" LOAD="1"
for a in "$@"; do case "$a" in --dry-run) DRY="1";; --no-load) LOAD="";; esac; done
run() { echo "+ $*"; [[ -n "$DRY" ]] || "$@"; }

# --- 1. deploy files (rsync-like copy; idempotent) ---
run mkdir -p "$PREFIX/hooks"
for f in phone_home.py register.sh deregister.sh; do run cp "$SRC/$f" "$PREFIX/$f"; done
for f in session-start.sh session-end.sh; do run cp "$SRC/hooks/$f" "$PREFIX/hooks/$f"; done
run chmod +x "$PREFIX/register.sh" "$PREFIX/deregister.sh" "$PREFIX/hooks/"*.sh

# --- 2. secrets in Keychain (generate once; reuse if present) ---
kc_get() { security find-generic-password -a "$USER" -s "$1" -w 2>/dev/null || true; }
kc_set() { security add-generic-password -a "$USER" -s "$1" -w "$2" -U >/dev/null; }
ensure_secret() {  # $1=service ; echoes nothing, just ensures it exists
  local svc="$1" cur; cur="$(kc_get "$svc")"
  if [[ -z "$cur" ]]; then
    local val; val="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    if [[ -n "$DRY" ]]; then echo "+ keychain set $svc (generated)"; else kc_set "$svc" "$val"; fi
  else echo "  keychain $svc already set"; fi
}
ensure_secret "$KC_TOKEN"
ensure_secret "$KC_REG"

# --- 3. run-wrapper that reads secrets from Keychain at launch (keeps them out
#        of the plist / process args) ---
WRAPPER="$PREFIX/run.sh"
if [[ -n "$DRY" ]]; then echo "+ write $WRAPPER"; else cat > "$WRAPPER" <<WRAP
#!/usr/bin/env bash
set -euo pipefail
# launchd hands user agents a minimal PATH that excludes Homebrew; the relay
# shells out to a bare \`tmux\`, so prepend the usual brew + system bin dirs
# (Apple-Silicon /opt/homebrew, Intel /usr/local) or every inject would fail.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\${PATH:-}"
export PHONE_HOME_TOKEN="\$(security find-generic-password -a "\$USER" -s $KC_TOKEN -w)"
export PHONE_HOME_REGISTER_SECRET="\$(security find-generic-password -a "\$USER" -s $KC_REG -w)"
exec /usr/bin/python3 "$PREFIX/phone_home.py" \\
  --port $PORT --registry "$PREFIX/registry.json" \\
  --replay-ttl $REPLAY_TTL ${DEFAULT_SESSION:+--default-session "$DEFAULT_SESSION"}
WRAP
chmod +x "$WRAPPER"; fi

# --- 4. LaunchAgent (user domain — per-user tmux; not a Daemon) ---
if [[ -n "$DRY" ]]; then echo "+ write $PLIST"; else mkdir -p "$(dirname "$PLIST")"; cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>${WRAPPER}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>${PREFIX}/phone-home.log</string>
  <key>StandardOutPath</key><string>${PREFIX}/phone-home.log</string>
</dict></plist>
PL
fi

# --- 5. (re)load the LaunchAgent. Prefer the modern bootstrap API, but fall back
#        to the legacy `load -w`: on a headless/Aqua Mac mini, `bootstrap user/$uid`
#        can fail with "5: Input/output error" while `load -w` succeeds. ---
if [[ -n "$LOAD" && -z "$DRY" ]]; then
  uid="$(id -u)"
  launchctl bootout "user/$uid/${LABEL}" 2>/dev/null || true
  launchctl unload "$PLIST" 2>/dev/null || true
  if launchctl bootstrap "user/$uid" "$PLIST" 2>/dev/null; then
    launchctl kickstart -k "user/$uid/${LABEL}" 2>/dev/null || true
    echo "  LaunchAgent loaded (bootstrap); relay on 127.0.0.1:$PORT"
  elif launchctl load -w "$PLIST" 2>/dev/null; then
    echo "  LaunchAgent loaded (load -w fallback); relay on 127.0.0.1:$PORT"
  else
    echo "  WARNING: could not load the LaunchAgent automatically — load it manually:" >&2
    echo "    launchctl load -w '$PLIST'" >&2
  fi
  echo "  log: $PREFIX/phone-home.log"
else
  echo "  (skipped LaunchAgent load)"
fi
echo "install.sh done. Next: bash cloudflare-setup.sh  +  add the SessionStart hook  +  build the iOS Shortcut."
