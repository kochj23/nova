"""
test_nova_writeas.py — All 7 test categories for nova_writeas.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_writeas.py"
_spec = importlib.util.spec_from_file_location("nova_writeas", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_password = _mod.get_password
get_token = _mod.get_token
api_request = _mod.api_request
publish_post = _mod.publish_post
list_posts = _mod.list_posts


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_password_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-writeas-password", src)
    def test_token_cache_permissions(self):
        src = _SCRIPT.read_text()
        self.assertIn("0o600", src, "Token cache file must be set to 0o600")
    def test_token_cache_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.TOKEN_CACHE))
    def test_api_uses_https(self):
        self.assertTrue(_mod.API_BASE.startswith("https://"))
    def test_no_plaintext_password_stored(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("pass = ", src.lower())


class TestPerformance(unittest.TestCase):
    def test_token_cache_ttl_7_days(self):
        self.assertEqual(_mod.TOKEN_TTL, 86400 * 7)
    def test_token_cache_hit_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token_123")
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch.object(_mod, "TOKEN_TTL", 3600):
                    start = time.perf_counter()
                    token = get_token()
                    elapsed = time.perf_counter() - start
        self.assertEqual(token, "fake_token_123")
        self.assertLess(elapsed, 0.01)
    def test_api_timeout_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=30", src)


class TestRetry(unittest.TestCase):
    def test_get_password_raises_on_missing_keychain(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            with self.assertRaises(RuntimeError):
                get_password()
    def test_api_request_raises_on_http_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token")
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch("urllib.request.urlopen",
                           side_effect=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)):
                    with self.assertRaises(urllib.error.HTTPError):
                        api_request("GET", "/collections/novakoch")
    def test_token_refresh_on_expired_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("old_token")
            import os
            old_time = time.time() - _mod.TOKEN_TTL - 100
            os.utime(str(cache), (old_time, old_time))
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"data": {"access_token": "new_token_xyz"}}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch.object(_mod, "get_password", return_value="testpass"):
                    with patch("urllib.request.urlopen", return_value=mock_resp):
                        token = get_token()
        self.assertEqual(token, "new_token_xyz")


class TestUnit(unittest.TestCase):
    def test_collection_constant(self):
        self.assertEqual(_mod.COLLECTION, "novakoch")
    def test_api_base_is_writeas(self):
        self.assertIn("write.as", _mod.API_BASE)
    def test_publish_post_sends_title_and_body(self):
        sent = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {"slug": "test-post", "id": "abc"}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, **kwargs):
            if req.data:
                sent.append(json.loads(req.data.decode()))
            return mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token")
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch("urllib.request.urlopen", side_effect=capture):
                    publish_post("Test Title", "Test body text here.")
        self.assertTrue(any(d.get("title") == "Test Title" for d in sent))

    def test_publish_post_with_tags(self):
        sent = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {"slug": "tagged-post"}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, **kwargs):
            if req.data:
                sent.append(json.loads(req.data.decode()))
            return mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token")
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch("urllib.request.urlopen", side_effect=capture):
                    publish_post("Tagged", "Body.", tags=["ai", "dream"])
        self.assertTrue(any("tags" in d for d in sent))


class TestIntegration(unittest.TestCase):
    def test_list_posts_returns_list(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {"posts": [
            {"title": "Post 1", "created": "2026-01-01"},
            {"title": "Post 2", "created": "2026-01-02"},
        ]}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token")
            with patch.object(_mod, "TOKEN_CACHE", cache):
                with patch("urllib.request.urlopen", return_value=mock_resp):
                    posts = list_posts(limit=10)
        self.assertIsInstance(posts, list)
        self.assertEqual(len(posts), 2)


class TestFunctional(unittest.TestCase):
    def test_main_no_args_prints_help(self):
        with patch("sys.argv", ["nova_writeas.py"]):
            with self.assertRaises(SystemExit):
                _mod.main()
    def test_main_post_requires_title_and_body(self):
        with patch("sys.argv", ["nova_writeas.py", "post"]):
            with self.assertRaises(SystemExit):
                _mod.main()
    def test_main_list_command(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {"posts": [
            {"title": "Test Post", "created": "2026-01-01T00:00:00"}
        ]}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / ".writeas_token"
            cache.write_text("fake_token")
            with patch("sys.argv", ["nova_writeas.py", "list"]):
                with patch.object(_mod, "TOKEN_CACHE", cache):
                    with patch("urllib.request.urlopen", return_value=mock_resp):
                        import io
                        from contextlib import redirect_stdout
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            _mod.main()
        self.assertIn("Test Post", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.API_BASE, str)
        self.assertIsInstance(_mod.COLLECTION, str)
        self.assertIsInstance(_mod.TOKEN_CACHE, Path)
        self.assertIsInstance(_mod.TOKEN_TTL, int)
    def test_functions_exist(self):
        for fn in ("get_password", "get_token", "api_request",
                   "publish_post", "list_posts", "test_connection", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
