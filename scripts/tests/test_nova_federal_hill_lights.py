#!/usr/bin/env python3
"""Tests for nova_federal_hill_lights.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import os
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_federal_hill_lights as M  # noqa: E402

SRC = (SCRIPTS / "nova_federal_hill_lights.py").read_text()
SCRIPT = str(SCRIPTS / "nova_federal_hill_lights.py")


def _no_network(*a, **k):
    raise OSError("offline test: network stubbed")


# Stub every outbound side effect at module load (only on M, never on shared modules).
M.notify = mock.MagicMock(name="notify")
M.URLOPEN = _no_network


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class FakeCur:
    """route(sql, params) -> rows; records every statement."""

    def __init__(self, route=None, boom=False):
        self.route, self.boom, self.sql, self._last = route or (lambda s, p: []), boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = list(self.route(sql, params) or [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def make_route(cfg=None, cand=(), pres=()):
    cfg = cfg or {}

    def route(sql, params):
        if "service_config" in sql:
            v = cfg.get(tuple(params))
            return [(v,)] if v is not None else []
        if "face_unknown_candidates" in sql:
            return cand
        if "face_presence" in sql:
            return pres
        return []
    return route


def recall_stub(casual_returns_canary, planted=True):
    calls = []

    def _open(url, timeout=None):
        calls.append(url)
        boxed = "include_boxed=true" in url
        hit = planted and (boxed or casual_returns_canary)
        mems = [{"text": f"canary {M.CANARY_MARK}"}] if hit else [{"text": "something else"}]
        return FakeResp({"memories": mems})
    _open.calls = calls
    return _open


class TestSecurity(unittest.TestCase):
    def test_no_secrets_ips_or_fstring_sql(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r"(?i)(password|token|api_key)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotIn(str(Path.home()), SRC)

    def test_probes_never_actuate(self):
        self.assertNotIn("block-sta", SRC)
        self.assertNotIn("_post(", SRC)
        self.assertNotIn("/forget", SRC)

    def test_run_never_plants_or_writes_memory(self):
        stub = recall_stub(False)
        with mock.patch.object(M, "URLOPEN", stub):
            M.run(dry=True, conn=mock.MagicMock(cursor=lambda: FakeCur(make_route())))
        self.assertTrue(stub.calls)
        self.assertFalse(any("/remember" in u for u in stub.calls))

    def test_mac_input_case_normalised(self):
        self.assertEqual(M.quarantine_verdict(["AA:BB"], [{"mac": "aa:bb", "blocked": True}])[0], M.HELD)


class TestPerformance(unittest.TestCase):
    def test_quarantine_verdict_10k(self):
        clients = [{"mac": f"aa:{i:06x}", "blocked": i % 2 == 0} for i in range(10000)]
        expected = [f"aa:{i:06x}" for i in range(0, 10000, 2)]
        t = time.monotonic()
        self.assertEqual(M.quarantine_verdict(expected, clients)[0], M.HELD)
        self.assertLess(time.monotonic() - t, 1.0)

    def test_canary_seen_10k(self):
        resp = {"memories": [{"text": f"memory {i}"} for i in range(10000)]}
        t = time.monotonic()
        self.assertFalse(M.canary_seen(resp))
        self.assertLess(time.monotonic() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_get_json_retries_with_backoff(self):
        n = {"c": 0}
        sleeps = []

        def flaky(url, timeout=None):
            n["c"] += 1
            if n["c"] < 3:
                raise OSError("replica restarting")
            return FakeResp({"memories": []})
        with mock.patch.object(M, "URLOPEN", flaky):
            self.assertEqual(M._get_json("http://x/recall", _sleep=sleeps.append), {"memories": []})
        self.assertEqual(n["c"], 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_get_json_fails_open(self):
        self.assertIsNone(M._get_json("http://x/recall", _sleep=lambda s: None))

    def test_udm_read_fails_open(self):
        # RETRY GAP: udm_known_clients — nova_unifi_poller retries by re-login only; a Keychain
        # failure sys.exit()s there, which must degrade to None (probe unavailable), never escape.
        fake = types.SimpleNamespace(_unifi_login=mock.Mock(side_effect=SystemExit(1)))
        with mock.patch.dict(sys.modules, {"nova_unifi_poller": fake}):
            self.assertIsNone(M.udm_known_clients())


class TestUnit(unittest.TestCase):
    def test_canary_verdicts(self):
        self.assertEqual(M.canary_verdict(False, False), (M.UNAVAILABLE, "canary not planted"))
        self.assertEqual(M.canary_verdict(None, None)[0], M.UNAVAILABLE)
        self.assertEqual(M.canary_verdict(True, None)[0], M.UNAVAILABLE)
        self.assertEqual(M.canary_verdict(True, True)[0], M.BREACHED)
        self.assertEqual(M.canary_verdict(True, False)[0], M.HELD)

    def test_canary_seen_edges(self):
        self.assertIsNone(M.canary_seen(None))
        self.assertFalse(M.canary_seen({}))
        self.assertTrue(M.canary_seen({"memories": [{"text": M.CANARY_MARK}]}))

    def test_quarantine_verdicts(self):
        self.assertEqual(M.quarantine_verdict([], None)[0], M.UNAVAILABLE)
        self.assertEqual(M.quarantine_verdict(["a"], None)[0], M.UNAVAILABLE)
        st, _w, open_ = M.quarantine_verdict(["a", "b"], [{"mac": "a", "blocked": True}, {"mac": "b"}])
        self.assertEqual((st, open_), (M.BREACHED, ["b"]))

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(M.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_face_retention_rules(self):
        self.assertIn("import nova_face_retention as FR", SRC)
        for name in ("FR.settings", "FR.candidates_to_delete", "FR.is_household"):
            self.assertIn(name, SRC)

    def test_face_ttl_uses_household_config_and_grace(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=72 + M.GRACE_H + 5)).isoformat()
        fresh = (now - timedelta(hours=80)).isoformat()      # past TTL but inside one purge cycle
        cur = FakeCur(make_route(
            cfg={("face_retention", "household"): {"dana scully": "dana"}, ("face_retention", "ttl_hours"): 72},
            cand=[("a", "", "", old, 0, None), ("b", "", "", fresh, 0, None)],
            pres=[("Dana Scully",), ("Fox Mulder",)]))
        r = M.probe_face_ttl(cur, now=now)
        self.assertEqual(r["status"], M.BREACHED)
        self.assertEqual((r["detail"]["unknown_candidates"], r["detail"]["face_presence"]), (1, 1))

    def test_quarantine_reads_config_then_udm(self):
        cur = FakeCur(make_route(cfg={("federal_hill_lights", "quarantined_macs"): ["aa:bb"]}))
        r = M.probe_quarantine(cur, _clients=lambda: [{"mac": "aa:bb", "blocked": True}])
        self.assertEqual(r["status"], M.HELD)
        r = M.probe_quarantine(FakeCur(make_route()), _clients=mock.Mock(side_effect=AssertionError))
        self.assertEqual(r["status"], M.UNAVAILABLE)     # no record -> UDM never touched

    def test_endpoints_are_dns_names(self):
        self.assertTrue(all(h.endswith(".digitalnoise.net") for h in M.DEFAULT_ENDPOINTS))


class TestFunctional(unittest.TestCase):

    def test_lockbox_leak_through_casual_search_is_a_breach(self):
        hit = {"memories": [{"text": f"x {M.CANARY_MARK}"}]}
        def fake(url, _sleep=None):
            if "include_boxed=true" in url or "/search" in url:
                return hit                      # casual /search leaks the canary
            return {"memories": []}
        with mock.patch.object(M, "_get_json", side_effect=fake):
            out = M.probe_lockbox(["h1"], _sleep=lambda s: None)
        self.assertEqual(out[0]["status"], M.BREACHED)

    def test_lockbox_held_when_only_the_opt_in_search_sees_it(self):
        hit = {"memories": [{"text": f"x {M.CANARY_MARK}"}]}
        with mock.patch.object(M, "_get_json",
                               side_effect=lambda url, _sleep=None: hit if "include_boxed=true" in url else {"memories": []}):
            out = M.probe_lockbox(["h1"], _sleep=lambda s: None)
        self.assertEqual(out[0]["status"], M.HELD)
    def setUp(self):
        M.notify.reset_mock()

    def test_breach_is_recorded_and_escalated(self):
        cur = FakeCur(make_route())
        with mock.patch.object(M, "URLOPEN", recall_stub(True)):
            res = M.run(dry=False, trigger="failover", conn=mock.MagicMock(cursor=lambda: cur))
        lock = [r for r in res if r["containment"].startswith("memory_lockbox@")]
        self.assertEqual(len(lock), len(M.DEFAULT_ENDPOINTS))
        self.assertTrue(all(r["status"] == M.BREACHED for r in lock))
        inserts = [p for s, p in cur.sql if "INSERT INTO federal_hill_lights" in s]
        self.assertEqual(len(inserts), len(res))
        self.assertTrue(all(p[3] == "failover" for p in inserts))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS federal_hill_lights" in s for s, _ in cur.sql))
        keys = {c.kwargs["dedup_key"] for c in M.notify.call_args_list}
        self.assertEqual(keys, {f"federal_hill_lights:{r['containment']}" for r in lock})
        self.assertTrue(all(c.kwargs["level"] == "warning" for c in M.notify.call_args_list))

    def test_dry_run_writes_nothing(self):
        cur = FakeCur(make_route())
        with mock.patch.object(M, "URLOPEN", recall_stub(True)):
            M.run(dry=True, conn=mock.MagicMock(cursor=lambda: cur))
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))
        M.notify.assert_not_called()

    def test_unreachable_and_db_down_are_unavailable_not_held(self):
        cur = FakeCur(boom=True)
        with mock.patch.object(M, "_get_json", return_value=None):
            res = M.run(dry=True, conn=mock.MagicMock(cursor=lambda: cur))
        self.assertTrue(res)
        self.assertTrue(all(r["status"] == M.UNAVAILABLE for r in res), res)
        M.notify.assert_not_called()

    def test_not_planted_is_unavailable(self):
        with mock.patch.object(M, "URLOPEN", recall_stub(False, planted=False)):
            res = M.probe_lockbox(["memory-server.digitalnoise.net"])
        self.assertEqual(res[0]["detail"]["reason"], "canary not planted")


class TestFrame(unittest.TestCase):
    def _cli(self, flag):
        return subprocess.run([sys.executable, SCRIPT, flag], capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, NOVA_TEST_QUIET="1"))

    def test_selftest_cli(self):
        r = self._cli("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = self._cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--plant-canary", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(M.main))


if __name__ == "__main__":
    unittest.main()
