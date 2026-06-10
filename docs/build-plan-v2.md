# Build plan v2 — amp-say: voice → registered tmux Claude session → redirect

**Supersedes v1.** Adds multi-session registration/routing (§9–10) and an
adversarial review with resulting design changes (§11). Sections 1–8 carry over
from v1 with edits marked.

**Target executor:** amp-agent, via todo-handler skill
**Canonical infra base:** ampworksstudio.com · **Host:** Mac Mini M4 (headless, macOS — launchd)
**Hard dependency:** Tailscale (hosted)

> **Open questions pending Adam (assumptions in brackets, revisit on answer):**
> Q1 viewer addressing [session supplies full `viewer_url`]; Q2 trigger [Claude Code
> hooks + CLI fallback]; Q3 phone UX [dynamic chooser + default fast-path]; Q4 tmux
> topology [capture socket, route with `tmux -S`]; Q5 inject safety [`--strict` on];
> Q6 persistence [JSON].

---

## 1. Objective (revised)

One-press voice capture on iPhone that injects transcribed text into a **chosen,
self-registered** local Claude Code tmux session on the Mac Mini, then redirects the
phone to that session's viewer.

```
Shortcut: [Get /sessions] -> Choose -> Dictate Text
  -> Open URLs (Safari) https://<host>/say?token=<secret>&session=<id>&q=<text>
     -> amp-say: validate target + fingerprint -> tmux -S <sock> send-keys (literal) + Enter
        -> 302 -> that session's viewer_url
```

## 2–3. Cross-references & inputs to resolve on the box

(Unchanged from v1: reconcile against the prior Action Button todo; reuse the proven
viewer URL; confirm tmux target, tailnet host, run user, secret storage.)
**Added input:** the per-session viewer addressing scheme (Q1) — needed to construct
`viewer_url` at registration.

## 4. File manifest (revised)

| File | Status | Built where |
| --- | --- | --- |
| `amp_say.py` | provided v1; **needs registry/routing rework per §9** | box |
| `register.sh` / `deregister.sh` (+ `amp-register` CLI) | to build | box |
| Claude Code SessionStart/SessionEnd hook entries | to build | box (per-project or global) |
| `com.ampworksstudio.amp-say.plist` (LaunchAgent) | to build | box |
| `install.sh` (idempotent) | to build | box |
| iOS Shortcut (chooser → Dictate → Open URLs) | to build | phone |

## 5. Build phases (additions in **bold**)

- **P0** Deploy reworked listener; loopback smoke test of `/say` against a manually
  registered session.
- **P0.5 (new) Registration path:** implement `/register` `/deregister` `/sessions`;
  verify a session appears in `/sessions` after `register.sh` and disappears after
  `deregister.sh` and after the pane is killed (lazy sweep).
- **P1** LaunchAgent in `user/$(id -u)` domain (not Daemon — per-user tmux). Headless:
  confirm load without GUI login (`launchctl bootstrap user/$UID`, auto-login if needed).
- **P2** Tailscale Serve: map **`/say` and `/sessions` only**; keep
  `/register` `/deregister` loopback-only.
- **P3** Secret(s) in Keychain; **optional second registration secret** (§11-f).
- **P4** Shortcut: Get Contents of URL `/sessions` (background OK) → Choose from List →
  Dictate → Open URLs (Safari) `/say`. Plus a static per-session fast-path Shortcut.
- **P5** End-to-end + `--submit-delay` tuning + **`--strict` guard verification**
  (inject is refused when the target pane is not at an idle prompt).

## 6. Security checklist (additions in **bold**)

- [ ] Listener loopback-only; only `/say` + `/sessions` exposed via Tailscale Serve.
- [ ] **`/register` `/deregister` never exposed off-loopback.**
- [ ] tmux invoked via argv, never a shell.
- [ ] `hmac.compare_digest` on token for tailnet endpoints.
- [ ] **Per-session fingerprint verified before every injection (§11-a).**
- [ ] **`--strict` idle-prompt guard on by default (§11-b).**
- [ ] Token rotatable; understood it transits in URL/Safari history.
- [ ] **Replay risk noted (§11-d): consider short-lived nonce param if it matters.**

## 7. Rollback / kill switch

`launchctl bootout …/com.ampworksstudio.amp-say` + `tailscale serve --https=443 off`.
**New:** clearing the registry file and restarting drops all routes; individual
sessions removable via `amp-deregister <label>`.

## 8. Out of scope

Hosted-mobile-session injection; multi-line batching; auth beyond shared secret
(mTLS / Tailscale identity headers) if this graduates past personal use.

