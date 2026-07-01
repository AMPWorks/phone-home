#!/usr/bin/env python3
"""phone-home — self-hosted relay: dictated text -> a registered local tmux
Claude Code session -> 302 redirect to that session's viewer.

Flow (one press on the phone):
    iOS Shortcut: GET /sessions (token)  -> Choose -> Dictate Text
      -> Open URLs (Safari): GET /say?token=<t>&session=<id>&q=<text>[&nonce=<n>]
         -> phone-home: validate token -> resolve session -> liveness+fingerprint
            -> strict idle-prompt guard -> tmux send-keys -l -- <text> ; Enter
            -> 302 -> that session's viewer_url

It is GENERAL: any local tmux session (typically a `claude --remote-control`
pane, but anything) self-registers over loopback; the phone picks one; the relay
routes injection + redirect per session. Nothing here is amp-agent-specific.

Why it is built this way — do NOT "simplify" these away:
  * GET + Safari navigation (NOT a background POST) is required for the 302 to
    launch the viewer. Shortcuts' "Get Contents of URL" ignores redirects.
  * Dictated text reaches tmux via argv, never a shell string. shell=True here
    would make every dictation arbitrary command execution on a headless box.
  * `send-keys -l` (literal) is LOAD-BEARING: it stops dictated words like
    "enter" / "C-c" from being parsed as tmux key names. Enter is sent
    separately after a short delay so the paste registers before the TUI submits.
  * Newlines are flattened: Enter submits in the Claude TUI, so a stray newline
    would fire a half-typed prompt.
  * `/register` + `/deregister` are loopback-ONLY (enforced in-process, not just
    by the tunnel). `/say` + `/sessions` are token-gated and may be exposed
    publicly (e.g. a Cloudflare-Tunnel subdomain).
  * The target session likely runs with permission prompts disabled
    (`--permission-mode bypassPermissions`), so the strict idle-prompt guard is
    the ONLY thing between a stray dictated phrase and tool execution. On by
    default; `--no-strict` to override (documented best-effort — TUI state read
    via capture-pane is heuristic).

Security knobs:
  * --token / $PHONE_HOME_TOKEN        shared secret for /say + /sessions (required).
  * --register-secret / $PHONE_HOME_REGISTER_SECRET  optional secret /register must
    present (§11-f; defends against a rogue local process registering a route).
  * --replay-ttl <secs>                optional /say replay window (§11-d): when >0,
    /say requires a &nonce and each nonce is single-use within the window, so a
    captured URL replayed from Safari history is refused.

3.9-stdlib-only (system python on the box is 3.9.6). No third-party deps.
"""
import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def is_loopback(addr):
    """True iff the client address is loopback. Defense-in-depth for the
    register/deregister endpoints — independent of any tunnel/serve config."""
    return addr in LOOPBACK


# --------------------------------------------------------------------------- versioning
#
# Every wire/file format carries an explicit version so future format changes are
# straightforward and non-breaking (cf. the agentmesh protocol's versioning):
#
#   * API path  — endpoints live under `/v{API_VERSION}/…` (e.g. `/v1/say`). The
#     iOS Shortcut URL embeds the version, so a server upgrade can serve `/v2/…`
#     ALONGSIDE `/v1/…` and never break already-installed shortcuts. An unknown
#     version path is rejected with a clear `unsupported api version` error.
#   * Registry file — carries a top-level `"version"`; load migrates/guards on it.
#   * Registration payload + stored entry — carry `format_version`; a client may
#     send `"v"` and the server rejects a *newer* format it doesn't understand.
#   * Responses — carry an `X-Phone-Home-Version` header (and a `version` field in
#     JSON object responses) so clients can detect the server's format.
#
# Deprecation policy (within a major version): fields are only ADDED, never
# repurposed or removed; unknown request fields are ignored (forward-compatible).
# A breaking change takes a NEW version (new path / bumped SCHEMA_VERSION) served
# next to the old until clients migrate.
API_VERSION = "1"          # URL path version: /v1/<endpoint>
SCHEMA_VERSION = 1         # registry-file schema version
REG_FORMAT_VERSION = 1     # registration payload / stored-entry format version

