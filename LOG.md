# Development Log — phone-home

A timestamped narrative of how phone-home was designed and built. Forward
chronological — oldest at the top, newest appended at the bottom. Each entry
captures substantive decisions, milestones, trade-offs, and discoveries; routine
implementation activity (file moves, typos) is out of scope — Git carries that.

Maintained by Adam and any AI agents on the project. Entries use ISO 8601 UTC
headers to the second (`## YYYY-MM-DD HH:MM:SS UTC — Subject`). This log was
started 2026-06-07; the entries below are written as the work landed.

This log is source material for a future technical writeup on building a
phone-to-home-server dictation relay.

---

## 2026-06-07 — Genesis: from "amp-say" to "phone-home"

The mechanism was first sketched in a Claude Code session under the working title
**amp-say** and copied into this repo as a build plan (`docs/build-plan-v2.md`):
voice on an iPhone → injected into a registered `tmux` Claude Code session on the
Mac mini → the phone redirected to that session's viewer. Adam renamed it
**phone-home** (the phone reaching its home server) and set the scope: build as
much as possible in one round on the Mac mini itself, generalize it to **any**
`tmux --remote-control` session (not just the amp-agent one), and package it so
others can reproduce the setup standalone.

Six open questions from the build plan were resolved up front. The biggest —
"what is a session's viewer, and is a web terminal wanted?" — collapsed a large
chunk of the plan: amp-agent already runs `claude --remote-control`, so the viewer
is simply the iOS Claude Code app reached by a `claude://code/{id}` deep-link.
**No web terminal**; the public surface is just a redirect. Auth for v1 is a
shared token (deliberately minimal — "insecure is OK for a bit, security comes
next"), tracked as an explicit hardening follow-up.

## 2026-06-07 — The relay, and versioning everything

The core is `phone_home.py`, a stdlib-only (Python 3.9) HTTP relay that is a
registry + router: local sessions self-register over loopback; the phone lists
them, picks one, and `/say` injects + 302-redirects. Two safety properties are
load-bearing and were carried verbatim from the v1 sketch: dictated text reaches
tmux via **`send-keys -l`** (literal, so words like "enter" aren't parsed as
keys) over **argv, never a shell**. Added: a per-session **fingerprint**
(pane pid+start-time) re-checked before every inject, so a tmux-server restart
that reuses pane ids can't misroute dictation; and a **strict idle-prompt guard**
— since the target session runs with permission prompts disabled, that heuristic
is the only thing between a stray phrase and tool execution.

Adam then asked that **every wire/file format be versioned** so future changes
are non-breaking — the same forethought the agentmesh protocol got. Endpoints
moved under `/v1/`, the registry file and registration payload carry versions,
responses carry a version header, and a deprecation policy (additive within a
major; a breaking change is a new version served alongside the old) is documented.
The installed iOS Shortcut embeds `/v1/`, so a future `/v2/say` can ship without
breaking it. 29 unit + loopback-integration tests cover it.

## 2026-06-07 — Live on the box

`register.sh`/`deregister.sh` + SessionStart/End hooks let a session self-register
generically; an on-box smoke test (real tmux, real relay) passed. `install.sh`
deploys everything, generates the token + register-secret into the macOS Keychain,
and runs the relay as a per-user LaunchAgent — the relay is **live on the Mac mini**
(`127.0.0.1:8765`). One discovery worth recording: `launchctl bootstrap user/$uid`
failed with `5: Input/output error` on the headless/Aqua mini, while the legacy
`launchctl load -w` succeeded — so the installer tries bootstrap and falls back to
`load -w`. `cloudflare-setup.sh` exposes only `/v1/say` + `/v1/sessions` at
`phone-home.ampworksstudio.com` via a Cloudflare Tunnel, keeping register/deregister
loopback-only as defense-in-depth over the relay's own in-process check.