---

## 9. Session registration & routing (NEW)

amp-say becomes a **registry/router**: local Claude tmux sessions self-register, the
phone picks one, the service routes injection and redirect per session.

**Endpoints**
- `POST /register` (loopback) — `{label, tmux_socket, pane_id, viewer_url, repo?, nonce}`.
  Upsert. Reject a duplicate *live* label pointing at a different pane unless `force`.
- `POST /deregister` (loopback) — by `label` or `pane_id`.
- `GET /sessions` (tailnet, token) — live, validated sessions only: `[{id,label,repo}]`.
- `GET /say` (tailnet, token) — `session` (id/label) + `q`. Validate liveness +
  fingerprint → inject → `302` to that session's `viewer_url`. If `session` omitted,
  use the configured default.

**Routing key:** `pane_id` (`%N`) **plus captured `tmux_socket`**; route with
`tmux -S <socket> send-keys -t <pane_id> …`. `label` is display/selector only and must
be unique among *live* sessions.

**Registration trigger [Q2 assumption]:** Claude Code `SessionStart` hook runs
`register.sh` (label from repo/dir basename, captures `$TMUX_PANE` + socket, writes a
nonce into the pane env, POSTs `/register`); `SessionEnd`/`Stop` hook runs
`deregister.sh`. Manual `amp-register` / `amp-deregister` CLI as fallback.

**Liveness:** lazy validation at `/say` and `/sessions` (pane exists AND fingerprint
matches); background sweep every 30s; deregister hook is best-effort (won't fire on
`kill -9`, covered by validation).

**Persistence [Q6 assumption]:** JSON file, atomic temp+rename, single-writer lock,
re-validated against tmux on load. Swap to SQLite if it grows.

**Phone UX [Q3 assumption]:** dynamic chooser (`/sessions` → Choose from List → Dictate
→ `/say`). Note: the `/sessions` fetch may be a background Get-Contents call, but the
final `/say` MUST be a foreground Open-URLs (Safari) navigation for the 302 to fire.
Static per-session Shortcut is the no-chooser fast path.

## 10. Strict injection guard (NEW, default on)

Before `send-keys`, best-effort confirm the target pane is at an **idle Claude input
prompt**; refuse (HTTP 409) if it appears to be awaiting a permission y/n, sitting in a
menu, or mid-stream — so stray dictation can't auto-approve an action. `--no-strict`
overrides. Documented as best-effort: external observation of TUI state via
`capture-pane` is heuristic, not authoritative.

## 11. Adversarial review — findings & dispositions

**a. Wrong-session injection after tmux server restart (HIGH).** Pane ids reset when the
server restarts; a stale entry's `%N` can resolve to a different live pane → misrouted
prompt. **Fix:** per-session nonce written into pane env at registration; re-read and
compare (`capture-pane`/env probe) before every inject; mismatch → refuse + sweep. Adopted.

**b. Auto-answering a permission/menu prompt (HIGH).** Text+Enter arriving while Claude
awaits a y/n or is in a menu can approve unseen actions. **Fix:** §10 `--strict` guard.
Adopted; residual risk documented (heuristic).

**c. tmux socket mismatch (MED).** Service and sessions must share a socket; multi-socket
setups silently fail or misroute. **Fix:** capture socket at registration, route with
`tmux -S`. Adopted (Q4).

**d. Replay of a captured `token+q` URL (MED).** The URL sits in Safari history; replay
re-injects the prompt. **Disposition:** documented; optional short-lived signed nonce
param deferred to §8 unless Adam wants it now.

**e. Concatenation onto a half-typed line (MED).** `send-keys -l` appends to whatever's in
the input. **Disposition:** optional per-session `clear_first` (send `C-u`) flag, default
OFF (clobber risk); rely on `--strict` to avoid the worst case. Flag for Q5 follow-up.

**f. Rogue local registration (LOW, single-user box).** Any loopback process could register
a label aimed at a pane it controls. **Disposition:** optional registration secret in
Keychain required by `/register`. Cheap; recommend adopting.

**g. Stale entries from `kill -9` skipping deregister (LOW).** Covered by lazy validation +
sweep (a). No extra work.

**h. Registry file corruption under concurrent writers (LOW).** Atomic temp+rename +
single-writer lock. Adopted.

**i. Fixed `--submit-delay` is timing-fragile (LOW).** Known fragility; tunable. A
verify-paste-landed-before-Enter loop is possible but adds `capture-pane` polling
complexity — deferred unless P5 tuning proves flaky.
