"""
test_nova_relay.py — All 7 test categories for nova_relay.py
Written by Jordan Koch.

nova_relay.py is the authenticated HTTP front door (127.0.0.1:37479) that external
agents (work laptop, phone, another Claude) use to reach Nova. It is the single
most security-sensitive script in the repo, so SECURITY is the fattest section:
loopback-only bind, two-layer auth, SQL verb whitelisting, outbound scrubbing and
structural ring capping.

HARD SAFETY (nothing here may touch the real world):
  * nova_notify is stubbed BEFORE load, so audit() can never enqueue a real event.
  * nova_config is stubbed; _contains_blocked_content defaults to False.
  * psycopg2 is never allowed to connect — every DB test hands in a fake conn, and
    the SQL-rejection tests replace the module's psycopg2 with a mock that fails
    the test if connect() is called at all.
  * urllib is patched for the JWKS tests — no network.
  * LOG_FILE is redirected to a temp file and log() is replaced with a recorder.
  * _keychain is patched everywhere it matters — the real Keychain is never read.
"""

import ast
import json
import re
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_relay.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_nova_cfg = MagicMock()
_nova_cfg._contains_blocked_content = lambda text: False
sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_relay")

# Never write the real relay log; keep a recorder so audit() still works.
_LOGS = []
_mod.LOG_FILE = Path(tempfile.gettempdir()) / "nova_relay_test.log"
_mod.log = lambda msg: _LOGS.append(msg)

config = _mod.config
identify = _mod.identify
scrub_outbound = _mod.scrub_outbound
verb_ask = _mod.verb_ask
verb_reply = _mod.verb_reply
verb_query = _mod.verb_query
verb_message = _mod.verb_message
verb_messages = _mod.verb_messages
verb_queue_post = _mod.verb_queue_post
verb_queue_get = _mod.verb_queue_get
rate_ok = _mod.rate_ok
_consteq = _mod._consteq
_SRC = _SCRIPT.read_text()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeHandler:
    """Stands in for BaseHTTPRequestHandler in identify() — headers + peer only."""

    def __init__(self, headers=None, peer="127.0.0.1"):
        self.headers = headers or {}
        self.client_address = (peer, 54321)


class FakeCursor:
    """Records executes; hands out scripted fetchone/fetchall results."""

    def __init__(self, fetchone_queue=None, fetchall_queue=None, executed=None):
        self._one = list(fetchone_queue or [])
        self._all = list(fetchall_queue or [])
        self.executed = executed if executed is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def fetchall(self):
        return self._all.pop(0) if self._all else []

    def fetchmany(self, n=None):
        return self._all.pop(0) if self._all else []

    def close(self):
        pass


class FakeConn:
    """Hands out one FakeCursor per cursor() call, sharing the execute log."""

    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self.executed = []
        self._one = list(fetchone_queue or [])
        self._all = list(fetchall_queue or [])
        self.closed = False

    def cursor(self, *a, **k):
        return FakeCursor(self._one, self._all, executed=self.executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeHTTPResponse:
    def __init__(self, body=b"{}"):
        self._body = body if isinstance(body, bytes) else body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _no_db():
    """A psycopg2 replacement whose connect() is a hard failure."""
    fake = MagicMock(name="psycopg2")
    fake.connect.side_effect = AssertionError(
        "verb must reject the request BEFORE touching the database")
    return fake


class _RelayCase(unittest.TestCase):
    """Base case that pins the nova_config stub for the duration of each test.

    scrub_outbound does `import nova_config` at CALL time, so when several Nova
    test modules share one interpreter (pytest) another module's bare MagicMock
    stub would otherwise answer _contains_blocked_content with a truthy Mock and
    block every reply. Pinning it keeps this file hermetic in isolation AND in a
    full-suite run.
    """

    def setUp(self):
        cfg = patch.dict(sys.modules, {"nova_config": _nova_cfg})
        cfg.start()
        self.addCleanup(cfg.stop)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurityBind(_RelayCase):
    """The relay must be reachable only through cloudflared, never the raw LAN."""

    def test_binds_loopback_literal(self):
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", PORT)', _SRC,
                      "the server must bind the 127.0.0.1 literal")

    def test_never_binds_all_interfaces(self):
        for bad in ("0.0.0.0", '""', "'::'"):
            self.assertNotIn(f'ThreadingHTTPServer(({bad}', _SRC,
                             f"must never bind {bad}")
        self.assertNotIn("0.0.0.0", _SRC, "0.0.0.0 must not appear anywhere")

    def test_port_is_the_documented_loopback_port(self):
        self.assertEqual(_mod.PORT, 37479)

    def test_no_hardcoded_credentials(self):
        """No real-looking secret literal may appear in source.

        Matched with regexes rather than substrings because the scrubber
        legitimately contains 'sk-', 'AKIA' and 'ghp_' inside its own patterns.
        """
        real_secret = [
            r"xox[baprs]-\d{5,}",
            r"\bsk-[A-Za-z0-9]{20,}",
            r"\bghp_[A-Za-z0-9]{20,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"(?i)password\s*=\s*['\"][^'\"]{4,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----\n",
        ]
        for pat in real_secret:
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_secrets_come_from_keychain(self):
        self.assertIn("find-generic-password", _SRC)
        self.assertIn("nova-relay-local-secret", _SRC)

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC,
                         "use Path.home() / Path(__file__), never a literal home path")

    def test_no_shell_or_arbitrary_sql_verb(self):
        """The vocabulary is fixed: there is no shell verb and no write verb."""
        self.assertNotIn("/shell", _SRC)
        self.assertNotIn("shell=True", _SRC)
        self.assertNotIn("os.system", _SRC)

    def test_query_runs_as_readonly_role_with_timeout(self):
        self.assertIn("set_session(readonly=True", _SRC)
        self.assertIn("SET LOCAL ROLE nova_relay_ro", _SRC)
        self.assertIn("SET LOCAL statement_timeout", _SRC)

    def test_response_sets_nosniff(self):
        self.assertIn("X-Content-Type-Options", _SRC)


