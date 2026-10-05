"""
test_nova_syslog_server.py — Tests for the unified syslog receiver.

FOCUS: untrusted UDP bytes -> PG.
  Unit:     the RFC 3164/5424 syslog parser + threat/anomaly detectors on crafted lines.
  Security: malformed/oversized/injection payloads don't crash or SQL-inject;
            the batch insert is parameterized.

These tests target THIS module's specific logic only. Generic shell=True /
f-string-SQL / eval scans live in test_security.py and are NOT duplicated here.

External deps (asyncpg pool, aiohttp, notify, subprocess) are never contacted —
the pure functions under test don't touch them, and _flush_batch runs against a
fake in-memory pool so no DB is hit.

Written for Jordan Koch.
"""

import asyncio
import re

import pytest

import nova_syslog_server as m


# ── Global-state reset ────────────────────────────────────────────────────────
# The detectors accumulate into module-level dicts. Clear them before every test
# so ordering never leaks state (e.g. a brute-force counter bleeding across tests).

_MUTABLE_DICTS = [
    "_recent_alerts", "_auth_failures", "_scan_events",
    "_device_event_counts", "_device_hour_count", "_crash_events",
    "_crash_storm_last_alert", "_crash_storm_confirm",
    "_lateral_scans", "_sensitive_access",
]


@pytest.fixture(autouse=True)
def _reset_state():
    for name in _MUTABLE_DICTS:
        getattr(m, name).clear()
    m._msg_count = 0
    m._threat_count = 0
    yield
    for name in _MUTABLE_DICTS:
        getattr(m, name).clear()


ADDR = ("192.168.1.99", 51000)


# ── Parser: RFC 5424 ──────────────────────────────────────────────────────────

def test_parse_rfc5424_full():
    data = b"<34>1 2023-10-11T22:14:15Z host.example.com su 1234 ID47 - the message"
    r = m.parse_syslog(data, ADDR)
    assert r is not None
    assert r["facility"] == 4          # 34 >> 3
    assert r["severity"] == 2          # 34 & 7
    assert r["hostname"] == "host.example.com"
    assert r["app_name"] == "su"
    assert r["proc_id"] == "1234"
    assert r["msg_id"] == "ID47"
    assert r["source_ip"] == "192.168.1.99"


def test_parse_rfc5424_nil_fields_become_none():
    data = b"<13>1 2023-10-11T22:14:15Z - - - - - a body"
    r = m.parse_syslog(data, ADDR)
    assert r["hostname"] is None
    assert r["app_name"] is None
    assert r["proc_id"] is None
    assert r["msg_id"] is None


# ── Parser: RFC 3164 ──────────────────────────────────────────────────────────

def test_parse_rfc3164_with_pid():
    data = b"<34>Oct 11 22:14:15 myhost sshd[1234]: Failed password"
    r = m.parse_syslog(data, ADDR)
    assert r["facility"] == 4
    assert r["severity"] == 2
    assert r["hostname"] == "myhost"
    assert r["app_name"] == "sshd"
    assert r["proc_id"] == "1234"
    assert r["message"] == "Failed password"


def test_parse_rfc3164_without_pid():
    data = b"<38>Jan  5 09:00:00 gw kernel: something happened"
    r = m.parse_syslog(data, ADDR)
    assert r["hostname"] == "gw"
    assert r["app_name"] == "kernel"
    assert r["proc_id"] is None
    assert r["message"] == "something happened"


# ── Parser: fallbacks & PRI decoding ──────────────────────────────────────────

def test_parse_pri_only_fallback():
    data = b"<13>raw message with no structure at all"
    r = m.parse_syslog(data, ADDR)
    assert r["facility"] == 1          # 13 >> 3
    assert r["severity"] == 5          # 13 & 7
    assert r["message"] == "raw message with no structure at all"
    assert r["hostname"] is None


def test_parse_no_pri_keeps_raw_message():
    data = b"just plain text no pri here"
    r = m.parse_syslog(data, ADDR)
    assert r["facility"] is None
    assert r["severity"] is None
    assert r["message"] == "just plain text no pri here"
    assert r["source_ip"] == "192.168.1.99"


