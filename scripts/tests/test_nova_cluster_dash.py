#!/usr/bin/env python3
"""Tests for nova_cluster_dash.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from itertools import combinations
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cluster_dash.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cd = _load("cluster_dash_under_test", SCRIPT)
GRID_COLS, GRID_ROWS = 24, 27            # 1920x1080 kiosk: 24 cols x 27 rows, no scroll (module docstring)
KNOWN_TABLES = {"service_registry", "health_checks", "telemetry.incidents", "pg_stat_replication", "node_status",
                "telemetry.replication_health", "telemetry.nova_meta", "telemetry.events", "scheduler_runs"}


def _panels():
    return cd.dashboard()["dashboard"]["panels"]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)                    # the POST to Grafana is done by the caller with its own key

    def test_every_raw_sql_is_read_only(self):
        write = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|CREATE)\b", re.I)
        for key, sql in cd.SQL.items():
            self.assertIsNone(write.search(sql), key)
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "WITH")), key)
        for p in _panels():
            for t in p.get("targets", []):
                self.assertIn(t["rawSql"], cd.SQL.values())        # panels only ever embed the audited statements

    def test_sql_has_no_runtime_interpolation_and_title_html_has_no_script(self):
        self.assertIsNone(re.search(r'rawSql.*\bf"', SRC))
        self.assertNotIn("<script", cd.TITLE_HTML.lower())
        self.assertNotIn("javascript:", cd.TITLE_HTML.lower())
        self.assertEqual(cd.OPS, {"type": "grafana-postgresql-datasource", "uid": "nova-ops-pg"})


class TestPerformance(unittest.TestCase):
    def test_dashboard_generation_and_serialisation_fast(self):
        t0 = time.perf_counter()
        for _ in range(200):
            body = json.dumps(cd.dashboard())
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertGreater(len(body), 10_000)

    def test_helpers_linear_on_10k_items(self):
        t0 = time.perf_counter()
        m = cd.vmap({str(i): (f"t{i}", cd.GOOD) for i in range(10_000)})
        t = cd.thr(*[(i, cd.WARN) for i in range(10_000)])
        o = cd.ov("f", *[(f"p{i}", i) for i in range(10_000)])
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual((len(m[0]["options"]), len(t["steps"]), len(o["properties"])), (10_000,) * 3)


class TestRetry(unittest.TestCase):
    def test_pure_generator_has_no_external_calls_to_retry(self):
        # RETRY GAP: none — nova_cluster_dash makes no HTTP/PG/subprocess call; the Grafana POST and psql run are the
        # caller's job. Prove it: only json/sys are imported and the build succeeds with every I/O door nailed shut.
        self.assertEqual(sorted(re.findall(r"^import (.+)$", SRC, re.M)), ["json, sys"])
        for banned in ("urllib", "requests", "psycopg2", "subprocess", "socket", "open("):
            self.assertNotIn(banned, SRC)
        with patch("urllib.request.urlopen", MagicMock(side_effect=AssertionError("network"))), \
             patch("subprocess.run", MagicMock(side_effect=AssertionError("subprocess"))):
            a, b = cd.dashboard(), cd.dashboard()
        self.assertEqual(a["dashboard"]["uid"], "nova-cluster")
        a["dashboard"]["panels"] = b["dashboard"]["panels"] = None     # ids differ per build (pid counter); the rest is identical
        self.assertEqual(a, b)


class TestUnit(unittest.TestCase):
    def test_pid_is_monotonic(self):
        a, b, c = cd.pid(), cd.pid(), cd.pid()
        self.assertEqual((b - a, c - b), (1, 1))

    def test_target_thr_vmap_ov(self):
        self.assertEqual(cd.target("SELECT 1"), {"datasource": cd.OPS, "refId": "A", "rawQuery": True, "editorMode": "code",
                                                 "format": "table", "rawSql": "SELECT 1"})
        self.assertEqual(cd.thr((None, "a"), (5, "b")), {"mode": "absolute", "steps": [{"color": "a", "value": None}, {"color": "b", "value": 5}]})
        self.assertEqual(cd.thr(), {"mode": "absolute", "steps": []})
        m = cd.vmap({"up": ("UP", "g"), -1: ("DOWN", "r")})
        self.assertEqual(m, [{"type": "value", "options": {"up": {"text": "UP", "color": "g", "index": 0},
                                                           "-1": {"text": "DOWN", "color": "r", "index": 1}}}])
        self.assertEqual(cd.ov("x"), {"matcher": {"id": "byName", "options": "x"}, "properties": []})

    def test_cell_helpers_optional_properties(self):
        ids = lambda o: [p["id"] for p in o["properties"]]
        self.assertEqual(ids(cd.cell_bg("f", cd.PCT_T)), ["custom.cellOptions", "thresholds", "color"])
        self.assertEqual(ids(cd.cell_bg("f", cd.PCT_T, unit="s", mappings=[1], width=9)),
                         ["custom.cellOptions", "thresholds", "color", "unit", "mappings", "custom.width"])
        self.assertEqual(cd.cell_text("f", cd.PCT_T)["properties"][0]["value"], {"type": "color-text"})
        g = dict((p["id"], p["value"]) for p in cd.cell_gauge("load", cd.PCT_T, maxv=150, width=100)["properties"])
        self.assertEqual((g["min"], g["max"], g["unit"], g["custom.width"], g["decimals"]), (0, 150, "percent", 100, 0))
        self.assertEqual(cd.fixed_color("n", "#fff")["properties"], [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#fff"}}])

    def test_panel_builders(self):
        p = cd.tile("t", "SELECT 1", 0, 0, 4, 5, cd.PCT_T, spark=False, fmt="table")
        self.assertEqual((p["type"], p["options"]["graphMode"], p["transparent"], p["targets"][0]["format"]), ("stat", "none", False, "table"))
        p = cd.tile("t", "SELECT 1", 0, 0, 4, 5, cd.PCT_T)
        self.assertEqual((p["options"]["graphMode"], p["targets"][0]["format"], p["fieldConfig"]["defaults"]["mappings"]), ("area", "time_series", []))
        w = cd.wall("w", "SELECT 1", 0, 0, 1, 1, cd.AGE_T)
        self.assertNotIn("unit", w["fieldConfig"]["defaults"]); self.assertTrue(w["options"]["reduceOptions"]["values"])
        self.assertEqual(cd.wall("w", "SELECT 1", 0, 0, 1, 1, cd.AGE_T, unit="s")["fieldConfig"]["defaults"]["unit"], "s")
        t = cd.table("t", "SELECT 1", 0, 0, 1, 1, [])
        self.assertEqual((t["type"], t["options"]["sortBy"], t["transparent"]), ("table", [], True))
        ts = cd.tseries("t", "SELECT 1", 0, 0, 1, 1, "ms", [], legend=False, bars=True, stacked=True, time_from="24h")
        c = ts["fieldConfig"]["defaults"]["custom"]
        self.assertEqual((c["drawStyle"], c["stacking"]["mode"], ts["timeFrom"], ts["options"]["legend"]["displayMode"]), ("bars", "normal", "24h", "hidden"))
        ts = cd.tseries("t", "SELECT 1", 0, 0, 1, 1, "ms", [])
        self.assertNotIn("timeFrom", ts); self.assertNotIn("drawStyle", ts["fieldConfig"]["defaults"]["custom"])


class TestIntegration(unittest.TestCase):
    def test_panels_tile_the_kiosk_grid_without_overlap(self):
        panels = _panels()
        self.assertEqual(len({p["id"] for p in panels}), len(panels))
        boxes = []
        for p in panels:
            g = p["gridPos"]
            self.assertLessEqual(g["x"] + g["w"], GRID_COLS, p["title"])
            self.assertLessEqual(g["y"] + g["h"], GRID_ROWS, p["title"])
            boxes.append((p["title"], g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]))
        for (ta, ax0, ay0, ax1, ay1), (tb, bx0, by0, bx1, by1) in combinations(boxes, 2):
            self.assertFalse(ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1, f"{ta!r} overlaps {tb!r}")
        self.assertEqual(max(b[4] for b in boxes), GRID_ROWS)
        self.assertEqual(sum((b[3] - b[1]) * (b[4] - b[2]) for b in boxes), GRID_COLS * GRID_ROWS)   # fully tiled

    def test_every_sql_statement_is_used_and_targets_known_tables(self):
        used = {t["rawSql"] for p in _panels() for t in p.get("targets", [])}
        self.assertEqual(used, set(cd.SQL.values()))
        for key, sql in cd.SQL.items():
            tables = set(re.findall(r"\bFROM\s+((?:\w+\.)?\w+)", sql))
            self.assertTrue(tables & KNOWN_TABLES or key == "clock", (key, tables))
        self.assertTrue(cd.SQL["status_line"].count("CRITICAL") == 1 and "NOMINAL" in cd.SQL["status_line"])

    def test_node_series_palette_is_fixed_and_the_dashboard_envelope_is_right(self):
        self.assertEqual(len(cd.NODE_ORDER), len(cd.NODE_COLORS))
        self.assertEqual(len(set(cd.NODE_COLORS)), len(cd.NODE_COLORS))
        llm = next(p for p in _panels() if p["title"].endswith("LLM ping latency"))
        self.assertEqual([o["matcher"]["options"] for o in llm["fieldConfig"]["overrides"]], cd.NODE_ORDER)
        d = cd.dashboard()
        self.assertEqual((d["overwrite"], d["dashboard"]["uid"], d["dashboard"]["refresh"], d["dashboard"]["schemaVersion"]),
                         (True, "nova-cluster", "30s", 41))
        self.assertEqual(d["dashboard"]["tags"], ["nova", "cluster"])


class TestFunctional(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NOVA_TEST_QUIET": "1"})

    def test_json_mode_emits_the_post_body(self):
        r = self._run("json")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["dashboard"]["title"], "Nova Cluster")
        self.assertEqual(len(body["dashboard"]["panels"]), len(_panels()))
        self.assertEqual(json.loads(self._run().stdout)["dashboard"]["uid"], "nova-cluster")   # no arg == json

    def test_sql_mode_prints_every_statement_for_psql(self):
        r = self._run("sql")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(re.findall(r"^--@@ (\w+)$", r.stdout, re.M), list(cd.SQL))
        self.assertIn(cd.SQL["clock"], r.stdout)

    def test_unknown_mode_still_yields_valid_json_not_a_crash(self):
        r = self._run("bogus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["message"], "nova-cluster kiosk dashboard")


class TestFrame(unittest.TestCase):
    def test_json_entrypoint_exits_zero_and_import_prints_nothing(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "json"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_cluster_dash"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
