#!/usr/bin/env python3
"""Tests for nova_valdemar.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_valdemar as V  # noqa: E402

SRC = (SCRIPTS / "nova_valdemar.py").read_text()
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 — computed at call time
PINNED = {"name": "nova:latest", "expires_at": "2319-01-18T15:02:06.752196807-08:00", "size_vram": 1}
LOADED = {"name": "qwen3:8b", "expires_at": "2026-10-08T16:35:42.762326-07:00"}


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last, self.rowcount = routes or {}, boom, [], [], 0

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


def ps_response(models):
    return io.BytesIO(json.dumps({"models": models}).encode())


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b(?!127\.0\.0\.1)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_name_stays_a_parameter(self):
        cur = FakeCur()
        evil = "x'; DROP TABLE claude_queue; --"
        V.write(cur, [V.hold("bak", evil, None)], ["bak"], NOW())
        sql, params = [(s, p) for s, p in cur.sql if "INSERT" in s][0]
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)

    def test_observer_never_deletes_or_unloads(self):
        for verb in ("unlink(", "os.remove", "rmtree", "DELETE FROM", "bootout", "launchctl\", \"unload"):
            self.assertNotIn(verb, SRC)


class TestPerformance(unittest.TestCase):
    def test_oldest_10k(self):
        now = NOW()
        hs = [V.hold("bak", f"f{i}", now - timedelta(minutes=i)) for i in range(10000)]
        t = time.monotonic()
        top = V.oldest(hs, now)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(top[0]["name"], "f9999")
        self.assertEqual(len(top), 10)

    def test_disabled_labels_10k(self):
        text = "\n".join(f'"com.x.{i}" => {"disabled" if i % 2 else "enabled"}' for i in range(10000))
        t = time.monotonic()
        self.assertEqual(len(V.disabled_labels(text)), 5000)
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_ollama_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return ps_response([PINNED, LOADED])
        with mock.patch("urllib.request.urlopen", side_effect=flaky), mock.patch("builtins.print"):
            pins = V.ollama_pins(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual([p["name"] for p in pins], ["nova:latest"])

    def test_ollama_down_fails_open(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), mock.patch("builtins.print"):
            self.assertIsNone(V.ollama_pins(_sleep=lambda s: None))

    def test_launchctl_failure_fails_open(self):
        # RETRY GAP: _print_disabled (local launchctl, single attempt; returns '' on failure)
        with mock.patch("subprocess.run", side_effect=OSError("no launchctl")), mock.patch("builtins.print"):
            self.assertEqual(V._print_disabled("system"), "")

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(V.suppressions(FakeCur(boom=True)), [])
            self.assertIsNone(V.live_holds(FakeCur(boom=True)))


class TestUnit(unittest.TestCase):
    def test_pinned_edges(self):
        now = NOW()
        self.assertEqual(V.pinned([], now), [])
        self.assertEqual(V.pinned([{"name": "x", "expires_at": "garbage"}, {"name": "y"}], now), [])
        self.assertEqual(len(V.pinned([PINNED, LOADED], now)), 1)

    def test_disabled_plists_and_baks_from_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            a, s = Path(d) / "agents", Path(d) / "daemons"
            (a / "_disabled").mkdir(parents=True)
            s.mkdir()
            (a / "com.nova.off.plist").write_text("<plist/>")
            (a / "com.nova.on.plist").write_text("<plist/>")
            (a / "com.nova.old.plist.disabled-x").write_text("")
            (a / "_disabled" / "com.nova.gone.plist").write_text("")
            (a / "com.nova.on.plist.bak-1").write_text("")
            got = V.disabled_plists(a, s, gui_text='"com.nova.off" => disabled\n"com.nova.on" => enabled\n'
                                    '"com.nova.missing" => disabled', sys_text="")
            names = sorted(Path(h["name"]).name for h in got)
            self.assertEqual(names, ["com.nova.gone.plist", "com.nova.off.plist", "com.nova.old.plist.disabled-x"])
            self.assertEqual([Path(h["name"]).name for h in V.baks((a, Path(d) / "nope"))], ["com.nova.on.plist.bak-1"])
            self.assertIsNotNone(got[0]["held_since"])

    def test_age_uses_first_seen_when_start_unknown(self):
        now = NOW()
        self.assertAlmostEqual(V.age_days({"held_since": None, "first_seen": now - timedelta(days=3)}, now), 3, 3)

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(V.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("W.retry(", SRC)
        self.assertIn("W.connect()", SRC)

    def test_schema_contract(self):
        for col in ("host text NOT NULL", "kind text NOT NULL", "name text NOT NULL", "held_since timestamptz",
                    "released_at timestamptz", "detail jsonb", "UNIQUE (host, kind, name)"):
            self.assertIn(col, V.SCHEMA)

    def test_suppressions_read_learned_baselines(self):
        t = NOW()
        cur = FakeCur({"FROM learned_baselines": [("sig1", "nova_llm_ping", t)]})
        self.assertEqual(V.suppressions(cur), [V.hold("suppression", "learned_baselines:sig1", t, source="nova_llm_ping")])

    def test_merged_into_yellow_eye(self):
        import nova_yellow_eye as Y
        self.assertIn("merged into nova_yellow_eye on 2026-10-09", SRC)
        self.assertIn("import nova_yellow_eye", SRC.split("def main(")[1])   # lazy: no import cycle
        self.assertIn("--holds", Y.__doc__)

    def test_write_releases_only_scanned_kinds(self):
        cur = FakeCur()
        V.write(cur, [], ["bak", "suppression"], NOW())
        upd = [p for s, p in cur.sql if s.startswith("UPDATE valdemar_holds")][0]
        self.assertEqual(upd[2], ["bak", "suppression"])          # ollama_pin skipped -> not released


class TestFunctional(unittest.TestCase):
    def _run(self, fn, dry, routes=None, pins=([PINNED, LOADED],)):
        cur = FakeCur(routes or {"FROM learned_baselines": [("sig", "src", NOW() - timedelta(days=40))]})
        old = V.hold("bak", "/x/a.py.bak", NOW() - timedelta(days=200))
        side = OSError("down") if pins is None else (lambda *a, **k: ps_response(pins[0]))
        with mock.patch.object(V.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(V, "baks", return_value=[old]), \
                mock.patch.object(V, "disabled_plists", return_value=[]), \
                mock.patch.object(V.W, "retry", side_effect=lambda fn_, *a, **k: fn_()), \
                mock.patch("urllib.request.urlopen", side_effect=side), \
                mock.patch("subprocess.run") as sp, mock.patch("builtins.print"):
            try:
                out = fn(dry=dry)
            except OSError:
                out = "raised"
        sp.assert_not_called()
        return cur, out

    def test_run_registers_and_releases(self):
        cur, holds = self._run(V.run, False)
        self.assertEqual(sorted(h["kind"] for h in holds), ["bak", "ollama_pin", "suppression"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS valdemar_holds" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO valdemar_holds" in s for s, _ in cur.sql), 3)
        self.assertTrue(any(s.startswith("UPDATE valdemar_holds SET released_at") for s, _ in cur.sql))

    def test_dry_run_writes_nothing(self):
        for fn in (V.run, V.seven_months):
            cur, out = self._run(fn, True)
            self.assertNotEqual(out, "raised")
            self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))

    def test_oldest_files_once_per_month(self):
        rows = [("bak", "/x/old.bak", NOW() - timedelta(days=210), NOW())]
        cur, top = self._run(V.seven_months, False, routes={"to_regclass": [("valdemar_holds",)],
                                                           "FROM valdemar_holds": rows})
        self.assertEqual(top[0]["name"], "/x/old.bak")
        ins = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(ins), 1)
        self.assertIn("210d", ins[0][2])
        cur, _ = self._run(V.seven_months, False, routes={"to_regclass": [("valdemar_holds",)],
                                                         "FROM valdemar_holds": rows, "SELECT 1 FROM claude_queue": [(1,)]})
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))

    def test_wrapper_forwards_to_yellow_eye(self):
        import nova_yellow_eye as Y
        with mock.patch.object(Y, "main", return_value=0) as ym, mock.patch("builtins.print") as pr:
            self.assertEqual(V.main(["--run", "--dry-run"]), 0)
            self.assertEqual(V.main(["--oldest"]), 0)
            self.assertEqual(V.main(["--run", "--oldest"]), 0)        # --run wins, as before the merge
        self.assertEqual([c.args[0] for c in ym.call_args_list],
                         [["--holds", "--dry-run"], ["--holds", "--oldest"], ["--holds"]])
        self.assertIn("merged into nova_yellow_eye on 2026-10-09", str(pr.call_args_list[0]))

    def test_yellow_eye_holds_mode_runs_this_register(self):
        import nova_yellow_eye as Y
        with mock.patch.object(V, "run", return_value=[]) as r, mock.patch.object(V, "seven_months", return_value=[]) as o:
            Y.holds(dry=True)
            Y.holds(dry=False, oldest=True)
        r.assert_called_once_with(dry=True)
        o.assert_called_once_with(dry=False)

    def test_ollama_down_keeps_pins(self):
        with mock.patch.object(V.W, "retry", return_value=False), mock.patch("builtins.print"):
            self.assertIsNone(V.ollama_pins())
        cur = FakeCur()
        with mock.patch.object(V.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(V, "baks", return_value=[]), mock.patch.object(V, "disabled_plists", return_value=[]), \
                mock.patch.object(V, "ollama_pins", return_value=None), mock.patch("builtins.print"):
            V.run(dry=False)
        upd = [p for s, p in cur.sql if s.startswith("UPDATE valdemar_holds")][0]
        self.assertNotIn("ollama_pin", upd[2])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_valdemar.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_valdemar.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