def test_pri_decoding_matches_facility_severity_tables():
    # local7.debug = facility 23, severity 7 -> PRI 23*8+7 = 191
    data = b"<191>Oct 11 22:14:15 h app: x"
    r = m.parse_syslog(data, ADDR)
    assert r["facility"] == 23
    assert r["severity"] == 7
    assert m.FACILITY_NAMES[r["facility"]] == "local7"
    assert m.SEVERITY_NAMES[r["severity"]] == "debug"


# ── Parser: SECURITY — malformed / oversized / non-utf8 never crash ───────────

def test_parse_empty_and_whitespace_return_none():
    assert m.parse_syslog(b"", ADDR) is None
    assert m.parse_syslog(b"   \n\t ", ADDR) is None


def test_parse_non_utf8_bytes_do_not_raise():
    # errors="replace" path — invalid continuation bytes must not blow up.
    data = b"<34>\xff\xfe\x80\x81 garbage"
    r = m.parse_syslog(data, ADDR)
    assert r is not None
    assert r["facility"] == 4


def test_parse_oversized_payload_does_not_crash():
    data = b"<34>" + b"A" * 70000
    r = m.parse_syslog(data, ADDR)
    assert r is not None                      # falls through to PRI-only
    assert r["message"].startswith("AAAA")


def test_parse_giant_pri_and_junk_do_not_raise():
    # PRI regex caps at 3 digits; a 4-digit "<9999>" must not match as PRI and
    # must not throw. Also throw assorted control bytes at it.
    for junk in (b"<9999>weird", b"<>", b"<abc>", b"<\x00\x01\x02>", b"<34>"):
        r = m.parse_syslog(junk, ADDR)
        assert r is None or isinstance(r, dict)


def test_parse_injection_payload_is_preserved_not_executed():
    # A SQL-injection-looking body survives as an opaque string in message.
    inj = b"<34>Oct 11 22:14:15 h app: '; DROP TABLE syslog_events; --"
    r = m.parse_syslog(inj, ADDR)
    assert "DROP TABLE" in r["message"]
    assert isinstance(r["message"], str)


# ── detect_threat: IPS / firewall / auth ──────────────────────────────────────

def test_detect_ips_signature_critical():
    ev = {"message": "ET TROJAN Suspicious activity SRC=1.2.3.4 DST=192.168.1.6 "
                     "SPT=6000 DPT=443", "source_ip": "10.0.0.1"}
    t = m.detect_threat(ev)
    assert t["threat_type"] == "ips"
    assert t["severity_level"] == "critical"
    assert t["signature"].startswith("trojan: ")
    assert t["src_addr"] == "1.2.3.4"
    assert t["dst_addr"] == "192.168.1.6"
    assert t["src_port"] == 6000
    assert t["dst_port"] == 443


def test_detect_ips_scan_is_warning():
    ev = {"message": "ET SCAN Potential SSH Scan", "source_ip": "10.0.0.1"}
    t = m.detect_threat(ev)
    assert t["threat_type"] == "ips"
    assert t["severity_level"] == "warning"


def test_detect_firewall_internal_source_is_critical():
    ev = {"message": "[FW-DROP] IN=eth0 SRC=192.168.1.50 DST=8.8.8.8 "
                     "SPT=1111 DPT=4321", "source_ip": "192.168.1.1"}
    t = m.detect_threat(ev)
    assert t["threat_type"] == "firewall"
    assert t["severity_level"] == "critical"   # internal src
    assert t["direction"] == "internal"
    assert t["src_addr"] == "192.168.1.50"


def test_detect_firewall_external_source_is_info():
    ev = {"message": "[FW-BLOCK] SRC=203.0.113.9 DST=192.168.1.6 SPT=9 DPT=22",
          "source_ip": "192.168.1.1"}
    t = m.detect_threat(ev)
    assert t["threat_type"] == "firewall"
    assert t["severity_level"] == "info"
    assert t["direction"] == "inbound"