# Provenance marker prepended to injected dictation so the receiving agent can
# tell phone-home-relayed input apart from text the user typed directly. A single
# server-global, VISIBLE prefix (left in the transcript — honest about provenance,
# not stripped); it lives INSIDE the literal `send-keys -l` payload, so the
# load-bearing literal-injection safety is unaffected. amp-agent recognises this
# exact prefix (see its phone-home marker handling). Empty disables the marker.
INJECT_MARKER_DEFAULT = "[via phone] "


def _flatten(text):
    """One line, trimmed. Enter submits in the TUI; a stray newline fires early."""
    return " ".join(text.splitlines()).strip()


# --------------------------------------------------------------------------- tmux

def _tmux(socket_path, *args):
    """Run a tmux command on a specific server socket; return CompletedProcess."""
    cmd = ["tmux"]
    if socket_path:
        cmd += ["-S", socket_path]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def pane_fingerprint(socket_path, pane_id):
    """(pid, start) of the pane, or None if the pane doesn't exist.

    Used as the §11-a fingerprint: pane ids (%N) are reused after a tmux server
    restart, so a stale entry's %N can resolve to a DIFFERENT live pane. Capturing
    pid+start at registration and re-checking before every inject detects that
    reuse (mismatch -> refuse + sweep). tmux-native; more reliable than writing a
    nonce into pane env (which tmux can't store per-pane anyway).
    """
    r = _tmux(socket_path, "display-message", "-p", "-t", pane_id,
              "-F", "#{pane_pid} #{pane_start_time}")
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return None
    return out  # opaque "pid start" string; compared verbatim


def capture_tail(socket_path, pane_id):
    """The last screenful of the pane (`capture-pane -p -S -12`), or "" if the
    capture fails. A single shared read so the strict guard (`pane_looks_idle`)
    and the survey-detect (`is_rating_survey`) see the SAME window."""
    r = _tmux(socket_path, "capture-pane", "-p", "-t", pane_id, "-S", "-12")
    return r.stdout if r.returncode == 0 else ""


def pane_looks_idle(socket_path, pane_id):
    """Best-effort §10 strict guard: does the pane look like an idle Claude input
    prompt (safe to inject), vs awaiting a y/n, in a menu, or mid-stream?

    Heuristic — capture-pane is not authoritative. Conservative: only return True
    when the tail looks like an idle prompt; refuse on the slightest doubt.
    """
    raw = capture_tail(socket_path, pane_id)
    if not raw:
        return False
    tail = raw.lower()
    nonempty = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    # Danger signals: a pending permission/confirmation prompt. Specific
    # tokens/phrases are safe as substrings.
    danger = ("y/n", "(y/n)", "yes/no", "[y/n]", "do you want", "proceed?",
              "press enter to")
    if any(d in tail for d in danger):
        return False
    # Generic verbs that also live inside ordinary words ("selector", "selected",
    # "chooses", "approved") must match as WHOLE WORDS — else a task title like
    # "iOS Shortcut session selector" sitting in the transcript 409'd an idle pane
    # (live amp-agent pane, 2026-07-01). A real menu still says "select"/"choose".
    if re.search(r"\b(?:approve|select|choose)\b", tail):
        return False
    # A pending question: any of the last few visible lines ends in "?" (e.g.
    # "Continue?", "Overwrite this file?") — a confirmation the danger keywords
    # above don't enumerate. Bounded to the last 3 non-empty lines so a rhetorical
    # "?" higher up in finished output doesn't permanently wedge the prompt.
    if any(ln.rstrip().endswith("?") for ln in nonempty[-3:]):
        return False
    # A numbered-choice menu (a selection list, or the periodic "How is Claude
    # doing this session?" survey: "1: Bad  2: Fine  3: Good  0: Dismiss"): two or
    # more option markers like "1." / "1:" / "1)". Detected here so the bare "❯"
    # prompt glyph does NOT have to be a blanket danger signal — it is the
    # amp-agent TUI's *idle* input prompt, not (only) a menu arrow.
    #
    # Each marker must be a **standalone token** — the digit preceded by
    # start-of-line or whitespace, and the separator followed by whitespace or
    # end. Otherwise incidental digits in ordinary output false-positived as a
    # menu and 409'd an idle pane: e.g. "(PR #2)" / "(PR #3)" in a transcript,
    # "v2.0", "$0.00", a time like "19:43" (observed on the live amp-agent pane,
    # 2026-07-01). The survey's inline "1: 2: 3: 0:" and multi-line lists still
    # match (their markers are whitespace-delimited).
    opt_markers = len(re.findall(r"(?<!\S)\d[.:)](?=\s|$)", tail))
    if opt_markers >= 2:
        return False
    # NB: we deliberately do NOT treat a "❯ <text>" line as a menu cursor — in the
    # Claude TUI past user turns render as "❯ <message>" in the transcript, so that
    # would refuse on any session with conversation history (the exact
    # over-refusal that made the bare-"❯" prompt unusable). Real confirmations are
    # caught above by the danger keywords, the trailing-"?" check, and the
    # numbered-menu check. A questionless/numberless "❯ Yes / No" menu is a known
    # residual fail-open, tracked on the security-hardening follow-up.
    # Idle signal: a Claude Code input prompt near the bottom. The amp-agent TUI
    # draws "❯" as its prompt glyph; the standard TUI draws "│ >" / a "╰" box edge
    # / the "for shortcuts" hint.
    idle = ("❯", "│ >", "> ", "╰", "esc to", "for shortcuts")
    return any(s in tail for s in idle)


