# phone-home

**Dictate to a Claude Code session on your home server from your phone, then get
dropped straight into that session.** One press: speak → the words are injected
into a registered local `tmux` Claude session on your Mac → your phone is
redirected to that session's viewer (the iOS Claude Code app).

```
iPhone Shortcut:  GET /v1/sessions  →  Choose  →  Dictate  →  URL-encode
   →  Open URLs (Safari):  https://<your-host>/v1/say?token=…&session=<id>&q=<enc-text>
      →  phone-home relay:  auth → liveness+fingerprint → strict idle-guard
         →  tmux send-keys -l (literal) ; Enter   →  302 → that session's viewer_url
```

It is **general**: any local `tmux` session (typically `claude --remote-control`,
but anything) self-registers; nothing here is tied to one project. And it's
**self-contained** — `install.sh` + a Cloudflare Tunnel (or any reverse proxy)
stand it up on your own box without any other infrastructure.

> ⚠️ **Security status: minimal, on purpose (v1).** Auth is a shared token carried
> in the URL, and the idle-prompt guard is heuristic. This is fine for a personal,
> single-user box to get it working; it is **not** hardened for hostile exposure.
> Hardening (HMAC/mTLS auth, replay-nonce enforcement, rate-limiting, audit log)
> is tracked as a follow-up. The target session typically runs with permission
> prompts disabled, so treat the public endpoint accordingly.

## How it works

- **`phone_home.py`** — a stdlib-only (Python 3.9+) HTTP relay:
  - `POST /v1/register` · `POST /v1/deregister` — **loopback-only** (enforced
    in-process), optional shared register-secret.
  - `GET /v1/sessions` · `GET /v1/say` — **token-gated**; safe to expose publicly.
  - A JSON registry (atomic, locked) of live sessions: `{label, tmux_socket,
    pane_id, viewer_url}`. Liveness = the pane exists **and** its fingerprint
    (pid+start-time, captured at registration) still matches — so a `tmux`-server
    restart that reuses pane ids can't misroute your dictation; stale entries are
    swept.
  - A **strict idle-prompt guard** (default on): refuses to inject unless the pane
    looks like an idle Claude prompt, so a stray phrase can't auto-answer a y/n.
  - `send-keys -l` (literal) so dictated words like "enter"/"control c" are never
    parsed as keys; Enter is a separate keypress after a short delay.
- **`register.sh` / `deregister.sh`** + **`hooks/session-{start,end}.sh`** — a
  session self-registers (Claude Code SessionStart hook) and deregisters
  (SessionEnd); `viewer_url` defaults to `claude://code/{session_id}`.
- **`install.sh`** — deploys everything, puts secrets in the macOS Keychain, and
  runs the relay as a per-user LaunchAgent.
- **`cloudflare-setup.sh`** — exposes only `/v1/say` + `/v1/sessions` at a public
  hostname via a Cloudflare Tunnel (register/deregister stay loopback-only).

## Install (standalone)

Prereqs: macOS, `python3` (3.9+), `tmux`. Optional: `cloudflared` (for public
exposure; `install.sh`/`cloudflare-setup.sh` will `brew install` it).

```bash
git clone <this repo> && cd phone-home
bash install.sh            # deploy + Keychain secrets + LaunchAgent (relay live on 127.0.0.1:8765)
```

Config via env before `install.sh` (all optional): `PHONE_HOME_PREFIX`,
`PHONE_HOME_PORT`, `PHONE_HOME_DEFAULT`, `PHONE_HOME_REPLAY_TTL`.

Your token (for the Shortcut) and register-secret live in the Keychain:

```bash
security find-generic-password -a "$USER" -s phone-home-token -w
```

## Register a session

Auto (recommended): add the hooks to your Claude Code `settings.json` SessionStart
/ SessionEnd (append — don't replace existing hooks):

```json
{"type":"command","command":"bash ~/.claude/phone-home/hooks/session-start.sh 2>/dev/null || true","async":true}
{"type":"command","command":"bash ~/.claude/phone-home/hooks/session-end.sh 2>/dev/null || true","async":true}
```

Manual (from inside the tmux pane): `~/.claude/phone-home/register.sh --session-id <id>`
(or pass `--viewer <url>` / `--label <name>`).

## Expose it publicly (optional)

```bash
PHONE_HOME_HOSTNAME=phone-home.example.com bash cloudflare-setup.sh
```

One-time: `cloudflared tunnel login` opens a browser to authorize your zone; after
that it's automatic and re-runnable. Only `/v1/say` + `/v1/sessions` are routed.

## iOS Shortcut

Build a Shortcut: **Get Contents of URL** `https://<host>/v1/sessions?token=<token>`
(background) → **Choose from List** (the session labels) → **Dictate Text** →
**URL Encode** the dictated text → **Open URLs (Safari)**
`https://<host>/v1/say?token=<token>&session=<chosen-id>&q=<url-encoded-dictation>`.
The **URL Encode** step is required — without it the first space ends the `q`
value, so only the first dictated word reaches the relay. The final step **must**
be a foreground Safari navigation for the 302 to open the viewer. Bind it to the
Action Button / Back Tap. A static per-session shortcut (skip the chooser) is the
fast path.

> **If you enable replay protection** (`PHONE_HOME_REPLAY_TTL` > 0, off by default
> in v1): the relay then **requires** a unique `&nonce=` on every `/v1/say`, and
> rejects a missing/reused one with `409`. Add a **UUID** action in the Shortcut
> before the final step and append `&nonce=<that-uuid>` to the `/v1/say` URL.
> With the default `PHONE_HOME_REPLAY_TTL=0` no nonce is needed.

## Versioning

Every wire/file format is versioned so changes stay non-breaking:

- **API path** — endpoints live under `/v1/…`. The Shortcut URL embeds the
  version, so a server can serve `/v2/…` alongside `/v1/…` and never break an
  installed shortcut. Unknown/unversioned paths are rejected.
- **Registry file** carries a `version` (load guards on it; a newer schema is left
  intact, never clobbered).
- **Registration payload** carries `format_version` (newer rejected, unknown
  fields ignored — additive).
- Responses carry an `X-Phone-Home-Version` header.

**Deprecation policy** (within a major): fields are only added, never repurposed;
a breaking change takes a new version served next to the old until clients migrate.

## License

See [LICENSE](LICENSE).
