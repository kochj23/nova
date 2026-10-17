#!/usr/bin/env python3
"""Tests for nova_minion_hub.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Focus: the 2026-10-17 cluster additions — every node's LLM job line
(llm_job: who sent the request, joined from the probes' connection tables) and its core purpose
(minion_nodes.purpose / alias / ip). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_minion_hub as hub  # noqa: E402

SRC = (SCRIPTS / "nova_minion_hub.py").read_text()

ROLE_ROWS = [  # node, role, state, fresh, purpose, alias, ip
    ("mac-studio", "minion", "dedicated", True, "Control plane · scheduler · Redis", "nova-core8", "192.168.1.6"),
    ("nova-core", "probe", "offline", False, "PG primary · app tier", "nova-core1", "192.168.1.2"),
    ("nova-core7", "probe", "offline", False, "PG standby (docker) · Ollama", None, "192.168.1.125"),
]


class FakeCur:
    """Answers the hub's queries by shape — offline, no PostgreSQL."""

    def __init__(self, roles=ROLE_ROWS):
        self.roles, self.sql, self.calls = roles, "", []

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, params))

    def fetchall(self):
        if "purpose, alias, ip" in self.sql:
            return list(self.roles)
        if "SELECT ip, node FROM minion_nodes" in self.sql:
            return [(r[6], r[0]) for r in self.roles if r[6]]
        return []

    def fetchone(self):
        s = self.sql
        if "SELECT state, since" in s:
            return None
        if "cardinality(belief_ids)" in s:
            return (0, 0, 0)
        if "audio_s" in s:
            return (0, 0.0)
        if "sum(lessons_kept),0) FROM work_lessons" in s:
            return (0, 0)
        return (0,)


def reset():
    for d in (hub._LLM, hub._JOBS, hub._LAST_JOB):
        d.clear()
    del hub._EVENTS[:]
    hub._IPS.update(t=0.0, map={})


