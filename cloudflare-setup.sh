#!/usr/bin/env bash
# cloudflare-setup.sh — expose the loopback relay at a public hostname via a
# Cloudflare Tunnel. ONLY /v1/say + /v1/sessions are routed publicly; /v1/register
# + /v1/deregister are NOT routed (loopback-only, defense-in-depth over the relay's
# own in-process loopback check). Idempotent.
#
# Config (env):
#   PHONE_HOME_HOSTNAME   public host (default phone-home.ampworksstudio.com)
#   PHONE_HOME_PORT       loopback relay port (default 8765)
#   PHONE_HOME_TUNNEL     tunnel name (default phone-home)
# One-time auth: `cloudflared tunnel login` opens a browser to authorize the zone
# (the only human step here). After that this script is fully automatic + re-runnable.
set -euo pipefail

HOST="${PHONE_HOME_HOSTNAME:-phone-home.ampworksstudio.com}"
PORT="${PHONE_HOME_PORT:-8765}"
TUN="${PHONE_HOME_TUNNEL:-phone-home}"
CFG_DIR="$HOME/.cloudflared"
CFG="$CFG_DIR/${TUN}.yml"

command -v cloudflared >/dev/null || { echo "installing cloudflared..."; brew install cloudflared; }

# 1. cert (one-time browser login). cert.pem authorizes tunnel + DNS-route ops.
if [[ ! -f "$CFG_DIR/cert.pem" ]]; then
  echo "==> cloudflared needs a one-time browser authorize for the ampworksstudio zone:"
  cloudflared tunnel login
fi

# 2. create the named tunnel if absent; capture its UUID + credentials file.
if ! cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUN"; then
  cloudflared tunnel create "$TUN"
fi
TUN_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUN" '$2==n{print $1}')"
[[ -n "$TUN_ID" ]] || { echo "could not resolve tunnel id for $TUN" >&2; exit 1; }

# 3. ingress config — public path allowlist. Anything not /v1/say|/v1/sessions -> 404.
cat > "$CFG" <<YML
tunnel: ${TUN_ID}
credentials-file: ${CFG_DIR}/${TUN_ID}.json
ingress:
  - hostname: ${HOST}
    path: ^/v1/(say|sessions)\b
    service: http://127.0.0.1:${PORT}
  - hostname: ${HOST}
    service: http_status:404
  - service: http_status:404
YML
echo "  wrote ${CFG} (only /v1/say + /v1/sessions routed to 127.0.0.1:${PORT})"

# 4. DNS route (CNAME ${HOST} -> ${TUN_ID}.cfargotunnel.com). Idempotent.
cloudflared tunnel route dns "$TUN" "$HOST" 2>/dev/null || true

# 5. run it as a LaunchAgent (per-user; KeepAlive).
LABEL="com.ampworksstudio.phone-home-tunnel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v cloudflared)</string><string>tunnel</string>
    <string>--config</string><string>${CFG}</string><string>run</string><string>${TUN}</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>${CFG_DIR}/${TUN}.log</string>
  <key>StandardOutPath</key><string>${CFG_DIR}/${TUN}.log</string>
</dict></plist>
PL
uid="$(id -u)"
launchctl bootout "user/$uid/${LABEL}" 2>/dev/null || true
launchctl bootstrap "user/$uid" "$PLIST"
echo "  tunnel running; ${HOST} -> 127.0.0.1:${PORT} (/v1/say,/v1/sessions). log: ${CFG_DIR}/${TUN}.log"