def test_detect_brute_force_only_after_threshold():
    src = "45.10.20.30"
    ev = {"message": f"Failed password for root from {src}", "source_ip": "192.168.1.6"}
    # First BRUTE_THRESHOLD-1 attempts return None (below threshold)...
    for _ in range(m.BRUTE_THRESHOLD - 1):
        assert m.detect_threat(ev) is None
    # ...the threshold-th trips the brute-force detector.
    t = m.detect_threat(ev)
    assert t["threat_type"] == "auth_failure"
    assert t["src_addr"] == src
    assert t["severity_level"] == "warning"


def test_detect_threat_benign_returns_none():
    ev = {"message": "user logged in successfully", "source_ip": "192.168.1.6"}
    assert m.detect_threat(ev) is None


# ── detect_anomaly ────────────────────────────────────────────────────────────

def _ev(message, **kw):
    d = {"message": message, "source_ip": "192.168.1.6", "hostname": "mac1",
         "app_name": None}
    d.update(kw)
    return d


def test_anomaly_c2_port_from_internal_host():
    ev = _ev("SRC=192.168.1.50 DST=8.8.8.8 SPT=54321 DPT=4444")
    t = m.detect_anomaly(ev)
    assert t["threat_type"] == "c2_suspect"
    assert t["dst_port"] == 4444
    assert t["src_addr"] == "192.168.1.50"
    assert t["severity_level"] == "critical"


def test_anomaly_c2_port_from_external_host_ignored():
    # C2 detection only fires for internal (192.168.1.x) sources.
    ev = _ev("SRC=8.8.8.8 DST=192.168.1.6 SPT=54321 DPT=4444")
    t = m.detect_anomaly(ev)
    assert t is None or t["threat_type"] != "c2_suspect"


def test_anomaly_lateral_movement_after_five_ports():
    ports = [22, 23, 80, 443, 3389]
    got = None
    for p in ports:
        got = m.detect_anomaly(
            _ev(f"SRC=192.168.1.10 DST=192.168.1.20 SPT=50000 DPT={p}"))
    assert got is not None
    assert got["threat_type"] == "lateral_movement"
    assert got["src_addr"] == "192.168.1.10"
    assert got["dst_addr"] == "192.168.1.20"


def test_anomaly_lateral_excludes_whitelisted_source():
    # A Kasa plug hitting many ports is normal discovery, never lateral movement.
    kasa = "192.168.1.45"
    assert kasa in m.LATERAL_EXCLUDE_SOURCES
    got = None
    for p in (22, 23, 80, 443, 3389, 8080, 8443):
        got = m.detect_anomaly(
            _ev(f"SRC={kasa} DST=192.168.1.20 SPT=50000 DPT={p}"))
    assert got is None or got["threat_type"] != "lateral_movement"


def test_anomaly_sensitive_path_after_three_hits():
    ev = _ev("proc read /etc/shadow for auth", hostname="host7")
    assert m.detect_anomaly(ev) is None
    assert m.detect_anomaly(ev) is None
    t = m.detect_anomaly(ev)
    assert t["threat_type"] == "sensitive_access"
    assert t["severity_level"] == "warning"


@pytest.mark.parametrize("line", [
    # 594 fake IPS blocks in 3 days (2026-10-05): 'ET DNS' matched the tail of 'fleET DNS'
    "Starting nova-dns-sync.service - Nova fleet DNS sync (UniFi -> BIND)...",
    "Failed to start nova-dns-sync.service - Nova fleet DNS sync (UniFi -> BIND).",
    "reset info: cabinet policy applied",          # 'SET INFO' / 'ET POLICY' inside words
])
def test_ips_signature_families_need_word_boundaries(line):
    assert m.detect_threat(_ev(line)) is None


def test_ips_real_signature_still_fires():
    t = m.detect_threat(_ev("ET TROJAN Win32/Agent CnC checkin SRC=203.0.113.5 DST=192.168.1.9"))
    assert t["threat_type"] == "ips" and t["signature"].startswith("trojan")


def test_anomaly_suspicious_dns_tld():
    # BIND query log (nova-core resolver) and dnsmasq/pi-hole formats both carry the name
    t = m.detect_anomaly(_ev("client @0x1 192.168.1.43#5555 (badhost.tk): query: badhost.tk IN A + (192.168.1.138)"))
    assert t["threat_type"] == "suspicious_dns"
    assert t["dst_port"] == 53
    assert "badhost.tk" in t["signature"]
    assert m.detect_anomaly(_ev("query[A] beacon-c2-check.xyz from 192.168.1.9"))["threat_type"] == "suspicious_dns"