def busy(model="qwen3:8b", decoded=240):
    return [{"port": 5, "model": model, "slots": [{"busy": True, "task": 1, "prompt": 900, "prompt_done": 900,
                                                    "decoded": decoded}]}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_tokens_or_passwords(self):
        self.assertIsNone(re.search(r"(?i)(password|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{12,}['\"]", SRC))

    def test_role_sql_is_parameterized(self):
        cur = FakeCur()
        hub.cluster(cur)
        q = next(p for s, p in cur.calls if "purpose, alias, ip" in s)
        self.assertEqual(q, (hub.FRESH_S,))
        self.assertNotRegex(SRC, r'execute\(f"')

    def test_probe_input_is_sanitized(self):
        u = hub.clean_users({"clients": [{"peer": "1.2.3.4:5", "proc": "<img src=x onerror=alert(1)>"},
                                         {"peer": "'; DROP TABLE x;--", "proc": "a"}] + [{"peer": "1.1.1.1:1"}] * 99,
                             "calls": [{"src": "1.2.3.4:5", "proc": "a\nb‮"}], "ips": ["x" * 99, "10.0.0.1"]})
        self.assertEqual(u["clients"][0]["proc"], "img srcx onerroralert1")
        self.assertLessEqual(len(u["clients"]), 32)
        self.assertNotIn("'; DROP", str(u))
        self.assertEqual(u["calls"][0]["proc"], "ab")
        self.assertEqual(u["ips"], ["10.0.0.1"])

    def test_cluster_never_echoes_raw_connection_tables(self):
        reset()
        hub.llm_push("mac-studio", {"node": "mac-studio", "runners": [], "loaded": [],
                                    "calls": [{"src": "192.168.1.6:1", "proc": "nova_x"}],
                                    "clients": [{"peer": "127.0.0.1:9", "proc": "curl"}]})
        n = hub.cluster(FakeCur())["nodes"][0]
        self.assertNotIn("calls", n)
        self.assertNotIn("clients", n)


class TestPerformance(unittest.TestCase):
    def test_attribute_10k_clients_fast(self):
        snaps = {f"n{i}": {"calls": [{"src": f"10.0.{i}.{j}:{k}", "proc": f"job{k}"} for j in range(10) for k in range(10)]}
                 for i in range(10)}
        clients = [{"peer": f"10.0.{i % 10}.{i % 7}:{i % 10}", "proc": None} for i in range(10000)]
        t = time.perf_counter()
        out = hub.attribute("x", clients, snaps, {})
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertTrue(out)

    def test_job_view_hot_path(self):
        t = time.perf_counter()
        for i in range(10000):
            hub.job_view(True, 1, True, 3, [], ["nova_journal @ mac-studio"], busy(), None, float(i))
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_db_ip_lookup_fails_open(self):
        # RETRY GAP: db_ips — a failed lookup keeps the previous map and retries after a minute, never raises
        reset()
        hub._IPS["map"] = {"192.168.1.6": "mac-studio"}
        cur = mock.Mock()
        cur.execute.side_effect = Exception("pg down")
        with mock.patch.object(hub, "log"):
            self.assertEqual(hub.db_ips(cur), {"192.168.1.6": "mac-studio"})
        self.assertGreater(hub._IPS["t"], 0)

    def test_push_survives_counter_failure(self):
        reset()
        cur = mock.Mock()
        cur.execute.side_effect = Exception("pg down")
        prev = {"node": "n", "runners": busy(decoded=10), "loaded": []}
        with mock.patch.object(hub, "log"):
            hub.llm_push("n", prev, cur)
            hub.llm_push("n", {"node": "n", "runners": [{"port": 5, "model": "qwen3:8b", "slots": [
                {"busy": False, "task": 2}]}], "loaded": []}, cur)
        self.assertIn("n", hub._LLM)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(hub.selftest(), 0)

    def test_job_line_states(self):
        self.assertEqual(hub.job_view(False, 3, True, 1, [], [], [], None, 0)["state"], "silent")
        self.assertEqual(hub.job_view(True, 1, False, 0, [], [], [], None, 0)["text"], "No LLM on this node")
        self.assertEqual(hub.job_view(True, 1, None, 2, [], [], [], None, 0)["text"], "Idle · 2 models loaded")
        jv = hub.job_view(True, 1, True, 1, [], [], busy(decoded=0), None, 0)
        self.assertEqual(jv["text"], "qwen3:8b · prefill 100% · caller not identified")

    def test_last_job_expires(self):
        jv = hub.job_view(True, 1, True, 1, [], [], [], ("x → m", 0.0), hub.LAST_JOB_MAX_S + 5)
        self.assertEqual(jv["text"], "Idle · 1 model loaded")

    def test_attribute_never_guesses(self):
        self.assertEqual(hub.attribute("a", [{"peer": "10.9.9.9:1", "proc": None}], {}, {}), ["client on 10.9.9.9"])
        self.assertEqual(hub.attribute("a", [{"peer": "10.9.9.9:1", "proc": None}], {}, {"10.9.9.9": "a"}),
                         ["local client"])

    def test_ago(self):
        self.assertEqual([hub._ago(x) for x in (5, 120, 7200, 200000)], ["5s", "2m", "2h", "2d"])


class TestIntegration(unittest.TestCase):
    def test_push_joins_caller_and_server(self):
        reset()
        hub.llm_push("mac-studio", {"node": "mac-studio", "runners": [], "loaded": [],
                                    "calls": [{"src": "192.168.1.6:5555", "dst": "192.168.1.125:11434", "proc": "nova_notifier"}],
                                    "ips": ["192.168.1.6"]}, None, "192.168.1.6")
        hub.llm_push("nova-core7", {"node": "nova-core7", "runners": busy(), "loaded": [{"name": "qwen3:8b"}],
                                    "clients": [{"peer": "192.168.1.6:5555", "port": 11434}]}, None, "192.168.1.125")
        self.assertIn("nova_notifier @ mac-studio", hub._JOBS["nova-core7"])
        self.assertEqual(hub._LAST_JOB["nova-core7"][0], "nova_notifier @ mac-studio → qwen3:8b")

    def test_cluster_carries_roles_and_job(self):
        reset()
        hub.llm_push("mac-studio", {"node": "mac-studio", "runners": [], "loaded": [], "llm": True})
        hub.llm_push("nova-core", {"node": "nova-core", "runners": [], "loaded": [], "llm": False})
        out = hub.cluster(FakeCur())
        by = {n["node"]: n for n in out["nodes"]}
        self.assertEqual(by["mac-studio"]["display_name"], "mac-studio (nova-core8)")
        self.assertEqual(by["mac-studio"]["purpose"], "Control plane · scheduler · Redis")
        self.assertEqual(by["nova-core"]["llm_job"]["text"], "No LLM on this node")
        self.assertEqual(by["mac-studio"]["llm_job"]["state"], "idle")


class TestFunctional(unittest.TestCase):
    def test_every_node_has_both_lines(self):
        reset()
        for n in ("mac-studio", "nova-core", "nova-core7"):
            hub.llm_push(n, {"node": n, "runners": busy() if n == "nova-core7" else [], "loaded": [], "llm": n != "nova-core",
                             "clients": [{"peer": "192.168.1.6:7", "port": 11434}] if n == "nova-core7" else [],
                             "calls": [{"src": "192.168.1.6:7", "proc": "nova_journal"}] if n == "mac-studio" else []})
        hub.llm_push("nova-core7", {"node": "nova-core7", "runners": busy(), "loaded": [], "llm": True,
                                    "clients": [{"peer": "192.168.1.6:7", "port": 11434}]})
        out = hub.cluster(FakeCur())
        self.assertEqual(len(out["nodes"]), 3)
        for n in out["nodes"]:
            self.assertTrue(n["llm_job"]["text"])
            self.assertTrue(n["purpose"])
        core7 = next(n for n in out["nodes"] if n["node"] == "nova-core7")
        self.assertEqual(core7["llm_job"]["text"], "nova_journal @ mac-studio → qwen3:8b · generating 240 tok")

    def test_job_lingers_then_goes_idle(self):
        reset()
        hub.note_jobs("n", ["nova_x"], "m", 100.0)
        hub.note_jobs("n", [], None, 100.0 + hub.JOB_LINGER_S + 1)
        self.assertEqual(hub._JOBS["n"], {})
        self.assertEqual(hub._LAST_JOB["n"][0], "nova_x → m")

    def test_bad_snapshot_is_rejected_not_crashing(self):
        h = hub.Handler.__new__(hub.Handler)
        h.path, h.headers = "/llm", {"Content-Length": "3"}
        h.rfile = mock.Mock(read=mock.Mock(return_value=b"{x}"))
        sent = []
        h._send = lambda code, body: sent.append(code)
        with mock.patch.object(hub, "log"), mock.patch.object(hub, "db"):
            h.do_POST()
        self.assertEqual(sent, [400])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_minion_hub.py"), "--selftest"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_serve(self):
        self.assertTrue(callable(hub.main))
        self.assertEqual(hub._LLM.__class__, dict)


if __name__ == "__main__":
    unittest.main()
