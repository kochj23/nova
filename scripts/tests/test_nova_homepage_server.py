#!/usr/bin/env python3
"""Tests for nova_homepage_server.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homepage_server.py"
SRC = SCRIPT.read_text()

_TMP = tempfile.TemporaryDirectory()
_HOME = Path(_TMP.name)
_SITE = _HOME / ".openclaw" / "digitalnoise-homepage"
_SITE.mkdir(parents=True)
(_SITE / "index.html").write_text("<html><body>digitalnoise</body></html>")
(_SITE / "style.css").write_text("body{color:#111}")
(_SITE / "secret.txt").write_text("not served outside the site dir")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=_HOME):      # serve from a tempdir, never the real site
        spec.loader.exec_module(mod)
    return mod


hs = _load("hs_mod", SCRIPT)
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(hs.app)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_path_traversal_is_rejected(self):
        outside = _HOME / "outside.txt"
        outside.write_text("private")
        for p in ("/../outside.txt", "/%2e%2e/outside.txt", "/..%2f..%2foutside.txt"):
            r = client.get(p)
            self.assertNotEqual(r.status_code, 200, p)
            self.assertNotIn("private", r.text)

    def test_served_from_main_ssd_not_volumes_data(self):
        code = SRC.split("SITE_DIR = ")[1].split("\n")[0].split("#")[0]   # the expression, not its comment
        self.assertNotIn("/Volumes/Data", code)
        self.assertIn('Path.home() / ".openclaw" / "digitalnoise-homepage"', SRC)


class TestPerformance(unittest.TestCase):
    def test_1k_index_requests_under_bound(self):
        t0 = time.perf_counter()
        for _ in range(1_000):
            self.assertEqual(client.get("/").status_code, 200)
        self.assertLess(time.perf_counter() - t0, 10.0)


class TestRetry(unittest.TestCase):
    def test_missing_file_fails_open_with_404_not_crash(self):
        # RETRY GAP: none — a static server has no outbound calls; the failure mode is a missing file,
        # which yields 404 and keeps serving (the original 2026-08-06 outage was a 404 storm, not a crash).
        self.assertEqual(client.get("/nope.html").status_code, 404)
        self.assertEqual(client.get("/").status_code, 200)


class TestUnit(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(hs.PORT, 37491)
        self.assertEqual(hs.SITE_DIR, _SITE)
        self.assertEqual(hs.app.title, "digitalnoise.net")

    def test_index_route_returns_file_response(self):
        import asyncio
        resp = asyncio.run(hs.index())
        self.assertEqual(Path(resp.path), _SITE / "index.html")


class TestIntegration(unittest.TestCase):
    def test_static_mount_and_root_route_compose(self):
        self.assertEqual(client.get("/").text, "<html><body>digitalnoise</body></html>")
        self.assertEqual(client.get("/style.css").text, "body{color:#111}")
        self.assertEqual(client.get("/index.html").status_code, 200)
        self.assertEqual(client.get("/secret.txt").status_code, 200)   # inside the site dir: served by design

    def test_mount_is_html_mode_static_files(self):
        routes = {getattr(r, "name", None) for r in hs.app.routes}
        self.assertIn("static", routes)
        self.assertIn("html=True", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_get_root_and_head(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))
        self.assertEqual(client.head("/").status_code, 200)

    def test_error_path_post_not_allowed(self):
        self.assertEqual(client.post("/", json={}).status_code, 405)


class TestFrame(unittest.TestCase):
    def test_import_never_starts_uvicorn(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("uvicorn.run(app", SRC.split('if __name__ == "__main__":')[1])
        snippet = ("import uvicorn; uvicorn.run = lambda *a, **k: (_ for _ in ()).throw(SystemExit('started!'))\n"
                   "import nova_homepage_server as m; assert m.PORT == 37491")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