class TestSecurityAuth(_RelayCase):
    """identify() must fail closed on every path."""

    def test_rejects_no_jwt_and_no_local_secret(self):
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: "topsecretvalue"):
            ident, err = identify(FakeHandler(headers={}, peer="127.0.0.1"))
        self.assertIsNone(ident)
        self.assertIn("X-Relay-Local-Secret", err)

    def test_rejects_wrong_local_secret(self):
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: "topsecretvalue"):
            ident, err = identify(FakeHandler(
                headers={"X-Relay-Local-Secret": "wrongvalue1234"},
                peer="127.0.0.1"))
        self.assertIsNone(ident)
        self.assertIsNotNone(err)

    def test_rejects_wrong_local_secret_of_identical_length(self):
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: "abcdefghij"):
            ident, err = identify(FakeHandler(
                headers={"X-Relay-Local-Secret": "abcdefghiX"},
                peer="127.0.0.1"))
        self.assertIsNone(ident)

    def test_accepts_correct_local_secret_on_loopback(self):
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: "topsecretvalue"):
            ident, err = identify(FakeHandler(
                headers={"X-Relay-Local-Secret": "topsecretvalue"},
                peer="127.0.0.1"))
        self.assertEqual(ident, "local-test")
        self.assertIsNone(err)

    def test_empty_keychain_secret_never_authenticates(self):
        """A missing Keychain item must not turn into an empty-string bypass."""
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: ""):
            ident, err = identify(FakeHandler(
                headers={"X-Relay-Local-Secret": ""}, peer="127.0.0.1"))
        self.assertIsNone(ident)

    def test_rejects_non_loopback_peer_without_jwt(self):
        """A LAN host cannot use the local-secret path at all."""
        with patch.object(_mod, "config", lambda: {}), \
             patch.object(_mod, "_keychain", lambda s: "topsecretvalue"):
            ident, err = identify(FakeHandler(
                headers={"X-Relay-Local-Secret": "topsecretvalue"},
                peer="192.168.1.50"))
        self.assertIsNone(ident)
        self.assertIn("Cf-Access-Jwt-Assertion", err)

    def test_jwt_without_access_config_is_rejected(self):
        ident, err = identify(FakeHandler(
            headers={"Cf-Access-Jwt-Assertion": "eyJhbGciOiJSUzI1NiJ9.x.y"},
            peer="10.0.0.9"))
        with patch.object(_mod, "config", lambda: {"team_domain": None}):
            ident, err = identify(FakeHandler(
                headers={"Cf-Access-Jwt-Assertion": "eyJhbGciOiJSUzI1NiJ9.x.y"},
                peer="10.0.0.9"))
        self.assertIsNone(ident)
        self.assertIn("not configured", err)

    def test_bare_jwt_header_is_independently_verified(self):
        """A forged header must not be trusted just because it exists."""
        fake_jwt = MagicMock()
        fake_jwt.decode.side_effect = ValueError("bad signature")
        fake_jwt.PyJWKClient = MagicMock()
        with patch.dict(sys.modules, {"jwt": fake_jwt}), \
             patch.object(_mod, "config",
                          lambda: {"team_domain": "t.cloudflareaccess.com", "aud": "aud1"}):
            ident, err = identify(FakeHandler(
                headers={"Cf-Access-Jwt-Assertion": "forged.token.here"},
                peer="10.0.0.9"))
        self.assertIsNone(ident)
        self.assertIn("JWT verification failed", err)

    def test_verified_jwt_maps_to_friendly_device_name(self):
        fake_jwt = MagicMock()
        fake_jwt.decode.return_value = {"common_name": "work-laptop-cn"}
        fake_jwt.PyJWKClient = MagicMock()
        with patch.dict(sys.modules, {"jwt": fake_jwt}), \
             patch.object(_mod, "config",
                          lambda: {"team_domain": "t.cloudflareaccess.com", "aud": "aud1",
                                   "devices": {"work-laptop-cn": "work-laptop"}}):
            ident, err = identify(FakeHandler(
                headers={"Cf-Access-Jwt-Assertion": "a.b.c"}, peer="10.0.0.9"))
        self.assertEqual(ident, "work-laptop")
        self.assertIsNone(err)

    def test_identity_is_length_capped(self):
        fake_jwt = MagicMock()
        fake_jwt.decode.return_value = {"email": "x" * 500 + "@example.com"}
        fake_jwt.PyJWKClient = MagicMock()
        with patch.dict(sys.modules, {"jwt": fake_jwt}), \
             patch.object(_mod, "config",
                          lambda: {"team_domain": "t.cloudflareaccess.com", "aud": "aud1"}):
            ident, err = identify(FakeHandler(
                headers={"Cf-Access-Jwt-Assertion": "a.b.c"}, peer="10.0.0.9"))
        self.assertEqual(len(ident), 64)

    def test_jwt_decode_pins_rs256_and_audience(self):
        self.assertIn('algorithms=["RS256"]', _SRC)
        self.assertIn("audience=aud", _SRC)
        self.assertIn("issuer=", _SRC)