def is_rating_survey(text):
    """True iff the captured tail is the dismissable Claude Code session-rating
    survey ("How is Claude doing this session?" / "1: Bad 2: Fine 3: Good
    0: Dismiss"). Requires BOTH the anchor phrase AND the `0: dismiss` affordance
    so a chat message that merely quotes the phrase can't be mistaken for the live
    menu. This survey is advisory and Escape-dismissable — unlike a real y/n
    confirmation or a genuine selection menu, which must still refuse."""
    t = (text or "").lower()
    if "how is claude doing" not in t:
        return False
    return any(aff in t for aff in ("0: dismiss", "0. dismiss", "0) dismiss"))


def dismiss_survey(socket_path, pane_id, submit_delay):
    """Dismiss the session-rating survey by pressing its own `0: Dismiss`
    affordance, sent as a LITERAL key (`-l -- "0"`).

    Escape does NOT dismiss this survey — verified against the live Claude Code
    TUI on 2026-07-01 (an `Escape` send-key left the survey on screen, so `/say`
    then 409'd "not idle after dismissing"). The survey is a numbered menu whose
    only exit that doesn't record a rating is `0: Dismiss`; the `1/2/3`
    Bad/Fine/Good options are never sent, so no rating is submitted. `-l` keeps
    the "0" a literal keystroke (not a tmux key-name)."""
    _tmux(socket_path, "send-keys", "-t", pane_id, "-l", "--", "0")
    time.sleep(submit_delay)


def wait_idle(socket_path, pane_id, attempts=3, interval=0.2):
    """Bounded re-check after dismissing the survey: a slow-settling TUI may take a
    moment to return to the idle prompt. Re-checks `pane_looks_idle` up to
    `attempts` times (~0.5s total at the default 3×0.2s), returning True on the
    first idle check and False if all attempts fail (one dismiss, no retry loop)."""
    for i in range(attempts):
        if pane_looks_idle(socket_path, pane_id):
            return True
        if i < attempts - 1:
            time.sleep(interval)
    return False


def inject(socket_path, pane_id, text, submit_delay, marker=""):
    """Paste literal text then submit. `-l` is the load-bearing safety flag.

    `marker` (the phone-home provenance prefix) is prepended to the flattened text
    and stays INSIDE the single literal `send-keys -l -- <payload>` argument, so
    the literal-injection safety property is preserved. Applied only to a
    non-empty message (a bare marker is never injected)."""
    text = _flatten(text)
    if not text:
        return
    if marker:
        text = marker + text
    # argv, never a shell. `-l` literal (no key-name parsing). `--` guards a
    # leading '-'. Target the exact pane on the captured socket.
    subprocess.run(["tmux", "-S", socket_path, "send-keys", "-t", pane_id,
                    "-l", "--", text], check=True)
    time.sleep(submit_delay)
    subprocess.run(["tmux", "-S", socket_path, "send-keys", "-t", pane_id, "Enter"],
                   check=True)


# ----------------------------------------------------------------------- registry