@pytest.mark.parametrize("line", [
    # incident #3675 (2026-10-05): ".ga" inside ".gateway", not a TLD — 2,142 pages in 14 days
    "client @0x1 192.168.1.43#54948 (attester.gateway.fe2.apple-dns.net): query: attester.gateway.fe2.apple-dns.net IN HTTPS + (192.168.1.138)",
    "query: carbon-cdn.ccgateway.net IN A",          # .cc inside ccgateway
    "query: assets.mlcdn.com IN A",                  # .ml inside mlcdn
    "query: api.pwnedpasswords.com IN A",            # .pw inside pwned
    # postgres STATEMENT logs quoting an investigator's own query text are not DNS
    "2026-10-05 15:03:09 PDT [2848746] STATEMENT: SELECT ... WHERE detail::text ILIKE '%.ga%' OR event_type ILIKE '%dns%'",
    "dns query for badhost.tk resolved",             # prose, no queried name -> nothing to judge
])
def test_anomaly_suspicious_dns_requires_the_tld_to_end_the_name(line):
    assert m.detect_anomaly(_ev(line)) is None


def test_anomaly_crash_storm_fires_when_confirmed(monkeypatch):
    # Shrink the storm gate so two crafted crash lines confirm+page deterministically.
    monkeypatch.setattr(m, "CRASH_STORM_THRESHOLD", 2)
    monkeypatch.setattr(m, "CRASH_STORM_CONFIRM", 1)
    msg = "ReportCrash: sshd terminated EXC_BAD_ACCESS"
    assert m.detect_anomaly(_ev(msg, hostname="crashy")) is None  # 1 crash < threshold
    t = m.detect_anomaly(_ev(msg, hostname="crashy"))             # 2nd -> confirmed
    assert t["threat_type"] == "crash_storm"
    assert t["severity_level"] == "warning"


def test_anomaly_crash_storm_excludes_simulated():
    # Simulated / FileProvider crashes are explicitly excluded and must not count.
    ev = _ev("SIMCRASH DFSFileProvider crash simulated EXC_BAD_ACCESS",
             hostname="simhost")
    for _ in range(30):
        m.detect_anomaly(ev)
    assert "simhost" not in m._crash_events or not m._crash_events["simhost"]


def test_anomaly_volume_spike(monkeypatch):
    host = "chatty"
    # Seed 24 hours of low baseline, then a 10x current-hour surge.
    m._device_event_counts[host].extend([20] * 24)   # baseline ~20/hr
    m._device_hour_count[host] = 500                 # current hour >> 10x
    t = m.detect_anomaly(_ev("some benign chatter", hostname=host))
    assert t is not None
    assert t["threat_type"] == "volume_spike"


def test_anomaly_benign_returns_none():
    assert m.detect_anomaly(_ev("routine info message, nothing to see")) is None


# ── Dedup / rate-limit decision logic ─────────────────────────────────────────

def test_should_alert_dedups_within_window():
    threat = {"signature": "trojan: bad", "src_addr": "1.2.3.4",
              "threat_type": "ips"}
    ev = {"source_ip": "10.0.0.1"}
    assert m.should_alert(threat, ev) is True    # first time -> alert
    assert m.should_alert(threat, ev) is False   # duplicate within window -> suppress


def test_should_alert_lateral_signature_normalized():
    # "hit 5 ports" and "hit 9 ports" must dedup together.
    ev = {"source_ip": "10.0.0.1"}
    t1 = {"signature": "Lateral scan: 192.168.1.10 hit 5 ports on 192.168.1.20 in 60s",
          "src_addr": "192.168.1.10", "threat_type": "lateral_movement"}
    t2 = {"signature": "Lateral scan: 192.168.1.10 hit 9 ports on 192.168.1.20 in 60s",
          "src_addr": "192.168.1.10", "threat_type": "lateral_movement"}
    assert m.should_alert(t1, ev) is True
    assert m.should_alert(t2, ev) is False


