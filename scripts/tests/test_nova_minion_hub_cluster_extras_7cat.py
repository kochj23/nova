#!/usr/bin/env python3
"""7-category tests for Nova Cluster 1.1.0's hub/probe additions (2026-10-19): the read-only extras the hub
serves in /cluster — UPS from NUT (service_config service='ups'), scheduler alerts (both schedulers' /tasks,
scheduler_runs fallback, leader_events), the "just published" ticker (journal git history), per-node recent
jobs — plus probe 1.2's link/Wi-Fi backdoor/temperatures and nova_leader_wrapper's takeover record.

Offline: PostgreSQL, upsd, the schedulers and git are faked or run locally (a socket server, a temp repo);
nothing touches the real fleet. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "cluster"))

import nova_minion_hub as hub  # noqa: E402
import nova_llm_probe as probe  # noqa: E402

NUT = ('BEGIN LIST VAR bedroom\nVAR bedroom ups.load "18"\nVAR bedroom ups.status "OL"\n'
       'VAR bedroom battery.charge "100"\nVAR bedroom battery.runtime "1764"\nVAR bedroom ups.realpower.nominal "900"\n'
       'VAR bedroom ups.model "Back-UPS BGM1500-US "\nEND LIST VAR bedroom\n')
TASKS = {"a": {"enabled": True, "consecutive_failures": 12}, "b": {"enabled": True, "consecutive_failures": 3},
         "off": {"enabled": False, "consecutive_failures": 99}, "ok": {"enabled": True, "consecutive_failures": 2},
         "q": {"enabled": True, "consecutive_failures": 0, "quarantined": True}}


class Cur:
    """Answers the extras' queries by shape."""

    def __init__(self, leader="nova-core", events=(), ups_rows=(), jobs=(), roles=()):
        self.leader, self.events, self.ups_rows, self.jobs, self.roles = leader, list(events), list(ups_rows), list(jobs), list(roles)
        self.sql, self.calls = "", []

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, params))

    def fetchone(self):
        if "FROM leader_standby" in self.sql:
            return (self.leader,) if self.leader else None
        if "cardinality(belief_ids)" in self.sql:
            return (0, 0, 0)
        if "audio_s" in self.sql:
            return (0, 0.0)
        if "sum(lessons_kept),0) FROM work_lessons" in self.sql:
            return (0, 0)
        return (0,)

    def fetchall(self):
        if "FROM leader_events" in self.sql:
            return self.events
        if "FROM service_config" in self.sql:
            return self.ups_rows
        if "FROM work_lessons WHERE status = 'done' AND host" in self.sql:
            return self.jobs
        if "purpose, alias, ip" in self.sql:
            return self.roles
        return []


def reset():
    for d in (hub._LLM, hub._JOBS, hub._LAST_JOB, hub._RECENT_LLM):
        d.clear()
    del hub._EVENTS[:]
    hub._EXTRAS.update(ups=[], recent_jobs={}, alerts={"items": [], "counts": {}}, published=[])


def fake_upsd(reply: str) -> int:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        c.recv(200)
        c.sendall(reply.encode())
        c.close()
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def git_repo(tmp: str) -> Path:
    r = Path(tmp)
    (r / "content" / "belief").mkdir(parents=True)
    (r / "hugo.yaml").write_text("menu:\n  main:\n    - identifier: belief\n      name: Belief and Friends\n")
    run = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (r / "content" / "belief" / "_index.md").write_text("---\ntitle: Belief\n---\n")
    (r / "content" / "belief" / "2026-10-19-a.md").write_text('---\ntitle: "🔍 Belief and Friends: A"\n---\nbody')
    run("add", "-A"); run("commit", "-qm", "a")
    (r / "content" / "belief" / "2026-10-19-b.md").write_text('---\ntitle: "Holy Days"\n---\nbody')
    run("add", "-A"); run("commit", "-qm", "b")
    return r


