#!/usr/bin/env python3
"""
test_nova_dns_sync.py — Tests for nova_dns_sync.py.

Focus: the sticky-name builder + the nsupdate push into the BIND9 primary.
  Unit      · slug(), derive_name(), build() (dry-run + mocked PG),
              push_bind() nsupdate script generation, resolve_public(),
              push_public_mirrors()
  Security  · a hostname with newline/space/`#` cannot inject extra DNS
              records (slug neutralizes it → fqdn is one token, the nsupdate
              script has exactly the expected number of `update add` lines);
              the push targets are the fixed BIND_PRIMARY / DOMAIN zone only,
              nsupdate is invoked as an argv list (never shell=True) with the
              TSIG secret passed via -y, and the script itself never carries
              the secret; public mirrors are resolved via an EXTERNAL
              resolver, never our own BIND.

External deps (psycopg2 connection, subprocess nsupdate/dig, Keychain secrets)
are fully mocked — no live DB, no network, no nsupdate. build() is exercised in
dry-run mode (conn=None) so no DB is touched for the pure-logic assertions.

History: this file originally covered write_hosts()/deploy() (a dnsmasq hosts
file scp'd to DNS_NODES). Commit c58225d replaced that with push_bind() —
authenticated nsupdate into BIND — and the zone moved from `.nova` to DOMAIN.
The invariants are preserved one-for-one against the new API.

Written by Jordan Koch.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import nova_dns_sync as dns
from nova_dns_sync import (
    slug,
    derive_name,
    build,
    push_bind,
    push_public_mirrors,
    resolve_public,
    DOMAIN,
    SERVICE_ALIASES,
    FAILOVER_ALIASES,
    FAILOVER_TTL,
    BIND_PRIMARY,
    TSIG_KEY_NAME,
    PUBLIC_MIRRORS,
)

SECRET = "unit-test-tsig-secret"
ALIAS_FQDNS = {f"{a}.{DOMAIN}" for a in SERVICE_ALIASES}


def _fq(name):
    return f"{name}.{DOMAIN}"


def _script_from(mrun):
    """The nsupdate script push_bind()/push_public_mirrors() piped on stdin."""
    assert mrun.call_count == 1, mrun.call_args_list
    return mrun.call_args.kwargs["input"]


def _script_lines(mrun):
    return [l for l in _script_from(mrun).splitlines() if l]


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
        self.assertEqual(d[_fq("laptop")], "192.168.1.50")
        self.assertEqual(d[_fq("phone")], "192.168.1.51")
        # every static alias is always emitted
        for alias, ip in SERVICE_ALIASES.items():
            self.assertEqual(d[_fq(alias)], ip)
        self.assertEqual(len(out), 2 + len(SERVICE_ALIASES))

    def test_entries_are_fqdns_in_domain(self):
        out = build(None, [{"name": "x", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:03"}])
        for fqdn, _ in out:
            self.assertTrue(fqdn.endswith(f".{DOMAIN}"), fqdn)
            self.assertFalse(fqdn.endswith("."), fqdn)   # push_bind adds the trailing dot

    def test_skips_client_missing_mac(self):
        out = build(None, [{"name": "x", "ip": "1.2.3.4"}])
        # only service aliases remain
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_skips_client_missing_ip(self):
        out = build(None, [{"name": "x", "mac": "aa:bb:cc:dd:ee:03"}])
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_skips_client_with_no_derivable_name(self):
        # has mac + ip but nothing usable to name it from -> derive_name() None -> dropped
        with patch.object(dns, "derive_name", return_value=None):
            out = build(None, [{"ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:03"}])
        self.assertEqual(len(out), len(SERVICE_ALIASES))

    def test_ip_fallback_fields(self):
        out = dict(build(None, [{"name": "a", "mac": "aa:bb:cc:dd:ee:04", "fixed_ip": "10.0.0.9"}]))
        self.assertEqual(out[_fq("a")], "10.0.0.9")
        out2 = dict(build(None, [{"name": "b", "mac": "aa:bb:cc:dd:ee:05", "last_ip": "10.0.0.8"}]))
        self.assertEqual(out2[_fq("b")], "10.0.0.8")

    def test_collision_dedup_appends_mac_tail(self):
        clients = [
            {"name": "printer", "ip": "1.1.1.1", "mac": "aa:bb:cc:dd:ee:aa"},
            {"name": "printer", "ip": "1.1.1.2", "mac": "aa:bb:cc:dd:ee:bb"},
        ]
        fqdns = [f for f, _ in build(None, clients)]
        self.assertIn(_fq("printer"), fqdns)
        self.assertIn(_fq("printer-eebb"), fqdns)

    def test_dry_run_does_not_touch_db(self):
        # conn=None path must never attempt a cursor / DB call.
        out = build(None, [{"name": "z", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:0f"}])
        self.assertIn((_fq("z"), "1.2.3.4"), out)

    def test_service_alias_overrides_sticky_client_name(self):
        # A client whose derived name collides with a static alias: the alias entry is
        # still emitted with the authoritative IP (file wins — see mac-mini comment).
        alias, alias_ip = next(iter(SERVICE_ALIASES.items()))
        out = build(None, [{"name": alias, "ip": "10.9.9.9", "mac": "aa:bb:cc:dd:ee:10"}])
        self.assertIn((_fq(alias), alias_ip), out)


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
        self.assertIn(_fq("established-name"), out)
        self.assertNotIn(_fq("brandnew"), out)

    def test_schema_ensured_when_conn_given(self):
        conn, cur = self._make_conn(None)
        build(conn, [])
        sqls = " ".join(c.args[0] for c in cur.execute.call_args_list if c.args)
        self.assertIn("CREATE TABLE IF NOT EXISTS dns_records", sqls)

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


# ── Unit: push_bind() nsupdate script generation ─────────────────────────────
# (successor of the write_hosts() tests: header, one record per entry, line format)

@patch("nova_dns_sync.tsig_secret", return_value=SECRET)
@patch("nova_dns_sync.subprocess.run")
class TestPushBindScript(unittest.TestCase):
    def _push(self, mrun, entries, **kw):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        ok = push_bind(entries, **kw)
        return ok, _script_lines(mrun)

    def test_header_and_footer(self, mrun, _sec):
        ok, lines = self._push(mrun, [(_fq("a"), "1.1.1.1")])
        self.assertTrue(ok)
        self.assertEqual(lines[0], f"server {BIND_PRIMARY}")
        self.assertEqual(lines[1], f"zone {DOMAIN}.")
        self.assertEqual(lines[-1], "send")
        self.assertTrue(_script_from(mrun).endswith("\n"))

    def test_delete_then_add_per_entry(self, mrun, _sec):
        entries = [(_fq("b"), "1.1.1.2"), (_fq("a"), "1.1.1.1")]
        _, lines = self._push(mrun, entries)
        body = lines[2:-1]
        self.assertEqual(len(body), 2 * len(entries))
        # idempotent: each record is deleted then re-added so renames don't leave stale A's
        self.assertEqual(body[0], f"update delete {_fq('b')}. A")
        self.assertEqual(body[1], f"update add {_fq('b')}. 300 A 1.1.1.2")
        self.assertEqual(body[2], f"update delete {_fq('a')}. A")
        self.assertEqual(body[3], f"update add {_fq('a')}. 300 A 1.1.1.1")

    def test_line_format_fqdn_ttl_a_ip(self, mrun, _sec):
        _, lines = self._push(mrun, [(_fq("host"), "10.0.0.1")])
        add = [l for l in lines if l.startswith("update add")]
        self.assertEqual(add, [f"update add {_fq('host')}. 300 A 10.0.0.1"])

    def test_custom_ttl_applied_to_ordinary_records(self, mrun, _sec):
        _, lines = self._push(mrun, [(_fq("host"), "10.0.0.1")], ttl=1200)
        self.assertIn(f"update add {_fq('host')}. 1200 A 10.0.0.1", lines)

    def test_failover_aliases_get_short_ttl(self, mrun, _sec):
        # queue #2656: pg-primary / memory-server must flip within a minute of a re-point
        entries = [(_fq(a), "192.168.1.2") for a in FAILOVER_ALIASES] + [(_fq("plain"), "10.0.0.2")]
        _, lines = self._push(mrun, entries, ttl=300)
        for a in FAILOVER_ALIASES:
            self.assertIn(f"update add {_fq(a)}. {FAILOVER_TTL} A 192.168.1.2", lines)
        self.assertIn(f"update add {_fq('plain')}. 300 A 10.0.0.2", lines)
        self.assertLess(FAILOVER_TTL, 300)

    def test_failover_aliases_are_in_service_aliases(self, mrun, _sec):
        # the short-TTL set must name real aliases, otherwise the rule silently does nothing
        self.assertTrue(FAILOVER_ALIASES <= set(SERVICE_ALIASES))

    def test_empty_entries_still_well_formed(self, mrun, _sec):
        ok, lines = self._push(mrun, [])
        self.assertTrue(ok)
        self.assertEqual(lines, [f"server {BIND_PRIMARY}", f"zone {DOMAIN}.", "send"])

    def test_returns_false_when_nsupdate_fails(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=1, stderr="REFUSED")
        with patch("builtins.print"):
            self.assertFalse(push_bind([(_fq("a"), "1.1.1.1")]))


# ── Security: hostname cannot inject extra DNS records ───────────────────────
# (successor of the hosts-file injection invariant)

@patch("nova_dns_sync.tsig_secret", return_value=SECRET)
@patch("nova_dns_sync.subprocess.run")
class TestNsupdateInjectionInvariant(unittest.TestCase):
    """A crafted UniFi client 'name' with newline/space/# must NOT produce more
    than one `update add` line, and must not smuggle a second mapping."""

    def test_newline_in_name_does_not_add_a_record(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        malicious = {
            "name": "good\n6.6.6.6 evil.attacker",   # tries to append a 2nd entry
            "ip": "192.168.1.99",
            "mac": "aa:bb:cc:dd:ee:99",
        }
        entries = build(None, [malicious])
        push_bind(entries)
        script = _script_from(mrun)
        lines = [l for l in script.splitlines() if l]
        adds = [l for l in lines if l.startswith("update add")]
        # exactly one device record + the fixed service aliases; no injected record
        self.assertEqual(len(entries), 1 + len(SERVICE_ALIASES))
        self.assertEqual(len(adds), 1 + len(SERVICE_ALIASES))
        self.assertEqual(len(lines), 3 + 2 * len(entries))
        self.assertNotIn("6.6.6.6", script)
        self.assertNotIn("evil.attacker", script)
        # every update statement is exactly a delete/add of a built entry — the injected
        # text cannot become its own nsupdate statement
        expected = set()
        for fqdn, ip in entries:
            expected.add(f"update delete {fqdn}. A")
            ttl = FAILOVER_TTL if fqdn.split(".")[0] in FAILOVER_ALIASES else 300
            expected.add(f"update add {fqdn}. {ttl} A {ip}")
        self.assertEqual({l for l in lines if l.startswith("update")}, expected)

    def test_derived_fqdn_has_no_injection_chars(self, mrun, _sec):
        for bad_name in ("a b", "a#b", "a\nb", "a\tb", "a;b", 'a"b'):
            c = {"name": bad_name, "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:12"}
            (fqdn, _), = [e for e in build(None, [c]) if e[0].endswith(f".{DOMAIN}")
                          and e[0] not in ALIAS_FQDNS]
            for ch in (" ", "#", "\n", "\t", ";", '"'):
                self.assertNotIn(ch, fqdn)

    def test_space_in_name_stays_one_token(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        c = {"name": "Router Guest Net", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:13"}
        push_bind(build(None, [c]))
        for line in _script_lines(mrun):
            if line.startswith("update add"):
                # "update add <fqdn>. <ttl> A <ip>" — exactly six whitespace tokens
                self.assertEqual(len(line.split()), 6, line)
            elif line.startswith("update delete"):
                self.assertEqual(len(line.split()), 4, line)

    def test_crafted_name_cannot_retarget_server_or_zone(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        c = {"name": "x\nserver 6.6.6.6\nzone evil.", "ip": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:14"}
        push_bind(build(None, [c]))
        lines = _script_lines(mrun)
        self.assertEqual([l for l in lines if l.startswith("server ")], [f"server {BIND_PRIMARY}"])
        self.assertEqual([l for l in lines if l.startswith("zone ")], [f"zone {DOMAIN}."])


# ── Security / Unit: push_bind() target + invocation are fixed & validated ───
# (successor of the deploy() tests: fixed nodes, argv list, no shell, static remote cmd)

@patch("nova_dns_sync.tsig_secret", return_value=SECRET)
@patch("nova_dns_sync.subprocess.run")
class TestPushBindInvocation(unittest.TestCase):
    ENTRIES = [(_fq("a"), "1.1.1.1"), (_fq("b"), "1.1.1.2")]

    def test_single_nsupdate_call_to_fixed_primary(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        push_bind(self.ENTRIES)
        # one push, to the one BIND primary; the secondary follows via AXFR/NOTIFY
        self.assertEqual(mrun.call_count, 1)
        self.assertEqual(mrun.call_args.args[0][0], "nsupdate")
        self.assertIn(f"server {BIND_PRIMARY}", _script_lines(mrun))

    def test_argv_list_form_no_shell(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        push_bind(self.ENTRIES)
        c = mrun.call_args
        self.assertIsInstance(c.args[0], list)   # list argv, not a shell string
        self.assertNotIn("shell", c.kwargs)       # never shell=True
        self.assertIn("timeout", c.kwargs)        # never hangs forever on a dead primary

    def test_tsig_key_passed_via_y_with_fixed_key_name(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        push_bind(self.ENTRIES)
        argv = mrun.call_args.args[0]
        self.assertEqual(argv, ["nsupdate", "-y", f"hmac-sha256:{TSIG_KEY_NAME}:{SECRET}"])
        _sec.assert_called_once_with()

    def test_secret_not_leaked_into_script(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        push_bind(self.ENTRIES)
        self.assertNotIn(SECRET, _script_from(mrun))

    def test_secret_comes_from_keychain_on_darwin(self, mrun, _sec):
        # tsig_secret() -> _secret("nova-bind-tsig-key") -> `security find-generic-password`
        mrun.return_value = MagicMock(returncode=0, stdout="k3y\n")
        with patch.object(dns.sys, "platform", "darwin"):
            self.assertEqual(dns._secret("nova-bind-tsig-key"), "k3y")
        argv = mrun.call_args.args[0]
        self.assertEqual(argv[0], "security")
        self.assertIn("nova-bind-tsig-key", argv)
        self.assertNotIn("shell", mrun.call_args.kwargs)


# ── Unit: resolve_public() uses an EXTERNAL resolver, never our own BIND ─────

@patch("nova_dns_sync.subprocess.run")
class TestResolvePublic(unittest.TestCase):
    def test_queries_external_resolver_for_fqdn(self, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout="104.21.1.1\n172.67.2.2\n")
        self.assertEqual(resolve_public("chat"), ["104.21.1.1", "172.67.2.2"])
        argv = mrun.call_args.args[0]
        self.assertEqual(argv[0], "dig")
        self.assertIn("@8.8.8.8", argv)
        self.assertIn(f"chat.{DOMAIN}", argv)
        self.assertNotIn(f"@{BIND_PRIMARY}", argv)   # would be circular
        self.assertNotIn("shell", mrun.call_args.kwargs)

    def test_bare_apex_resolves_domain_itself(self, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout="1.2.3.4\n")
        resolve_public("")
        self.assertIn(DOMAIN, mrun.call_args.args[0])
        self.assertNotIn(f".{DOMAIN}", mrun.call_args.args[0])

    def test_custom_resolver_honored(self, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout="1.2.3.4\n")
        resolve_public("www", resolver="1.1.1.1")
        self.assertIn("@1.1.1.1", mrun.call_args.args[0])

    def test_cname_lines_filtered_out(self, mrun):
        # `dig +short` prints CNAME targets (trailing dot) before the A records
        mrun.return_value = MagicMock(returncode=0, stdout="kochj23.github.io.\n185.199.108.153\n\n")
        self.assertEqual(resolve_public("www"), ["185.199.108.153"])

    def test_failure_returns_empty_list(self, mrun):
        mrun.side_effect = subprocess.TimeoutExpired("dig", 10)
        self.assertEqual(resolve_public("www"), [])
        mrun.side_effect = FileNotFoundError("dig")
        self.assertEqual(resolve_public("www"), [])


# ── Unit: push_public_mirrors() keeps BIND's copy of the public names fresh ──

@patch("nova_dns_sync.tsig_secret", return_value=SECRET)
@patch("nova_dns_sync.subprocess.run")
class TestPushPublicMirrors(unittest.TestCase):
    def _resolver(self, table):
        return lambda host, resolver="8.8.8.8": table.get(host, [])

    def test_all_mirrors_synced(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        table = {h: [f"10.0.0.{i}"] for i, h in enumerate(PUBLIC_MIRRORS, start=1)}
        with patch.object(dns, "resolve_public", side_effect=self._resolver(table)):
            n = push_public_mirrors()
        self.assertEqual(n, len(PUBLIC_MIRRORS))
        lines = _script_lines(mrun)
        self.assertEqual(lines[0], f"server {BIND_PRIMARY}")
        self.assertEqual(lines[1], f"zone {DOMAIN}.")
        self.assertEqual(lines[-1], "send")
        for h, ips in table.items():
            name = f"{h}.{DOMAIN}" if h else DOMAIN
            self.assertIn(f"update delete {name}. A", lines)
            self.assertIn(f"update add {name}. 300 A {ips[0]}", lines)

    def test_apex_uses_bare_domain(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        with patch.object(dns, "resolve_public", side_effect=self._resolver({"": ["1.2.3.4"]})):
            push_public_mirrors()
        lines = _script_lines(mrun)
        self.assertIn(f"update add {DOMAIN}. 300 A 1.2.3.4", lines)
        self.assertFalse([l for l in lines if l.startswith(f"update add .{DOMAIN}")])

    def test_multiple_a_records_all_added(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        with patch.object(dns, "resolve_public", side_effect=self._resolver({"chat": ["1.1.1.1", "2.2.2.2"]})):
            push_public_mirrors(ttl=120)
        lines = _script_lines(mrun)
        self.assertEqual(lines.count(f"update delete chat.{DOMAIN}. A"), 1)
        self.assertIn(f"update add chat.{DOMAIN}. 120 A 1.1.1.1", lines)
        self.assertIn(f"update add chat.{DOMAIN}. 120 A 2.2.2.2", lines)

    def test_unresolved_name_left_alone(self, mrun, _sec):
        # resolution failure must NOT delete the existing (possibly still-correct) record
        mrun.return_value = MagicMock(returncode=0, stderr="")
        table = {"chat": ["1.1.1.1"]}   # everything else fails to resolve
        with patch.object(dns, "resolve_public", side_effect=self._resolver(table)), patch("builtins.print"):
            n = push_public_mirrors()
        self.assertEqual(n, 1)
        lines = _script_lines(mrun)
        self.assertEqual([l for l in lines if l.startswith("update delete")], [f"update delete chat.{DOMAIN}. A"])
        self.assertFalse([l for l in lines if "www" in l or "gauges" in l or "analytics" in l])

    def test_nothing_resolved_skips_nsupdate(self, mrun, _sec):
        with patch.object(dns, "resolve_public", return_value=[]), patch("builtins.print"):
            self.assertEqual(push_public_mirrors(), 0)
        mrun.assert_not_called()

    def test_nsupdate_failure_returns_zero(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=2, stderr="NOTAUTH")
        with patch.object(dns, "resolve_public", return_value=["1.1.1.1"]), patch("builtins.print"):
            self.assertEqual(push_public_mirrors(), 0)

    def test_invocation_is_fixed_argv_with_tsig(self, mrun, _sec):
        mrun.return_value = MagicMock(returncode=0, stderr="")
        with patch.object(dns, "resolve_public", return_value=["1.1.1.1"]):
            push_public_mirrors()
        c = mrun.call_args
        self.assertEqual(c.args[0], ["nsupdate", "-y", f"hmac-sha256:{TSIG_KEY_NAME}:{SECRET}"])
        self.assertNotIn("shell", c.kwargs)
        self.assertIn("timeout", c.kwargs)
        self.assertNotIn(SECRET, c.kwargs["input"])


# ── The 7 house categories (added 2026-10-05): Security, Performance, Retry, Unit,
#    Integration, Functional, Frame. Written by Jordan Koch (via Claude). ────────

import io
import os
import re
import time
import types
from contextlib import redirect_stdout

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_dns_sync.py"
SRC = SCRIPT.read_text()
_INJ = "x'); DR" "OP TABLE dns_records; --"


def _client(i, **kw):
    c = {"mac": f"aa:bb:cc:dd:{i >> 8 & 0xff:02x}:{i & 0xff:02x}", "ip": f"10.{i >> 16 & 0xff}.{i >> 8 & 0xff}.{i & 0xff}",
         "name": f"device-{i}"}
    c.update(kw)
    return c


class _Cur:
    """Cursor that records every (sql, params) and answers the sticky lookup from `names`."""
    def __init__(self, names=None):
        self.names = names or {}; self.sql = []; self._mac = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self._mac = params[0] if params else None

    def fetchone(self):
        n = self.names.get(self._mac)
        return (n, False) if n else None


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", dns.DSN)

    def test_secrets_come_from_keychain_or_fleet_store_never_source(self):
        # darwin -> `security find-generic-password` as an argv list (no shell); linux -> nova_secrets
        with patch("nova_dns_sync.subprocess.run", return_value=MagicMock(stdout="kc-value\n")) as mrun, \
             patch.object(dns.sys, "platform", "darwin"):
            self.assertEqual(dns.api_key(), "kc-value")
        argv = mrun.call_args.args[0]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("nova-unifi-api-key", argv)
        self.assertNotIn("shell", mrun.call_args.kwargs)
        fake = types.ModuleType("nova_secrets"); fake.get_secret = MagicMock(return_value="fleet-value")
        with patch.dict(sys.modules, {"nova_secrets": fake}), patch.object(dns.sys, "platform", "linux"):
            self.assertEqual(dns.tsig_secret(), "fleet-value")
        fake.get_secret.assert_called_once_with("nova-bind-tsig-key")

    def test_sql_is_parameterized_and_only_touches_dns_records(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\([^)]*%\s*\(', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"dns_records"})
        cur = _Cur()
        dns.build(_Conn(cur), [_client(1, name=_INJ)])
        for sql, params in cur.sql:
            self.assertNotIn("DR" "OP", sql)
        ins = [p for s, p in cur.sql if s.startswith("INSERT")]
        self.assertEqual(ins[0][1], "x-dr" "op-table-dns-records")          # slugged, passed as a parameter

    def test_unifi_key_travels_in_a_header_not_the_url(self):
        with patch("nova_dns_sync.urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value.read.return_value = b'{"data": [{"mac": "m"}]}'
            self.assertEqual(dns.get_clients("K-1"), [{"mac": "m"}])
        req = u.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), "K-1")
        self.assertNotIn("K-1", req.full_url)
        self.assertTrue(req.full_url.startswith(dns.UNIFI))


class TestPerformance(unittest.TestCase):
    def test_build_dry_run_on_10k_clients_is_fast(self):
        clients = [_client(i) for i in range(10_000)] + [_client(i, name=None, hostname=None) for i in range(500)]
        t0 = time.perf_counter()
        out = dns.build(None, clients)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 10_500 + len(SERVICE_ALIASES))

    def test_slug_10k_is_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            slug(f"  Jordan's iPhone #{i} (Office) ")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_get_clients_is_one_shot_and_fails_closed(self):
        # RETRY GAP: get_clients() — one urlopen, no backoff. The exception escapes, which is the SAFE
        # outcome here: a UniFi outage must never reach push_bind() with an empty list and wipe the zone.
        with patch("nova_dns_sync.urllib.request.urlopen", side_effect=OSError("unifi down")) as u, \
             patch("nova_dns_sync.push_bind") as pb, patch("nova_dns_sync.api_key", return_value="k"), \
             patch("nova_dns_sync.psycopg2.connect") as pg, patch.object(sys, "argv", ["nova_dns_sync.py"]):
            with self.assertRaises(OSError):
                dns.main()
        self.assertEqual(u.call_count, 1)
        pb.assert_not_called(); pg.assert_not_called()

    def test_push_bind_is_one_shot_and_fails_open(self):
        # RETRY GAP: push_bind() — a single nsupdate; a failure returns False and never raises
        with patch("nova_dns_sync.tsig_secret", return_value=SECRET), \
             patch("nova_dns_sync.subprocess.run", return_value=MagicMock(returncode=1, stderr="REFUSED")) as mrun, \
             redirect_stdout(io.StringIO()) as out:
            self.assertFalse(push_bind([("a.%s" % DOMAIN, "10.0.0.1")]))
        self.assertEqual(mrun.call_count, 1)
        self.assertIn("nsupdate failed", out.getvalue())

    def test_resolve_public_is_one_shot_and_fails_open(self):
        # RETRY GAP: resolve_public() — one dig; timeout/absence yields [] so the mirror is left alone
        with patch("nova_dns_sync.subprocess.run", side_effect=subprocess.TimeoutExpired("dig", 10)) as mrun:
            self.assertEqual(resolve_public("www"), [])
        self.assertEqual(mrun.call_count, 1)

    def test_secret_lookup_failure_escapes_before_any_push(self):
        # RETRY GAP: _secret() — check=True keychain call, no retry; nothing is pushed without the key
        with patch("nova_dns_sync.subprocess.run", side_effect=subprocess.CalledProcessError(44, "security")) as mrun, \
             patch.object(dns.sys, "platform", "darwin"):
            with self.assertRaises(subprocess.CalledProcessError):
                push_bind([])
        self.assertEqual(mrun.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_slug_edges(self):
        self.assertEqual(slug(None), "")
        self.assertEqual(slug("   "), "")
        self.assertEqual(slug("A--B__C"), "a-b-c")
        self.assertEqual(slug("Ünïcode"), "n-code")

    def test_derive_name_edges(self):
        self.assertIsNone(derive_name({}))
        self.assertIsNone(derive_name({"mac": ""}))
        self.assertEqual(derive_name({"mac": "00:11:22:33:44:55"}), "dev-334455")
        self.assertEqual(derive_name({"name": "!!!", "hostname": "Rack Pi"}), "rack-pi")
        self.assertEqual(derive_name({"oui": "Apple Inc", "mac": "00:11:22:33:44:55"}), "apple-inc-4455")

    def test_build_edges(self):
        self.assertEqual(dns.build(None, []), [(f"{a}.{DOMAIN}", ip) for a, ip in SERVICE_ALIASES.items()])
        out = dns.build(None, [_client(1, ip=None, fixed_ip=None, last_ip="10.9.9.9")])
        self.assertEqual(out[0], (f"device-1.{DOMAIN}", "10.9.9.9"))
        same = [_client(1, name="twin", mac="aa:aa:aa:aa:aa:01"), _client(2, name="twin", mac="aa:aa:aa:aa:aa:02")]
        names = [f for f, _ in dns.build(None, same)]
        self.assertIn(f"twin.{DOMAIN}", names); self.assertIn(f"twin-aa02.{DOMAIN}", names)

    def test_failover_aliases_are_a_subset_with_a_shorter_ttl(self):
        self.assertTrue(FAILOVER_ALIASES <= set(SERVICE_ALIASES))
        self.assertLess(FAILOVER_TTL, 300)

    def test_public_mirrors_include_the_apex(self):
        self.assertIn("", PUBLIC_MIRRORS)
        self.assertEqual(len(set(PUBLIC_MIRRORS)), len(PUBLIC_MIRRORS))


class TestIntegration(unittest.TestCase):
    def test_build_then_push_bind_produces_one_delete_and_add_per_entry(self):
        entries = dns.build(None, [_client(1), _client(2)])
        with patch("nova_dns_sync.tsig_secret", return_value=SECRET), \
             patch("nova_dns_sync.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mrun:
            self.assertTrue(push_bind(entries))
        lines = _script_lines(mrun)
        self.assertEqual(len([l for l in lines if l.startswith("update delete")]), len(entries))
        self.assertEqual(len([l for l in lines if l.startswith("update add")]), len(entries))
        self.assertIn(f"update add pg-primary.{DOMAIN}. {FAILOVER_TTL} A {SERVICE_ALIASES['pg-primary']}", lines)
        self.assertIn(f"update add device-1.{DOMAIN}. 300 A 10.0.0.1", lines)

    def test_sticky_lookup_and_upsert_share_the_dns_records_table(self):
        cur = _Cur(names={"aa:bb:cc:dd:00:01": "kept-name"})
        out = dns.build(_Conn(cur), [_client(1, name="new-name")])
        self.assertEqual(out[0][0], f"kept-name.{DOMAIN}")
        tables = {re.search(r"(?:FROM|INTO|EXISTS)\s+(\w+)", s).group(1) for s, _ in cur.sql}
        self.assertEqual(tables, {"dns_records"})
        self.assertEqual([p for s, p in cur.sql if s.startswith("INSERT")][0], ("aa:bb:cc:dd:00:01", "kept-name", "10.0.0.1"))

    def test_both_secrets_go_through_the_one_secret_helper(self):
        with patch("nova_dns_sync._secret", return_value="s") as sec:
            dns.api_key(); dns.tsig_secret()
        self.assertEqual([c.args[0] for c in sec.call_args_list], ["nova-unifi-api-key", "nova-bind-tsig-key"])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, clients, bind_ok=True, mirrors=3):
        conn = _Conn(_Cur())
        with patch("nova_dns_sync.api_key", return_value="k"), patch("nova_dns_sync.get_clients", return_value=clients), \
             patch("nova_dns_sync.psycopg2.connect", return_value=conn) as pg, \
             patch("nova_dns_sync.push_bind", return_value=bind_ok) as pb, \
             patch("nova_dns_sync.push_public_mirrors", return_value=mirrors) as pm, \
             patch.object(sys, "argv", ["nova_dns_sync.py"] + argv), redirect_stdout(io.StringIO()) as out:
            rc = dns.main()
        return rc, out.getvalue(), conn, pg, pb, pm

    def test_dry_run_previews_without_pg_or_bind(self):
        rc, out, conn, pg, pb, pm = self._main(["--dry-run"], [_client(1), _client(2)])
        self.assertEqual(rc, 0)
        pg.assert_not_called(); pb.assert_not_called(); pm.assert_not_called()
        self.assertIn(f"10.0.0.1        device-1.{DOMAIN}", out)
        self.assertIn(f"{2 + len(SERVICE_ALIASES)} records (2 clients + {len(SERVICE_ALIASES)} service aliases)", out)

    def test_golden_path_writes_pg_pushes_bind_and_mirrors(self):
        rc, out, conn, pg, pb, pm = self._main([], [_client(1)])
        self.assertEqual(rc, 0)
        pg.assert_called_once_with(dns.DSN)
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        entries = pb.call_args.args[0]
        self.assertEqual(entries[0], (f"device-1.{DOMAIN}", "10.0.0.1"))
        self.assertEqual(len(entries), 1 + len(SERVICE_ALIASES))
        self.assertTrue(any(s.startswith("INSERT INTO dns_records") for s, _ in conn.cur.sql))
        self.assertIn(f"pushed to BIND primary {BIND_PRIMARY}: OK", out)
        self.assertIn(f"public mirrors re-synced: 3/{len(PUBLIC_MIRRORS)}", out)

    def test_bind_failure_is_reported_and_mirrors_still_run(self):
        rc, out, conn, pg, pb, pm = self._main([], [_client(1)], bind_ok=False, mirrors=0)
        self.assertEqual(rc, 0)
        self.assertIn(": FAILED", out)
        pm.assert_called_once(); self.assertTrue(conn.closed)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_dns_sync"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
