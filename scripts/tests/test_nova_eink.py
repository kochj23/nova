#!/usr/bin/env python3
"""Tests for nova_eink.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). collect() (its PG reads) is mocked; rendering is pure Pillow and
fully offline. No server is bound. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass + PEP-563 annotations need the module registered
    spec.loader.exec_module(mod)
    return mod


ek = _load("nova_eink_t", SCRIPTS / "nova_eink.py")
SRC = (SCRIPTS / "nova_eink.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_trmnl_api_key_is_derived_from_mac_not_a_secret(self):
        # the only "api_key" the shim returns is a deterministic label off the device MAC
        self.assertIn('"nova-" + mac.replace', SRC)
        self.assertNotRegex(SRC, r'api_key"?\s*[:=]\s*["\'][A-Za-z0-9]{20,}')


class TestPerformance(unittest.TestCase):
    def test_render_under_bound(self):
        snap = ek.sample()
        t0 = time.perf_counter()
        ek.render(snap, mode="color")
        ek.render(snap, mode="mono")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_collect_pg_down_returns_stale_snapshot(self):
        # RETRY GAP: collect()/psycopg2 — single attempt; on outage it returns a (stale) Snapshot,
        # never raising, so the panel still renders something.
        with mock.patch("psycopg2.connect", side_effect=RuntimeError("pg down")):
            snap = ek.collect()
        self.assertIsInstance(snap, ek.Snapshot)
        # still renders without throwing
        self.assertTrue(ek.png_color(snap))


class TestUnit(unittest.TestCase):
    def test_quantize_maps_to_six_colors(self):
        from PIL import Image
        img = Image.new("RGB", (16, 16), (123, 200, 50))
        q = ek.quantize_spectra6(img)
        used = {c for _, c in q.getcolors(maxcolors=4096)}
        self.assertTrue(used.issubset(set(ek.IDEAL)))

    def test_to_mono_is_1bit(self):
        from PIL import Image
        img = Image.new("RGB", (8, 8), (10, 10, 10))
        self.assertEqual(ek.to_mono_1bit(img).mode, "1")

    def test_theme_mono_is_all_black_on_white(self):
        t = ek.Theme.mono()
        self.assertEqual(t.bg, (255, 255, 255))
        self.assertEqual({t.fg, t.accent, t.warn, t.ok, t.info, t.hi}, {(0, 0, 0)})


class TestIntegration(unittest.TestCase):
    def test_render_dimensions_match_panel(self):
        img = ek.render(ek.sample(), mode="color")
        self.assertEqual(img.size, (ek.WIDTH, ek.HEIGHT))

    def test_outputs_are_valid_images(self):
        from PIL import Image
        snap = ek.sample()
        with Image.open(io.BytesIO(ek.png_color(snap))) as im:
            self.assertEqual(im.size, (ek.WIDTH, ek.HEIGHT))
        with Image.open(io.BytesIO(ek.bmp_mono(snap))) as im:
            self.assertEqual(im.mode, "1")

    def test_cache_reuses_within_ttl(self):
        calls = {"n": 0}
        def producer(snap):
            calls["n"] += 1
            return b"x"
        ek._cache.clear()
        with mock.patch.object(ek, "collect", return_value=ek.sample()):
            ek._cached("k", producer)
            ek._cached("k", producer)
        self.assertEqual(calls["n"], 1)   # second call served from cache


class TestFunctional(unittest.TestCase):
    def test_http_endpoints_serve_images(self):
        from fastapi.testclient import TestClient
        with mock.patch.object(ek, "collect", return_value=ek.sample()):
            ek._cache.clear()
            client = TestClient(ek.build_app())
            r = client.get("/eink/nova.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/png")
            rb = client.get("/eink/nova.bmp")
            self.assertEqual(rb.headers["content-type"], "image/bmp")
            h = client.get("/eink/health").json()
            self.assertTrue(h["ok"] and h["w"] == ek.WIDTH)

    def test_trmnl_display_points_at_cache_busting_bmp(self):
        from fastapi.testclient import TestClient
        with mock.patch.object(ek, "collect", return_value=ek.sample()):
            client = TestClient(ek.build_app())
            d = client.get("/api/display").json()
            self.assertIn("/eink/nova.bmp?v=", d["image_url"])
            self.assertEqual(d["refresh_rate"], ek.REFRESH_RATE)
            self.assertEqual(client.get("/api/log").status_code, 204)


class TestFrame(unittest.TestCase):
    def test_render_smoke_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_eink.py"), "--render"], capture_output=True,
                           text=True, timeout=40, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote", r.stdout)

    def test_import_never_starts_server(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotIn("uvicorn.run(build_app()", SRC.split('if __name__')[0])


if __name__ == "__main__":
    unittest.main()
