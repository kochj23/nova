#!/usr/bin/env python3
"""Tests for nova_swarm.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_swarm.py"
SRC = SCRIPT.read_text()
ROUTER_URL = "http://router.test:37475/v1/chat/completions"
_REAL_RUN = subprocess.run                                  # TestFrame needs the genuine one


def _router_stub():
    m = types.ModuleType("nova_router")
    m.chat_url = lambda timeout=2.0: ROUTER_URL               # the real one probes .2/.10 over HTTP at import
    m.base = lambda timeout=2.0: "http://router.test:37475"
    return m


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_router": _router_stub()}):   # restored after; the module keeps its binding
        spec.loader.exec_module(mod)
    return mod


sw = _load("swarm_under_test", SCRIPT)
# Safety net: nothing in this file may shell out, scp, ssh, psql or reach the router unless a test says so.
_NO_RUN = patch.object(sw.subprocess, "run", side_effect=AssertionError("subprocess.run must be mocked"))
_NO_NET = patch.object(sw.urllib.request, "urlopen", side_effect=AssertionError("urlopen must be mocked"))


def setUpModule():
    _NO_RUN.start(); _NO_NET.start()


def tearDownModule():
    _NO_RUN.stop(); _NO_NET.stop()


class _Resp:
    def __init__(self, content): self._d = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    def read(self): return self._d


def _proc(stdout="", stderr=""):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)


def _agent_result(node, task):
    return {"node": node[0], "assessment": f"{node[0]} looked at: {task}", "steps": 2, "secs": 1}


def _dispatch(job, fan_each=False, decompose=None, run_agent=_agent_result, synth="all good", psql=None):
    psql = psql or MagicMock(return_value="")
    llm = MagicMock(side_effect=synth if isinstance(synth, Exception) else [synth])
    buf, err = io.StringIO(), io.StringIO()
    with ExitStack() as st:
        for cm in (patch.object(sw, "psql", psql), patch.object(sw, "deploy", MagicMock()), patch.object(sw, "llm", llm),
                   patch.object(sw, "run_agent", MagicMock(side_effect=run_agent)), redirect_stdout(buf), redirect_stderr(err)):
            st.enter_context(cm)
        if decompose is not None:
            st.enter_context(patch.object(sw, "decompose", MagicMock(return_value=decompose)))
        job_id = sw.dispatch(job, fan_each=fan_each)
    return job_id, psql, llm, buf.getvalue() + err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertIn('"BatchMode=yes"', SRC)                     # ssh/scp never prompt for a password

    def test_psql_literals_are_escaped_and_quotes_stay_balanced(self):
        self.assertEqual(sw.q(None), "")
        self.assertEqual(sw.q("it's"), "it''s")
        evil = "x'; DROP TABLE swarm.jobs; --"
        job_id, psql, _, _ = _dispatch(evil, fan_each=True, run_agent=lambda n, t: {"node": n[0], "assessment": evil, "steps": 1})
        for (sql,), _kw in psql.call_args_list:
            self.assertEqual(sql.count("'") % 2, 0, sql)             # every literal closed -> payload stays a string
            self.assertNotRegex(sql, r"(?<!')'; DR" + "OP")          # no un-doubled quote ever ends a literal early
            if "TABLE swarm.jobs" in sql:
                self.assertIn("''; DR" + "OP", sql)

    def test_psql_runs_as_argv_against_nova_ops(self):
        run = MagicMock(return_value=_proc(" 42 \n"))
        with patch.object(sw.subprocess, "run", run):
            self.assertEqual(sw.psql("SELECT 1;"), "42")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:7], ["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops"])
        self.assertEqual(run.call_args[1]["timeout"], 30)


class TestPerformance(unittest.TestCase):
    def test_escaping_and_decomposition_fast_on_10k(self):
        raw = json.dumps([{"node": n[0], "task": f"check {n[0]}"} for n in sw.NODES])
        t0 = time.perf_counter()
        with patch.object(sw, "llm", MagicMock(return_value="prose " + raw + " trailing")):
            for i in range(10_000):
                sw.q(f"task {i} with 'quotes' and {i % 7}")
                if i % 10 == 0:
                    self.assertEqual(len(sw.decompose("job")), len(sw.NODES))
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRetry(unittest.TestCase):
    def test_llm_is_one_shot_and_decompose_fails_open_to_fan_all(self):
        # RETRY GAP: llm() — a single urlopen to the router (no backoff); decompose() falls back to every node
        uo = MagicMock(side_effect=OSError("router down"))
        err = io.StringIO()
        with patch.object(sw.urllib.request, "urlopen", uo), redirect_stderr(err):
            out = sw.decompose("audit things")
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(out, [(n, "audit things") for n in sw.NODES])
        self.assertIn("decompose fell back to fan-to-all", err.getvalue())

    def test_run_agent_fails_open_on_timeout_and_junk(self):
        # RETRY GAP: run_agent — one ssh/subprocess attempt; a timeout becomes an ERROR assessment, never an exception
        with patch.object(sw.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 240)) as run:
            r = sw.run_agent(sw.BYNAME["nova-core"], "check disks")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(r["node"], "nova-core")
        self.assertTrue(r["assessment"].startswith("ERROR:"))
        self.assertEqual(r["steps"], 0)
        with patch.object(sw.subprocess, "run", return_value=_proc("garbage\n{not json")):
            r = sw.run_agent(sw.BYNAME["mac-studio"], "t")
        self.assertTrue(r["assessment"].startswith("ERROR:"))

    def test_synthesis_failure_still_closes_the_job(self):
        # RETRY GAP: dispatch()/llm synthesis — one attempt; the job row still goes to status='done'
        job_id, psql, _, out = _dispatch("job", fan_each=True, synth=OSError("router down"))
        self.assertIn("(synthesis failed: router down)", out)
        done = [c[0][0] for c in psql.call_args_list if "UPDATE swarm.jobs SET status='done'" in c[0][0]]
        self.assertEqual(len(done), 1)
        self.assertIn("synthesis='(synthesis failed: router down)'", done[0])

    def test_deploy_swallows_nothing_but_scp_failures_are_non_fatal(self):
        # RETRY GAP: deploy() — one scp per node; a non-zero exit is ignored (capture_output, no check)
        run = MagicMock(return_value=types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied"))
        with patch.object(sw.subprocess, "run", run):
            sw.deploy()
        remote = [n for n in sw.NODES if n[1] != "local"]
        self.assertEqual(run.call_count, len(remote))
        self.assertEqual([c[0][0][-1] for c in run.call_args_list], [f"kochj@{n[1]}:{sw.AGENT_REMOTE}" for n in remote])


class TestUnit(unittest.TestCase):
    def test_decompose_parses_json_and_filters_unknown_nodes(self):
        raw = ('Sure:\n[{"node":"nova-core","task":"check pg"},{"node":"mars","task":"x"},'
               '{"node":"mac-mini","task":""},{"node":"tv-movies","task":"check plex"}]')
        with patch.object(sw, "llm", MagicMock(return_value=raw)) as llm:
            out = sw.decompose("audit")
        self.assertEqual(out, [(sw.BYNAME["nova-core"], "check pg"), (sw.BYNAME["tv-movies"], "check plex")])
        self.assertIn("JOB: audit", llm.call_args[0][1])
        with patch.object(sw, "llm", MagicMock(return_value='[{"node":"mars","task":"x"}]')), redirect_stderr(io.StringIO()):
            self.assertEqual(sw.decompose("audit"), [(n, "audit") for n in sw.NODES])

    def test_run_agent_takes_the_last_json_line(self):
        out = '{"assessment":"first","steps":1}\nlog line\n{"assessment":"final","steps":3}\n'
        with patch.object(sw.subprocess, "run", return_value=_proc(out)) as run:
            r = sw.run_agent(sw.BYNAME["mac-studio"], "task text")
        self.assertEqual((r["assessment"], r["steps"], r["node"]), ("final", 3, "mac-studio"))
        self.assertIsInstance(r["secs"], int)
        self.assertEqual(run.call_args[0][0], ["/opt/homebrew/bin/python3", sw.AGENT_LOCAL])
        self.assertEqual(run.call_args[1]["input"], "task text")

    def test_run_agent_no_result_carries_stderr(self):
        with patch.object(sw.subprocess, "run", return_value=_proc("", "boom " * 50)) as run:
            r = sw.run_agent(sw.BYNAME["nova-core2"], "t")
        self.assertTrue(r["assessment"].startswith("(no result) boom"))
        self.assertLessEqual(len(r["assessment"]), len("(no result) ") + 120)
        self.assertEqual(run.call_args[0][0][:2], ["ssh", "-o"])
        self.assertIn("kochj@192.168.1.86", run.call_args[0][0])

    def test_llm_request_shape(self):
        uo = MagicMock(return_value=_Resp("answer"))
        with patch.object(sw.urllib.request, "urlopen", uo):
            self.assertEqual(sw.llm("sys", "usr", model="conversation", max_tokens=7), "answer")
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, ROUTER_URL)
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["max_tokens"], body["stream"]), ("conversation", 7, False))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])


class TestIntegration(unittest.TestCase):
    def test_router_comes_from_nova_router_not_a_hardcoded_ip(self):
        self.assertIn("ROUTER = nova_router.chat_url()", SRC)
        self.assertEqual(sw.ROUTER, ROUTER_URL)
        self.assertNotIn("37475", SRC)

    def test_decompose_output_feeds_run_agent(self):
        raw = json.dumps([{"node": "nova-core5", "task": "ping"}])
        with patch.object(sw, "llm", MagicMock(return_value=raw)):
            (node, task), = sw.decompose("j")
        with patch.object(sw.subprocess, "run", return_value=_proc('{"assessment":"up","steps":1}')) as run:
            r = sw.run_agent(node, task)
        self.assertEqual(r["node"], "nova-core5")
        self.assertIn("kochj@192.168.1.10", run.call_args[0][0])
        self.assertEqual(run.call_args[0][0][-1], f"python3 {sw.AGENT_REMOTE}")

    def test_node_roster_is_consistent(self):
        self.assertEqual(set(sw.BYNAME), {n[0] for n in sw.NODES})
        self.assertEqual(sum(1 for n in sw.NODES if n[1] == "local"), 1)
        for name, host, py, cap in sw.NODES:
            self.assertTrue(host == "local" or re.fullmatch(r"192\.168\.1\.\d+", host), name)
            self.assertTrue(py.endswith("python3") and cap, name)


class TestFunctional(unittest.TestCase):
    def test_golden_path_records_job_tasks_synthesis_and_telemetry(self):
        sub = [(sw.BYNAME["nova-core"], "check pg"), (sw.BYNAME["mac-mini"], "check inference")]
        job_id, psql, llm, out = _dispatch("audit the fleet", decompose=sub, synth="Fleet is healthy.")
        self.assertRegex(job_id, r"^swarm-[0-9a-f]{10}$")
        sqls = [c[0][0] for c in psql.call_args_list]
        self.assertIn(f"INSERT INTO swarm.jobs (job_id, description, status) VALUES ('{job_id}', 'audit the fleet', 'running');", sqls[0])
        self.assertIn(f"VALUES ('{job_id}', 'nova-core', 'check pg', 'running')", sqls[1])
        self.assertIn(f"VALUES ('{job_id}', 'mac-mini', 'check inference', 'running')", sqls[2])
        done = [s for s in sqls if "UPDATE swarm.tasks SET status='done'" in s]
        self.assertEqual(len(done), 2)
        self.assertIn("result='nova-core looked at: check pg', steps=2", done[0])
        self.assertIn(f"synthesis='Fleet is healthy.', finished_at=now() WHERE job_id='{job_id}'", sqls[-2])
        self.assertRegex(sqls[-1], rf"INSERT INTO telemetry\.events .*'Swarm job {job_id}: 2 agents, \d+s', 'audit the fleet'")
        self.assertEqual(llm.call_args[1]["model"], "conversation")
        self.assertIn("nova-core: nova-core looked at: check pg", llm.call_args[0][1])
        self.assertIn("=== SYNTHESIS ===\nFleet is healthy.", out)
        self.assertIn(f"job {job_id}: 2 subtasks fanned", out)

    def test_each_mode_fans_the_same_task_to_every_node(self):
        job_id, psql, _, out = _dispatch("assess health", fan_each=True)
        tasks = [c[0][0] for c in psql.call_args_list if "INSERT INTO swarm.tasks" in c[0][0]]
        self.assertEqual(len(tasks), len(sw.NODES))
        self.assertTrue(all("'assess health'" in t for t in tasks))
        self.assertIn(f"{len(sw.NODES)} subtasks fanned", out)

    def test_agent_error_is_recorded_not_fatal(self):
        def flaky(node, task):
            if node[0] == "nova-core":
                raise_free = {"node": "nova-core", "assessment": "ERROR: ssh timed out", "steps": 0, "secs": 240}
                return raise_free
            return _agent_result(node, task)
        job_id, psql, _, out = _dispatch("j", fan_each=True, run_agent=flaky)
        done = [c[0][0] for c in psql.call_args_list if "UPDATE swarm.tasks" in c[0][0]]
        self.assertTrue(any("result='ERROR: ssh timed out', steps=0" in s and "node='nova-core'" in s for s in done))
        self.assertIn("[nova-core]  0 tool-calls · 240s", out)


class TestFrame(unittest.TestCase):
    def test_import_never_dispatches_and_usage_exits_1(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        prelude = ("import urllib.request, sys, runpy\n"
                   "def _no(*a, **k): raise OSError('offline')\n"
                   "urllib.request.urlopen = _no\n")               # nova_router's import-time probe stays offline
        r = _REAL_RUN([sys.executable, "-c", prelude + "import nova_swarm; assert nova_swarm.ROUTER.endswith('/v1/chat/completions')"],
                      cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        r = _REAL_RUN([sys.executable, "-c", prelude + "sys.argv=['nova_swarm.py']; runpy.run_path('nova_swarm.py', run_name='__main__')"],
                      cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage: nova_swarm.py [--each]", r.stdout)


if __name__ == "__main__":
    unittest.main()
