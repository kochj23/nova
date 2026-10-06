#!/usr/bin/env python3
"""Tests for nova_context_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_context_bridge.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_FEED = "C_FEED"
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_config": cfg, "nova_notify": nn}


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stubbed(_stub_modules()):
        spec.loader.exec_module(mod)
    return mod


cb = _load("cb", SCRIPT)
# Offline guards at module load: the module's own subprocess / urllib bindings are replaced (never the real
# modules), and every state/workspace path is pointed at a tempdir that outlives the test run.
_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
cb.STATE_FILE = TMP / "state" / "nova_context_bridge_state.json"
cb.STATE_FILE.parent.mkdir(parents=True)
cb.MEMORY_DIR = TMP / "memory"; cb.MEMORY_DIR.mkdir()
cb.JOURNAL_DIR = TMP / "journal"; cb.JOURNAL_DIR.mkdir()
cb.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("offline")))
cb.urllib = types.SimpleNamespace(parse=urllib.parse,
                                  request=types.SimpleNamespace(urlopen=MagicMock(side_effect=RuntimeError("offline"))))


def _resp(payload):
    r = MagicMock()
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    return r


def _ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _mem(days_ago=90, sim=0.6, text="Back then you were rebuilding the DNS sync for the whole fleet.", source="journal"):
    return {"text": text, "similarity": sim, "source": source, "metadata": {"date": _ago(days_ago)}}


def _reset(memory_lines=None, gh=None, meetings=None, recall=None, state=None):
    """Install fresh stubs; `recall` is a list of result lists consumed per urlopen call (or an Exception)."""
    for f in cb.MEMORY_DIR.glob("*"):
        f.unlink()
    for f in cb.JOURNAL_DIR.glob("*"):
        f.unlink()
    if cb.STATE_FILE.exists():
        cb.STATE_FILE.unlink()
    if state is not None:
        cb.STATE_FILE.write_text(json.dumps(state))
    if memory_lines is not None:
        (cb.MEMORY_DIR / f"{cb.TODAY}.md").write_text("\n".join(memory_lines), encoding="utf-8")

    def run(argv, **kw):
        r = MagicMock(); r.returncode = 0
        if argv[0] == "gh":
            if isinstance(gh, Exception):
                raise gh
            r.stdout = json.dumps(gh or [])
        else:
            if isinstance(meetings, Exception):
                raise meetings
            r.stdout = json.dumps(meetings or [])
        return r
    cb.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=run))
    queue = list(recall or [])

    def urlopen(url, timeout=None):
        item = queue.pop(0) if queue else []
        if isinstance(item, Exception):
            raise item
        return _resp(item)
    cb.urllib = types.SimpleNamespace(parse=urllib.parse, request=types.SimpleNamespace(urlopen=MagicMock(side_effect=urlopen)))
    cb.notify = MagicMock(return_value=True)
    cb.random = types.SimpleNamespace(choice=lambda seq: seq[0])


def _main():
    with redirect_stdout(io.StringIO()) as out:
        cb.main()
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_calls_are_argv_lists_without_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        _reset()
        cb.gather_today_signals()
        for call in cb.subprocess.run.call_args_list:
            self.assertIsInstance(call[0][0], list)
            self.assertIn("timeout", call[1])

    def test_recall_query_is_url_encoded(self):
        _reset(recall=[[]])
        cb.recall("fix & break?x=1 #tag")
        url = cb.urllib.request.urlopen.call_args[0][0]
        self.assertTrue(url.startswith(cb.VECTOR_URL + "/recall?"))
        self.assertNotIn("&break", url)
        self.assertEqual(urllib.parse.parse_qs(url.split("?", 1)[1])["q"], ["fix & break?x=1 #tag"])

    def test_echo_text_is_bounded_before_posting(self):
        echo = {"text": "Q" * 5000, "date": _ago(60), "source": "s", "similarity": 0.5, "days_ago": 60}
        msg = cb.build_bridge_message("sig", echo)
        self.assertEqual(msg.count("Q"), 200)
        self.assertIn("...", msg)
        self.assertEqual(len(cb.filter_echoes([_mem(text="X" * 5000)])[0]["text"]), 300)


class TestPerformance(unittest.TestCase):
    def test_filter_echoes_on_10k_results(self):
        results = [_mem(days_ago=(i % 400), sim=(i % 100) / 100, text=f"memory number {i} with enough text") for i in range(10_000)]
        t0 = time.perf_counter()
        echoes = cb.filter_echoes(results)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(echoes)
        self.assertTrue(all(cb.SIMILARITY_FLOOR <= e["similarity"] <= cb.SIMILARITY_CEILING for e in echoes))
        self.assertEqual([e["days_ago"] for e in echoes], sorted((e["days_ago"] for e in echoes), reverse=True))

    def test_main_caps_recall_fan_out_at_ten_signals(self):
        _reset(memory_lines=[f"## topic number {i} is interesting" for i in range(50)], recall=[[]] * 50)
        _main()
        self.assertEqual(cb.urllib.request.urlopen.call_count, 10)


class TestRetry(unittest.TestCase):
    def test_recall_fails_open_to_empty_list(self):
        # RETRY GAP: recall — one urlopen attempt; any error is logged and [] returned
        _reset(recall=[RuntimeError("memory server down"), [_mem()]])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cb.recall("anything"), [])
        self.assertEqual(cb.urllib.request.urlopen.call_count, 1)
        self.assertIn("Recall error: memory server down", out.getvalue())

    def test_signal_sources_fail_independently(self):
        # RETRY GAP: gather_today_signals/gh + curl — each probe is one attempt; a failure drops that source only
        _reset(memory_lines=["## Memory topic survives the outage"], gh=RuntimeError("gh timeout"),
               meetings=RuntimeError("novacontrol down"))
        signals = cb.gather_today_signals()
        self.assertEqual(signals, ["Memory topic survives the outage"])
        self.assertEqual(cb.subprocess.run.call_count, 2)

    def test_corrupt_state_file_resets_cleanly(self):
        _reset()
        cb.STATE_FILE.write_text("{not json")
        self.assertEqual(cb.load_state(), {"date": cb.TODAY, "bridges_sent": [], "topics_used": []})


class TestUnit(unittest.TestCase):
    def test_filter_echoes_rules(self):
        too_recent = _mem(days_ago=cb.MIN_ECHO_AGE_DAYS - 1)
        weak, identical, short = _mem(sim=0.44), _mem(sim=0.86), _mem(text="tiny")
        undated = {"text": "no date at all but long enough", "similarity": 0.6}
        self.assertEqual(cb.filter_echoes([too_recent, weak, identical, short, undated]), [])
        e = cb.filter_echoes([_mem(days_ago=30, sim=0.5), _mem(days_ago=200, sim=0.7, source="email")])
        self.assertEqual([x["days_ago"] for x in e], [200, 30])
        self.assertEqual(e[0]["source"], "email")
        edge = cb.filter_echoes([{"text": "x" * 30, "score": 0.45, "created_at": _ago(14) + "T10:00:00"}])
        self.assertEqual(len(edge), 1)                    # floor and exact cutoff day are inclusive

    def test_build_bridge_message_shape(self):
        cb.random = types.SimpleNamespace(choice=lambda seq: seq[1])
        echo = {"text": "  old thought  ", "date": "2026-03-01", "source": "journal", "similarity": 0.5, "days_ago": 218}
        msg = cb.build_bridge_message("sig", echo)
        self.assertEqual(msg.split("\n")[0], "*Thread from the past*")
        self.assertIn("back on 2026-03-01, you were working on something similar", msg)
        self.assertIn("> old thought", msg)
        self.assertTrue(msg.endswith("_Source: journal (2026-03-01)_"))

    def test_state_round_trip_and_date_rollover(self):
        _reset()
        self.assertEqual(cb.load_state()["date"], cb.TODAY)
        cb.save_state({"date": "2001-01-01", "bridges_sent": [1], "topics_used": ["x"]})
        self.assertEqual(cb.load_state(), {"date": cb.TODAY, "bridges_sent": [], "topics_used": []})
        cb.save_state({"date": cb.TODAY, "bridges_sent": [1], "topics_used": ["x"]})
        self.assertEqual(cb.load_state()["topics_used"], ["x"])

    def test_gather_signals_from_every_source(self):
        gh = [{"type": "PushEvent", "created_at": cb.TODAY + "T10:00:00Z",
               "payload": {"commits": [{"message": "fix: retry\n\nbody"}, {"message": ""}]}},
              {"type": "IssuesEvent", "created_at": cb.TODAY + "T11:00:00Z", "payload": {"issue": {"title": "Bug 1"}}},
              {"type": "PushEvent", "created_at": "2000-01-01T00:00:00Z", "payload": {"commits": [{"message": "old"}]}}]
        meetings = {"meetings": [{"date": cb.TODAY, "title": "1:1 with Sam"}, {"date": "2000-01-01", "title": "old"}]}
        (cb.JOURNAL_DIR / "a.md").write_text(f"# Journal\n{_ago(1)} wrote about the fleet DNS migration again\n")
        _reset(memory_lines=["## Big header topic", "- short", "- a longer bullet point here", "plain line"], gh=gh, meetings=meetings)
        (cb.JOURNAL_DIR / "a.md").write_text(f"# Journal\n{_ago(1)} wrote about the fleet DNS migration again\n")
        s = cb.gather_today_signals()
        self.assertEqual(s[:3], ["coding: fix: retry", "issue: Bug 1", "meeting: 1:1 with Sam"])
        self.assertIn("Big header topic", s)
        self.assertIn("a longer bullet point here", s)
        self.assertNotIn("short", s)
        self.assertTrue(any(x.startswith("journal: ") and "fleet DNS" in x for x in s))


class TestIntegration(unittest.TestCase):
    def test_slack_post_routes_through_the_notify_bus_as_journal_info(self):
        _reset()
        cb.slack_post("*Thread from the past*\n_intro_\n\n> text")
        cb.notify.assert_called_once_with("Thread from the past", body="_intro_\n\n> text", level="info", category="journal")
        self.assertNotIn("def notify", SRC)
        self.assertIn("from nova_notify import notify", SRC)

    def test_recall_then_filter_chain_shapes(self):
        _reset(recall=[{"results": [_mem(days_ago=100, sim=0.5), _mem(days_ago=3)]}])
        echoes = cb.filter_echoes(cb.recall("dns"))
        self.assertEqual(len(echoes), 1)
        self.assertEqual(set(echoes[0]), {"text", "date", "source", "similarity", "days_ago"})
        self.assertEqual(echoes[0]["days_ago"], 100)

    def test_used_topics_are_not_recalled_again(self):
        _reset(memory_lines=["## Already used topic today"], recall=[[_mem()]],
               state={"date": cb.TODAY, "bridges_sent": [], "topics_used": ["Already used topic today"]})
        _main()
        cb.urllib.request.urlopen.assert_not_called()
        cb.notify.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_the_most_distant_echo_and_records_state(self):
        _reset(memory_lines=["## Rebuilding the fleet DNS sync", "## Second signal of the day"],
               recall=[[_mem(days_ago=40, sim=0.5)], [_mem(days_ago=300, sim=0.6, text="Three hundred days ago you tried this exact migration.")]])
        out = _main()
        cb.notify.assert_called_once()
        title, kw = cb.notify.call_args[0][0], cb.notify.call_args[1]
        self.assertEqual(title, "Thread from the past")
        self.assertIn("300 days ago", kw["body"])
        self.assertIn("> Three hundred days ago", kw["body"])
        state = json.loads(cb.STATE_FILE.read_text())
        self.assertEqual(state["bridges_sent"][0]["signal"], "Second signal of the day")
        self.assertEqual(state["bridges_sent"][0]["echo_date"], _ago(300))
        self.assertEqual(state["topics_used"], ["Rebuilding the fleet DNS sync", "Second signal of the day"])
        self.assertIn("Bridge posted", out)

    def test_no_signals_posts_nothing_and_leaves_state_alone(self):
        _reset()
        out = _main()
        cb.notify.assert_not_called()
        self.assertFalse(cb.STATE_FILE.exists())
        self.assertIn("No signals today", out)

    def test_daily_bridge_budget_of_two_is_honored(self):
        _reset(memory_lines=["## A fresh signal with history"], recall=[[_mem()]],
               state={"date": cb.TODAY, "bridges_sent": [{"a": 1}, {"b": 2}], "topics_used": []})
        out = _main()
        cb.notify.assert_not_called()
        self.assertIn("No compelling bridges", out)
        self.assertEqual(len(json.loads(cb.STATE_FILE.read_text())["bridges_sent"]), 2)

    def test_error_path_memory_server_down(self):
        _reset(memory_lines=["## A signal long enough to count"], recall=[RuntimeError("down")])
        out = _main()
        cb.notify.assert_not_called()
        self.assertIn("Recall error: down", out)
        self.assertIn("No compelling bridges", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--signals", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_context_bridge"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
