# Hardware-testing the phone-home relay (on the Mac Mini)

Some phone-home changes can only be verified against the live relay + a real
`tmux` Claude session (the strict idle-guard, the survey dismiss, the injected
marker, session auto-pick). Unit tests mock the `tmux` layer, so they can't catch
e.g. "the wrong key was sent to dismiss the survey." This is the runbook for the
on-Mini checks that back the `## Testing Required` items in the plans.

> Everything here runs **on the Mini** (`amp-mini.local`) — SSH in or sit at it.
> The relay is a per-user LaunchAgent (`com.ampworksstudio.phone-home`) serving
> `127.0.0.1:8765`, exposed publicly only for `/v1/say` + `/v1/sessions` via the
> Cloudflare Tunnel. `install.sh` is idempotent — re-running is always safe.

## 0. Prerequisites

```bash
# Token (needed for /v1/sessions and /v1/say). Do not paste it anywhere public.
TOKEN=$(security find-generic-password -a "$USER" -s phone-home-token -w)

# Relay healthy? (403 without a token, 200 with one)
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8765/v1/sessions?token=$TOKEN"   # → 200

# Which sessions are registered? (a live Claude session self-registers via its
# SessionStart hook; label is what /say targets)
curl -s "http://127.0.0.1:8765/v1/sessions?token=$TOKEN" | python3 -m json.tool
```

## 1. Deploy the branch under test

Each PR is a branch. Point the live relay at it, test, then restore `main`:

```bash
cd ~/src/phone-home
git checkout <branch> && bash install.sh          # copies phone_home.py + reloads the LaunchAgent

# The relay restart takes ~1–2s; a curl during that window returns 000. Wait for 200:
TOKEN=$(security find-generic-password -a "$USER" -s phone-home-token -w)
for i in $(seq 1 8); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8765/v1/sessions?token=$TOKEN")" = 200 ] && break
  sleep 1
done

# Confirm the deployed file is the branch's:
diff -q phone_home.py ~/.claude/phone-home/phone_home.py && echo "deployed == branch"
```

**Restore production when done:**
```bash
git checkout main && bash install.sh
```

## 2. Drive `/say` over loopback (no iPhone needed)

The iPhone Shortcut just does a GET against `/v1/say` — the same code path you can
hit directly. This is the fastest way to exercise injection:

```bash
SOCK=/private/tmp/tmux-501/default       # the registered session's tmux socket (see registry.json)
PANE=%0                                   # its pane_id

# Capture the pane BEFORE:
tmux -S "$SOCK" capture-pane -p -t "$PANE" | tail -20

# Fire /say at a session by label. Success = HTTP 302 (redirect to the viewer_url).
curl -s -G -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  "http://127.0.0.1:8765/v1/say" \
  --data-urlencode "token=$TOKEN" \
  --data-urlencode "session=agent" \
  --data-urlencode "q=<your test message>"

# Capture AFTER — confirm the message was injected + submitted:
tmux -S "$SOCK" capture-pane -p -t "$PANE" | tail -20
```

> ⚠️ Injecting reaches the **live** session — the agent will read and act on the
> message. Use a benign, self-limiting message (e.g. "reply exactly TEST_OK and do
> nothing else"). Replay protection is off by default (`replay_ttl=0`), so no
> `&nonce=` is needed; if you enable it, append `&nonce=<unique>`.

## 3. Specific checks

### Survey dismiss (message must not drop while the rating survey is up)
1. Wait until the `agent` pane shows **`How is Claude doing this session? … 0: Dismiss`**.
2. Deploy the survey branch (or current `main`, which has the fix) and fire a `/say`.
3. **Pass:** the survey is dismissed and the message is injected (HTTP 302). The
   dismiss presses the survey's own **`0`** affordance — **not** `Escape` (Escape
   does *not* clear this survey; that was a real bug caught here on 2026-07-01).
4. Sanity-check detection directly without injecting:
   ```bash
   python3 - <<'PY'
   import importlib.util
   s=importlib.util.spec_from_file_location("ph","/Users/amp/.claude/phone-home/phone_home.py")
   ph=importlib.util.module_from_spec(s); s.loader.exec_module(ph)
   sock="/private/tmp/tmux-501/default"; pane="%0"
   print("is_rating_survey:", ph.is_rating_survey(ph.capture_tail(sock, pane)))
   PY
   ```

### Provenance marker
Fire a `/say` and confirm the injected line is prefixed with **`[via phone] `** in
the pane. The prefix stays inside the single literal `send-keys -l` payload.

### Single-session auto-pick
With **exactly one** session registered, omit `session=` entirely:
```bash
curl -s -G -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8765/v1/say" \
  --data-urlencode "token=$TOKEN" --data-urlencode "q=<msg>"     # → 302 (auto-picked)
```
With zero or two-plus sessions and no `session=`, expect **400** (never a guess).

## 4. Register a second session (for chooser / 2-session tests)

To exercise the iOS chooser you need two live sessions. Register a throwaway from
inside a second `tmux` pane (loopback-only endpoint), then deregister it after:

```bash
# register (from the pane you want to add; --label is what the chooser shows)
~/.claude/phone-home/register.sh --label "test-2" --viewer "https://example/x" --force

curl -s "http://127.0.0.1:8765/v1/sessions?token=$TOKEN" | python3 -m json.tool   # now two

# deregister when done
~/.claude/phone-home/deregister.sh --label "test-2"
```

## 5. After testing

Always restore production and confirm health:
```bash
cd ~/src/phone-home && git checkout main && bash install.sh
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8765/v1/sessions?token=$TOKEN"   # → 200
```
