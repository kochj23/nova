#!/usr/bin/env python3
"""Tests for nova_goblin_map.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import re
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_goblin_map.py"
spec = importlib.util.spec_from_file_location("goblin_under_test", SCRIPT)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
C = G.C
SRC = SCRIPT.read_text()
SENTINEL = "SENTINEL-SECRET-VALUE-77c1"


def _probe(fs="network", root=True, procs=()):
    return {"sudo_rules": ["(ALL) NOPASSWD: ALL"] if root else [], "journal_start": None, "sudo_use": {},
            "procs": list(procs), "scripts_fs": fs, "complete": True}


def _ha_dir(tmp: Path) -> Path:
    st = tmp / ".storage"
    st.mkdir(parents=True, exist_ok=True)
    (st / "core.entity_registry").write_text(json.dumps({"data": {"entities": [
        {"entity_id": "light.a", "platform": "hue"}, {"entity_id": "switch.b", "platform": "lutron_caseta"},
        {"entity_id": "sensor.c", "platform": "x"}, {"entity_id": "camera.d", "platform": "generic"},
        {"entity_id": "light.off", "platform": "hue", "disabled_by": "user"}]}}))
    (st / "core.device_registry").write_text(json.dumps({"data": {"devices": [{"id": "1"}, {"id": "2"}]}}))
    (st / "core.config_entries").write_text(json.dumps({"data": {"entries": [
        {"domain": "unifi", "data": {"host": "10.9.8.7", "password": SENTINEL, "api_key": SENTINEL}},
        {"domain": "mobile_app", "data": {"token": SENTINEL}}]}}))
    return tmp


class Cur:
    """Fake cursor answering the controller queries."""
    def __init__(self):
        self.q, self.last = [], ""

    def execute(self, sql, args=None):
        self.q.append(sql)
        self.last = sql

    def fetchall(self):
        s = self.last
        if "GROUP BY 1" in s and "net_inventory" in s:
            return [("infra", 20), ("camera", 30)]
        if "to_regclass" in s:
            return [(None,)]
        if "homekit_outlets" in s:
            return [(31,)]
        if "unifi_client_state" in s:
            return [(100,)]
        if "hue_light_state" in s:
            return [(33,)]
        if "tier='camera'" in s:
            return [(30,)]
        if "node_status" in s:
            return [(3,)]
        if "outbound_ledger" in s:
            return [(1,)]
        return []

    def fetchone(self):
        r = self.fetchall()
        return r[0] if r else None


def _ctx(tmp: Path, probes=None, **over):
    idx = {"nova_task_a": "import nova_lib\nrequests.post('/api/services/light/turn_on')\n",
           "nova_lib": "from nova_notify import notify\nget('nova-tok-1')\nhost='nova-core'\n",
           "nova_task_b": "smtplib\nget('nova-tok-2')\n",
           "nova_gateway.tools": "x", "nova_gateway.channels.slack": "chat.postMessage",
           "nova_gateway_v2": "import nova_gateway\n"}
    cfg = _ha_dir(tmp)
    ctx = {"probes": probes if probes is not None else
           {"mac-studio": _probe(procs=["nova_gateway_v2.py"]), "nova-core": _probe(), "nas-less": _probe("local", False)},
           "index": idx, "graph": C.import_graph(idx), "refs": C.name_refs({"nova-tok-1", "nova-tok-2"}, idx),
           "fleet": {"nova-tok-1": None, "nova-tok-2": None, "nova-tok-3": None}, "keychain": {"nova-tok-4": None},
           "nodes": ["mac-studio", "nova-core", "nas-less"],
           "ha": {k: C.ha_storage(cfg, f"core.{k}") for k in ("entity_registry", "device_registry", "config_entries")},
           "schedulers": {"scheduler_studio": (["nova_task_a.py"], "mac-studio"),
                          "scheduler_core": (["nova_task_b.py", "missing.py"], "nova-core")},
           "gateway": (["read", "act1", "act2"], {"read"}), "claude": {"mcp": ["m"], "allow": ["Bash(ls:*)"]},
           "cur": Cur()}
    ctx.update(over)
    return ctx


class Security(unittest.TestCase):
    def test_never_reads_a_secret_value_in_source(self):
        self.assertNotIn("find-generic-password", SRC)
        self.assertNotRegex(SRC, r"\bget_secret\(|\bvault_secret\(|nova_secrets|ciphertext")
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"/Users/|192\.168\.|\b\d{1,3}(?:\.\d{1,3}){3}\b")

    def test_ha_registry_parse_drops_integration_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d))
        blob = json.dumps(ctx["ha"])
        self.assertNotIn(SENTINEL, blob)
        self.assertNotIn("10.9.8.7", blob)
        self.assertNotIn("password", blob)

    def test_no_secret_value_or_ip_reaches_output(self):
        with tempfile.TemporaryDirectory() as d:
            rows = G.compare(G.build(_ctx(Path(d))), {})
        out = G.render(rows, G.measures(rows, 100), 100) + json.dumps(rows, default=str)
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn("nova-tok-1", out)                 # secrets are counted, never named
        self.assertNotRegex(out, r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


class Performance(unittest.TestCase):
    def test_build_and_score_are_fast(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d))
            t = time.monotonic()
            for _ in range(200):
                G.compare(G.build(ctx), {"nas": (10.0, {})})
        self.assertLess(time.monotonic() - t, 10.0)

    def test_reach_of_many_modules(self):
        idx = {f"nova_m{i}": f"import nova_m{(i + 1) % 800}\nget('nova-s-{i % 50}')\n" for i in range(800)}
        refs = C.name_refs({f"nova-s-{i}" for i in range(50)}, idx)
        t = time.monotonic()
        r = G.reach_of_modules(set(list(idx)[:50]), idx, C.import_graph(idx), refs, ["a", "b"])
        self.assertLess(time.monotonic() - t, 20.0)
        self.assertEqual(r["secrets"], 50)


class Retry(unittest.TestCase):
    def test_failing_controller_is_unknown_not_a_crash(self):
        with redirect_stdout(io.StringIO()):
            rows = G.build({}, {"ok": lambda c: {"hosts": 1}, "boom": lambda c: {}["x"]})
        boom = [r for r in rows if r["controller"] == "boom"][0]
        self.assertEqual(boom["score"], 0)
        self.assertEqual(boom["reach"]["unknown"], ["all"])

    def test_unreadable_sources_are_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), probes={"a": None, "b": None},
                       ha={"entity_registry": None, "device_registry": None, "config_entries": None},
                       schedulers={"scheduler_studio": (None, "a"), "scheduler_core": (None, "b")},
                       fleet=None, keychain=None, gateway=(None, set()))
            with redirect_stdout(io.StringIO()):
                rows = {r["controller"]: r for r in G.build(ctx)}
        self.assertIn("tasks", rows["scheduler_studio"]["reach"]["unknown"])
        self.assertIn("hosts", rows["nas"]["reach"]["unknown"])
        self.assertIn("secrets", rows["pg_primary"]["reach"]["unknown"])
        self.assertIn("tools", rows["nova_gateway"]["reach"]["unknown"])

    def test_scheduler_yaml_missing_returns_none(self):
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(G.scheduler_tasks(Path("/nonexistent/scheduler.yaml")))

    def test_fleet_probe_retries_through_the_cellar(self):
        with mock.patch.object(C, "_run_probe", side_effect=[None, _probe()]):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(C.probe_host("n", "host.invalid", False, _sleep=lambda s: None))


class Unit(unittest.TestCase):
    def test_score_and_unknowns(self):
        self.assertEqual(G.score({"hosts": 2, "root_hosts": 1, "secrets": None, "source": "x"}), 35)
        self.assertEqual(G.unknowns({"hosts": None, "cameras": 1, "source": None}), ["hosts"])

    def test_compare_thresholds(self):
        rows = [{"controller": "a", "reach": {"hosts": 12}, "score": 120.0},
                {"controller": "b", "reach": {"hosts": 2}, "score": 20.0},
                {"controller": "c", "reach": {"hosts": 2}, "score": 1.5}]
        G.compare(rows, {"a": (100.0, {"hosts": 10}), "b": (19.0, {"hosts": 2}), "c": (1.0, {})})
        self.assertTrue(rows[0]["grew"])
        self.assertFalse(rows[1]["grew"])            # +5% only
        self.assertFalse(rows[2]["grew"])            # +50% but under MIN_DELTA points
        self.assertEqual(rows[0]["grew_metrics"], ["hosts"])

    def test_root_ok_and_suggestion(self):
        self.assertTrue(G.root_ok(_probe()))
        self.assertFalse(G.root_ok(_probe(root=False)))
        self.assertIsNone(G.root_ok(None))
        s = G.suggestion({"controller": "claude_ssh", "reach": {"root_hosts": 4}})
        self.assertIn("4 reachable hosts", s)
        self.assertIn("?", G.suggestion({"controller": "nas", "reach": {}}))


class Integration(unittest.TestCase):
    def _run(self, prev, dry=False):
        cur = Cur()
        conn = mock.Mock(cursor=lambda: cur)
        notify = mock.Mock(return_value=True)
        fake_notify = types.SimpleNamespace(notify=notify)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(G.W, "connect", return_value=conn), \
                mock.patch.object(G, "gather", return_value=_ctx(Path(d))), \
                mock.patch.object(G, "previous", return_value=prev), \
                mock.patch.dict(sys.modules, {"nova_notify": fake_notify}), \
                mock.patch("nova_pg_schema.safe_ddl") as ddl, redirect_stdout(io.StringIO()):
            rows = G.run(dry=dry)
        return rows, cur, notify, ddl

    def test_first_snapshot_stores_and_stays_quiet(self):
        rows, cur, notify, ddl = self._run({})
        self.assertEqual(sum("INSERT INTO goblin_map" in q for q in cur.q), len(G.CONTROLLERS))
        self.assertTrue(any("service_config" in q for q in cur.q))
        notify.assert_not_called()
        ddl.assert_called_once()

    def test_growth_notifies(self):
        rows, cur, notify, _ = self._run({"home_assistant": (1.0, {"actuators": 0})})
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["level"], "warning")
        self.assertIn("home_assistant", notify.call_args.kwargs["body"])

    def test_dry_run_writes_nothing(self):
        rows, cur, notify, ddl = self._run({"home_assistant": (1.0, {})}, dry=True)
        self.assertFalse([q for q in cur.q if re.search(r"\b(INSERT|UPDATE|DELETE|CREATE)\b", q)])
        notify.assert_not_called()
        ddl.assert_not_called()


class Functional(unittest.TestCase):
    def test_reach_from_inventories(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                rows = {r["controller"]: r["reach"] for r in G.build(_ctx(Path(d)))}
        ha = rows["home_assistant"]
        self.assertEqual((ha["actuators"], ha["cameras"], ha["devices"], ha["secrets"], ha["outbound"]), (2, 1, 2, 2, 1))
        self.assertEqual(rows["lutron"]["actuators"], 1)
        st = rows["scheduler_studio"]
        self.assertEqual((st["tasks"], st["secrets"], st["acting_tools"], st["root_hosts"]), (1, 1, 1, 1))
        self.assertEqual(rows["scheduler_core"]["tasks"], 2)
        self.assertEqual((rows["nas"]["hosts"], rows["nas"]["root_hosts"], rows["nas"]["tasks"]), (2, 2, 3))
        self.assertEqual(rows["claude_ssh"]["root_hosts"], 2)
        self.assertEqual(rows["claude_ssh"]["secrets"], 4)
        gw = rows["nova_gateway"]
        self.assertEqual((gw["tools"], gw["acting_tools"], gw["hosts"]), (3, 2, 1))
        self.assertEqual((rows["unifi_controller"]["devices"], rows["unifi_controller"]["actuators"]), (100, 20))

    def test_render_has_top3_and_measures(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                rows = G.compare(G.build(_ctx(Path(d))), {})
        m = G.measures(rows, 50)
        txt = G.render(rows, m, 50)
        self.assertIn("first snapshot", txt)
        self.assertEqual(len(re.findall(r"^  [123]\. ", txt, re.M)), 3)
        self.assertEqual(m["top"], rows[0]["controller"])
        self.assertEqual(m["above_threshold"], sum(r["score"] >= 50 for r in rows))


class Frame(unittest.TestCase):
    def test_cli_flags(self):
        for f in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(f'"{f}"', SRC)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            G.main(["--help"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(G.main(["--selftest"]), 0)

    def test_docstring_house_style(self):
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("Green Goblin", SRC)
        self.assertEqual(set(G.CONTROLLERS), {"home_assistant", "scheduler_studio", "scheduler_core", "nova_gateway",
                                              "homekit_bridge", "unifi_controller", "lutron", "hue_bridge", "frigate",
                                              "pg_primary", "nas", "claude_ssh"})
        self.assertTrue(set(G.SUGGEST) <= set(G.CONTROLLERS))

    def test_read_only_never_edits_config_or_acts(self):
        self.assertNotRegex(SRC, r"\.write_text\(|open\([^)]*['\"]w|systemctl |launchctl |yaml\.(safe_)?dump")


if __name__ == "__main__":
    unittest.main()
