#!/usr/bin/env python3
"""Unit + loopback-integration tests for phone_home.py. Stdlib only (py3.9).

The tmux layer is mocked, so these run anywhere (no real tmux/sessions needed).
Run: python3 -m unittest discover -s tests   (or: python3 tests/test_phone_home.py)
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import phone_home as ph  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_flatten(self):
        self.assertEqual(ph._flatten("a\nb\n c "), "a b  c")
        self.assertEqual(ph._flatten("  \n  "), "")

    def test_hmac_eq(self):
        self.assertFalse(ph.hmac_eq("x", ""))      # empty secret never matches
        self.assertFalse(ph.hmac_eq("", ""))
        self.assertTrue(ph.hmac_eq("s3cret", "s3cret"))
        self.assertFalse(ph.hmac_eq("s3cret", "other"))

    def test_is_loopback(self):
        self.assertTrue(ph.is_loopback("127.0.0.1"))
        self.assertTrue(ph.is_loopback("::1"))
        self.assertFalse(ph.is_loopback("10.0.0.5"))
        self.assertFalse(ph.is_loopback("100.64.0.1"))  # tailnet / public-ish


class TestInjectLiteralFlag(unittest.TestCase):
    def test_inject_uses_literal_flag(self):
        """Regression guard: dictated text MUST be sent with `-l` so words like
        'enter' aren't parsed as tmux keys; Enter is a separate keypress."""
        calls = []

        class FakeCompleted:
            returncode = 0

        def fake_run(argv, **kw):
            calls.append(argv)
            return FakeCompleted()

        orig = ph.subprocess.run
        ph.subprocess.run = fake_run
        try:
            ph.inject("/tmp/sock", "%3", "say enter now", submit_delay=0)
        finally:
            ph.subprocess.run = orig

        self.assertEqual(len(calls), 2)
        paste, enter = calls
        self.assertIn("-l", paste)
        self.assertEqual(paste[-2:], ["--", "say enter now"])
        self.assertEqual(paste[:5], ["tmux", "-S", "/tmp/sock", "send-keys", "-t"])
        self.assertEqual(enter[-1], "Enter")
        self.assertNotIn("-l", enter)


class TestNonceCache(unittest.TestCase):
    def test_disabled_always_true(self):
        c = ph.NonceCache(0)
        self.assertTrue(c.check_and_consume(""))
        self.assertTrue(c.check_and_consume("anything"))

    def test_single_use_within_window(self):
        c = ph.NonceCache(10)
        self.assertFalse(c.check_and_consume(""))        # nonce required when enabled
        self.assertTrue(c.check_and_consume("n1"))       # first use ok
        self.assertFalse(c.check_and_consume("n1"))      # replay refused
        self.assertTrue(c.check_and_consume("n2"))

    def test_expiry(self):
        c = ph.NonceCache(0.05)
        self.assertTrue(c.check_and_consume("n"))
        time.sleep(0.08)
        self.assertTrue(c.check_and_consume("n"))         # expired -> reusable


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "registry.json")
        # Mock the fingerprint: pane "%alive" -> stable fp; "%dead" -> None.
        self._fps = {("/s", "%alive"): "1234 999", ("/s", "%two"): "5678 111"}
        self._orig_fp = ph.pane_fingerprint
        ph.pane_fingerprint = lambda sock, pane: self._fps.get((sock, pane))

    def tearDown(self):
        ph.pane_fingerprint = self._orig_fp

    def test_register_resolve_by_id_and_label(self):
        r = ph.Registry(self.path)
        e = r.register("repoA", "/s", "%alive", "claude://code/abc", repo="repoA")
        self.assertTrue(e["id"])
        self.assertEqual(r.resolve(e["id"])["label"], "repoA")
        self.assertEqual(r.resolve("repoA")["id"], e["id"])
        self.assertIsNone(r.resolve("nope"))

    def test_register_rejects_dead_pane(self):
        r = ph.Registry(self.path)
        with self.assertRaises(ValueError):
            r.register("x", "/s", "%dead", "claude://code/x")

    def test_dedupe_by_pane(self):
        r = ph.Registry(self.path)
        e1 = r.register("repoA", "/s", "%alive", "u1")
        e2 = r.register("repoA2", "/s", "%alive", "u2")  # same pane re-registers
        self.assertEqual(len(r.live()), 1)
        self.assertNotEqual(e1["id"], e2["id"])
        self.assertEqual(r.resolve(e2["id"])["viewer_url"], "u2")

    def test_live_sweeps_fingerprint_mismatch(self):
        """Simulate a tmux server restart: same %N, different pid/start -> stale."""
        r = ph.Registry(self.path)
        r.register("repoA", "/s", "%alive", "u1")
        self.assertEqual(len(r.live()), 1)
        self._fps[("/s", "%alive")] = "9999 222"  # %N now resolves to a different proc
        self.assertEqual(r.live(), [])             # swept
        self.assertIsNone(r.resolve("repoA"))

    def test_deregister(self):
        r = ph.Registry(self.path)
        r.register("a", "/s", "%alive", "u1")
        r.register("b", "/s", "%two", "u2")
        self.assertEqual(r.deregister(label="a"), 1)
        self.assertEqual(r.deregister(pane_id="%two", tmux_socket="/s"), 1)
        self.assertEqual(len(r.live()), 0)

    def test_deregister_by_pane_is_socket_scoped(self):
        """A bare pane_id (no socket) must NOT drop a same-pane session on
        another tmux server — pane ids like %0/%1 repeat across servers."""
        # Two servers, same pane id %alive — distinct fingerprints per socket.
        self._fps[("/s2", "%alive")] = "4321 777"
        r = ph.Registry(self.path)
        r.register("serverA", "/s", "%alive", "u1")
        r.register("serverB", "/s2", "%alive", "u2")
        # pane_id alone matches nothing (fail-closed): both survive.
        self.assertEqual(r.deregister(pane_id="%alive"), 0)
        self.assertEqual(len(r.live()), 2)
        # Socket-scoped dereg removes only the intended one.
        self.assertEqual(r.deregister(pane_id="%alive", tmux_socket="/s2"), 1)
        labels = {s["label"] for s in r.live()}
        self.assertEqual(labels, {"serverA"})

    def test_persists_atomically_across_instances(self):
        r1 = ph.Registry(self.path)
        r1.register("a", "/s", "%alive", "u1")
        r2 = ph.Registry(self.path)  # reload from disk
        self.assertEqual(len(r2.live()), 1)

    def test_registry_file_is_versioned(self):
        r = ph.Registry(self.path)
        r.register("a", "/s", "%alive", "u1")
        with open(self.path, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        self.assertEqual(disk["version"], ph.SCHEMA_VERSION)
        self.assertEqual(disk["sessions"][0]["format_version"], ph.REG_FORMAT_VERSION)

    def test_newer_schema_not_clobbered_on_load(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 999, "sessions": [{"id": "x"}]}, fh)
        r = ph.Registry(self.path)            # refuses to load a newer schema
        self.assertEqual(r.live(), [])
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], 999)  # file left intact (not downgraded)


