#!/usr/bin/env python3
"""7-category tests for nova_unifi_toolkit.py + the UDM certificate pin in nova_unifi_poller.py (Security,
Performance, Retry, Unit, Integration, Functional, Frame). The UDM API, PostgreSQL and nova_notify are all
mocked — nothing touches the network or the firewall. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_unifi_toolkit_7cat.py
"""
import contextlib
import hashlib
import io
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_unifi_poller as poller   # noqa: E402
import nova_unifi_toolkit as m       # noqa: E402
import unifi_ha_policies as P        # noqa: E402

UPSTREAM_SHA256 = "c25e768da818781047dc498b1a6d39f080b07e745563aab451b92f2c1f9d02b4"   # policies.py @ 24add95
ZONES = [{"_id": "z" * 24, "name": "Internal", "zone_key": "internal", "network_ids": ["n" * 24]},
         {"_id": "e" * 24, "name": "External", "zone_key": "external", "network_ids": []}]
NETS = [{"_id": "n" * 24, "name": "Default", "purpose": "corporate", "vlan": None, "ip_subnet": "192.168.1.1/24"},
        {"_id": "g" * 24, "name": "Guest", "purpose": "guest", "vlan": "3", "ip_subnet": "192.168.3.1/24"}]
FAKE = "02:00:5e:be:ef:01"


class Cur:
    """Minimal psycopg2 cursor stand-in: records SQL, answers fetchone/fetchall from queues."""
    def __init__(self, one=None, all_=None):
        self.sql, self.one, self.all = [], list(one or []), list(all_ or [])
        self.connection = MagicMock()

    def execute(self, q, params=None):
        self.sql.append((q, params))

    def fetchone(self):
        return self.one.pop(0) if self.one else None

    def fetchall(self):
        return self.all.pop(0) if self.all else []


def conn_for(cur):
    c = MagicMock()
    c.cursor.return_value = cur
    return c


def flow(i=1, risk="high", action="blocked", src="203.0.113.9"):
    return {"id": f"{i:024x}", "time": 1_791_000_000_000 + i, "risk": risk, "action": action,
            "source": {"ip": src, "port": 4444, "region": "NL"}, "destination": {"ip": "192.168.1.50", "port": 22},
            "ips": {"advanced_information": "ET SCAN <!channel> probe", "signature_id": 2001219,
                    "category_name": "scan"}, "protocol": "TCP", "service": "SSH", "in": {"network_name": "Default"}}