class TestSecurityConstEq(_RelayCase):

    def test_length_mismatch_is_false_not_error(self):
        self.assertFalse(_consteq("short", "muchlongervalue"))
        self.assertFalse(_consteq("", "x"))

    def test_equal_strings_true(self):
        self.assertTrue(_consteq("abc123", "abc123"))
        self.assertTrue(_consteq("", ""))

    def test_same_length_difference_is_false(self):
        self.assertFalse(_consteq("abcdef", "abcdeg"))
        self.assertFalse(_consteq("abcdef", "Xbcdef"))

    def test_compare_loop_has_no_early_return(self):
        """Constant-shaped: the loop must XOR every char, never bail early."""
        fn = next(n for n in ast.walk(ast.parse(_SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "_consteq")
        loop = next(n for n in fn.body if isinstance(n, ast.For))
        for node in ast.walk(loop):
            self.assertNotIsInstance(node, ast.Return,
                                     "_consteq must not return from inside the loop")
            self.assertNotIsInstance(node, ast.Break,
                                     "_consteq must not break out of the loop")


class TestSecuritySQLVerb(_RelayCase):
    """verb_query is the only DB-facing verb: SELECT/WITH reads and nothing else."""

    WRITES = [
        "INSERT INTO claude_queue (description) VALUES ('x')",
        "UPDATE claude_queue SET status='done'",
        "DELETE FROM claude_messages",
        "DROP TABLE claude_messages",
        "ALTER TABLE claude_messages ADD COLUMN x int",
        "GRANT ALL ON claude_messages TO PUBLIC",
        "TRUNCATE claude_messages",
        "COPY claude_messages TO '/tmp/out.csv'",
        "CREATE TABLE evil (x int)",
        "REVOKE ALL ON claude_messages FROM kochj",
        "VACUUM FULL",
        "DO $$ BEGIN PERFORM 1; END $$",
        "CALL some_proc()",
        "SET ROLE postgres",
    ]

    def test_rejects_every_write_statement(self):
        with patch.object(_mod, "psycopg2", _no_db()):
            for sql in self.WRITES:
                code, obj = verb_query("dev", {"sql": sql})
                self.assertGreaterEqual(code, 400, f"not rejected: {sql}")
                self.assertIn("error", obj)

    def test_rejects_cte_hidden_write(self):
        """WITH x AS (DELETE ... RETURNING 1) SELECT * FROM x starts with WITH."""
        sql = "WITH x AS (DELETE FROM claude_messages RETURNING 1) SELECT * FROM x"
        with patch.object(_mod, "psycopg2", _no_db()):
            code, obj = verb_query("dev", {"sql": sql})
        self.assertEqual(code, 403)
        self.assertIn("forbidden", obj["error"])

    def test_rejects_cte_hidden_update_and_insert(self):
        for inner in ("UPDATE claude_queue SET status='x' RETURNING 1",
                      "INSERT INTO claude_queue (description) VALUES ('x') RETURNING 1"):
            sql = f"WITH t AS ({inner}) SELECT * FROM t"
            with patch.object(_mod, "psycopg2", _no_db()):
                code, obj = verb_query("dev", {"sql": sql})
            self.assertEqual(code, 403, sql)

    def test_rejects_stacked_statements(self):
        with patch.object(_mod, "psycopg2", _no_db()):
            code, obj = verb_query("dev", {"sql": "SELECT 1; DROP TABLE y"})
        self.assertGreaterEqual(code, 400)
        code2 = None
        with patch.object(_mod, "psycopg2", _no_db()):
            code2, obj2 = verb_query("dev", {"sql": "SELECT 1; SELECT 2"})
        self.assertEqual(code2, 400)
        self.assertIn("multiple statements", obj2["error"])

    def test_rejects_file_and_large_object_functions(self):
        for sql in ("SELECT pg_read_file('/etc/passwd')",
                    "SELECT pg_ls_dir('/')",
                    "SELECT lo_import('/etc/shadow')",
                    "SELECT lo_export(1, '/tmp/x')"):
            with patch.object(_mod, "psycopg2", _no_db()):
                code, obj = verb_query("dev", {"sql": sql})
            self.assertEqual(code, 403, sql)

    def test_rejects_lowercase_and_mixed_case_writes(self):
        for sql in ("delete from claude_messages", "DeLeTe FROM claude_messages",
                    "select 1; drop table y"):
            with patch.object(_mod, "psycopg2", _no_db()):
                code, obj = verb_query("dev", {"sql": sql})
            self.assertGreaterEqual(code, 400, sql)

    def test_rejects_empty_sql(self):
        with patch.object(_mod, "psycopg2", _no_db()):
            code, obj = verb_query("dev", {"sql": "   "})
        self.assertEqual(code, 400)
        self.assertIn("sql required", obj["error"])

    def test_plain_select_is_allowed_and_row_capped(self):
        fake_pg = MagicMock(name="psycopg2")
        conn = fake_pg.connect.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchmany.return_value = [{"n": 1}, {"n": 2}]
        with patch.object(_mod, "psycopg2", fake_pg):
            code, obj = verb_query("dev", {"sql": "SELECT n FROM t"})
        self.assertEqual(code, 200)
        self.assertEqual(obj["row_count"], 2)
        cur.fetchmany.assert_called_once_with(_mod.QUERY_ROW_CAP)


class TestSecurityScrubOutbound(_RelayCase):
    """Nothing secret and nothing private may cross to a monitored device."""

    def test_redacts_slack_bot_token(self):
        out, notes = scrub_outbound("token is xoxb-1234567890-abcdefghij here")
        self.assertNotIn("xoxb-1234567890", out)
        self.assertIn("[REDACTED-SECRET]", out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_slack_app_token(self):
        out, notes = scrub_outbound("xapp-1-A0123456789-abcdef")
        self.assertNotIn("xapp-1-A0123456789", out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_sk_style_key(self):
        out, notes = scrub_outbound("key sk-" + "a" * 40)
        self.assertNotIn("a" * 40, out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_github_token(self):
        out, notes = scrub_outbound("ghp_" + "B" * 36)
        self.assertNotIn("B" * 36, out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_aws_access_key_id(self):
        out, notes = scrub_outbound("AKIAIOSFODNN7EXAMPLE is the key")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_pem_private_key_block(self):
        pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
               "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\nmoremoremore\n"
               "-----END OPENSSH PRIVATE KEY-----")
        out, notes = scrub_outbound(f"here it is:\n{pem}\ndone")
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_jwt(self):
        jwt_like = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                    "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        out, notes = scrub_outbound(f"bearer {jwt_like}")
        self.assertNotIn(jwt_like, out)
        self.assertIn("secret-redacted", notes)

    def test_redacts_password_and_api_key_pairs(self):
        for text in ("password=hunter2hunter2", "api_key = abcdef123456",
                     "PASSWORD: sup3rs3cret", "token=abcdef1234567890",
                     "secret = zzzzzzzzzz"):
            out, notes = scrub_outbound(text)
            self.assertIn("[REDACTED-SECRET]", out, text)
            self.assertIn("secret-redacted", notes, text)

    def test_clean_text_is_untouched(self):
        out, notes = scrub_outbound("postgres is up, 4 probes green")
        self.assertEqual(out, "postgres is up, 4 probes green")
        self.assertEqual(notes, [])

    def test_blocks_health_topics(self):
        for text in ("healthkit shows 8h sleep", "apple health export attached",
                     "blood pressure was 118/74", "resting heart rate 52",
                     "the prescription was refilled", "new diagnosis on file"):
            out, notes = scrub_outbound(text)
            self.assertIn("private-topic-blocked", notes, text)
            self.assertIn("[BLOCKED BY RELAY]", out, text)
            self.assertNotIn("blood pressure", out.lower())

    def test_blocks_financial_topics(self):
        for text in ("1099 arrived today", "your W-2 is ready",
                     "bank statement for July", "account number 12345678",
                     "routing number on file", "the tax return is filed",
                     "HSA balance"):
            out, notes = scrub_outbound(text)
            self.assertIn("private-topic-blocked", notes, text)
            self.assertIn("[BLOCKED BY RELAY]", out, text)

    def test_blocks_home_security_topics(self):
        for text in ("the alarm code is on the fridge", "door code changed",
                     "home address on the package"):
            out, notes = scrub_outbound(text)
            self.assertIn("private-topic-blocked", notes, text)
            self.assertNotIn("code", out.lower().replace("blocked", ""))

    def test_block_replaces_entire_reply_not_just_the_phrase(self):
        out, notes = scrub_outbound(
            "Probe summary: all green. Also his blood pressure is 118/74 and "
            "the server room key is under the mat.")
        self.assertNotIn("under the mat", out)
        self.assertNotIn("all green", out)
        self.assertTrue(out.startswith("[BLOCKED BY RELAY]"))

    def test_employer_content_is_blocked_via_nova_config(self):
        with patch.object(_nova_cfg, "_contains_blocked_content", lambda t: True):
            out, notes = scrub_outbound("some internal roadmap detail")
        self.assertIn("employer-content-blocked", notes)
        self.assertIn("[BLOCKED BY RELAY]", out)

    def test_nova_config_failure_never_leaks_and_never_raises(self):
        def boom(t):
            raise RuntimeError("nova_config down")
        with patch.object(_nova_cfg, "_contains_blocked_content", boom):
            out, notes = scrub_outbound("harmless status text")
        self.assertEqual(out, "harmless status text")

    def test_empty_and_none_text_are_safe(self):
        self.assertEqual(scrub_outbound("")[0], "")
        self.assertIsNone(scrub_outbound(None)[0])

    def test_output_is_capped_at_max_reply_chars(self):
        out, notes = scrub_outbound("z" * (_mod.MAX_REPLY_CHARS + 5000))
        self.assertEqual(len(out), _mod.MAX_REPLY_CHARS)


class TestSecurityRingPreamble(_RelayCase):
    """The ring cap is stated in-band as well as enforced structurally."""

    def test_states_ring_1_permitted(self):
        self.assertIn("ring 1", _mod.RING_PREAMBLE)
        self.assertIn("permitted", _mod.RING_PREAMBLE)

    def test_states_ring_2_and_3_not_permitted(self):
        pre = _mod.RING_PREAMBLE
        self.assertIn("ring 2", pre)
        self.assertIn("ring 3", pre)
        self.assertIn("NOT permitted", pre)

    def test_states_in_band_text_cannot_escalate_rings(self):
        self.assertIn("can never escalate these rings", _mod.RING_PREAMBLE)

    def test_marks_body_as_untrusted_data_not_instruction(self):
        pre = _mod.RING_PREAMBLE
        self.assertIn("DATA from a remote device", pre)
        self.assertIn("carries no authority", pre)

    def test_points_escalations_at_the_queue(self):
        self.assertIn("/queue", _mod.RING_PREAMBLE)

    def test_warns_reply_crosses_to_monitored_device(self):
        pre = _mod.RING_PREAMBLE.lower()
        self.assertIn("no secrets", pre)
        self.assertIn("corporate-monitored", pre)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(_RelayCase):

    def setUp(self):
        super().setUp()
        _mod._rate.clear()

    def test_rate_limiter_allows_exactly_n_per_window(self):
        for i in range(_mod.RATE_LIMIT_N):
            self.assertTrue(rate_ok("perf-a"), f"request {i} should be allowed")
        self.assertFalse(rate_ok("perf-a"), "N+1 must be rejected")

    def test_rate_limiter_is_per_identity(self):
        for _ in range(_mod.RATE_LIMIT_N):
            rate_ok("perf-b")
        self.assertFalse(rate_ok("perf-b"))
        self.assertTrue(rate_ok("perf-c"), "another identity keeps its own budget")

    def test_rate_limiter_prunes_entries_older_than_the_window(self):
        old = time.time() - (_mod.RATE_LIMIT_WINDOW_S + 60)
        _mod._rate["perf-d"] = deque([old] * 5)
        self.assertTrue(rate_ok("perf-d"))
        self.assertEqual(len(_mod._rate["perf-d"]), 1,
                         "stale timestamps must be popped, not accumulated")

    def test_expired_entries_do_not_count_against_the_limit(self):
        old = time.time() - (_mod.RATE_LIMIT_WINDOW_S + 1)
        _mod._rate["perf-e"] = deque([old] * _mod.RATE_LIMIT_N)
        self.assertTrue(rate_ok("perf-e"))

    def test_queue_never_grows_beyond_the_limit(self):
        for _ in range(_mod.RATE_LIMIT_N * 3):
            rate_ok("perf-f")
        self.assertLessEqual(len(_mod._rate["perf-f"]), _mod.RATE_LIMIT_N,
                             "deque must be bounded by the limit, no unbounded growth")

    def test_rate_state_is_a_bounded_deque(self):
        rate_ok("perf-g")
        self.assertIsInstance(_mod._rate["perf-g"], deque)

    def test_caps_are_finite_and_sane(self):
        self.assertIsInstance(_mod.MAX_BODY, int)
        self.assertGreater(_mod.MAX_BODY, 1024)
        self.assertLessEqual(_mod.MAX_BODY, 1024 * 1024)
        self.assertIsInstance(_mod.MAX_REPLY_CHARS, int)
        self.assertGreater(_mod.MAX_REPLY_CHARS, 1000)
        self.assertLessEqual(_mod.MAX_REPLY_CHARS, 100_000)
        self.assertIsInstance(_mod.QUERY_ROW_CAP, int)
        self.assertGreater(_mod.QUERY_ROW_CAP, 0)
        self.assertLessEqual(_mod.QUERY_ROW_CAP, 1000)
        self.assertIsInstance(_mod.QUERY_TIMEOUT_MS, int)
        self.assertGreater(_mod.QUERY_TIMEOUT_MS, 0)
        self.assertLessEqual(_mod.QUERY_TIMEOUT_MS, 60_000)
        self.assertGreater(_mod.RATE_LIMIT_N, 0)
        self.assertGreater(_mod.RATE_LIMIT_WINDOW_S, 0)

    def test_reply_long_poll_is_bounded(self):
        """A caller cannot ask the relay to hold a thread forever."""
        self.assertIn("min(int((qs.get(\"wait\") or [\"25\"])[0]), 55)", _SRC)

    def test_ask_text_is_truncated_before_insert(self):
        self.assertIn("text[:8000]", _SRC)

    def test_db_connects_have_bounded_timeout(self):
        self.assertIn("connect_timeout=5", _SRC)
        self.assertNotIn("psycopg2.connect(DSN)", _SRC)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(_RelayCase):

    def setUp(self):
        super().setUp()
        _mod._jwks_cache.update({"keys": None, "fetched": 0})

    def test_jwks_fetched_once_inside_the_ttl(self):
        resp = lambda *a, **k: FakeHTTPResponse(json.dumps({"keys": [{"kid": "k1"}]}))
        with patch.object(_mod.urllib.request, "urlopen", side_effect=resp) as m:
            first = _mod._jwks("t.cloudflareaccess.com")
            second = _mod._jwks("t.cloudflareaccess.com")
        self.assertEqual(m.call_count, 1, "JWKS must be cached, not refetched")
        self.assertEqual(first, second)

    def test_jwks_refetches_after_ttl_expiry(self):
        resp = lambda *a, **k: FakeHTTPResponse(json.dumps({"keys": [{"kid": "k1"}]}))
        with patch.object(_mod.urllib.request, "urlopen", side_effect=resp) as m:
            _mod._jwks("t.cloudflareaccess.com")
            _mod._jwks_cache["fetched"] = time.time() - 3601
            _mod._jwks("t.cloudflareaccess.com")
        self.assertEqual(m.call_count, 2, "a stale cache must refetch")

    def test_jwks_fetch_has_a_timeout(self):
        self.assertIn("urlopen(url, timeout=10)", _SRC)

    def test_jwks_fetch_failure_propagates_and_denies(self):
        """A JWKS outage must fail closed (no identity), never fail open."""
        with patch.object(_mod.urllib.request, "urlopen",
                          side_effect=OSError("no network")):
            with self.assertRaises(OSError):
                _mod._jwks("t.cloudflareaccess.com")

    def test_reply_polls_until_deadline_without_busy_spinning(self):
        conn = FakeConn(fetchone_queue=[None])
        with patch.object(_mod.time, "sleep") as slept:
            code, obj = verb_reply(conn, "dev", {"id": ["7"], "wait": ["0"]})
        self.assertEqual(obj["status"], "pending")
        slept.assert_not_called()

    def test_query_db_error_is_reported_not_raised(self):
        fake_pg = MagicMock(name="psycopg2")
        fake_pg.connect.side_effect = RuntimeError("pg down")
        with patch.object(_mod, "psycopg2", fake_pg):
            code, obj = verb_query("dev", {"sql": "SELECT 1 FROM t"})
        self.assertEqual(code, 400)
        self.assertIn("RuntimeError", obj["error"])

    def test_audit_notify_failure_is_swallowed(self):
        _notify_stub.notify.side_effect = RuntimeError("bus down")
        try:
            _mod.audit("dev", "ask", "detail")   # must not raise
        finally:
            _notify_stub.notify.side_effect = None

    def test_keychain_failure_returns_empty_string(self):
        with patch.object(_mod.subprocess, "run", side_effect=OSError("no security bin")):
            self.assertEqual(_mod._keychain("nova-relay-local-secret"), "")

    def test_log_write_failure_is_swallowed(self):
        """An unwritable log path must not take the relay down (OSError guarded)."""
        self.assertIn("except OSError:", _SRC)
        fn = next(n for n in ast.walk(ast.parse(_SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "log")
        self.assertTrue(any(isinstance(n, ast.Try) for n in fn.body),
                        "log() must guard its file write with try/except")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(_RelayCase):

    def test_config_returns_empty_dict_when_file_missing(self):
        with patch.object(_mod, "CONFIG_FILE",
                          Path(tempfile.gettempdir()) / "relay-does-not-exist.json"):
            self.assertEqual(config(), {})

    def test_config_returns_empty_dict_on_corrupt_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json at all")
            p = Path(f.name)
        try:
            with patch.object(_mod, "CONFIG_FILE", p):
                self.assertEqual(config(), {})
        finally:
            p.unlink()

    def test_config_parses_valid_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"team_domain": "t", "aud": "a", "devices": {"cn": "phone"}}, f)
            p = Path(f.name)
        try:
            with patch.object(_mod, "CONFIG_FILE", p):
                cfg = config()
        finally:
            p.unlink()
        self.assertEqual(cfg["devices"]["cn"], "phone")

    def test_keychain_returns_empty_on_nonzero_exit(self):
        r = MagicMock(returncode=44, stdout="")
        with patch.object(_mod.subprocess, "run", return_value=r):
            self.assertEqual(_mod._keychain("whatever"), "")

    def test_keychain_strips_trailing_newline(self):
        r = MagicMock(returncode=0, stdout="thesecret\n")
        with patch.object(_mod.subprocess, "run", return_value=r):
            self.assertEqual(_mod._keychain("whatever"), "thesecret")

    def test_verb_ask_rejects_empty_text(self):
        for payload in ({}, {"text": ""}, {"text": "   \n "}, {"text": None}):
            code, obj = verb_ask(FakeConn(), "dev", payload)
            self.assertEqual(code, 400)
            self.assertEqual(obj["error"], "text required")

    def test_verb_reply_rejects_bad_id(self):
        code, obj = verb_reply(FakeConn(), "dev", {"id": ["not-a-number"]})
        self.assertEqual(code, 400)
        self.assertEqual(obj["error"], "bad id")

    def test_verb_message_rejects_empty_body(self):
        code, obj = verb_message(FakeConn(), "dev", {"topic": "t", "body": "  "})
        self.assertEqual(code, 400)
        self.assertEqual(obj["error"], "body required")

    def test_verb_queue_post_rejects_empty_description(self):
        for payload in ({}, {"description": ""}, {"description": "   "}):
            code, obj = verb_queue_post(FakeConn(), "dev", payload)
            self.assertEqual(code, 400)
            self.assertEqual(obj["error"], "description required")

    def test_verb_queue_post_truncates_and_tags_the_relay(self):
        conn = FakeConn(fetchone_queue=[(11,)])
        code, obj = verb_queue_post(conn, "work-laptop",
                                    {"description": "d" * 900, "context": "c" * 9000})
        self.assertEqual(code, 200)
        self.assertEqual(obj["queue_id"], 11)
        sql, params = conn.executed[-1]
        self.assertIn("[via relay/work-laptop]", params[1])
        self.assertLessEqual(len(params[1]), len("[via relay/work-laptop] ") + 400)
        self.assertEqual(len(params[2]), 4000)
        self.assertIn("approval", obj["note"])

    def test_verb_message_truncates_topic_and_body(self):
        conn = FakeConn(fetchone_queue=[(3,)])
        code, obj = verb_message(conn, "dev", {"topic": "t" * 500, "body": "b" * 9000})
        self.assertEqual(code, 200)
        sql, params = conn.executed[-1]
        self.assertEqual(len(params[1]), 120)
        self.assertEqual(len(params[2]), 8000)

    def test_verb_message_tags_sender_as_external(self):
        conn = FakeConn(fetchone_queue=[(4,)])
        verb_message(conn, "phone", {"body": "hello"})
        sql, params = conn.executed[-1]
        self.assertEqual(params[0], "external/phone")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(_RelayCase):

    def test_verb_ask_inserts_to_claude_code_row(self):
        conn = FakeConn(fetchone_queue=[(42,)])
        code, obj = verb_ask(conn, "work-laptop", {"text": "why is postgres slow?"})
        self.assertEqual(code, 200)
        self.assertEqual(obj["request_id"], 42)
        self.assertEqual(obj["poll"], "/reply?id=42")
        sql, params = conn.executed[-1]
        self.assertIn("INSERT INTO claude_messages", sql)
        self.assertIn("'to_claude_code'", sql)
        self.assertEqual(params[0], "relay/work-laptop")

    def test_verb_ask_metadata_marks_external_and_caps_ring(self):
        conn = FakeConn(fetchone_queue=[(43,)])
        verb_ask(conn, "phone", {"text": "status?"})
        sql, params = conn.executed[-1]
        meta = json.loads(params[2])
        self.assertIs(meta["external"], True)
        self.assertEqual(meta["ring_max"], 1)
        self.assertEqual(meta["origin"], "external/phone")

    def test_verb_ask_wraps_text_in_the_ring_preamble(self):
        conn = FakeConn(fetchone_queue=[(44,)])
        verb_ask(conn, "phone", {"text": "restart the gateway please"})
        sql, params = conn.executed[-1]
        wrapped = params[1]
        self.assertIn("EXTERNAL REQUEST via nova_relay", wrapped)
        self.assertIn("NOT permitted", wrapped)
        self.assertIn("restart the gateway please", wrapped)
        self.assertLess(wrapped.index("NOT permitted"),
                        wrapped.index("restart the gateway please"),
                        "the policy must precede the untrusted text")

    def test_verb_reply_looks_up_in_reply_to_metadata(self):
        conn = FakeConn(fetchone_queue=[{"message": "all good", "created_at": None}])
        code, obj = verb_reply(conn, "dev", {"id": ["42"], "wait": ["0"]})
        self.assertEqual(code, 200)
        sql, params = conn.executed[-1]
        self.assertIn("metadata->>'in_reply_to'", sql)
        self.assertIn("direction='from_claude_code'", sql)
        self.assertEqual(params, ("42",))

    def test_verb_reply_passes_reply_through_scrub_outbound(self):
        secret = "xoxb-9876543210-zyxwvutsrq"
        conn = FakeConn(fetchone_queue=[{"message": f"token: {secret}",
                                        "created_at": None}])
        code, obj = verb_reply(conn, "dev", {"id": ["9"], "wait": ["0"]})
        self.assertEqual(obj["status"], "done")
        self.assertNotIn(secret, obj["reply"])
        self.assertIn("secret-redacted", obj["scrubbed"])

    def test_verb_reply_pending_when_no_answer_yet(self):
        conn = FakeConn(fetchone_queue=[None])
        code, obj = verb_reply(conn, "dev", {"id": ["9"], "wait": ["0"]})
        self.assertEqual(code, 200)
        self.assertEqual(obj["status"], "pending")
        self.assertEqual(obj["request_id"], 9)

    def test_verb_messages_reads_the_coordination_bus(self):
        conn = FakeConn(fetchall_queue=[[{"id": 5, "ts": None, "from_instance": "x",
                                          "topic": "t", "message": "m",
                                          "status": "new"}]])
        code, obj = verb_messages(conn, "dev", {"since": ["4"]})
        self.assertEqual(code, 200)
        self.assertEqual(obj["cursor"], 5)
        sql, params = conn.executed[-1]
        self.assertIn("FROM claude_coordination WHERE id > %s", sql)
        self.assertEqual(params, (4,))

    def test_verb_messages_withholds_a_batch_that_trips_policy(self):
        conn = FakeConn(fetchall_queue=[[{"id": 6, "ts": None, "from_instance": "x",
                                          "topic": "t",
                                          "message": "his blood pressure is 118/74",
                                          "status": "new"}]])
        code, obj = verb_messages(conn, "dev", {"since": ["0"]})
        self.assertEqual(obj["messages"], [])
        self.assertIn("private-topic-blocked", obj["scrubbed"])

    def test_verb_query_result_withheld_when_policy_trips(self):
        fake_pg = MagicMock(name="psycopg2")
        cur = fake_pg.connect.return_value.cursor.return_value.__enter__.return_value
        cur.fetchmany.return_value = [{"note": "alarm code 4417"}]
        with patch.object(_mod, "psycopg2", fake_pg):
            code, obj = verb_query("dev", {"sql": "SELECT note FROM t"})
        self.assertEqual(code, 200)
        self.assertEqual(obj["rows"], [])
        self.assertIn("private-topic-blocked", obj["scrubbed"])
        self.assertIn("withheld", obj["error"])

    def test_verb_queue_get_summarizes_counts_and_open_items(self):
        conn = FakeConn(fetchall_queue=[
            [{"status": "queued", "count": 2}],
            [{"id": 1, "status": "queued", "priority": 5, "description": "d"}],
        ])
        code, obj = verb_queue_get(conn, "dev")
        self.assertEqual(code, 200)
        self.assertEqual(obj["counts"], {"queued": 2})
        self.assertEqual(len(obj["open_items"]), 1)

    def test_audit_emits_a_relay_category_notification(self):
        _notify_stub.notify.reset_mock()
        conn = FakeConn(fetchone_queue=[(50,)])
        verb_ask(conn, "phone", {"text": "hello"})
        _notify_stub.notify.assert_called()
        kwargs = _notify_stub.notify.call_args.kwargs
        self.assertEqual(kwargs["category"], "relay")
        self.assertEqual(kwargs["source"], "nova_relay.py")
        self.assertEqual(kwargs["meta"]["identity"], "phone")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(_RelayCase):

    def setUp(self):
        super().setUp()
        _mod._rate.clear()

    def test_full_ask_then_reply_cycle_returns_scrubbed_reply(self):
        ask_conn = FakeConn(fetchone_queue=[(77,)])
        code, ask = verb_ask(ask_conn, "work-laptop", {"text": "is the tunnel up?"})
        self.assertEqual(code, 200)
        rid = ask["request_id"]

        answer = ("Tunnel is up. Internal note: password=supersecretvalue "
                  "and token ghp_" + "C" * 36)
        reply_conn = FakeConn(fetchone_queue=[{"message": answer, "created_at": None}])
        code, rep = verb_reply(reply_conn, "work-laptop",
                              {"id": [str(rid)], "wait": ["0"]})
        self.assertEqual(code, 200)
        self.assertEqual(rep["status"], "done")
        self.assertIn("Tunnel is up", rep["reply"])
        self.assertNotIn("supersecretvalue", rep["reply"])
        self.assertNotIn("C" * 36, rep["reply"])
        self.assertIn("secret-redacted", rep["scrubbed"])

    def test_unauthenticated_request_yields_403(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {}
        h.client_address = ("192.168.1.99", 5555)
        sent = []
        h._send = lambda code, obj: sent.append((code, obj))
        with patch.object(_mod, "config", lambda: {}):
            self.assertIsNone(h._auth())
        self.assertEqual(sent[0][0], 403)
        self.assertIn("error", sent[0][1])

    def test_rate_limited_identity_yields_429(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {}
        h.client_address = ("127.0.0.1", 5555)
        sent = []
        h._send = lambda code, obj: sent.append((code, obj))
        _mod._rate["phone"] = deque([time.time()] * _mod.RATE_LIMIT_N)
        with patch.object(_mod, "identify", lambda handler: ("phone", None)):
            self.assertIsNone(h._auth())
        self.assertEqual(sent[0][0], 429)
        self.assertIn("rate limit", sent[0][1]["error"])

    def test_authenticated_and_under_limit_returns_identity(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {}
        h.client_address = ("127.0.0.1", 5555)
        h._send = lambda code, obj: None
        with patch.object(_mod, "identify", lambda handler: ("phone2", None)):
            self.assertEqual(h._auth(), "phone2")

    def test_health_endpoint_needs_no_auth_and_lists_verbs(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {}
        h.client_address = ("192.168.1.99", 5555)
        h.path = "/health"
        sent = []
        h._send = lambda code, obj: sent.append((code, obj)) or (code, obj)
        h.do_GET()
        code, obj = sent[0]
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertIn("/ask", obj["verbs"])

    def test_get_verbs_are_unreachable_without_auth(self):
        """Every non-health GET must go through _auth first — no DB connect."""
        for path in ("/reply?id=1", "/messages", "/queue", "/bogus"):
            h = _mod.Handler.__new__(_mod.Handler)
            h.headers = {}
            h.client_address = ("192.168.1.99", 5555)
            h.path = path
            sent = []
            h._send = lambda code, obj: sent.append((code, obj))
            with patch.object(_mod, "config", lambda: {}), \
                 patch.object(_mod, "psycopg2", _no_db()):
                h.do_GET()
            self.assertEqual(sent[0][0], 403, path)

    def test_post_verbs_are_unreachable_without_auth(self):
        for path in ("/ask", "/query", "/message", "/queue", "/bogus"):
            h = _mod.Handler.__new__(_mod.Handler)
            h.headers = {}
            h.client_address = ("192.168.1.99", 5555)
            h.path = path
            sent = []
            h._send = lambda code, obj: sent.append((code, obj))
            with patch.object(_mod, "config", lambda: {}), \
                 patch.object(_mod, "psycopg2", _no_db()):
                h.do_POST()
            self.assertEqual(sent[0][0], 403, path)

    def test_oversized_body_is_refused(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {"Content-Length": str(_mod.MAX_BODY + 1)}
        h.client_address = ("127.0.0.1", 5555)
        h.rfile = MagicMock()
        self.assertIsNone(h._body())
        h.rfile.read.assert_not_called()

    def test_malformed_json_body_is_refused(self):
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {"Content-Length": "5"}
        h.client_address = ("127.0.0.1", 5555)
        h.rfile = MagicMock()
        h.rfile.read.return_value = b"{oops"
        self.assertIsNone(h._body())

    def test_well_formed_body_is_parsed(self):
        payload = json.dumps({"text": "hi"}).encode()
        h = _mod.Handler.__new__(_mod.Handler)
        h.headers = {"Content-Length": str(len(payload))}
        h.client_address = ("127.0.0.1", 5555)
        h.rfile = MagicMock()
        h.rfile.read.return_value = payload
        self.assertEqual(h._body(), {"text": "hi"})

    def test_queue_verb_is_the_only_escalation_path(self):
        conn = FakeConn(fetchone_queue=[(101,)])
        code, obj = verb_queue_post(conn, "work-laptop",
                                    {"description": "restart nova-gateway"})
        self.assertEqual(code, 200)
        sql, params = conn.executed[-1]
        self.assertIn("INSERT INTO claude_queue", sql)
        self.assertIn("not auto-executed", obj["note"])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(_RelayCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_relay.py has syntax errors: {e}")

    def test_shebang_and_docstring(self):
        self.assertTrue(_SRC.startswith("#!/usr/bin/env python3"))
        doc = ast.get_docstring(ast.parse(_SRC))
        self.assertIn("nova_relay.py", doc)
        self.assertIn("RINGS", doc, "the ring policy must be documented in the header")

    def test_module_imports_cleanly_and_main_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_every_verb_is_defined_and_callable(self):
        for fn in ("verb_ask", "verb_reply", "verb_query", "verb_message",
                   "verb_messages", "verb_queue_post", "verb_queue_get"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing verb: {fn}")

    def test_helpers_are_defined_and_callable(self):
        for fn in ("config", "identify", "_consteq", "_keychain", "_jwks",
                   "rate_ok", "audit", "scrub_outbound", "log"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing helper: {fn}")

    def test_handler_implements_both_methods(self):
        self.assertTrue(hasattr(_mod.Handler, "do_GET"))
        self.assertTrue(hasattr(_mod.Handler, "do_POST"))
        self.assertEqual(_mod.Handler.server_version, "nova-relay")

    def test_dsn_targets_the_ops_database(self):
        self.assertIn("dbname=nova_ops", _mod.DSN)

    def test_main_does_not_run_on_import(self):
        self.assertIn('if __name__ == "__main__":', _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
