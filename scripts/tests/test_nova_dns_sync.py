#!/usr/bin/env python3
"""
test_nova_dns_sync.py — Tests for nova_dns_sync.py.

Focus (per task): the hosts-file builder + scp/ssh deploy.
  Unit      · slug(), derive_name(), build() (dry-run), write_hosts()
  Security  · a hostname with newline/space/`#` cannot inject extra host
              entries (slug neutralizes it → fqdn is one token, output line
              count is exactly what we expect); deploy targets are the fixed,
              validated DNS_NODES only (kochj@<node>, fixed dest path).

External deps (psycopg2 connection, subprocess scp/ssh) are fully mocked — no
live DB, no network, no ssh. build() is exercised in dry-run mode (conn=None)
so no DB is touched for the pure-logic assertions.

Written by Jordan Koch.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))
import nova_dns_sync as dns
from nova_dns_sync import (
    slug,
    derive_name,
    build,
    write_hosts,
    deploy,
    DOMAIN,
    SERVICE_ALIASES,
    DNS_NODES,
    HOSTS_OUT,
)


# ── Unit: slug() ─────────────────────────────────────────────────────────────

class TestSlug(unittest.TestCase):
    def test_lowercases_and_replaces_spaces(self):
        self.assertEqual(slug("My Laptop"), "my-laptop")

    def test_collapses_runs_of_separators(self):
        self.assertEqual(slug("a   b___c"), "a-b-c")

    def test_strips_leading_trailing_dashes(self):
        self.assertEqual(slug("--Foo--"), "foo")

    def test_none_and_empty(self):
        self.assertEqual(slug(None), "")
        self.assertEqual(slug(""), "")
        self.assertEqual(slug("   "), "")

    def test_only_special_chars_yields_empty(self):
        self.assertEqual(slug("#!@$%"), "")

    def test_alnum_preserved(self):
        self.assertEqual(slug("Pixel7Pro"), "pixel7pro")

    def test_newline_and_hash_become_separators(self):
        # The core security primitive: control/meta chars never survive.
        self.assertEqual(slug("evil\n1.2.3.4 host"), "evil-1-2-3-4-host")
        self.assertNotIn("\n", slug("a\nb"))
        self.assertNotIn("#", slug("a#b"))
        self.assertNotIn(" ", slug("a b"))


# ── Unit: derive_name() priority order ───────────────────────────────────────

class TestDeriveName(unittest.TestCase):
    def test_name_field_wins(self):
        c = {"name": "Office Printer", "hostname": "hp123", "mac": "aa:bb:cc:dd:ee:ff"}
        self.assertEqual(derive_name(c), "office-printer")

    def test_falls_through_to_device_name(self):
        c = {"name": "  ", "device_name": "Nest Cam", "mac": "aa:bb:cc:dd:ee:ff"}
        self.assertEqual(derive_name(c), "nest-cam")

    def test_falls_through_to_hostname(self):
        c = {"name": "", "device_name": "", "hostname": "Living-Room-TV", "mac": "1:2:3:4:5:6"}
        self.assertEqual(derive_name(c), "living-room-tv")

    def test_vendor_plus_mac_suffix(self):
        c = {"oui": "Ubiquiti", "mac": "aa:bb:cc:dd:ee:ff"}
        self.assertEqual(derive_name(c), "ubiquiti-eeff")

    def test_dev_vendor_fallback_field(self):
        c = {"dev_vendor": "Sonos", "mac": "11:22:33:44:55:66"}
        self.assertEqual(derive_name(c), "sonos-5566")

    def test_mac_only_dev_fallback(self):
        c = {"mac": "aa:bb:cc:dd:ee:ff"}
        self.assertEqual(derive_name(c), "dev-ddeeff")

    def test_no_usable_data_returns_none(self):
        self.assertIsNone(derive_name({}))
        self.assertIsNone(derive_name({"name": "###"}))

    def test_malicious_name_is_slugged(self):
        c = {"name": "a b\nc # d", "mac": "aa:bb:cc:dd:ee:ff"}
        n = derive_name(c)
        for bad in ("\n", " ", "#"):
            self.assertNotIn(bad, n)


# ── Unit: build() in dry-run mode (conn=None, no DB) ─────────────────────────

class TestBuildDryRun(unittest.TestCase):
    def test_basic_entries_plus_service_aliases(self):
        clients = [
            {"name": "Laptop", "ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:01"},
            {"name": "Phone", "ip": "192.168.1.51", "mac": "aa:bb:cc:dd:ee:02"},
        ]
        out = build(None, clients)
        d = dict(out)
        self.assertEqual(d["laptop.nova"], "192.168.1.50")
        self.assertEqual(d["phone.nova"], "192.168.1.51")
        # every static alias is always emitted
        for alias, ip in SERVICE_ALIASES.items():
            self.assertEqual(d[f"{alias}.{DOMAIN}"], ip)
        self.assertEqual(len(out), 2 + len(SERVICE_ALIASES))

    def test_skips_client_missing_mac(self):
        out = build(None, [{"name": "x", "ip": "1.2.3.4"}])
        # only service aliases remain
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_skips_client_missing_ip(self):
        out = build(None, [{"name": "x", "mac": "aa:bb:cc:dd:ee:03"}])
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_skips_client_with_no_derivable_name(self):
        out = build(None, [{"ip": "1.2.3.4"}])  # no mac -> derive_name None
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_ip_fallback_fields(self):
        out = dict(build(None, [{"name": "a", "mac": "aa:bb:cc:dd:ee:04", "fixed_ip": "10.0.0.9"}]))
        self.assertEqual(out["a.nova"], "10.0.0.9")
        out2 = dict(build(None, [{"name": "b", "mac": "aa:bb:cc:dd:ee:05", "last_ip": "10.0.0.8"}]))
        self.assertEqual(out2["b.nova"], "10.0.0.8")

    def test_collision_dedup_appends_mac_tail(self):
        clients = [
            {"name": "printer", "ip": "1.1.1.1", "mac": "aa:bb:cc:dd:ee:aa"},
            {"name": "printer", "ip": "1.1.1.2", "mac": "aa:bb:cc:dd:ee:bb"},
        ]
        fqdns = [f for f, _ in build(None, clients)]
        self.assertIn("printer.nova", fqdns)
        self.assertIn("printer-eebb.nova", fqdns)

    def test_dry_run_does_not_touch_db(self):
        # conn=None path must never attempt a cursor / DB call.
        out = build(None, [{"name": "z", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:0f"}])
        self.assertIn(("z.nova", "1.2.3.4"), out)


# ── build() with a mocked DB connection (sticky + upsert) ────────────────────

class TestBuildWithMockedDB(unittest.TestCase):
    def _make_conn(self, existing_row):
        cur = MagicMock()
        cur.fetchone.return_value = existing_row
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_sticky_name_kept_from_db(self):
        conn, cur = self._make_conn(("established-name", False))
        clients = [{"name": "BrandNew", "ip": "9.9.9.9", "mac": "aa:bb:cc:dd:ee:07"}]
        out = dict(build(conn, clients))
        # existing DB name wins over freshly-derived "brandnew"
        self.assertIn("established-name.nova", out)
        self.assertNotIn("brandnew.nova", out)

    def test_upsert_executed_for_client(self):
        conn, cur = self._make_conn(None)  # no existing row
        clients = [{"name": "New Dev", "ip": "9.9.9.8", "mac": "aa:bb:cc:dd:ee:08"}]
        build(conn, clients)
        sqls = " ".join(c.args[0] for c in cur.execute.call_args_list if c.args)
        self.assertIn("INSERT INTO dns_records", sqls)
        self.assertIn("ON CONFLICT (mac) DO UPDATE", sqls)

    def test_upsert_uses_parameterized_query(self):
        # No f-string SQL: mac/name/ip pass as bind params, not interpolated.
        conn, cur = self._make_conn(None)
        build(conn, [{"name": "p", "ip": "9.9.9.7", "mac": "aa:bb:cc:dd:ee:09"}])
        insert_calls = [c for c in cur.execute.call_args_list
                        if c.args and "INSERT" in c.args[0]]
        self.assertTrue(insert_calls)
        for c in insert_calls:
            self.assertEqual(len(c.args), 2)          # (sql, params)
            self.assertIsInstance(c.args[1], tuple)   # params bound separately


# ── Unit: write_hosts() ──────────────────────────────────────────────────────

class TestWriteHosts(unittest.TestCase):
    def _write(self, entries):
        tmp = Path(self.tmpdir) / "hosts"
        n = write_hosts(entries, path=str(tmp))
        return n, tmp.read_text()

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmpdir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_header_and_line_count(self):
        entries = [("b.nova", "1.1.1.2"), ("a.nova", "1.1.1.1")]
        n, text = self._write(entries)
        self.assertEqual(n, 2)
        self.assertTrue(text.startswith("# generated by nova_dns_sync"))

    def test_sorted_by_fqdn(self):
        entries = [("b.nova", "1.1.1.2"), ("a.nova", "1.1.1.1")]
        _, text = self._write(entries)
        body = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(body, ["1.1.1.1 a.nova", "1.1.1.2 b.nova"])

    def test_line_format_ip_then_fqdn(self):
        _, text = self._write([("host.nova", "10.0.0.1")])
        self.assertIn("10.0.0.1 host.nova", text)


# ── Security: hostname cannot inject extra host entries ──────────────────────

class TestHostsInjectionInvariant(unittest.TestCase):
    """A crafted UniFi client 'name' with newline/space/# must NOT produce
    more than one host line, and must not smuggle a second mapping."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.path = str(Path(self._td.name) / "hosts")

    def tearDown(self):
        self._td.cleanup()

    def test_newline_in_name_does_not_add_a_line(self):
        malicious = {
            "name": "good\n6.6.6.6 evil.attacker",   # tries to append a 2nd entry
            "ip": "192.168.1.99",
            "mac": "aa:bb:cc:dd:ee:99",
        }
        entries = build(None, [malicious])
        n = write_hosts(entries, path=self.path)
        text = Path(self.path).read_text()
        body = [l for l in text.splitlines() if l and not l.startswith("#")]
        # exactly one device line + the fixed service aliases; no injected line
        self.assertEqual(n, 1 + len(SERVICE_ALIASES))
        self.assertEqual(len(body), 1 + len(SERVICE_ALIASES))
        self.assertNotIn("6.6.6.6", text)
        self.assertNotIn("evil.attacker", text)

    def test_derived_fqdn_has_no_injection_chars(self):
        for bad_name in ("a b", "a#b", "a\nb", "a\tb", "a;b", 'a"b'):
            c = {"name": bad_name, "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:12"}
            (fqdn, _), = [e for e in build(None, [c]) if e[0].endswith(".nova")
                          and e[0] not in {f"{a}.{DOMAIN}" for a in SERVICE_ALIASES}]
            for ch in (" ", "#", "\n", "\t", ";", '"'):
                self.assertNotIn(ch, fqdn)

    def test_space_in_name_stays_one_token(self):
        c = {"name": "Router Guest Net", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:13"}
        entries = build(None, [c])
        write_hosts(entries, path=self.path)
        for line in Path(self.path).read_text().splitlines():
            if line.startswith("#") or not line:
                continue
            # each non-comment line is exactly "IP FQDN" — two whitespace tokens
            self.assertEqual(len(line.split()), 2, line)


# ── Security / Unit: deploy() targets are fixed & validated, best-effort ─────

class TestDeploy(unittest.TestCase):
    @patch("nova_dns_sync.subprocess.run")
    def test_targets_are_only_fixed_dns_nodes(self, mrun):
        mrun.return_value = MagicMock(returncode=0)
        ok = deploy(path="/tmp/x")
        self.assertEqual(ok, DNS_NODES)
        # every scp/ssh remote target is kochj@<one of DNS_NODES>, never attacker-controlled
        seen_nodes = set()
        for c in mrun.call_args_list:
            argv = c.args[0]
            remote = [a for a in argv if isinstance(a, str) and a.startswith("kochj@")]
            self.assertTrue(remote, argv)
            node = remote[0].split("@", 1)[1].split(":", 1)[0]
            seen_nodes.add(node)
            self.assertIn(node, DNS_NODES)
        self.assertEqual(seen_nodes, set(DNS_NODES))

    @patch("nova_dns_sync.subprocess.run")
    def test_scp_then_ssh_per_node(self, mrun):
        mrun.return_value = MagicMock(returncode=0)
        deploy(path="/tmp/x")
        # two nodes -> scp + ssh each = 4 subprocess invocations
        self.assertEqual(mrun.call_count, 2 * len(DNS_NODES))
        prog0 = [c.args[0][0] for c in mrun.call_args_list]
        self.assertEqual(prog0.count("scp"), len(DNS_NODES))
        self.assertEqual(prog0.count("ssh"), len(DNS_NODES))

    @patch("nova_dns_sync.subprocess.run")
    def test_argv_list_form_no_shell(self, mrun):
        mrun.return_value = MagicMock(returncode=0)
        deploy(path="/tmp/x")
        for c in mrun.call_args_list:
            self.assertIsInstance(c.args[0], list)   # list argv, not a shell string
            self.assertNotIn("shell", c.kwargs)       # never shell=True

    @patch("nova_dns_sync.subprocess.run")
    def test_fixed_remote_dest_path(self, mrun):
        mrun.return_value = MagicMock(returncode=0)
        deploy(path="/tmp/whatever")
        scp_calls = [c for c in mrun.call_args_list if c.args[0][0] == "scp"]
        for c in scp_calls:
            dest = c.args[0][-1]
            self.assertTrue(dest.endswith(":/tmp/nova_dns_hosts"), dest)

    @patch("nova_dns_sync.subprocess.run")
    def test_best_effort_one_node_fails(self, mrun):
        import subprocess as sp

        def side(argv, **kw):
            # fail the very first node's scp, succeed everything else
            if argv[0] == "scp" and f"kochj@{DNS_NODES[0]}" in argv[-1]:
                raise sp.CalledProcessError(1, argv)
            return MagicMock(returncode=0)

        mrun.side_effect = side
        ok = deploy(path="/tmp/x")
        self.assertNotIn(DNS_NODES[0], ok)
        self.assertIn(DNS_NODES[1], ok)

    @patch("nova_dns_sync.subprocess.run")
    def test_all_nodes_fail_returns_empty(self, mrun):
        mrun.side_effect = TimeoutError("boom")
        self.assertEqual(deploy(path="/tmp/x"), [])

    @patch("nova_dns_sync.subprocess.run")
    def test_ssh_reload_command_is_static_string(self, mrun):
        # The remote command is a fixed literal — no client data interpolated.
        mrun.return_value = MagicMock(returncode=0)
        deploy(path="/tmp/x")
        ssh_calls = [c for c in mrun.call_args_list if c.args[0][0] == "ssh"]
        for c in ssh_calls:
            remote_cmd = c.args[0][-1]
            self.assertIn("dnsmasq", remote_cmd)
            self.assertIn("/etc/nova_dns_hosts", remote_cmd)


if __name__ == "__main__":
    unittest.main()