class TestSecurity(unittest.TestCase):
    def test_safe_neutralises_slack_mentions_links_and_control_chars(self):
        out = m.safe("<!channel> <@U123> <http://x|click>\x07\nhi")
        for bad in ("<!channel>", "<@U123>", "<http", "\x07", "\n"):
            self.assertNotIn(bad, out)

    def test_safe_truncates(self):
        self.assertLessEqual(len(m.safe("a" * 500, 50)), 51)

    def test_delete_refuses_predefined_unmarked_unledgered_and_bad_ids(self):
        ours = {"_id": "a" * 24, "description": P.MARKER, "name": "House Arrest: x — no LAN"}
        cases = [({"_id": "b" * 24, "predefined": True, "description": P.MARKER}, True),
                 ({"_id": "c" * 24, "description": "user rule"}, True),
                 ({**ours, "_id": "677da54fa30e90682a0f82b6677da54f2147483647"}, True),
                 (ours, False)]                                    # marked, but NOT in the ledger
        for pol, ledger in cases:
            with patch.object(m, "in_ledger", return_value=ledger), patch.object(m, "_call") as call:
                self.assertFalse(m.delete_policy(pol["_id"], [pol]))
                call.assert_not_called()

    def test_delete_allows_only_ledgered_marked_policy(self):
        pol = {"_id": "a" * 24, "description": P.MARKER, "name": "House Arrest: x — no LAN"}
        with patch.object(m, "in_ledger", return_value=True), patch.object(m, "_call", return_value=(200, {})) as call, \
                patch.object(m, "_release_ledger") as rel:
            self.assertTrue(m.delete_policy(pol["_id"], [pol]))
        self.assertIn("batch-delete", call.call_args.args[1])
        rel.assert_called_once_with("a" * 24)

    def test_guard_refuses_infrastructure(self):
        ctl = MagicMock(is_infrastructure=lambda mac, c: "UniFi device 'UDM'")
        with patch.dict(sys.modules, {"nova_unifi_ctl": ctl}), patch.object(m, "live_clients", return_value={}), \
                patch.object(m, "_ops", return_value=conn_for(Cur(one=[None]))):
            self.assertIn("REFUSED", m.guard_devices([FAKE], P.INTERNET_ONLY, False))

    def test_guard_refuses_never_lock_list_and_unreadable_list(self):
        ctl = MagicMock(is_infrastructure=lambda mac, c: None)
        with patch.dict(sys.modules, {"nova_unifi_ctl": ctl}), patch.object(m, "live_clients", return_value={}):
            with patch.object(m, "_ops", return_value=conn_for(Cur(one=[([FAKE],)]))):
                self.assertIn("never-lock", m.guard_devices([FAKE], P.INTERNET_ONLY, False))
            with patch.object(m, "_ops", side_effect=OSError("pg down")):
                self.assertIn("unreadable", m.guard_devices([FAKE], P.INTERNET_ONLY, False))

    def test_guard_comms_fails_closed_for_internet_cutting_presets(self):
        ctl = MagicMock(is_infrastructure=lambda mac, c: None)
        guards = MagicMock(); guards.comms_guard.side_effect = RuntimeError("no owner table")
        with patch.dict(sys.modules, {"nova_unifi_ctl": ctl, "nova_safety_guards": guards}), \
                patch.object(m, "live_clients", return_value={}), patch.object(m, "_ops", return_value=conn_for(Cur())):
            self.assertIn("comms guard unavailable", m.guard_devices([FAKE], P.CUT_OFF, False))
            self.assertIsNone(m.guard_devices([FAKE], P.CUT_OFF, True))         # explicit override
            self.assertIsNone(m.guard_devices([FAKE], P.INTERNET_ONLY, False))  # doesn't cut internet

    def test_before_snapshot_holds_only_changed_fields(self):
        doc = {"_id": "w" * 24, "name": "KOCH", "x_passphrase": "hunter2", "l2_isolation": False}
        with patch.object(m, "get_list", return_value=[doc]), patch.object(m, "ledger") as led, \
                patch.object(m, "_call", side_effect=[(200, {}), (200, {"data": [{**doc, "l2_isolation": True}]})]):
            self.assertTrue(m._put_verified("/g", "/p", {"l2_isolation": True}, strict=False))
        payload, before = led.call_args.args[3], led.call_args.args[4]
        self.assertNotIn("hunter2", repr((payload, before)))
        self.assertEqual(before, {"l2_isolation": False})

    def test_pin_mismatch_fails_closed_and_never_retries(self):
        err = urllib.error.URLError(poller.CertPinError("UDM certificate CHANGED"))
        opener = MagicMock(); opener.open.side_effect = err
        with patch.object(m.u, "_opener", opener), patch.object(m, "alert") as al:
            with self.assertRaises(m.UniFiError):
                m._call("GET", "/x")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(al.call_args.kwargs["level"], "critical")

    def test_pinned_connection_rejects_other_cert(self):
        conn = poller._PinnedHTTPSConnection("192.0.2.1")
        sock = MagicMock(); sock.getpeercert.return_value = b"evil-cert"
        with patch("http.client.HTTPSConnection.connect", lambda self: setattr(self, "sock", sock)), \
                patch.object(poller, "_udm_pin", return_value="0" * 64):
            with self.assertRaises(poller.CertPinError):
                conn.connect()

    def test_pinned_connection_fails_closed_when_pin_unreadable(self):
        conn = poller._PinnedHTTPSConnection("192.0.2.1")
        sock = MagicMock(); sock.getpeercert.return_value = b"cert"
        with patch("http.client.HTTPSConnection.connect", lambda self: setattr(self, "sock", sock)), \
                patch.object(poller, "_udm_pin", side_effect=OSError("pg down")):
            with self.assertRaises(poller.CertPinError):
                conn.connect()

    def test_redirects_refused(self):
        self.assertIsNone(poller._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil/"))

    def test_sql_is_parameterised(self):
        src = (SCRIPTS / "nova_unifi_toolkit.py").read_text()
        for bad in ('execute(f"', "execute(f'", ".format("):
            self.assertNotIn(bad, src.replace('cur.execute(f"""SELECT ap, sum(samples) * {STALK_INTERVAL_MIN}', ""))


class TestPerformance(unittest.TestCase):
    def test_diff_5000_clients_fast(self):
        live = {f"02:00:00:00:{i // 256:02x}:{i % 256:02x}": {"ap_mac": "aa:bb"} for i in range(5000)}
        prev = {k: (True, "old-ap") for k in live}
        t = time.perf_counter()
        ev = m.diff_clients(prev, live, {"aa:bb": "Office"})
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual(len(ev), 5000)

    def test_normalize_10k_flows_fast(self):
        t = time.perf_counter()
        for i in range(10_000):
            m.normalize_flow(flow(i))
        self.assertLess(time.perf_counter() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_get_retries_transport_errors_then_raises(self):
        opener = MagicMock(); opener.open.side_effect = OSError("reset")
        with patch.object(m.u, "_opener", opener), patch("time.sleep"):
            with self.assertRaises(m.UniFiError):
                m._call("GET", "/x")
        self.assertEqual(opener.open.call_count, 3)

    def test_writes_do_not_auto_retry(self):
        opener = MagicMock(); opener.open.side_effect = OSError("timeout")
        with patch.object(m.u, "_opener", opener), patch("time.sleep"):
            with self.assertRaises(m.UniFiError):
                m._call("POST", "/x", {"a": 1})
        self.assertEqual(opener.open.call_count, 1)

    def test_http_error_returned_not_retried(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"nope"))
        with patch.object(m.u, "_opener", opener):
            self.assertEqual(m._call("GET", "/x")[0], 404)
        self.assertEqual(opener.open.call_count, 1)

    def test_get_recovers_after_transient_error(self):
        ok = MagicMock(); ok.__enter__.return_value = MagicMock(status=200, read=lambda: b'{"data": []}')
        opener = MagicMock(); opener.open.side_effect = [OSError("reset"), ok]
        with patch.object(m.u, "_opener", opener), patch("time.sleep"):
            self.assertEqual(m._call("GET", "/x"), (200, {"data": []}))


class TestUnit(unittest.TestCase):
    def test_normalize_flow(self):
        r = m.normalize_flow(flow(action="allowed"))
        self.assertEqual((r["action"], r["risk"], r["src_ip"], r["src_port"], r["src_country"]),
                         ("alert", "high", "203.0.113.9", 4444, "NL"))
        self.assertEqual(m.normalize_flow(flow(action="dropped"))["action"], "block")

    def test_normalize_flow_rejects_junk_ip_and_port(self):
        f = flow(src="not-an-ip"); f["source"]["port"] = "x"
        r = m.normalize_flow(f)
        self.assertIsNone(r["src_ip"]); self.assertIsNone(r["src_port"])

    def test_diff_clients_connect_roam_disconnect(self):
        names = {"ap1": "Office", "ap2": "Garage"}
        prev = {"a": (True, "Office"), "b": (True, "Office"), "c": (False, "Office")}
        live = {"a": {"ap_mac": "AP1"}, "b": {"ap_mac": "ap2"}, "c": {"ap_mac": "ap1"}, "d": {"ap_mac": "ap1"}}
        ev = {(mac, k) for mac, k, _, _ in m.diff_clients({**prev, "e": (True, "Garage")}, live, names)}
        self.assertEqual(ev, {("b", "roam"), ("c", "connect"), ("d", "connect"), ("e", "disconnect")})

    def test_where_wired_and_wireless(self):
        self.assertEqual(m.where({"is_wired": True, "sw_mac": "SW", "sw_port": 7}, {"sw": "Rack"}), "Rack port 7")
        self.assertEqual(m.where({"ap_mac": "x"}, {}), "x")

    def test_digest_and_changes(self):
        mat = {"rows": [{"name": "Guest", "cells": {"isolation": {"state": "warn"}}}]}
        d1 = m.matrix_digest(mat, [{"name": "KOCH", "l2_isolation": False, "enabled": True}])
        d2 = {**d1, "Guest": {"isolation": "good"}}
        self.assertEqual(m.digest_changes(d1, d2), ["Guest isolation: warn -> good"])

    def test_norm_mac(self):
        self.assertEqual(m.norm_mac("02-00-5E-BE-EF-01"), FAKE)
        for bad in ("zz", "02:00:5e:be:ef", "02:00:5e:be:ef:01:02", "'; drop table x;--"):
            with self.assertRaises(SystemExit):
                m.norm_mac(bad)

    def test_networks_vlan_normalised(self):
        with patch.object(m, "get_list", return_value=[dict(n) for n in NETS] + [{"_id": "x", "vlan": "abc"}]):
            vl = [n["vlan"] for n in m.networks()]
        self.assertEqual(vl, [None, 3, None])

    def test_posture_findings(self):
        nets = [{**NETS[1], "vlan": 3, "mdns_enabled": True}]
        f = {x["finding"] for x in m.posture_findings(nets, ZONES, [])}
        self.assertEqual(f, {"Network isolation is off", "mDNS forwarding is on"})


class TestIntegration(unittest.TestCase):
    def test_threats_stores_and_alerts_high_unignored(self):
        hot_row = ("f" * 24, __import__("datetime").datetime(2026, 10, 13, 12, 0), "high", "alert",
                   "ET SCAN <!channel>", "203.0.113.9", "NL", "192.168.1.50", 22)
        cur = Cur(one=[(None,), ("f" * 24,)], all_=[[hot_row]])
        with patch.object(m, "_ops", return_value=conn_for(cur)), patch.object(m, "_login"), \
                patch.object(m, "fetch_ips_flows", return_value=[flow()]), patch.object(m, "alert") as al:
            self.assertEqual(m.cmd_threats(m.build_parser().parse_args(["threats"])), 0)
        title, body = al.call_args.args[:2]
        self.assertIn("1 not blocked", title)
        self.assertNotIn("<!channel>", body)
        self.assertEqual(al.call_args.kwargs["level"], "critical")
        self.assertTrue(any("SET ignored = true" in q for q, _ in cur.sql))

    def test_threats_no_alert_when_nothing_new(self):
        cur = Cur(one=[(None,), None])
        with patch.object(m, "_ops", return_value=conn_for(cur)), patch.object(m, "_login"), \
                patch.object(m, "fetch_ips_flows", return_value=[flow()]), patch.object(m, "alert") as al:
            m.cmd_threats(m.build_parser().parse_args(["threats"]))
        al.assert_not_called()

    def test_stalk_first_run_is_silent_baseline(self):
        cur = Cur(all_=[[], []])
        with patch.object(m, "_ops", return_value=conn_for(cur)), patch.object(m, "_login"), \
                patch.object(m, "live_clients", return_value={FAKE: {"ap_mac": "ap1", "name": "phone"}}), \
                patch.object(m, "devices", return_value=[]), patch.object(m, "alert") as al, \
                patch("psycopg2.extras.execute_values") as ev:
            m.cmd_stalk(m.build_parser().parse_args(["stalk"]))
        self.assertFalse(any("unifi_client_events" in c.args[1] for c in ev.call_args_list))
        al.assert_not_called()

    def test_stalk_tracked_device_alerts_on_roam(self):
        cur = Cur(all_=[[(FAKE, True, "Office")], [(FAKE, "Jordan's phone")]])
        with patch.object(m, "_ops", return_value=conn_for(cur)), patch.object(m, "_login"), \
                patch.object(m, "live_clients", return_value={FAKE: {"ap_mac": "ap2"}}), \
                patch.object(m, "devices", return_value=[{"mac": "AP2", "name": "Garage"}]), \
                patch.object(m, "alert") as al, patch("psycopg2.extras.execute_values"):
            m.cmd_stalk(m.build_parser().parse_args(["stalk"]))
        self.assertIn("roamed Office -> Garage", al.call_args.args[0])


def lockdown_env(created=None, policies_list=None):
    """Patch everything cmd_lockdown touches; returns an ExitStack + the create mock."""
    st = contextlib.ExitStack()
    st.enter_context(patch.object(m, "_login"))
    st.enter_context(patch.object(m, "_call", side_effect=AssertionError("test reached the real UDM API")))
    st.enter_context(patch.object(m, "guard_devices", return_value=None))
    st.enter_context(patch.object(m, "zones", return_value=ZONES))
    st.enter_context(patch.object(m, "policies", return_value=policies_list or []))
    st.enter_context(patch.object(m, "networks", return_value=NETS))
    st.enter_context(patch.object(m, "known_clients", return_value=[]))
    st.enter_context(patch.object(m, "live_clients", return_value={}))
    st.enter_context(patch.object(m, "acl_rules", return_value=None))     # switches unreadable: no network I/O
    st.enter_context(patch.object(m, "devices", return_value=[]))
    st.enter_context(patch.object(m, "record"))
    st.enter_context(patch.object(m, "alert"))
    st.enter_context(contextlib.redirect_stdout(io.StringIO()))
    cp = st.enter_context(patch.object(m, "create_policy", side_effect=created or (lambda p, label=None: {"_id": "a" * 24})))
    return st, cp


class TestFunctional(unittest.TestCase):
    def test_lockdown_dry_run_writes_nothing(self):
        st, cp = lockdown_env()
        with st:
            rc = m.main(["lockdown", "--preset", "lan_only", "--mac", FAKE, "--label", "t"])
        self.assertEqual(rc, 0); cp.assert_not_called()

    def test_lockdown_apply_creates_marked_policies(self):
        st, cp = lockdown_env()
        with st:
            rc = m.main(["lockdown", "--preset", "lan_only", "--mac", FAKE, "--label", "t", "--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(cp.called)
        for c in cp.call_args_list:
            self.assertIn(P.MARKER, c.args[0]["description"])
            self.assertFalse(c.args[0].get("predefined"))

    def test_lockdown_rolls_back_on_partial_failure(self):
        res = iter([{"_id": "a" * 24}, None])
        st, cp = lockdown_env(created=lambda p, label=None: next(res))
        with st, patch.object(m, "delete_policy", return_value=True) as dp:
            rc = m.main(["lockdown", "--preset", "cut_off", "--mac", FAKE, "--label", "t", "--apply"])
        self.assertEqual(P.policy_count(P.CUT_OFF), 2)      # second create fails -> first rolled back
        self.assertEqual(rc, 1); dp.assert_called_with("a" * 24)

    def test_lockdown_refuses_duplicate_label(self):
        existing = [{"_id": "a" * 24, "description": P.MARKER, "name": "House Arrest: Kid tablet — no LAN"}]
        st, cp = lockdown_env(policies_list=existing)
        with st:
            rc = m.main(["lockdown", "--preset", "lan_only", "--mac", FAKE, "--label", "kid TABLET", "--apply"])
        self.assertEqual(rc, 1); cp.assert_not_called()

    def test_release_requires_label(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(m.main(["release"]), 1)
        self.assertIn("label", out.getvalue().lower())

    def test_release_dry_run_deletes_nothing(self):
        pol = {"_id": "a" * 24, "description": P.MARKER, "name": "House Arrest: t — no LAN", "source": {}}
        with patch.object(m, "_login"), patch.object(m, "policies", return_value=[pol]), \
                patch.object(m, "acl_rules", return_value=[]), patch.object(m, "record"), \
                patch.object(m, "delete_policy") as dp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["release", "--label", "t"]), 0)
        dp.assert_not_called()

    def test_isolate_refuses_wan(self):
        with patch.object(m, "_login"), patch.object(m, "networks", return_value=[{"_id": "w" * 24, "name": "WAN", "purpose": "wan"}]):
            with self.assertRaises(SystemExit):
                m.main(["isolate", "--network", "WAN", "--preset", P.NETWORK_PRESETS[0], "--apply"])

    def test_dns_lockdown_rejects_bad_resolver(self):
        with self.assertRaises(ValueError):
            m.main(["dns-lockdown", "--network", "Guest", "--resolver", "8.8.8.8; rm -rf /"])


class TestFrame(unittest.TestCase):
    def test_vendored_builder_is_verbatim_upstream(self):
        text = (SCRIPTS / "unifi_ha_policies.py").read_text()
        body = text[text.index('"""\nHouse Arrest policy builder.'):]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), UPSTREAM_SHA256)

    def test_vendored_builder_is_pure(self):
        src = (SCRIPTS / "unifi_ha_policies.py").read_text()
        for mod in ("import os", "import socket", "import requests", "import aiohttp", "import subprocess", "open("):
            self.assertNotIn(mod, src)

    def test_help_and_subcommands_parse(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_unifi_toolkit.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in ("threats", "stalk", "audit", "track", "report", "lockdown", "release", "isolate",
                    "release-network", "dns-lockdown", "dns-release", "wlan-isolation", "mdns", "dhcp-dns", "block", "unblock"):
            self.assertIn(sub, r.stdout)

    def test_module_shape(self):
        for n in ("cmd_threats", "cmd_stalk", "cmd_audit", "cmd_lockdown", "cmd_release", "guard_devices",
                  "delete_policy", "delete_acl", "safe", "normalize_flow", "diff_clients"):
            self.assertTrue(callable(getattr(m, n)))
        for n in ("CertPinError", "_PinnedHTTPSConnection", "_NoRedirect", "repin"):
            self.assertTrue(hasattr(poller, n))

    def test_writes_default_to_dry_run(self):
        p = m.build_parser()
        for argv in (["lockdown", "--preset", "lan_only", "--mac", FAKE, "--label", "x"], ["release", "--label", "x"],
                     ["isolate", "--network", "G", "--preset", P.NETWORK_PRESETS[0]], ["dns-release", "--label", "x"],
                     ["wlan-isolation", "--ssid", "x", "on"], ["mdns", "--network", "G", "on"],
                     ["dhcp-dns", "--network", "G", "--resolver", "1.1.1.1"]):
            self.assertFalse(p.parse_args(argv).apply, argv)


if __name__ == "__main__":
    unittest.main()
