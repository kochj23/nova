#!/usr/bin/env python3
"""Tests for nova_hendershot_cellar.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hendershot_cellar.py"
spec = importlib.util.spec_from_file_location("cellar_under_test", SCRIPT)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-SECRET-VALUE-9f3a"

PROBE_OUT = ("@@SUDO\nUser k may run the following commands on h:\n    (ALL) ALL\n    (ALL) NOPASSWD: ALL\n"
             "    (root) NOPASSWD: /usr/bin/tool a, /usr/bin/other\n@@JSTART\n1780000000.0\n"
             "@@SUDOUSE\n1792000000.0 /usr/bin/apt\n@@PS\nnova_live.py\n-m nova_pkg\n@@FS\n/dev/sda1\n@@END\n")

KEYCHAIN = ('keychain: "k"\nclass: "genp"\nattributes:\n    "acct"<blob>="nova"\n'
            '    "mdat"<timedate>=0x00  "20260506024048Z\\000"\n    "svce"<blob>="nova-old-token"\n'
            'keychain: "k"\nclass: "genp"\nattributes:\n    "acct"<blob>="nova"\n    "svce"<blob>="nova-live-token"\n'
            'keychain: "k"\nclass: "inet"\nattributes:\n    "acct"<blob>="nova"\n    "srvr"<blob>="x"\n'
            'keychain: "k"\nclass: "genp"\nattributes:\n    "acct"<blob>="someone"\n    "svce"<blob>="Bank"\n')


def _ha_auth_file(tmp: Path) -> Path:
    st = tmp / ".storage"
    st.mkdir(parents=True, exist_ok=True)
    (st / "auth").write_text(json.dumps({"version": 1, "data": {
        "users": [{"id": "u1", "password": SENTINEL}],
        "refresh_tokens": [
            {"id": "aaaaaaaa1", "client_name": "Nova", "token_type": "long_lived_access_token",
             "token": SENTINEL, "jwt_key": SENTINEL, "created_at": "2026-06-01T00:00:00+00:00",
             "last_used_at": "2026-10-14T00:00:00+00:00"},
            {"id": "bbbbbbbb2", "client_id": "http://10.1.2.3:8123/", "token_type": "normal",
             "token": SENTINEL, "jwt_key": SENTINEL, "created_at": "2026-06-01T00:00:00+00:00",
             "last_used_at": "2026-08-01T00:00:00+00:00"},
            {"id": "cccccccc3", "token_type": "system", "token": SENTINEL}]}}))
    return tmp


def _ctx(tmp: Path, probes=None, **over):
    idx = {"nova_live": "import nova_common\n", "nova_common": "t = get('nova-live-token')\n",
           "nova_dead": "t = get('nova-dead-token')\n", "nova_pkg.mod": "x = 1\n"}
    ctx = {"now": NOW, "me": "studio", "probes": probes if probes is not None else {"h": C.parse_probe(PROBE_OUT)},
           "index": idx, "graph": C.import_graph(idx),
           "refs": C.name_refs({"nova-live-token", "nova-dead-token", "nova-old-token", "nova-apps-only"},
                               {**idx, "repo:apps/x.py": "nova-apps-only"}),
           "runs": {"nova_dead.py": NOW - timedelta(days=60)}, "runs_since": NOW - timedelta(days=150),
           "live": {"nova_live.py": "h"}, "fleet_secrets": {"nova-dead-token": NOW, "nova-apps-only": NOW},
           "keychain": C.keychain_names(KEYCHAIN), "threshold_names": {"nova-dead-token"},
           "ssh_keys": [("h", "SHA256:abcdefghijklmnopqrstuvwxyz", "me@laptop", NOW)],
           "config_tokens": [("svc", "api_token", NOW)],
           "ha_auth": C.ha_storage(_ha_auth_file(tmp), "auth"),
           "claude": {"mcp": ["used", "idle"], "allow": ["Bash(git push:*)"]},
           "transcript_use": {"mcp:used": NOW - timedelta(days=1)}, "transcripts_since": NOW - timedelta(days=31),
           "gateway_registry": (["read_tool", "act_tool"], {"read_tool"}),
           "gateway_use": ({"read_tool": NOW - timedelta(days=2)}, NOW - timedelta(days=7))}
    ctx.update(over)
    return ctx


def _classified(ctx):
    rows, scopes, unknown = C.inventory(None, ctx)
    for r in rows:
        r["status"], r["days_dormant"] = C.classify(r, NOW)
    return rows, scopes, unknown


class Security(unittest.TestCase):
    def test_never_reads_a_secret_value_in_source(self):
        self.assertNotIn("find-generic-password", SRC)
        self.assertNotRegex(SRC, r"\bget_secret\(|\bvault_secret\(|import nova_secrets|from nova_secrets")
        self.assertNotRegex(SRC, r"SELECT[^\"\n]*ciphertext")
        self.assertIn('["security", "dump-keychain"]', SRC)
        self.assertNotRegex(SRC, r"dump-keychain\"?,\s*\"-d\"")

    def test_keychain_listing_never_asks_for_values(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(list(args))
            return mock.Mock(stdout=KEYCHAIN, returncode=0)
        with mock.patch.object(C.subprocess, "run", side_effect=fake_run):
            names = C.keychain_names()
        self.assertEqual(sorted(names), ["nova-live-token", "nova-old-token"])
        for a in calls:
            self.assertNotIn("-w", a)
            self.assertNotIn("-d", a)
            self.assertNotIn("find-generic-password", a)

    def test_ha_parse_drops_every_secret_value(self):
        with tempfile.TemporaryDirectory() as d:
            parsed = C.ha_storage(_ha_auth_file(Path(d)), "auth")
        blob = json.dumps(parsed)
        self.assertNotIn(SENTINEL, blob)
        self.assertNotIn("jwt_key", blob)
        self.assertNotIn("password", blob)

    def test_no_secret_value_or_ip_is_ever_printed(self):
        with tempfile.TemporaryDirectory() as d:
            rows, _, _ = _classified(_ctx(Path(d)))
        out = "\n".join(C.line(r) for r in rows) + C.digest(rows, C.summarize(rows), 0)[1] + json.dumps(rows, default=str)
        self.assertNotIn(SENTINEL, out)
        self.assertNotRegex(out, r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

    def test_sql_is_parameterized_and_no_hardcoded_paths(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"/Users/|192\.168\.")


class Performance(unittest.TestCase):
    def test_name_scan_scales(self):
        idx = {f"nova_m{i}": f"import nova_m{i + 1}\nx = 'nova-name-{i % 300}'\n" * 20 for i in range(900)}
        names = {f"nova-name-{i}" for i in range(300)}
        t = time.monotonic()
        refs = C.name_refs(names, idx)
        g = C.import_graph(idx)
        C.usage_for("nova-name-7", refs, g, {}, {}, NOW)
        self.assertLess(time.monotonic() - t, 20.0)
        self.assertEqual(len(refs), 900)

    def test_classify_is_fast(self):
        r = C._row("secret", "s", "n", None, "", NOW - timedelta(days=90))
        t = time.monotonic()
        for _ in range(20000):
            C.classify(r, NOW)
        self.assertLess(time.monotonic() - t, 2.0)


class Retry(unittest.TestCase):
    def test_probe_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(ip, local, timeout=25):
            calls["n"] += 1
            if calls["n"] == 1:
                raise C.subprocess.TimeoutExpired("ssh", 25)
            return C.parse_probe(PROBE_OUT)
        with mock.patch.object(C, "_run_probe", side_effect=flaky):
            p = C.probe_host("h", "host.invalid", False, _sleep=lambda s: None)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(p["sudo_rules"][0], "(ALL) NOPASSWD: ALL")

    def test_unreachable_node_is_unknown_not_a_crash(self):
        with mock.patch.object(C, "_run_probe", return_value=None):
            self.assertIsNone(C.probe_host("h", "host.invalid", False, _sleep=lambda s: None))
        with tempfile.TemporaryDirectory() as d:
            rows, scopes, unknown = _classified(_ctx(Path(d), probes={"h": None}))
        self.assertIn("sudo rules on h", unknown)
        self.assertNotIn(("sudo_rule", "h"), scopes)          # an unread node can never mark rules revoked

    def test_every_source_missing_still_runs(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), fleet_secrets=None, keychain=None, ha_auth=None, transcript_use=None,
                       gateway_registry=(None, set()), probes={})
            rows, scopes, unknown = _classified(ctx)
        self.assertTrue({"fleet secret store", "keychain", "home assistant auth store", "claude transcripts",
                         "gateway tool registry"} <= set(unknown))
        self.assertTrue(all(r["status"] in ("unknown", "active", "proposed") for r in rows))

    def test_transcripts_without_rg_are_unknown(self):
        with mock.patch.object(C.shutil, "which", return_value=None), \
                mock.patch.object(C.os, "access", return_value=False):
            self.assertEqual(C.transcript_use({"k": "x"}), (None, None))


class Unit(unittest.TestCase):
    def test_parse_probe(self):
        p = C.parse_probe(PROBE_OUT)
        self.assertEqual(p["sudo_rules"], ["(ALL) NOPASSWD: ALL", "(root) NOPASSWD: /usr/bin/tool a, /usr/bin/other"])
        self.assertEqual(p["scripts_fs"], "local")
        self.assertIn("/usr/bin/apt", p["sudo_use"])
        self.assertFalse(C.parse_probe("@@SUDO\n")["complete"])
        self.assertEqual(C.parse_probe("@@FS\n//u@h/share\n@@END\n")["scripts_fs"], "network")

    def test_classify(self):
        r = C._row("k", "h", "c", None, "", NOW - timedelta(days=100), NOW - timedelta(days=45))
        self.assertEqual(C.classify(r, NOW), ("proposed", 45))
        self.assertEqual(C.classify(dict(r, held_since=NOW - timedelta(days=5)), NOW), ("unknown", 5))
        self.assertEqual(C.classify(dict(r, last_used=NOW - timedelta(days=30)), NOW), ("proposed", 30))
        self.assertEqual(C.classify(dict(r, last_used=NOW - timedelta(days=29)), NOW), ("active", 29))
        self.assertEqual(C.classify(dict(r, history_since=None), NOW), ("unknown", None))

    def test_helpers(self):
        self.assertEqual(C.allow_needle("Bash(git push:*)"), '"command":"git push')
        self.assertIsNone(C.allow_needle("WebFetch(domain:x)"))
        self.assertEqual(C.runnable("nova_gateway.tools"), "tools.py")
        self.assertEqual(C.runnable("nova_x.sh"), "nova_x.sh")
        self.assertEqual(C.module_key(SCRIPTS / "nova_gateway" / "__init__.py"), "nova_gateway")

    def test_usage_follows_imports_and_live_processes(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d))
        lu, ev, obs = C.usage_for("nova-live-token", ctx["refs"], ctx["graph"], ctx["runs"], ctx["live"], NOW)
        self.assertEqual(lu, NOW)
        self.assertIn("live process nova_live.py", ev)
        self.assertFalse(C.usage_for("nova-apps-only", ctx["refs"], ctx["graph"], {}, {}, NOW)[2])


class Integration(unittest.TestCase):
    def test_upsert_is_read_only_elsewhere_and_scoped(self):
        class Cur:
            def __init__(self): self.q, self.rowcount = [], 1
            def execute(self, sql, args=None): self.q.append((sql, args))
        cur = Cur()
        rows = [dict(C._row("secret", "fleet-store", "n", None, "e", NOW), status="proposed", days_dormant=40)]
        with mock.patch.object(C, "ensure_schema"):
            gone = C.upsert(cur, rows, [("secret", "fleet-store"), ("config_token", "*")], NOW)
        sql = " ".join(q for q, _ in cur.q)
        self.assertNotIn("DELETE", sql)
        self.assertIn("WHEN cellar_inventory.status='kept' THEN 'kept'", sql)   # a human 'kept' survives
        self.assertTrue(all("cellar_inventory" in q for q, _ in cur.q))
        self.assertEqual(gone, 2)

    def test_run_dry_writes_nothing(self):
        class Cur:
            def __init__(self): self.q = []
            def execute(self, sql, args=None): self.q.append(sql)
            def fetchone(self): return None
            def fetchall(self): return []
        cur = Cur()
        conn = mock.Mock(cursor=lambda: cur)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(C.W, "connect", return_value=conn), \
                mock.patch.object(C, "gather", return_value=_ctx(Path(d))), redirect_stdout(io.StringIO()) as out:
            rows = C.run(dry=True)
        self.assertTrue(rows)
        self.assertFalse([q for q in cur.q if re.search(r"\b(INSERT|UPDATE|DELETE|CREATE)\b", q)])
        self.assertIn("would record", out.getvalue())


class Functional(unittest.TestCase):
    def test_inventory_finds_the_dormant_and_spares_the_live(self):
        with tempfile.TemporaryDirectory() as d:
            rows, _, _ = _classified(_ctx(Path(d)))
        st = {(r["kind"], r["holder"], r["capability"].split(" ")[0]): r["status"] for r in rows}
        self.assertEqual(st[("secret", "fleet-store", "nova-dead-token")], "proposed")       # last run 60d ago
        self.assertEqual(st[("secret", "keychain:studio", "nova-live-token")], "active")     # live process
        self.assertEqual(st[("secret", "keychain:studio", "nova-old-token")], "proposed")    # nothing names it
        self.assertEqual(st[("secret", "fleet-store", "nova-apps-only")], "unknown")         # use not logged
        self.assertEqual(st[("mcp_server", "claude-code", "idle")], "proposed")
        self.assertEqual(st[("mcp_server", "claude-code", "used")], "active")
        self.assertEqual(st[("ssh_key", "h", "me@laptop")], "unknown")
        self.assertEqual(st[("gateway_tool", "nova-gateway", "act_tool")], "unknown")        # 7d of history only
        ha = {r["capability"].split(":")[0]: r["status"] for r in rows if r["kind"] == "ha_token"}
        self.assertEqual(ha, {"long_lived_access_token": "active", "normal": "proposed"})
        sudo = {r["capability"]: r["status"] for r in rows if r["kind"] == "sudo_rule"}
        self.assertEqual(sudo["(ALL) NOPASSWD: ALL"], "active")

    def test_digest_lists_top_dormant_and_measures(self):
        with tempfile.TemporaryDirectory() as d:
            rows, _, _ = _classified(_ctx(Path(d)))
        s = C.summarize(rows)
        title, body = C.digest(rows, s, 3, top=2)
        self.assertIn(f"{s['dormant']} of {s['held']}", title)
        self.assertIn("revoked since last month 3", body)
        self.assertIn("nothing was changed", body)
        self.assertEqual(body.count("\n• "), 2)
        self.assertIn("more: nova_hendershot_cellar.py --report", body)


class Frame(unittest.TestCase):
    def test_cli_flags(self):
        for f in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(f'"{f}"', SRC)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            C.main(["--help"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(C.main(["--selftest"]), 0)

    def test_docstring_house_style(self):
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("Maximum Overdrive", SRC)
        self.assertIn("Threshold Ledger", SRC)

    def test_read_only_never_acts(self):
        self.assertNotRegex(SRC, r"systemctl (restart|stop)|launchctl (kickstart|unload|bootout)|\brm -|"
                                 r"authorized_keys.*write|delete_secret|set_secret|\.write_text\(")
        self.assertEqual(set(re.findall(r"sudo -n [a-z-]+", SRC)), {"sudo -n -l"})


if __name__ == "__main__":
    unittest.main()