class TestSecurity(unittest.TestCase):
    def setUp(self):
        reset()

    def test_ups_names_cannot_smuggle_protocol_commands(self):
        for bad in ("bedroom\nFSD bedroom", "../x", "", "a b"):
            with self.assertRaises(ValueError):
                hub.nut_vars("127.0.0.1", 1, bad)

    def test_nut_values_are_capped_data(self):
        raw = hub.parse_nut('VAR u ups.status "' + "X" * 500 + '"\nVAR u ups.serial "secret"\n', "u")
        self.assertEqual(len(raw["ups.status"]), 64)
        self.assertNotIn("ups.serial", raw)                    # only the vars Nova shows

    def test_scheduler_text_is_capped(self):
        al = hub.classify_tasks("s", {"x" * 500: {"enabled": True, "consecutive_failures": 4}}, {"x" * 500: "y" * 900})
        self.assertLessEqual(len(al[0]["task"]), 80)
        self.assertLessEqual(len(al[0]["reason"]), 140)

    def test_published_paths_outside_content_are_ignored(self):
        self.assertEqual(hub.parse_added("@@1\n../../etc/passwd.md\ncontent/a/b/c.md\ncontent/x/y.md\n"), [(1, "x", "y")])

    def test_core_scheduler_url_needs_a_valid_node_name(self):
        seen = []
        with mock.patch.object(hub, "_get_json", side_effect=lambda u: seen.append(u) or {}):
            hub.collect_alerts(Cur(leader="evil.example/x"))
        self.assertTrue(all("evil" not in u for u in seen))

    def test_probe_net_reports_no_secrets(self):
        out = probe.parse_ifconfig("en1: flags\n\tssid MyNet\n\tinet 192.168.1.5 netmask 0xff\n\tstatus: active\n")
        self.assertEqual(set(out), {"up", "speed_mbps", "ip"})


class TestPerformance(unittest.TestCase):
    def setUp(self):
        reset()

    def test_cluster_never_collects_inline(self):
        with mock.patch.object(hub, "collect_ups", side_effect=AssertionError("inline")), \
             mock.patch.object(hub, "collect_alerts", side_effect=AssertionError("inline")), \
             mock.patch.object(hub, "collect_published", side_effect=AssertionError("inline")), \
             mock.patch.object(hub, "fleet", return_value={"minions": []}):
            hub.cluster(Cur())

    def test_alerts_list_is_capped(self):
        many = {f"t{i}": {"enabled": True, "consecutive_failures": 5} for i in range(300)}
        with mock.patch.object(hub, "_get_json", return_value=many):
            self.assertLessEqual(len(hub.collect_alerts(Cur())["items"]), 24)

    def test_published_is_bounded(self):
        lg = "".join(f"@@{i}\ncontent/nova/p{i}.md\n" for i in range(500, 0, -1))
        with mock.patch.object(hub.subprocess, "run", return_value=mock.Mock(stdout=lg)), \
             mock.patch("builtins.open", mock.mock_open(read_data='title: "T"\n')):
            self.assertEqual(len(hub.collect_published()), 12)

    def test_collectors_respect_their_intervals(self):
        calls = mock.Mock(return_value=[])
        hub._EXTRAS.pop("_t_x", None)
        with mock.patch.object(hub, "COLLECTORS", [("x", calls, 60)]), mock.patch.object(hub, "db", return_value=Cur()):
            hub.refresh_extras(); hub.refresh_extras(); hub.refresh_extras()
        self.assertEqual(calls.call_count, 1)

    def test_probe_link_and_temps_are_cached(self):
        probe._NET.update(t=time.time(), v={"default": "x"})
        with mock.patch.object(probe, "sh", side_effect=AssertionError("re-read")):
            self.assertEqual(probe.net_info(), {"default": "x"})
        probe._NET.update(t=0.0, v=None)


