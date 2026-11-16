"""Fixture-driven route regression harness (#53, #135, #136).

Boots the real app in mock mode and exercises routes over plain HTTP with
urllib — no TestClient/httpx2, no Chrome, no Pulse/X, no Apple account.
Every 2xx JSON response is a server-side model-conformance pass; this
harness adds route coverage, envelope checks, and a synthetic corpus walk.

Synthetic/anonymized only (no tokens, no real library rows). Runs under
`python -m unittest discover -s tests` and in CI.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RouteHarness(unittest.TestCase):
    """Mock server + urllib corpus (#135)."""

    @classmethod
    def setUpClass(cls):
        # configure before importing the app/singletons
        os.environ["TONEARM_MOCK"] = "1"
        os.environ["TONEARM_AUTO_CHROME"] = "0"
        os.environ["TONEARM_STORE"] = "0"
        os.environ["TONEARM_WEBHOOKS"] = "0"
        from tonearm.config import config
        config.mock_enabled = True
        config.auto_chrome = False
        config.store_enabled = False
        config.webhooks_enabled = False
        config.api_key = ""

        import tonearm.keys as keys_mod
        import tonearm.musickit as mk
        from tonearm.mock import install as mock_install
        mock_install(mk)
        cls._registry = keys_mod.registry
        cls._tmp = tempfile.mkdtemp()
        keys_mod.registry = keys_mod.KeysRegistry(os.path.join(cls._tmp, "keys.json"))

        from tonearm.main import app
        cls.app = app

        try:
            import uvicorn
        except Exception as e:
            raise unittest.SkipTest(f"uvicorn unavailable: {e}")

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        ucfg = uvicorn.Config(app, host="127.0.0.1", port=cls.port,
                              log_level="warning", lifespan="on")
        cls.server = uvicorn.Server(ucfg)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(120):
            try:
                with urllib.request.urlopen(cls.base + "/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("mock harness server did not start")

    @classmethod
    def tearDownClass(cls):
        srv = getattr(cls, "server", None)
        if srv is not None:
            srv.should_exit = True
        t = getattr(cls, "thread", None)
        if t is not None:
            t.join(timeout=5)
        try:
            import tonearm.keys as keys_mod
            keys_mod.registry = cls._registry
        except Exception:
            pass

    # --- helpers ---------------------------------------------------------

    def _req(self, method, path, body=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
                payload = json.loads(raw) if "json" in ctype else raw
                return r.status, payload, r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"_raw": raw[:200].decode("utf-8", "ignore")}
            return e.code, payload, e.headers

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body=None):
        return self._req("POST", path, body=body or {})

    # --- corpus: read routes must answer --------------------------------

    def test_health_and_metrics(self):
        s, j, _ = self.get("/health")
        self.assertEqual(s, 200)
        self.assertIn("status", j)
        self.assertIn("surface", j)
        s, body, h = self.get("/metrics")
        self.assertEqual(s, 200)
        self.assertIn("text/plain", h.get("Content-Type", ""))

    def test_library_reads(self):
        cases = [
            "/api/library/songs?limit=5",
            "/api/library/albums?limit=5",
            "/api/library/artists?limit=5",
            "/api/library/playlists?limit=5",
            "/api/library/search?term=mock",
            "/api/library/recently-added?limit=3",
        ]
        for path in cases:
            with self.subTest(path=path):
                s, j, _ = self.get(path)
                self.assertEqual(s, 200, f"{path} -> {s} {j}")
                self.assertIsInstance(j, (dict, list))

    def test_catalog_reads(self):
        cases = [
            "/api/catalog/search?term=mock&types=songs&limit=3",
            "/api/catalog/search?term=mock&types=albums,artists&limit=3",
            "/api/catalog/charts?types=songs&limit=3",
        ]
        for path in cases:
            with self.subTest(path=path):
                s, j, _ = self.get(path)
                self.assertEqual(s, 200, f"{path} -> {s} {j}")

    def test_personal_and_playback_reads(self):
        for path in [
            "/api/personal/recommendations",
            "/api/personal/heavy-rotation",
            "/api/personal/recently-played",
            "/api/playback/now-playing",
            "/api/playback/queue",
            "/api/playback/settings",
            "/api/playback/volume",
            "/api/debug",
        ]:
            with self.subTest(path=path):
                s, j, _ = self.get(path)
                self.assertEqual(s, 200, f"{path} -> {s} {j}")

    def test_library_list_envelope_shape(self):
        s, j, _ = self.get("/api/library/songs?limit=5")
        self.assertEqual(s, 200)
        self.assertIn("items", j)
        self.assertIn("count", j)
        self.assertEqual(j["count"], len(j["items"]))
        self.assertLessEqual(j["count"], 5)
        if j["items"]:
            row = j["items"][0]
            self.assertIn("id", row)
            self.assertIn("attributes", row)

    def test_now_playing_contract(self):
        s, j, _ = self.get("/api/playback/now-playing")
        self.assertEqual(s, 200)
        for key in ("title", "artist", "album", "isPlaying", "elapsedMs", "durationMs"):
            self.assertIn(key, j)

    # --- corpus: gated/disabled surfaces are honest ----------------------

    def test_disabled_surfaces_return_structured_errors(self):
        # writes/store/webhooks default off in this harness
        for path in [
            "/api/history/recently-played",
            "/api/history/stats",
            "/api/library/diff?since=0",
        ]:
            with self.subTest(path=path):
                s, j, _ = self.get(path)
                self.assertIn(s, (501, 503), f"{path} -> {s} {j}")
                self.assertIn("error", j)
                self.assertIn("code", j["error"])
        s, j, _ = self.get("/api/webhooks")
        self.assertIn(s, (501, 503))

    def test_audio_honest_in_mock(self):
        # mock has no renderer/audio pipeline
        s, _, _ = self.get("/api/audio/stream.mp3")
        self.assertIn(s, (404, 501, 503))

    # --- corpus: demo page & openapi served ------------------------------

    def test_demo_and_openapi(self):
        s, body, h = self.get("/demo")
        self.assertEqual(s, 200)
        self.assertIn("text/html", h.get("Content-Type", ""))
        s, j, _ = self.get("/openapi.json")
        self.assertEqual(s, 200)
        self.assertIn("paths", j)
        self.assertIn("openapi", j)


if __name__ == "__main__":
    unittest.main()