class Registry:
    """JSON registry of live sessions, atomic + single-writer-locked (§11-h).

    Entry: {id, label, tmux_socket, pane_id, viewer_url, repo, fingerprint,
            registered_at}. `id` is an unguessable token (the addressing nonce).
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return {"version": SCHEMA_VERSION, "sessions": []}
        if not (isinstance(d, dict) and isinstance(d.get("sessions"), list)):
            return {"version": SCHEMA_VERSION, "sessions": []}
        ver = d.get("version", 1)  # files written before versioning are v1
        if ver > SCHEMA_VERSION:
            # A newer schema than we understand: do NOT clobber it. Start empty
            # in-memory; we won't overwrite the file unless a write is forced.
            print("phone-home: registry schema v%s > supported v%s; ignoring on load"
                  % (ver, SCHEMA_VERSION), file=sys.stderr)
            return {"version": SCHEMA_VERSION, "sessions": []}
        # ver <= SCHEMA_VERSION: (future) run migrations here; v1 needs none.
        d["version"] = SCHEMA_VERSION
        return d

    def _save_locked(self):
        self._data["version"] = SCHEMA_VERSION
        tmp = self.path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # atomic

    def register(self, label, tmux_socket, pane_id, viewer_url, repo=""):
        """Upsert by (tmux_socket, pane_id). A duplicate *live* label pointing at a
        different pane is rejected by the caller; here we key on the pane."""
        fp = pane_fingerprint(tmux_socket, pane_id)
        if fp is None:
            raise ValueError("pane %s on socket %s does not exist" % (pane_id, tmux_socket))
        with self._lock:
            self._data["sessions"] = [
                s for s in self._data["sessions"]
                if not (s["tmux_socket"] == tmux_socket and s["pane_id"] == pane_id)
            ]
            entry = {
                "format_version": REG_FORMAT_VERSION,
                "id": secrets.token_urlsafe(16),
                "label": label,
                "tmux_socket": tmux_socket,
                "pane_id": pane_id,
                "viewer_url": viewer_url,
                "repo": repo,
                "fingerprint": fp,
                "registered_at": int(time.time()),
            }
            self._data["sessions"].append(entry)
            self._save_locked()
            return entry

    def deregister(self, label=None, pane_id=None, tmux_socket=None):
        """Remove sessions matching the given selector. A pane match is ALWAYS
        scoped to its socket: bare pane ids (the first pane of every tmux server
        is %0/%1) are reused across servers, so deregistering by pane_id alone
        would silently drop an unrelated live session in another tmux server."""
        def _match(s):
            if label is not None and s["label"] == label:
                return True
            if (pane_id is not None and tmux_socket is not None
                    and s["pane_id"] == pane_id and s["tmux_socket"] == tmux_socket):
                return True
            return False
        with self._lock:
            before = len(self._data["sessions"])
            self._data["sessions"] = [s for s in self._data["sessions"] if not _match(s)]
            removed = before - len(self._data["sessions"])
            if removed:
                self._save_locked()
            return removed

    def _drop_locked(self, entry_id):
        self._data["sessions"] = [s for s in self._data["sessions"] if s["id"] != entry_id]
        self._save_locked()

    def live(self):
        """Validated sessions: pane exists AND fingerprint matches. Lazily sweeps
        stale/mismatched entries (covers kill -9 + tmux-server-restart %N reuse)."""
        out, stale = [], []
        with self._lock:
            for s in list(self._data["sessions"]):
                if pane_fingerprint(s["tmux_socket"], s["pane_id"]) == s["fingerprint"]:
                    out.append(s)
                else:
                    stale.append(s["id"])
            if stale:
                self._data["sessions"] = [s for s in self._data["sessions"] if s["id"] not in stale]
                self._save_locked()
        return out

    def resolve(self, key):
        """Find a LIVE session by id or label (id preferred). None if absent/stale."""
        live = self.live()
        for s in live:
            if s["id"] == key:
                return s
        for s in live:
            if s["label"] == key:
                return s
        return None


# -------------------------------------------------------------------------- replay

class NonceCache:
    """Single-use nonces within a TTL window (§11-d). ttl<=0 disables (no nonce required)."""

    def __init__(self, ttl):
        self.ttl = ttl
        self._seen = {}
        self._lock = threading.Lock()

    def check_and_consume(self, nonce):
        if self.ttl <= 0:
            return True  # replay protection disabled
        if not nonce:
            return False
        now = time.time()
        with self._lock:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self.ttl}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


# ---------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def _client_is_loopback(self):
        return is_loopback(self.client_address[0])

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Phone-Home-Version", API_VERSION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _endpoint(self):
        """Strip the required `/v{API_VERSION}/` prefix; return the endpoint name,
        or None (and send an error) if the version is missing/unsupported."""
        parts = urlparse(self.path).path.strip("/").split("/", 1)
        if not parts or not parts[0].startswith("v") or not parts[0][1:].isdigit():
            self.send_error(404, "missing api version prefix (use /v%s/...)" % API_VERSION)
            return None
        if parts[0] != "v" + API_VERSION:
            self.send_error(404, "unsupported api version %r (this server speaks v%s)"
                            % (parts[0], API_VERSION))
            return None
        return parts[1] if len(parts) > 1 else ""

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    # ---- routing (all endpoints under /v{API_VERSION}/)
    def do_POST(self):
        ep = self._endpoint()
        if ep is None:
            return
        if ep == "register":
            return self._register()
        if ep == "deregister":
            return self._deregister()
        self.send_error(404)

    def do_GET(self):
        ep = self._endpoint()
        if ep is None:
            return
        q = parse_qs(urlparse(self.path).query)
        if ep == "sessions":
            return self._sessions(q)
        if ep == "say":
            return self._say(q)
        self.send_error(404)

    # ---- loopback-only
    def _register(self):
        if not self._client_is_loopback():
            return self.send_error(403, "register is loopback-only")
        b = self._body()
        if b is None:
            return self.send_error(400, "bad json")
        srv = self.server
        # Registration payload format version: reject a NEWER format we can't
        # parse; ignore unknown fields (forward-compatible, additive-only).
        try:
            bver = int(b.get("v", REG_FORMAT_VERSION))
        except (TypeError, ValueError):
            return self.send_error(400, "bad registration format version")
        if bver > REG_FORMAT_VERSION:
            return self.send_error(400, "unsupported registration format v%d (server speaks v%d)"
                                   % (bver, REG_FORMAT_VERSION))
        if srv.register_secret and not hmac_eq(b.get("register_secret", ""), srv.register_secret):
            return self.send_error(403, "bad register secret")
        for k in ("label", "tmux_socket", "pane_id", "viewer_url"):
            if not b.get(k):
                return self.send_error(400, "missing %s" % k)
        # reject a duplicate *live* label pointing at a different pane (unless force)
        if not b.get("force"):
            for s in srv.registry.live():
                if s["label"] == b["label"] and s["pane_id"] != b["pane_id"]:
                    return self.send_error(409, "live label %r already bound to another pane" % b["label"])
        try:
            entry = srv.registry.register(b["label"], b["tmux_socket"], b["pane_id"],
                                          b["viewer_url"], b.get("repo", ""))
        except ValueError as e:
            return self.send_error(400, str(e))
        self._json(200, {"version": REG_FORMAT_VERSION, "id": entry["id"], "label": entry["label"]})

    def _deregister(self):
        if not self._client_is_loopback():
            return self.send_error(403, "deregister is loopback-only")
        b = self._body()
        if b is None:
            return self.send_error(400, "bad json")
        # A pane match must be socket-scoped (pane ids repeat across tmux servers).
        if not (b.get("label") or (b.get("pane_id") and b.get("tmux_socket"))):
            return self.send_error(400, "need label, or pane_id + tmux_socket")
        removed = self.server.registry.deregister(
            label=b.get("label"), pane_id=b.get("pane_id"), tmux_socket=b.get("tmux_socket"))
        self._json(200, {"removed": removed})

    # ---- token-gated (may be public via tunnel)
    def _authed(self, q):
        return hmac_eq((q.get("token") or [""])[0], self.server.token)

    def _sessions(self, q):
        if not self._authed(q):
            return self.send_error(403)
        live = self.server.registry.live()
        self._json(200, [{"id": s["id"], "label": s["label"], "repo": s["repo"]} for s in live])

    def _say(self, q):
        if not self._authed(q):
            return self.send_error(403)
        srv = self.server
        if not srv.nonces.check_and_consume((q.get("nonce") or [""])[0]):
            return self.send_error(409, "replay or missing nonce")
        key = (q.get("session") or [srv.default_session or ""])[0]
        text = (q.get("q") or [""])[0]
        if not key:
            return self.send_error(400, "no session and no default")
        s = srv.registry.resolve(key)
        if s is None:
            return self.send_error(404, "no live session %r" % key)
        try:
            if srv.strict and not pane_looks_idle(s["tmux_socket"], s["pane_id"]):
                # Not idle. If the ONLY blocker is the dismissable Claude
                # session-rating survey ("How is Claude doing this session?"), a
                # phone message must not be lost to that advisory poll: dismiss it
                # (its "0" affordance), then re-check (bounded) and inject. Real
                # confirmations / genuine selection menus / mid-stream output still
                # refuse. One dismiss attempt — a pane that keeps re-prompting
                # can't wedge the request.
                tail = capture_tail(s["tmux_socket"], s["pane_id"])
                if not is_rating_survey(tail):
                    return self.send_error(409, "target not at an idle prompt (strict guard)")
                dismiss_survey(s["tmux_socket"], s["pane_id"], srv.submit_delay)
                if not wait_idle(s["tmux_socket"], s["pane_id"]):
                    return self.send_error(409, "target not idle after dismissing the rating survey")
            inject(s["tmux_socket"], s["pane_id"], text, srv.submit_delay, srv.inject_marker)
        except (subprocess.CalledProcessError, OSError) as e:
            # OSError covers a missing `tmux` binary (FileNotFoundError) — e.g. a
            # launchd PATH that lacks /opt/homebrew/bin — so it 502s, not 500s.
            return self.send_error(502, "inject failed: %s" % e)
        self.send_response(302)
        self.send_header("Location", s["viewer_url"])
        self.end_headers()

    def log_message(self, *_a):  # keep dictated text out of stderr
        return


def hmac_eq(a, b):
    import hmac
    return bool(b) and hmac.compare_digest(str(a), str(b))


def main(argv=None):
    p = argparse.ArgumentParser(description="phone-home relay: dictated text -> registered tmux session -> redirect.")
    p.add_argument("--registry", default=os.environ.get("PHONE_HOME_REGISTRY",
                   os.path.expanduser("~/.config/phone-home/registry.json")))
    p.add_argument("--token", default=os.environ.get("PHONE_HOME_TOKEN"),
                   help="shared secret for /say + /sessions (or $PHONE_HOME_TOKEN)")
    p.add_argument("--register-secret", default=os.environ.get("PHONE_HOME_REGISTER_SECRET", ""),
                   help="optional secret /register must present (§11-f)")
    p.add_argument("--default-session", default=os.environ.get("PHONE_HOME_DEFAULT", ""),
                   help="session id/label used by /say when none is given")
    p.add_argument("--bind", default="127.0.0.1", help="keep loopback; expose /say+/sessions via a tunnel")
    p.add_argument("--port", type=int, default=int(os.environ.get("PHONE_HOME_PORT", "8765")))
    p.add_argument("--submit-delay", type=float, default=0.2,
                   help="seconds between paste and Enter (200ms is empirically safe for the Claude TUI)")
    p.add_argument("--replay-ttl", type=float, default=float(os.environ.get("PHONE_HOME_REPLAY_TTL", "0")),
                   help="single-use /say nonce window in seconds (§11-d); 0 disables")
    p.add_argument("--inject-marker",
                   default=os.environ.get("PHONE_HOME_INJECT_MARKER", INJECT_MARKER_DEFAULT),
                   help="visible provenance prefix prepended to injected dictation so the "
                        "receiving agent can tell it came via phone-home (default %r; "
                        "set empty to disable)" % INJECT_MARKER_DEFAULT)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--strict", dest="strict", action="store_true", default=True,
                   help="refuse injection unless the pane looks idle (default; §10)")
    g.add_argument("--no-strict", dest="strict", action="store_false")
    args = p.parse_args(argv)

    if not args.token:
        print("phone-home: --token / $PHONE_HOME_TOKEN is required", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(args.registry), exist_ok=True)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.registry = Registry(args.registry)
    srv.token = args.token
    srv.register_secret = args.register_secret
    srv.default_session = args.default_session
    srv.submit_delay = args.submit_delay
    srv.strict = args.strict
    srv.inject_marker = args.inject_marker
    srv.nonces = NonceCache(args.replay_ttl)
    print("phone-home on %s:%d (registry=%s, strict=%s, replay_ttl=%s)"
          % (args.bind, args.port, args.registry, args.strict, args.replay_ttl), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