class TestRetry(unittest.TestCase):
    def setUp(self):
        reset()

    def test_unreachable_ups_is_reported_not_raised(self):
        out = hub.collect_ups(Cur(ups_rows=[("mb", {"host": "127.0.0.1", "port": 1, "ups": "bedroom", "label": "MB"})]))
        self.assertFalse(out[0]["ok"])
        self.assertTrue(out[0]["error"])

    def test_unreachable_scheduler_is_an_alert(self):
        with mock.patch.object(hub, "_get_json", side_effect=OSError("down")):
            out = hub.collect_alerts(Cur())
        self.assertEqual({a["kind"] for a in out["items"]}, {"unreachable"})

    def test_core_scheduler_down_falls_back_to_scheduler_runs(self):
        def get(url):
            if "37464" in url:
                raise OSError("down")
            return {}
        with mock.patch.object(hub, "_get_json", side_effect=get), \
             mock.patch.object(hub, "_failing_from_runs", return_value={"x": {"enabled": True, "consecutive_failures": 4}}):
            out = hub.collect_alerts(Cur())
        self.assertIn("x", [a.get("task") for a in out["items"]])

    def test_refresher_keeps_last_value_on_failure(self):
        hub._EXTRAS["ups"] = [{"key": "old"}]
        with mock.patch.object(hub, "COLLECTORS", [("ups", mock.Mock(side_effect=RuntimeError("boom")), 0)]), \
             mock.patch.object(hub, "db", return_value=Cur()):
            hub.refresh_extras(force=True)
        self.assertEqual(hub._EXTRAS["ups"], [{"key": "old"}])

    def test_moved_post_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = git_repo(tmp)
            (r / "content" / "belief" / "2026-10-19-b.md").unlink()
            with mock.patch.object(hub, "JOURNAL", str(r)):
                self.assertEqual([p["title"] for p in hub.collect_published()], ["Belief and Friends: A"])


class TestUnit(unittest.TestCase):
    def setUp(self):
        reset()

    def test_ups_view(self):
        u = hub.ups_view("mb", {"label": "Master Bedroom", "powers": ["nova-core4", 5]}, hub.parse_nut(NUT, "bedroom"), None)
        self.assertEqual((u["load_pct"], u["watts"], u["charge_pct"], u["runtime_s"], u["model"]),
                         (18.0, 162, 100.0, 1764.0, "Back-UPS BGM1500-US"))
        self.assertTrue(u["online"] and not u["on_battery"])
        self.assertEqual(u["powers"], ["nova-core4"])

    def test_classify_tasks(self):
        al = hub.classify_tasks("studio", TASKS)
        self.assertEqual([(a["task"], a["kind"]) for a in al], [("a", "dead-letter"), ("b", "failing"), ("q", "quarantined")])

    def test_leader_items(self):
        ev = [("nova-core3", datetime(2026, 10, 18, 23, 36, tzinfo=timezone.utc), "failover")]
        li = hub.leader_items("nova-core3", ev, datetime.now(timezone.utc))
        self.assertEqual([x["kind"] for x in li], ["failover", "leader"])

    def test_recent_jobs_merge(self):
        j = hub.recent_jobs_for("n", [{"t": 100.0, "at": "a", "kind": "reading", "text": "Dune"}],
                                [(200.0, "nova_journal → qwen3:8b"), (50.0, "x")])
        self.assertEqual([x["kind"] for x in j], ["llm", "reading", "llm"])

    def test_post_view_and_menu(self):
        p = hub.post_view(1, "nova", "s", "🪞 Nobody Ever Asked HAL", {})
        self.assertEqual((p["emoji"], p["title"]), ("🪞", "Nobody Ever Asked HAL"))
        self.assertEqual(hub.menu_names("    - identifier: belief\n      name: Belief and Friends\n"), {"belief": "Belief and Friends"})

    def test_probe_parsers(self):
        self.assertEqual(probe.parse_media_speed("autoselect (2500Base-T <full-duplex>)"), 2500)
        self.assertEqual(probe.pick_temps([("k10temp", "Tctl", 58.0), ("eno1", "PHY", 0.0)]), {"cpu": 58.0})


class TestIntegration(unittest.TestCase):
    def setUp(self):
        reset()

    def test_reads_a_live_upsd_socket(self):
        port = fake_upsd(NUT)
        out = hub.collect_ups(Cur(ups_rows=[("mb", {"host": "127.0.0.1", "port": port, "ups": "bedroom", "label": "MB"})]))
        self.assertTrue(out[0]["ok"])
        self.assertEqual(out[0]["load_pct"], 18.0)

    def test_reads_a_real_git_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = git_repo(tmp)
            with mock.patch.object(hub, "JOURNAL", str(r)):
                out = hub.collect_published()
        self.assertEqual([p["title"] for p in out], ["Holy Days", "Belief and Friends: A"])
        self.assertEqual(out[0]["section_name"], "Belief and Friends")

    def test_both_schedulers_by_dns_name(self):
        seen = []
        with mock.patch.object(hub, "_get_json", side_effect=lambda u: seen.append(u) or TASKS):
            hub.collect_alerts(Cur(leader="nova-core3"))
        self.assertIn("http://localhost:37460/tasks", seen)
        self.assertIn("http://nova-core3.digitalnoise.net:37464/tasks", seen)

    def test_wrapper_records_takeovers(self):
        spec = importlib.util.spec_from_file_location("wrapper", SCRIPTS / "nova_leader_wrapper.py")
        w = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"psycopg2": mock.Mock()}):
            spec.loader.exec_module(w)
        self.assertIn("leader_events", w.DDL)
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        child = mock.Mock(pid=42)
        child.poll.return_value = 0
        child.returncode = 0
        with mock.patch.object(w.subprocess, "Popen", return_value=child), mock.patch.object(w.signal, "signal"):
            w.run_leader(conn, "nova-scheduler-core", ["true"])
        self.assertTrue(any("leader_events" in str(c.args[0]) for c in cur.execute.call_args_list))


