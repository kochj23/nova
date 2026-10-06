#!/usr/bin/env python3
"""Tests for memory_cleanup.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "memory_cleanup.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = _load("mc", SCRIPT)
# Offline guard at module load: the script's only outbound paths are requests.get/.delete and the
# pacing sleeps. Replace the module's own bindings (never the real `requests`/`time` modules).
mc.requests = types.SimpleNamespace(get=MagicMock(side_effect=RuntimeError("offline")),
                                    delete=MagicMock(side_effect=RuntimeError("offline")),
                                    exceptions=requests.exceptions)
mc.time = types.SimpleNamespace(sleep=lambda s: None)


def _resp(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def _mem(mid, text, source="email_archive", sender="", subject="", created=""):
    return {"id": mid, "text": text, "source": source, "created_at": created,
            "metadata": {"sender": sender, "subject": subject}}


class _Server:
    """A fake memory server answering GET by endpoint and recording every call."""
    def __init__(self, random_=None, search=None, recall=None, stats=None):
        self.random_, self.search, self.recall = random_ or [], search or [], recall or []
        self.stats = stats if stats is not None else {"count": 100, "db_size": "1 GB"}
        self.calls = []; self.deleted = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/random"):
            return _resp({"memories": self.random_})
        if url.endswith("/search"):
            return _resp({"results": self.search})
        if url.endswith("/recall"):
            return _resp({"memories": self.recall})
        if url.endswith("/stats"):
            return _resp(self.stats)
        raise AssertionError(url)

    def delete(self, url, params=None, timeout=None):
        self.deleted.append(params["id"])
        return _resp({"ok": True})

    def install(self):
        mc.requests = types.SimpleNamespace(get=self.get, delete=self.delete, exceptions=requests.exceptions)
        return self


def _quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()) as out:
        r = fn(*a, **k)
    return r, out.getvalue()


def _run_main(argv, server):
    server.install()
    with patch.object(sys, "argv", ["memory_cleanup.py"] + argv), redirect_stdout(io.StringIO()) as out:
        mc.main()
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_protected_sources_are_never_selected(self):
        s = _Server(random_=[_mem(1, "x", source="work_knowledge"), _mem(2, "y", source="local_knowledge"),
                             _mem(3, "z", source="email_archive")]).install()
        found, _ = _quiet(mc.find_short_memories)
        self.assertEqual([f[0] for f in found], [3])
        self.assertEqual(mc.PROTECTED_SOURCES, {"work_knowledge", "local_knowledge"})

    def test_ids_and_queries_go_as_params_never_in_the_url(self):
        s = _Server().install()
        mc.api_delete("abc&id=999")
        self.assertEqual(s.deleted, ["abc&id=999"])          # passed verbatim as a param, never spliced into the path
        mc.api_get("/search", {"q": "x' OR 1=1"})
        self.assertEqual(s.calls[-1], (f"{mc.BASE_URL}/search", {"q": "x' OR 1=1"}))
        self.assertIn('requests.get(f"{BASE_URL}{path}", params=params', SRC)      # ids/queries ride in params only
        self.assertNotIn("quote(memory_id)", SRC)

    def test_execute_is_opt_in_and_exclusive(self):
        self.assertIn("required=True", SRC)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            with patch.object(sys, "argv", ["memory_cleanup.py"]):
                mc.main()


class TestPerformance(unittest.TestCase):
    def test_dedup_grouping_on_10k_self_sent_emails(self):
        mems = [_mem(i, "t", sender="nova@digitalnoise.net", subject=f"Nova Morning Mail Summary {i % 50}",
                     created=f"2026-01-{(i % 28) + 1:02d}") for i in range(10_000)]
        _Server(search=mems).install()
        t0 = time.perf_counter()
        to_delete, _ = _quiet(mc.find_duplicate_morning_summaries)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(to_delete), 10_000 - 50)       # one kept per subject

    def test_sampling_rounds_are_bounded(self):
        s = _Server(random_=[]).install()
        _quiet(mc.find_short_memories)
        self.assertEqual(len(s.calls), mc.SHORT_MEMORY_ROUNDS)


class TestRetry(unittest.TestCase):
    def test_short_scan_survives_transient_failures_without_retrying(self):
        # RETRY GAP: api_get — a failed /random batch is logged and skipped, never re-requested
        calls = []

        def flaky(url, params=None, timeout=None):
            calls.append(url)
            if len(calls) <= 2:
                raise requests.exceptions.ConnectionError("down")
            return _resp({"memories": [_mem(7, "short", source="email_archive")]})
        mc.requests = types.SimpleNamespace(get=flaky, delete=MagicMock(), exceptions=requests.exceptions)
        found, out = _quiet(mc.find_short_memories)
        self.assertEqual(len(calls), mc.SHORT_MEMORY_ROUNDS)          # no extra attempts for the 2 failures
        self.assertEqual(found, [(7, "short", "email_archive")])
        self.assertIn("Error on batch 1", out)

    def test_delete_errors_are_counted_not_raised(self):
        # RETRY GAP: api_delete — one attempt per id; 404 is treated as already-gone, other errors counted
        err404 = requests.exceptions.HTTPError(response=MagicMock(status_code=404))
        err500 = requests.exceptions.HTTPError(response=MagicMock(status_code=500))
        dele = MagicMock(side_effect=[err404, err500, RuntimeError("boom"), _resp({})])
        mc.requests = types.SimpleNamespace(get=MagicMock(), delete=dele, exceptions=requests.exceptions)
        n, out = _quiet(mc.delete_memories, [(i, "t", "s") for i in range(4)], "cat", dry_run=False)
        self.assertEqual(n, 1)
        self.assertEqual(dele.call_count, 4)
        self.assertIn("Deleted: 1, Errors: 2", out)

    def test_unreachable_server_exits_one_before_any_scan(self):
        # RETRY GAP: main()/stats — a single failed /stats probe aborts with exit 1
        s = _Server()
        s.stats = None
        get = MagicMock(side_effect=RuntimeError("refused"))
        mc.requests = types.SimpleNamespace(get=get, delete=MagicMock(), exceptions=requests.exceptions)
        with patch.object(sys, "argv", ["memory_cleanup.py", "--dry-run"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                mc.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(get.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_short_memory_threshold_and_dedup(self):
        s = _Server(random_=[_mem(1, "x" * 24), _mem(2, "y" * 25), _mem(1, "x" * 24)]).install()
        found, _ = _quiet(mc.find_short_memories)
        self.assertEqual(found, [(1, "x" * 24, "email_archive")])

    def test_batch_archive_matching_is_prefix_only(self):
        s = _Server(search=[_mem(1, "Email subject archive (batch 3/9, 50 entries)"),
                            _mem(2, "not Email subject archive (batch 1/9)")],
                    recall=[_mem(3, "Email subject archive (batch 9/9, 12 entries)")],
                    random_=[_mem(4, "Email subject archive (batch 1/9, 50 entries)")]).install()
        found, out = _quiet(mc.find_batch_subject_archives)
        self.assertEqual(sorted(f[0] for f in found), [1, 3, 4])
        self.assertIn("Batch numbering: X/9", out)
        self.assertIn("Batches not yet found: 6", out)

    def test_batch_search_falls_back_to_memories_key(self):
        srv = _Server()
        srv.get = lambda url, params=None, timeout=None: _resp({"memories": [_mem(5, "Email subject archive (batch 1/1, 1 entries)")]})
        srv.install()
        found, _ = _quiet(mc.find_batch_subject_archives)
        self.assertEqual(found[0][0], 5)

    def test_duplicate_summaries_keep_earliest_and_ignore_other_senders(self):
        s = _Server(search=[_mem(1, "t", sender="nova@digitalnoise.net", subject="S", created="2026-02-02"),
                            _mem(2, "t", sender="nova@digitalnoise.net", subject="S", created="2026-01-01"),
                            _mem(3, "t", sender="kochj@example.com", subject="S", created="2026-01-01"),
                            _mem(4, "t", sender="nova@digitalnoise.net", subject="Solo", created="2026-01-01")]).install()
        to_delete, _ = _quiet(mc.find_duplicate_morning_summaries)
        self.assertEqual(to_delete, [(1, "S", "email_archive")])

    def test_delete_memories_edges(self):
        s = _Server().install()
        n, out = _quiet(mc.delete_memories, [], "cat", dry_run=True)
        self.assertEqual((n, s.deleted), (0, []))
        self.assertIn("Nothing to delete", out)
        n, out = _quiet(mc.delete_memories, [(1, "t", "s")], "cat", dry_run=True)
        self.assertEqual((n, s.deleted), (0, []))
        self.assertIn("[DRY RUN] Would delete 1", out)

    def test_api_get_raises_on_http_error(self):
        bad = _resp({}, 500); bad.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
        mc.requests = types.SimpleNamespace(get=lambda *a, **k: bad, delete=MagicMock(), exceptions=requests.exceptions)
        with self.assertRaises(requests.exceptions.HTTPError):
            mc.api_get("/stats")


class TestIntegration(unittest.TestCase):
    def test_finders_feed_delete_memories_with_the_same_tuple_shape(self):
        s = _Server(random_=[_mem(1, "hi")]).install()
        found, _ = _quiet(mc.find_short_memories)
        n, _ = _quiet(mc.delete_memories, found, "short", dry_run=False)
        self.assertEqual((n, s.deleted), (1, [1]))

    def test_every_endpoint_is_scoped_to_email_archive_except_short_scan(self):
        s = _Server().install()
        _quiet(mc.find_batch_subject_archives); _quiet(mc.find_duplicate_morning_summaries)
        self.assertTrue(all(p.get("source") == "email_archive" for _, p in s.calls))
        self.assertTrue(all(p.get("n", 0) <= 100 for _, p in s.calls))

    def test_delete_targets_the_forget_endpoint_with_a_bounded_timeout(self):
        s = _Server().install()
        mc.api_delete(42)
        self.assertEqual(s.deleted, [42])
        self.assertIn('requests.delete(f"{BASE_URL}/forget", params={"id": memory_id}, timeout=10)', SRC)


class TestFunctional(unittest.TestCase):
    def test_dry_run_reports_and_deletes_nothing(self):
        s = _Server(random_=[_mem(1, "hi")], search=[_mem(2, "Email subject archive (batch 1/2, 50 entries)")])
        out = _run_main(["--dry-run"], s)
        self.assertEqual(s.deleted, [])
        self.assertIn("Mode: DRY RUN", out)
        self.assertIn("Total to delete:                       2", out)
        self.assertIn("Run with --execute", out)

    def test_execute_deletes_each_category_and_reports_reduction(self):
        s = _Server(random_=[_mem(1, "hi")], search=[_mem(2, "Email subject archive (batch 1/2, 50 entries)")])
        out = _run_main(["--execute"], s)
        self.assertEqual(sorted(s.deleted), [1, 2])
        self.assertIn("Successfully deleted 2 memories", out)
        self.assertIn("Memories after:", out)

    def test_nothing_to_clean_short_circuits(self):
        s = _Server()
        out = _run_main(["--execute"], s)
        self.assertIn("Nothing to clean up!", out)
        self.assertEqual(s.deleted, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--execute", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import memory_cleanup"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
