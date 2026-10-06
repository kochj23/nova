#!/usr/bin/env python3
"""Tests for nova_outreach_intelligence.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_outreach_intelligence.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("noi_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oi = _load()
_REAL_EMAIL_HISTORY = oi.get_email_history
# never touch the real herd, outreach log, or intelligence file
_ROOT = Path(_TMP.name)
oi.OUTREACH_LOG = _ROOT / "outreach.log"
oi.INTELLIGENCE_FILE = _ROOT / "intel.json"
oi.HERD_DIR = _ROOT / "herd"
oi.WORKSPACE = _ROOT / "ws"
HERD = [{"name": "Alice", "email": "alice@example.com", "profile": "alice.md"},
        {"name": "Bob", "email": "bob@example.com", "profile": "bob.md"},
        {"name": "Cara", "email": "cara@example.com", "profile": "cara.md"}]


def _d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _write_log(entries):
    oi.OUTREACH_LOG.write_text("\n".join(f"[{_d(d)} 09:00:00] Outreach sent to {n}" for n, d in entries) + "\n")


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch.object(oi, "HERD", HERD); p.start(); self.addCleanup(p.stop)
        p = patch.object(oi, "get_email_history", return_value={}); self.eh = p.start(); self.addCleanup(p.stop)
        oi.OUTREACH_LOG.unlink(missing_ok=True)
        oi.HERD_DIR.mkdir(parents=True, exist_ok=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_herd(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"[\w.]+@(gmail|yahoo|icloud)\.com", SRC))  # herd lives in gitignored config
        self.assertIn("from herd_config import HERD", SRC)

    def test_read_only_never_sends(self):
        self.assertNotIn("smtplib", SRC)
        self.assertNotIn("post_both", SRC)


class TestPerformance(_Base):
    def test_history_parse_10k_lines(self):
        _write_log([(f"P{i % 100}", i % 60) for i in range(10_000)])
        t0 = time.perf_counter()
        h = oi.get_outreach_history()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(h), 100)


class TestRetry(_Base):
    def test_email_history_fails_open(self):
        # RETRY GAP: get_email_history()/urlopen — one attempt per member, failures skipped silently
        with patch.object(oi.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(_REAL_EMAIL_HISTORY(), {})
        self.assertEqual(u.call_count, len(HERD))

    def test_signals_fail_open_when_gh_missing(self):
        # RETRY GAP: get_today_signals()/gh — one attempt, [] on error
        with patch.object(oi.subprocess, "run", side_effect=FileNotFoundError("gh")):
            self.assertEqual(oi.get_today_signals(), [])


class TestUnit(_Base):
    def test_history_parsing(self):
        oi.OUTREACH_LOG.write_text(f"[{_d(2)} 09:15:32] Outreach sent to Alice\nnoise line\n")
        self.assertEqual(dict(oi.get_outreach_history()), {"Alice": [_d(2)]})

    def test_warmth_scores(self):
        _write_log([("Alice", 1), ("Alice", 5), ("Bob", 25)])
        self.eh.return_value = {"Bob": {"last_exchange": _d(40)}}
        s = oi.compute_warmth_scores()
        self.assertEqual(s["Alice"], 50 + 20 + 10)
        self.assertEqual(s["Bob"], 50 - 15 + 5 - 10)
        self.assertEqual(s["Cara"], 30)

    def test_intelligence_roundtrip(self):
        self.assertEqual(oi.load_intelligence(), {"contacts": {}, "last_updated": ""})
        oi.save_intelligence({"contacts": {"a": 1}})
        self.assertEqual(oi.load_intelligence()["contacts"], {"a": 1})
        oi.INTELLIGENCE_FILE.write_text("{bad")
        self.assertEqual(oi.load_intelligence()["contacts"], {})


class TestIntegration(_Base):
    def test_signal_matches_interested_member(self):
        (oi.HERD_DIR / "bob.md").write_text("Loves python and AI tinkering")
        _write_log([("Alice", 10), ("Bob", 10), ("Cara", 10)])
        sig = [{"type": "commit", "repo": "MLXCode", "message": "x"}]
        pick = oi.pick_best_recipient(oi.compute_warmth_scores(), sig)
        self.assertEqual(pick["name"], "Bob")
        self.assertEqual(pick["signal"], sig[0])

    def test_github_signals_today_only(self):
        today = date.today().isoformat()
        events = [{"created_at": f"{today}T01:00:00Z", "repo": {"name": "kochj23/Repo"}, "type": "PushEvent",
                   "payload": {"commits": [{"message": "fix"}]}},
                  {"created_at": "2000-01-01T00:00:00Z", "repo": {"name": "kochj23/Old"}, "type": "PushEvent",
                   "payload": {"commits": [{"message": "old"}]}}]
        with patch.object(oi, "TODAY", today), \
             patch.object(oi.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(events))):
            self.assertEqual(oi.get_today_signals(), [{"type": "commit", "repo": "Repo", "message": "fix"}])


class TestFunctional(_Base):
    def test_suggest_skips_recent_and_picks_overdue(self):
        _write_log([("Alice", 1), ("Bob", 40)])
        buf = io.StringIO()
        with patch.object(oi, "get_today_signals", return_value=[]), contextlib.redirect_stdout(buf):
            oi.status_report(); oi.suggest()
        out = buf.getvalue()
        self.assertIn("To: Cara", out)          # never contacted: lowest warmth, 999 days since
        self.assertIn("Alice", out)             # listed in the status report

    def test_nobody_available(self):
        _write_log([("Alice", 1), ("Bob", 1), ("Cara", 1)])
        self.assertIsNone(oi.pick_best_recipient({}, []))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--suggest", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_outreach_intelligence"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