class TestFunctional(unittest.TestCase):
    def setUp(self):
        reset()

    def test_cluster_payload_carries_every_extra(self):
        now = time.time()
        hub._LLM["nova-core7"] = {"snap": {"node": "nova-core7", "net": {"eth": {"iface": "eno1", "up": True, "speed_mbps": 1000},
                                                                          "wifi": {"iface": "wlp194s0", "up": True, "ip": "192.168.1.197"}},
                                           "temps": {"cpu": 50.4}, "runners": [], "loaded": []},
                                  "t": now, "tok_s": 0, "prefill_tok_s": 0}
        hub.note_jobs("nova-core7", ["nova_journal @ mac-studio"], "qwen3:8b", now)
        hub._EXTRAS.update(ups=[{"key": "mb"}], alerts={"items": [{"level": "warn"}], "counts": {"warn": 1}},
                           published=[{"title": "T"}],
                           recent_jobs={"nova-core7": [{"t": now - 60, "at": "x", "kind": "transcribing", "text": "Cannon"}]})
        with mock.patch.object(hub, "fleet", return_value={"minions": []}):
            out = hub.cluster(Cur())
        n = next(x for x in out["nodes"] if x["node"] == "nova-core7")
        self.assertEqual(n["net"]["wifi"]["ip"], "192.168.1.197")
        self.assertEqual(n["temps"], {"cpu": 50.4})
        self.assertEqual([j["kind"] for j in n["recent_jobs"]], ["llm", "transcribing"])
        self.assertEqual((out["ups"], out["published"]), ([{"key": "mb"}], [{"title": "T"}]))
        self.assertEqual(out["alerts"]["counts"], {"warn": 1})
        json.dumps(out)

    def test_alerts_order_bad_warn_info(self):
        ev = [("nova-core", datetime(2026, 10, 20, 0, 59, tzinfo=timezone.utc), "back")]
        with mock.patch.object(hub, "_get_json", return_value=TASKS):
            out = hub.collect_alerts(Cur(events=ev))
        levels = [a["level"] for a in out["items"]]
        self.assertEqual(levels, sorted(levels, key={"bad": 0, "warn": 1, "info": 2}.get))

    def test_llm_job_log_dedupes_and_caps(self):
        for i in range(30):
            hub.note_jobs("n", [f"j{i}"], None, 1000.0 + i)
        self.assertEqual(len(hub._RECENT_LLM["n"]), 10)
        hub.note_jobs("n", ["j29"], None, 1030.0)
        self.assertEqual(len(hub._RECENT_LLM["n"]), 10)


class TestFrame(unittest.TestCase):
    def test_hub_selftest_passes(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_minion_hub.py"), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_probe_selftest_passes_on_stdlib_python(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "cluster" / "nova_llm_probe.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_serve_starts_the_extras_thread(self):
        src = (SCRIPTS / "nova_minion_hub.py").read_text()
        self.assertIn('threading.Thread(target=extras_loop, name="extras", daemon=True).start()', src)

    def test_no_leader_is_itself_an_alert(self):
        with mock.patch.object(hub, "_get_json", return_value={}):
            out = hub.collect_alerts(Cur(leader=None))
        self.assertEqual(out["items"][0]["kind"], "no-leader")

    def test_deploy_script_uses_dns_not_ip(self):
        src = (SCRIPTS / "cluster" / "deploy_probe.sh").read_text()
        self.assertIn("nova-core8.digitalnoise.net", src)
        self.assertNotIn("getifaddr", src)


if __name__ == "__main__":
    unittest.main()