class TestStrictGuard(unittest.TestCase):
    def _patch_capture(self, text, rc=0):
        class R:
            returncode = rc
            stdout = text
        ph._tmux = lambda sock, *a: R()

    def setUp(self):
        self._orig = ph._tmux

    def tearDown(self):
        ph._tmux = self._orig

    def test_refuses_on_yes_no(self):
        self._patch_capture("Do you want to proceed? (y/n)")
        self.assertFalse(ph.pane_looks_idle("/s", "%a"))

    def test_refuses_on_menu(self):
        self._patch_capture("1. option one\n2. option two\n❯ select")
        self.assertFalse(ph.pane_looks_idle("/s", "%a"))

    def test_allows_idle_prompt(self):
        self._patch_capture("│ > \n╰─ esc to interrupt · ? for shortcuts")
        self.assertTrue(ph.pane_looks_idle("/s", "%a"))

    def test_refuses_on_capture_error(self):
        self._patch_capture("", rc=1)
        self.assertFalse(ph.pane_looks_idle("/s", "%a"))


class TestHTTP(unittest.TestCase):
    """End-to-end over a real loopback server, with the tmux layer mocked."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.injected = []
        self._orig = (ph.pane_fingerprint, ph.pane_looks_idle, ph.inject)
        ph.pane_fingerprint = lambda sock, pane: "fp-" + pane
        ph.pane_looks_idle = lambda sock, pane: getattr(self, "idle", True)
        ph.inject = lambda sock, pane, text, delay: self.injected.append((pane, text))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), ph.Handler)
        self.srv.registry = ph.Registry(os.path.join(self.dir, "r.json"))
        self.srv.token = "TOK"
        self.srv.register_secret = "REG"
        self.srv.default_session = ""
        self.srv.submit_delay = 0
        self.srv.strict = True
        self.srv.nonces = ph.NonceCache(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        ph.pane_fingerprint, ph.pane_looks_idle, ph.inject = self._orig

    def _post(self, path, obj):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            c.request("POST", path, json.dumps(obj), {"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read().decode()
        finally:
            c.close()

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.getheader("Location"), r.read().decode()
        finally:
            c.close()

    def test_register_requires_secret(self):
        st, _ = self._post("/v1/register", {"label": "a", "tmux_socket": "/s",
                                            "pane_id": "%1", "viewer_url": "claude://x"})
        self.assertEqual(st, 403)  # missing register_secret

    def test_full_flow(self):
        st, body = self._post("/v1/register", {"register_secret": "REG", "label": "repoA",
                              "tmux_socket": "/s", "pane_id": "%1", "viewer_url": "claude://code/abc"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["version"], ph.REG_FORMAT_VERSION)
        sid = json.loads(body)["id"]

        st, _, body = self._get("/v1/sessions?token=TOK")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)[0]["label"], "repoA")

        # wrong token rejected
        st, _, _ = self._get("/v1/sessions?token=WRONG")
        self.assertEqual(st, 403)

        # /say injects + 302s to the viewer_url
        st, loc, _ = self._get("/v1/say?token=TOK&session=%s&q=hello%%20world" % sid)
        self.assertEqual(st, 302)
        self.assertEqual(loc, "claude://code/abc")
        self.assertEqual(self.injected[-1], ("%1", "hello world"))

    def test_say_wrong_token(self):
        st, _, _ = self._get("/v1/say?token=NOPE&session=x&q=hi")
        self.assertEqual(st, 403)

    def test_say_missing_tmux_is_502_not_500(self):
        self._post("/v1/register", {"register_secret": "REG", "label": "r", "tmux_socket": "/s",
                                    "pane_id": "%1", "viewer_url": "v"})
        sid = json.loads(self._get("/v1/sessions?token=TOK")[2])[0]["id"]
        def _boom(sock, pane, text, delay):
            raise FileNotFoundError(2, "No such file or directory: 'tmux'")
        ph.inject = _boom  # simulate launchd PATH without /opt/homebrew/bin
        st, _, _ = self._get("/v1/say?token=TOK&session=%s&q=hi" % sid)
        self.assertEqual(st, 502)  # clean gateway error, not an uncaught 500

    def test_say_strict_refuses_when_not_idle(self):
        self._post("/v1/register", {"register_secret": "REG", "label": "r", "tmux_socket": "/s",
                                    "pane_id": "%1", "viewer_url": "v"})
        sid = json.loads(self._get("/v1/sessions?token=TOK")[2])[0]["id"]
        self.idle = False
        st, _, _ = self._get("/v1/say?token=TOK&session=%s&q=do+it" % sid)
        self.assertEqual(st, 409)
        self.assertEqual(self.injected, [])  # nothing injected

    def test_deregister(self):
        self._post("/v1/register", {"register_secret": "REG", "label": "r", "tmux_socket": "/s",
                                    "pane_id": "%1", "viewer_url": "v"})
        st, body = self._post("/v1/deregister", {"label": "r"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["removed"], 1)
        self.assertEqual(json.loads(self._get("/v1/sessions?token=TOK")[2]), [])

    def test_deregister_pane_requires_socket(self):
        self._post("/v1/register", {"register_secret": "REG", "label": "r", "tmux_socket": "/s",
                                    "pane_id": "%1", "viewer_url": "v"})
        # pane_id without tmux_socket is rejected (pane ids repeat across servers)
        st, _ = self._post("/v1/deregister", {"pane_id": "%1"})
        self.assertEqual(st, 400)
        self.assertEqual(len(json.loads(self._get("/v1/sessions?token=TOK")[2])), 1)
        # pane_id + tmux_socket succeeds
        st, body = self._post("/v1/deregister", {"pane_id": "%1", "tmux_socket": "/s"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["removed"], 1)

    # ---- versioning
    def test_unversioned_path_rejected(self):
        st, _, _ = self._get("/sessions?token=TOK")          # no /vN/ prefix
        self.assertEqual(st, 404)

    def test_unknown_version_rejected(self):
        st, _, _ = self._get("/v2/sessions?token=TOK")       # future version
        self.assertEqual(st, 404)

    def test_response_carries_version_header(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            c.request("GET", "/v1/sessions?token=TOK")
            r = c.getresponse()
            self.assertEqual(r.getheader("X-Phone-Home-Version"), ph.API_VERSION)
            r.read()
        finally:
            c.close()

    def test_future_registration_format_rejected(self):
        st, _ = self._post("/v1/register", {"register_secret": "REG", "v": 99, "label": "r",
                            "tmux_socket": "/s", "pane_id": "%1", "viewer_url": "v"})
        self.assertEqual(st, 400)  # newer payload format than the server understands

    def test_unknown_fields_ignored(self):
        st, _ = self._post("/v1/register", {"register_secret": "REG", "label": "r", "tmux_socket": "/s",
                            "pane_id": "%1", "viewer_url": "v", "future_field": "x"})
        self.assertEqual(st, 200)  # additive/forward-compatible


if __name__ == "__main__":
    unittest.main()
