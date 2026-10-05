#!/usr/bin/env python3
"""Tests for nova_house_facts.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hf = _load("hf", SCRIPTS / "nova_house_facts.py")
SRC = (SCRIPTS / "nova_house_facts.py").read_text()
SEEN = datetime(2026, 10, 5, 9, 30)

DEVICES = [{"friendly_name": "Coordinator", "type": "Coordinator"},
           {"friendly_name": "master_bedroom_plug", "type": "Router", "ieee_address": "0x00124b00",
            "definition": {"model": "TS011F"}, "manufacturer": "Tuya", "software_build_id": "1.0.5", "date_code": "20230901"},
           {"friendly_name": "garage_sensor", "type": "EndDevice", "model_id": "SNZB-02"},
           {"type": "Router"}]                                          # no name -> skipped
STATES = [("zigbee2mqtt/master_bedroom_plug", {"linkquality": 120, "update": {"installed_version": 5, "latest_version": 6}}),
          ("zigbee2mqtt/bridge/state", {"state": "online"}),            # bridge topics are not devices
          ("zigbee2mqtt/garage_sensor", "not-a-dict")]


def _mqtt_stdout(topic):
    if topic == "zigbee2mqtt/bridge/devices":
        return f"{topic} {json.dumps(DEVICES)}\n"
    return "".join(f"{t} {json.dumps(p)}\n" for t, p in STATES) + "zigbee2mqtt/broken {not json\n"


class _Run:
    def __init__(self): self.calls = []

    def __call__(self, argv, **k):
        self.calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=_mqtt_stdout(argv[argv.index("-t") + 1]), stderr="")


class _Cur:
    def __init__(self, answers=(), fail=None):
        self.answers = list(answers); self.sql = []; self.params = []; self.many = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail and (self.fail is True or self.fail in sql):
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def executemany(self, sql, seq):
        self.many.append((" ".join(sql.split()), list(seq)))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


class _Conn:
    def __init__(self, cur): self.cur = cur; self.autocommit = False

    def cursor(self): return self.cur


PG_ANSWERS = [[("sensor.master_bedroom_temp", "Master Bedroom")],                                   # ha_sensors areas
              [("nova-core4", "192.168.1.250", "aa:bb", "sw1/7", "2026-10-05 08:00"), ("ghost", None, None, None, None)],
              [("nova-hue", "nova-core4", "192.168.1.250", 8611, "ok", "2026-10-05 09:00"),
               ("nova-soil", "mac-studio", "192.168.1.6", 8700, None, None)]]


def _ha_stub(states):
    m = types.ModuleType("nova_ha_metrics"); m.ha_get_states = lambda: states
    return patch.dict(sys.modules, {"nova_ha_metrics": m})


HA_STATES = [{"entity_id": "update.slzb_06_firmware", "state": "on",
              "attributes": {"installed_version": "2.3.6", "latest_version": "2.5.2", "title": "SLZB-06"}},
             {"entity_id": "sensor.temp", "state": "21", "attributes": {}}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)                      # the HA token lives in nova_ha_metrics/Keychain

    def test_sql_is_parameterized_and_writes_only_the_ledger(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn('executemany(f"', SRC)
        self.assertEqual(re.findall(r"\b(?<!DO )(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC), ["house_facts"])
        self.assertIn("VALUES (%s,%s,%s,%s,now())", SRC)

    def test_question_text_never_reaches_sql(self):
        cur = _Cur([[("master_bedroom_plug", "firmware", "1.0", SEEN)]])
        hf.lookup_sql(cur, "zigbee'; DROP TABLE house_facts; --")
        self.assertEqual(len(cur.sql), 1)
        self.assertNotIn("DROP", cur.sql[0])
        self.assertIsNone(cur.params[0])
        self.assertEqual(hf.tokens("x'; DROP TABLE --"), ["drop", "table"])    # regex-tokenised, lowercased

    def test_mqtt_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        run = _Run()
        with patch.object(hf.subprocess, "run", run):
            hf._mqtt("zigbee2mqtt/+; id", 1, 1)
        self.assertEqual(run.calls[0][0], "mosquitto_sub")
        self.assertIn("zigbee2mqtt/+; id", run.calls[0])            # one argv element, never interpreted


class TestPerformance(unittest.TestCase):
    def test_scoring_fast_on_a_10k_entity_ledger(self):
        rows = [(f"room{i % 40}_device{i:05d}", "firmware", str(i), SEEN) for i in range(10_000)]
        t0 = time.perf_counter()
        hits = hf.lookup_sql(_Cur([rows]), "What firmware does the room7 device00007 run?")
        for i in range(10_000):
            hf.score(f"room{i}_device", ["room", "device"])
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(hits[0][0], "room7_device00007")


class TestRetry(unittest.TestCase):
    def test_mqtt_is_one_shot_and_fails_open(self):
        # RETRY GAP: _mqtt()/subprocess.run — one call; a timeout yields [] and a log line
        calls = []

        def hang(argv, **k):
            calls.append(argv); raise subprocess.TimeoutExpired(argv, 15)
        with patch.object(hf.subprocess, "run", hang), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(hf._mqtt("zigbee2mqtt/bridge/devices", 1, 5), [])
            self.assertEqual(hf.collect_zigbee(), [])
        self.assertEqual(len(calls), 3)                              # 1 + the two collect_zigbee topics
        self.assertIn("mqtt zigbee2mqtt/bridge/devices failed", buf.getvalue())

    def test_home_assistant_is_one_shot_and_fails_open(self):
        # RETRY GAP: collect_ha()/nova_ha_metrics.ha_get_states — one call; failure yields []
        attempts = []
        m = types.ModuleType("nova_ha_metrics")

        def boom():
            attempts.append(1); raise OSError("HA down")
        m.ha_get_states = boom
        with patch.dict(sys.modules, {"nova_ha_metrics": m}), redirect_stdout(io.StringIO()):
            self.assertEqual(hf.collect_ha(), [])
        self.assertEqual(len(attempts), 1)

    def test_pg_collectors_fail_open_per_source(self):
        # RETRY GAP: collect_pg() — each of the three reads is one attempt; a failure drops only that source
        cur = _Cur(PG_ANSWERS[1:], fail="ha_sensors")
        with redirect_stdout(io.StringIO()) as buf:
            facts = hf.collect_pg(cur)
        self.assertIn("ha_sensors areas failed", buf.getvalue())
        self.assertTrue(facts)
        self.assertFalse(any(a == "room" for _, a, _, _ in facts))
        self.assertTrue(any(src == "unifi" for *_, src in facts))


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(hf.demo(), 0)

    def test_tokens_and_score_edges(self):
        self.assertEqual(hf.tokens(""), [])
        self.assertEqual(hf.tokens(None), [])
        self.assertEqual(hf.tokens("is the ON of a"), [])
        self.assertEqual(hf.score("garage_plug", []), 0)
        self.assertEqual(hf.score("slzb_06u", ["slz"]), 0)            # substring match needs 4+ chars
        self.assertEqual(hf.score("slzb_06u", ["slzb"]), 1)

    def test_lookup_and_format(self):
        self.assertEqual(hf.lookup_sql(_Cur(), "the and"), [])     # no tokens -> no query
        cur = _Cur([[("master_bedroom_plug", "firmware", "1.0", SEEN), ("master_bedroom_plug", "room", "Master Bedroom", SEEN),
                     ("garage_plug", "firmware", "2.0", SEEN)]])
        hits = hf.lookup_sql(cur, "master bedroom plug firmware", limit=1)
        self.assertEqual(hits, [("master_bedroom_plug", {"firmware": "1.0", "room": "Master Bedroom"}, SEEN)])
        blk = hf.format_block(hits)
        self.assertIn("master_bedroom_plug: firmware=1.0, room=Master Bedroom  (as of 2026-10-05 09:30)", blk)
        self.assertTrue(blk.endswith("[End house facts]\n\n"))
        self.assertEqual(hf.format_block([]), "")

    def test_collect_zigbee_parses_devices_and_states(self):
        with patch.object(hf.subprocess, "run", _Run()):
            facts = hf.collect_zigbee()
        names = {f[0] for f in facts}
        self.assertEqual(names, {"master_bedroom_plug", "garage_sensor"})
        d = {(e, a): v for e, a, v, _ in facts}
        self.assertEqual(d[("master_bedroom_plug", "model")], "TS011F")
        self.assertEqual(d[("master_bedroom_plug", "firmware")], "1.0.5")
        self.assertEqual(d[("garage_sensor", "model")], "SNZB-02")      # model_id fallback
        self.assertEqual(d[("master_bedroom_plug", "link_quality")], "120")
        self.assertEqual(d[("master_bedroom_plug", "latest_version")], "6")
        self.assertTrue(all(src == "zigbee2mqtt" for *_, src in facts))

    def test_collect_ha_keeps_only_update_entities(self):
        with _ha_stub(HA_STATES):
            facts = hf.collect_ha()
        self.assertEqual({f[0] for f in facts}, {"slzb_06_firmware"})
        self.assertIn(("slzb_06_firmware", "update_available", "yes", "home_assistant"), facts)
        self.assertIn(("slzb_06_firmware", "title", "SLZB-06", "home_assistant"), facts)

    def test_collect_pg_shapes(self):
        facts = hf.collect_pg(_Cur(PG_ANSWERS))
        self.assertIn(("master_bedroom_temp", "room", "Master Bedroom", "home_assistant"), facts)
        self.assertIn(("nova-core4", "switch_port", "sw1/7", "unifi"), facts)
        self.assertFalse(any(e == "ghost" for e, *_ in facts))         # all-null unifi row contributes nothing
        self.assertIn(("nova-hue@nova-core4", "endpoint", "192.168.1.250:8611", "service_registry"), facts)
        self.assertIn(("nova-soil@mac-studio", "status", "?", "service_registry"), facts)
        self.assertFalse(any(e == "nova-soil@mac-studio" and a == "last_heartbeat" for e, a, *_ in facts))


class TestIntegration(unittest.TestCase):
    def test_schema_and_upsert_agree_on_the_key(self):
        cur = _Cur()
        hf.ensure_schema(cur)
        self.assertIn("PRIMARY KEY (entity, attr)", cur.sql[0])
        hf.upsert(cur, [("a", "b", "c", "zigbee2mqtt")])
        sql, rows = cur.many[0]
        self.assertIn("ON CONFLICT (entity, attr)", sql)
        self.assertEqual(rows, [("a", "b", "c", "zigbee2mqtt")])

    def test_collect_then_upsert_then_lookup_round_trip(self):
        with patch.object(hf.subprocess, "run", _Run()), _ha_stub(HA_STATES):
            facts = hf.collect_zigbee() + hf.collect_ha() + hf.collect_pg(_Cur(PG_ANSWERS))
        cur = _Cur()
        hf.upsert(cur, facts)
        ledger = [(e, a, v, SEEN) for e, a, v, _ in cur.many[0][1]]
        hits = hf.lookup_sql(_Cur([ledger]), "What firmware is the master bedroom plug running?")
        self.assertEqual(hits[0][0], "master_bedroom_plug")
        self.assertEqual(hits[0][1]["firmware"], "1.0.5")
        self.assertIn("firmware=1.0.5", hf.format_block(hits))

    def test_ask_mode_is_what_the_gateway_sees(self):
        cur = _Cur([[("master_bedroom_plug", "firmware", "1.0.5", SEEN)]])
        real_pg, real_argv = hf.psycopg2, sys.argv
        hf.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: _Conn(cur))
        sys.argv = ["nova_house_facts.py", "--ask", "zigbee master bedroom firmware"]
        try:
            with redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(hf.main(), 0)
        finally:
            hf.psycopg2, sys.argv = real_pg, real_argv
        self.assertIn("[House facts — live inventory, trust these over memory]", buf.getvalue())
        self.assertIn("CREATE TABLE IF NOT EXISTS house_facts", cur.sql[0])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur, run=None, ha=HA_STATES):
        real_pg, real_argv = hf.psycopg2, sys.argv
        hf.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: _Conn(cur)); sys.argv = ["nova_house_facts.py", *argv]
        ha_mod = types.ModuleType("nova_ha_metrics")
        ha_mod.ha_get_states = (lambda: ha) if ha is not None else (lambda: (_ for _ in ()).throw(OSError("HA down")))
        try:
            with patch.object(hf.subprocess, "run", run or _Run()), patch.dict(sys.modules, {"nova_ha_metrics": ha_mod}), \
                    redirect_stdout(io.StringIO()) as buf:
                rc = hf.main()
        finally:
            hf.psycopg2, sys.argv = real_pg, real_argv
        return rc, buf.getvalue()

    def test_golden_path_collects_and_upserts(self):
        cur = _Cur(PG_ANSWERS)
        rc, out = self._main([], cur)
        self.assertEqual(rc, 0)
        self.assertIn("upserted", out)
        rows = cur.many[0][1]
        self.assertGreater(len(rows), 15)
        self.assertEqual({src for *_, src in rows}, {"zigbee2mqtt", "home_assistant", "unifi", "service_registry"})
        self.assertRegex(out, r"collected \d+ fact\(s\) over \d+ entities")

    def test_dry_run_writes_nothing(self):
        cur = _Cur(PG_ANSWERS)
        rc, out = self._main(["--dry-run"], cur)
        self.assertEqual(rc, 0)
        self.assertEqual(cur.many, [])
        self.assertIn("('master_bedroom_plug', 'model', 'TS011F', 'zigbee2mqtt')", out)

    def test_ask_with_no_match_says_so(self):
        cur = _Cur([[("garage_plug", "firmware", "2.0", SEEN)]])
        rc, out = self._main(["--ask", "espresso machine"], cur)
        self.assertEqual(rc, 0)
        self.assertIn("(no house facts match)", out)

    def test_every_source_down_still_exits_clean(self):
        def hang(argv, **k):
            raise subprocess.TimeoutExpired(argv, 15)
        cur = _Cur(fail="FROM")
        rc, out = self._main([], cur, run=hang, ha=None)
        self.assertEqual(rc, 0)
        self.assertIn("collected 0 fact(s) over 0 entities", out)
        self.assertEqual(cur.many[0][1], [])


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_house_facts.py"), "--selftest"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all house-facts assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_house_facts"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
