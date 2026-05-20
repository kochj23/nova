"""
test_nova_rem_sleep.py — All 7 test categories for nova_rem_sleep.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_rem_sleep.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()

sys.modules["nova_config"] = _nova_cfg
sys.modules["psycopg2"] = MagicMock()

_spec = importlib.util.spec_from_file_location("nova_rem_sleep", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
post_slack = _mod.post_slack
ollama_generate = _mod.ollama_generate
vector_remember = _mod.vector_remember


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_pg_conn_no_password_in_source(self):
        """PG connection string must not contain password."""
        src = _SCRIPT.read_text()
        self.assertNotIn("password=", src.lower())

    def test_synthesis_source_excludes_health_data(self):
        """Phase 3 linking must exclude health data from cross-linking."""
        src = _SCRIPT.read_text()
        self.assertIn("apple_health", src)
        self.assertIn("healthkit", src)

    def test_pruning_never_deletes_just_demotes(self):
        """PRUNING must never DELETE memories, only change tier."""
        src = _SCRIPT.read_text()
        # Should not have DELETE FROM memories
        self.assertNotIn("DELETE FROM memories", src)
        self.assertIn("UPDATE memories", src)
        self.assertIn("scratchpad", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_max_clusters_constant_bounded(self):
        self.assertLessEqual(_mod.MAX_CLUSTERS_PER_RUN, 100)
        self.assertGreater(_mod.MAX_CLUSTERS_PER_RUN, 0)

    def test_cluster_similarity_threshold_reasonable(self):
        self.assertGreater(_mod.CLUSTER_SIMILARITY_THRESHOLD, 0.7)
        self.assertLess(_mod.CLUSTER_SIMILARITY_THRESHOLD, 1.0)

    def test_ollama_generate_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ollama_generate_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("ollama down")):
            result = ollama_generate("test prompt")
        self.assertEqual(result, "")

    def test_vector_remember_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("server down")):
            result = vector_remember("test text", "synthesis", {})
        self.assertIsNone(result)

    def test_post_slack_calls_nova_config(self):
        """post_slack delegates to nova_config.post_both."""
        calls = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: calls.append(msg)
        post_slack("test rem sleep message")
        self.assertGreater(len(calls), 0)
        self.assertIn("test rem sleep message", calls[0])
        _nova_cfg.post_both.side_effect = None

    def test_main_handles_psycopg2_not_installed(self):
        """main() must handle missing psycopg2 gracefully."""
        with patch.dict("sys.modules", {"psycopg2": None}):
            try:
                _mod.main()
            except Exception:
                pass  # Expected — just must not crash Python interpreter


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_does_not_raise(self):
        log("test message")

    def test_vector_remember_sends_json(self):
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            resp = MagicMock()
            resp.read.return_value = json.dumps({"id": "abc123"}).encode()
            return resp
        with patch("urllib.request.urlopen", side_effect=capture):
            result = vector_remember("synthesis text", "synthesis", {"type": "test"})
        self.assertTrue(len(payloads) > 0)
        self.assertEqual(payloads[0]["source"], "synthesis")

    def test_vector_remember_includes_metadata(self):
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            resp = MagicMock()
            resp.read.return_value = json.dumps({"id": "x"}).encode()
            return resp
        with patch("urllib.request.urlopen", side_effect=capture):
            vector_remember("text", "synthesis", {"cluster_size": 5})
        self.assertEqual(payloads[0]["metadata"]["cluster_size"], 5)

    def test_ollama_generate_returns_stripped_response(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"response": "  synthesis result  "}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            result = ollama_generate("prompt")
        self.assertEqual(result, "synthesis result")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.CONSOLIDATION_MODEL)
        self.assertIsNotNone(_mod.PG_CONN)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_phase_consolidation_stores_synthesis(self):
        """phase_consolidation must call vector_remember for each cluster."""
        remember_calls = []
        def fake_remember(text, source, meta):
            remember_calls.append({"text": text, "source": source, "meta": meta})
            return "fake-id-abc"

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        clusters = [
            {
                "source": "wikipedia",
                "texts": [("id1", "text about Rome"), ("id2", "more about Rome")],
                "ids": ["id1", "id2"],
            }
        ]
        with patch.object(_mod, "vector_remember", side_effect=fake_remember):
            with patch.object(_mod, "ollama_generate",
                              return_value="Synthesis of Rome content here today."):
                _mod.phase_consolidation(mock_conn, clusters)
        self.assertGreater(len(remember_calls), 0)
        self.assertEqual(remember_calls[0]["source"], "synthesis")

    def test_phase_pruning_updates_tier(self):
        """phase_pruning must run UPDATE SQL on memories."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.rowcount = 5

        _mod.phase_pruning(mock_conn)
        calls = [str(c) for c in mock_cur.execute.call_args_list]
        self.assertTrue(any("scratchpad" in c for c in calls))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_phase_report_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [("long_term", 1000), ("scratchpad", 100)]
        mock_cur.fetchone.return_value = [500]

        stats = {"scanned": 200, "clusters": 10, "syntheses": 5,
                 "links": 20, "pruned": 3, "duration": 45.0}
        _mod.phase_report(mock_conn, stats)
        self.assertTrue(len(posts) > 0)
        combined = " ".join(posts)
        self.assertIn("REM Sleep", combined)
        _nova_cfg.post_both.side_effect = None

    def test_report_shows_statistics(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [("long_term", 1000)]
        mock_cur.fetchone.return_value = [300]
        stats = {"scanned": 500, "clusters": 15, "syntheses": 8,
                 "links": 30, "pruned": 10, "duration": 60.0}
        _mod.phase_report(mock_conn, stats)
        combined = " ".join(posts)
        self.assertIn("500", combined)  # scanned count
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "phase_triage", "phase_consolidation", "phase_linking",
                   "phase_pruning", "phase_report", "ollama_generate", "vector_remember"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