def test_should_alert_scan_requires_threshold():
    threat = {"src_addr": "66.66.66.66"}
    ev = {"source_ip": "x"}
    results = [m.should_alert_scan(threat, ev) for _ in range(m.SCAN_THRESHOLD)]
    assert results[:-1] == [False] * (m.SCAN_THRESHOLD - 1)
    assert results[-1] is True


# ── format_alert: injection-safe string assembly ──────────────────────────────

def test_format_alert_includes_label_and_host():
    threat = {"threat_type": "ips", "signature": "trojan: x", "action": "blocked",
              "src_addr": "1.2.3.4", "src_port": 6000, "dst_addr": "192.168.1.6",
              "dst_port": 443, "direction": "inbound"}
    ev = {"hostname": "gw", "source_ip": "192.168.1.1", "message": "raw"}
    out = m.format_alert(threat, ev)
    assert isinstance(out, str)
    assert "IPS Alert" in out
    assert "gw" in out


def test_format_alert_hostile_fields_do_not_break_formatting():
    # Injection/markup in signature & hostname must stay inert text, not crash.
    threat = {"threat_type": "firewall",
              "signature": "'; DROP TABLE x; --\n`whoami`",
              "action": "drop", "src_addr": "192.168.1.9", "direction": "internal"}
    ev = {"hostname": "*}{:rotating_light:", "source_ip": "192.168.1.9",
          "message": "SRC=192.168.1.9"}
    out = m.format_alert(threat, ev)
    assert isinstance(out, str)
    assert "Firewall Block" in out


# ── SECURITY INVARIANT: batch insert is parameterized (no SQL injection) ───────

class _FakeConn:
    def __init__(self):
        self.calls = []

    async def executemany(self, sql, args):
        self.calls.append((sql, args))


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _FakeAcquire(self.conn)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_flush_batch_uses_parameterized_placeholders():
    pool = _FakePool()
    evil = "'; DROP TABLE syslog_events; --"
    batch = [{
        "timestamp": None, "hostname": evil, "facility": 4, "severity": 2,
        "app_name": "app", "proc_id": "1", "msg_id": None,
        "message": evil, "source_ip": "192.168.1.6",
    }]
    _run(m._flush_batch(batch, pool))

    assert len(pool.conn.calls) == 1
    sql, args = pool.conn.calls[0]

    # 1. The SQL is a fixed template with numbered placeholders...
    assert "$1" in sql and "$18" in sql
    assert "VALUES" in sql
    # 2. ...and the hostile payload is NOT interpolated into the SQL text.
    assert "DROP TABLE" not in sql
    # 3. The payload is carried as a bound parameter value, verbatim.
    row = args[0]
    assert evil in row                 # message / hostname passed as data
    assert isinstance(args, list)


def test_flush_batch_maps_threat_and_alert_columns():
    pool = _FakePool()
    batch = [{
        "message": "m", "source_ip": "192.168.1.6",
        "_threat": {"threat_type": "ips", "signature": "sig", "action": "blocked",
                    "direction": "inbound", "src_addr": "1.2.3.4",
                    "dst_addr": "192.168.1.6", "src_port": 6000, "dst_port": 443},
        "_alert_fired": True,
    }]
    _run(m._flush_batch(batch, pool))
    row = pool.conn.calls[0][1][0]
    assert "ips" in row               # threat_type column
    assert "1.2.3.4" in row           # src_addr column
    assert True in row                # alert_fired boolean


def test_flush_batch_swallows_db_errors():
    # A failing DB must be logged, not propagated (drop-and-continue semantics).
    class _BoomPool:
        def acquire(self):
            raise RuntimeError("pg down")
    # Should not raise.
    _run(m._flush_batch([{"message": "x", "source_ip": "192.168.1.6"}], _BoomPool()))


def test_insert_sql_has_no_fstring_interpolation():
    # Static guard: the INSERT template must contain only $-placeholders for values.
    import inspect
    src = inspect.getsource(m._flush_batch)
    # The message/hostname columns must be bound params, never f-string'd in.
    assert "$9::inet" in src
    assert 'VALUES ($1' in src
