#!/usr/bin/env python3
"""Tests for nova_card_rack.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_card_rack as C  # noqa: E402

SRC = (SCRIPTS / "nova_card_rack.py").read_text()
MAC = ":".join(["3c"] * 6)
TOKEN = "xoxb-" + "998877665544-QwErTyUiOpAsDfGh"    # built at runtime: never a literal token in source
_PATCHERS = []


def setUpModule():
    import nova_config
    import nova_notify
    for target in (mock.patch.object(nova_notify, "notify", return_value=True),
                   mock.patch.object(nova_config, "post_both", return_value=True, create=True)):
        _PATCHERS.append(target)
        target.start()


def tearDownModule():
    while _PATCHERS:
        _PATCHERS.pop().stop()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def article(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text)
    return p


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_parameterized_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\([^,]+,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotIn("kochj23" + "@", SRC)

    def test_matched_text_never_leaves_the_scanner(self):
        found = C.scan_text(f"lan {MAC}\nkey {TOKEN}\n")
        self.assertEqual({k for _, k, _ in found}, {"mac", "credential"})
        blob = repr(found)
        self.assertNotIn(MAC, blob)
        self.assertNotIn(TOKEN, blob)
        _, desc, ctx = C.queue_text([{"kind": k, "path": "p.md", "line": ln, "fp": fp} for ln, k, fp in found])
        self.assertNotIn(TOKEN, desc + ctx)
        self.assertNotIn(MAC, desc + ctx)

    def test_allowlist_honored(self):
        with tempfile.TemporaryDirectory() as d:
            p = article(Path(d), "a.md", f"device {MAC}\n")
            self.assertEqual(len(C.scan([("hugo", p)])), 1)
            self.assertEqual(C.scan([("hugo", p)], frozenset({C.fingerprint(MAC)})), [])

    def test_hostile_path_stays_a_parameter(self):
        cur = FakeCur({"RETURNING id": [(9,)]})
        evil = "x'; DROP TABLE claude_queue; --"
        f = {"surface": "hugo", "path": evil, "line": 1, "kind": "mac", "fp": "f"}
        C.write(cur, [f], [f])
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO card_rack_findings" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1])


class TestPerformance(unittest.TestCase):
    def test_scan_10k_lines(self):
        text = "\n".join(f"line {i} about the network, nothing secret, sha {'ab' * 32}" for i in range(10000))
        t = time.monotonic()
        found = C.scan_text(text)
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual(found, [])


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        sleeps = []
        with mock.patch("psycopg2.connect", side_effect=flaky):
            C.W.connect(_sleep=sleeps.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_unmounted_surface_fails_open(self):
        # RETRY GAP: corpus — a missing NAS dir is skipped for this run, not retried.
        with mock.patch("builtins.print"):
            self.assertEqual(C.corpus((("speaks", Path(tempfile.gettempdir()) / "no-such-dir-cr", "*.txt"),)), [])

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.allowlist(FakeCur(boom=True)), frozenset())
            self.assertEqual(C.known(FakeCur(boom=True)), set())


class TestUnit(unittest.TestCase):
    def test_household_whole_names_only(self):
        kinds = lambda s: [k for _, k, _ in C.scan_text(s)]  # noqa: E731
        self.assertIn("household", kinds("Dylan's room plug is pulling 80W"))
        self.assertIn("household", kinds("dylans_room_plug went up"))
        self.assertNotIn("household", kinds("the amygdala tags emotion"))
        self.assertNotIn("household", kinds('Bob Dylan\'s "Blowin\' in the Wind"'))

    def test_credentials_need_a_word_start(self):
        self.assertEqual(list(C._credentials("desk-satellite-android-tablets-finally-2026")), [])
        self.assertEqual(list(C._credentials("the industry's quietest open secret: everyone")), [])
        self.assertEqual(len(list(C._credentials(f"token {TOKEN}"))), 1)

    def test_posture_and_entropy(self):
        self.assertTrue(C.POSTURE.search("your security cameras are offline tonight"))
        self.assertFalse(C.POSTURE.search("like leaving the front door open with a sign"))
        self.assertGreater(C.entropy("Zq9Xk3LmP7vRt2Wy8NbQ4sJh6Gd"), C.ENTROPY_MIN)
        self.assertLessEqual(C.entropy("0123456789abcdef" * 4), 4.0)
        self.assertEqual(C.entropy(""), 0.0)

    def test_corpus_window(self):
        with tempfile.TemporaryDirectory() as d:
            new, old = article(Path(d), "new.md", "x"), article(Path(d), "old.md", "x")
            os.utime(old, (time.time() - 40 * 86400,) * 2)
            got = C.corpus((("hugo", Path(d), "*.md"),))
        self.assertEqual(got, [("hugo", new)])

    def test_info_kinds_not_filed(self):
        pri, desc, ctx = C.queue_text([{"kind": "private_ip", "path": "p", "line": 1, "fp": "a"},
                                       {"kind": "posture", "path": "p", "line": 2, "fp": "b"}])
        self.assertEqual(pri, 4)
        self.assertIn("posture 1", desc)
        self.assertNotIn("private_ip", ctx)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_existing_detectors(self):
        for ref in ("NJ._MAC_RE", "NJ._HOME_PATH_RE", "NJ._SAFE_EMAILS", "OS._HOUSEHOLD_RE", "RL.SECRET_PATTERNS"):
            self.assertIn(ref, SRC)
        self.assertIs(C._EMAIL_RE, C.NJ._SCRUB_PATTERNS[-1])
        self.assertEqual(C.SURFACES[0][1], C.NJ.HUGO_ROOT / "content")

    def test_table_and_config_keys(self):
        for col in ("surface text", "path text", "kind text", "fingerprint text", "queue_id int",
                    "UNIQUE (path, kind, fingerprint)"):
            self.assertIn(col, C.SCHEMA)
        cur = FakeCur({"service_config": [(["abc"],)]})
        self.assertEqual(C.allowlist(cur), frozenset({"abc"}))
        self.assertEqual(cur.sql[0][1], ("nova_card_rack",))

    def test_scan_then_queue_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p = article(Path(d), "a.md", f"one\nkey {TOKEN}\n")
            found = C.scan([("hugo", p)])
        pri, desc, ctx = C.queue_text(found)
        self.assertEqual((pri, found[0]["line"]), (2, 2))
        self.assertIn("a.md:2", ctx)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes, text):
        cur = FakeCur(routes)
        with tempfile.TemporaryDirectory() as d:
            p = article(Path(d), "post.md", text)
            with mock.patch.object(C.W, "connect", return_value=fake_conn(cur)), \
                    mock.patch.object(C, "corpus", return_value=[("hugo", p)]), mock.patch("builtins.print"):
                res = C.run(dry=dry)
        return cur, res

    def test_run_records_and_files_new(self):
        cur, res = self._run(False, {"RETURNING id": [(42,)], "to_regclass": [(None,)]},
                             f"key {TOKEN}\nthe house is empty until Sunday\n")
        self.assertEqual(res["new"], {"credential": 1, "posture": 1})
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS card_rack_findings" in s for s, _ in cur.sql))
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0][1], 2)
        self.assertTrue(all(TOKEN not in str(p) for _, p in cur.sql))

    def test_known_findings_not_refiled(self):
        with tempfile.TemporaryDirectory() as d:
            p = article(Path(d), "post.md", f"{MAC}\n")
            cur = FakeCur({"to_regclass": [("card_rack_findings",)],
                           "SELECT path, kind": [(C.display(p), "mac", C.fingerprint(MAC))]})
            with mock.patch.object(C.W, "connect", return_value=fake_conn(cur)), \
                    mock.patch.object(C, "corpus", return_value=[("hugo", p)]), mock.patch("builtins.print"):
                res = C.run(dry=False)
        self.assertEqual((res["findings"], res["new"]), ({"mac": 1}, {}))
        self.assertTrue(any("ON CONFLICT" in s for s, _ in cur.sql))      # last_seen refreshed
        self.assertFalse(any("claude_queue" in s for s, _ in cur.sql))

    def test_dry_run_writes_nothing(self):
        cur, res = self._run(True, {}, f"key {TOKEN}\n")
        self.assertEqual(res["findings"], {"credential": 1})
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "ALTER")))

    def test_pg_down_raises_before_writing(self):
        with mock.patch.object(C.W, "connect", side_effect=OSError("down")), \
                mock.patch.object(C, "corpus", return_value=[]), mock.patch("builtins.print"):
            with self.assertRaises(OSError):
                C.run(dry=True)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_card_rack.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_card_rack.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
