#!/usr/bin/env python3
"""nova_chaos_proofs.py — Chaos Phase 0: prove each never-red monitor CAN go red, in a sandbox.

Approved by Little Mister 2026-10-15 (plan item #46b). The Chanos filter (nova_chanos_filter.py) lists the
critical monitors that have been green for 90 days and were never once shown able to go red. This module
proves them one by one WITHOUT touching anything live:

  mesh_check      the mesh agent's real probe + reconcile code, run in-process against a closed loopback
                  port and an in-memory decoy registry row (fake psycopg2: nothing reaches the database).
                  A positive control (a listening socket / tiny HTTP server) must come back up.
  security_organ  the organ's real cycle() with the UniFi fetch forced to fail and a recording connection:
                  its heartbeat must go 'degraded'.
  core_liveness   the gate's real run_once() with one keystone on a closed port (heartbeat 'degraded') and
                  with its check failing (heartbeat 'down'); notify is a local stub.
  task_sentinel   the sentinel's real run_once() on a scratch scheduler_runs list (6 straight failures ->
                  critical escalation) and on an empty one / an undelivered page (heartbeat 'down').
  task_exit       the scheduled task run once as a subprocess in a sandbox environment
                  (NOVA_PG_HOST=invalid.invalid, or an empty HOME) must exit non-zero, and the sentinel's
                  classifier must call 6 such runs critical.
  slo_page        synthetic bad events inserted INSIDE ONE TRANSACTION THAT IS ALWAYS ROLLED BACK, at
                  timestamps in the future (no real row falls in the windows), fed to the objective's own
                  SLI query with nova_slo's own burn/evaluate: it must say 'page'; a control without the bad
                  events must not. Nothing is ever committed (no other session can see the rows).

Each proof is a row in chaos_proofs (what was run, what came back). A PASSED proof also writes one MARKER
the Chanos filter already recognises, so the monitor moves out of never_red_unproven:
  health:*  a health_checks row status 'ok', error_message "chaos drill proof #N ..." (Chanos counts rows
            matching its TEST_RE), checked_at one day back so it can never be anyone's "latest" reading.
            health_checks keeps 30 days, so --scheduled re-proves weekly.
  slo:*     a slo_status row state 'drill', burn = {"chaos_proof": N, ...}, at = now - 91 days.
  task:*    a scheduler_runs row status 'failure', run_id 'chaos-proof-N', task_group 'chaos_proof',
            started_at = now - 91 days.
  The slo/task markers are BACKDATED on purpose: the Chanos SQL for those sources has no test-row column,
  so the only signal it reads is "ever red". 91 days puts the marker outside the Chanos window (verdict
  never_red_proven, not 'live'), outside every SLO window (28 d: no budget is burned) and outside the
  sentinel's 7-day judgement (no page). The real proof time is in chaos_proofs.proved_at and in the marker.

Never live: no service is stopped, no port blocked, no row committed outside chaos_proofs and the markers.
Monitors that cannot be proven this way are listed as Phase 2 candidates (nova_wargame.py --chaos).

CLI:  --run [--series S ...]   run the proofs (default: every registered one) and record them
      --dry-run                run the proofs, print, write nothing (no chaos_proofs row, no marker)
      --scheduled              weekly: --run for every registered proof
      --from-chanos            run the proofs for the latest Chanos never-red list; list what has no proof
      --report                 latest proof per series
      --selftest               pure assertions, no I/O
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nova_dsn  # noqa: E402

SANDBOX_PG_HOST = "invalid.invalid"     # RFC 6761: guaranteed never to resolve
MARKER_BACKDATE_DAYS = 91               # outside the Chanos 90-day window and every SLO window
HEALTH_MARKER_BACKDATE = "1 day"
HEALTH_MARKER_EVERY_DAYS = 7
PROOF_TAG = "chaos drill proof"         # matches Chanos TEST_RE (\mdrill\M)
STATEMENT_TIMEOUT_MS = 30000

SCHEMA = """
CREATE TABLE IF NOT EXISTS chaos_proofs (
  id         bigserial PRIMARY KEY,
  proved_at  timestamptz NOT NULL DEFAULT now(),
  series     text NOT NULL,
  method     text NOT NULL,
  sandbox    text NOT NULL,
  passed     boolean NOT NULL,
  evidence   jsonb NOT NULL DEFAULT '{}'::jsonb,
  marker     text);
CREATE INDEX IF NOT EXISTS chaos_proofs_series ON chaos_proofs (series, proved_at DESC);
"""

# series -> (method, args). The Chanos list of 2026-10-15 (14 critical never-red unproven monitors).
REGISTRY = {
    "health:ollama@nova-core5/mac-studio": ("mesh_check", {}),
    "health:postgresql@mac-studio/mac-studio": ("mesh_check", {}),
    "health:security_organ@nova-core/self": ("security_organ", {}),
    "health:core_liveness@mac-studio/core_liveness": ("core_liveness", {}),
    "health:task_sentinel@mac-studio/nova_task_sentinel": ("task_sentinel", {}),
    "task:session_watchdog": ("task_exit", {"argv": ["bash", "nova_session_reset.sh", "--check", "--threshold", "12"],
                                            "sandbox_env": "empty_home", "script": "nova_session_reset.sh"}),
    "task:slo": ("task_exit", {"argv": ["{python}", "nova_slo.py", "--dry-run"], "sandbox_env": "pg_invalid",
                               "script": "nova_slo.py"}),
    "slo:scheduled_jobs": ("slo_page", {}),
    "slo:alert_delivery": ("slo_page", {}),
    "slo:web": ("slo_page", {}),
    "slo:chat": ("slo_page", {}),
    "slo:morning_bluf": ("slo_page", {}),
    "slo:memory_recall": ("slo_page", {}),
    "slo:database": ("slo_page", {}),
}


def log(m: str) -> None:
    print(f"[chaos-proofs {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure helpers ──────────────────────────────────────────────────────────────

def parse_series(series: str) -> dict:
    """'health:svc@node/by' -> {kind, service, node, checked_by}; 'slo:x' / 'task:x' -> {kind, name}. Pure."""
    kind, _, rest = series.partition(":")
    if kind == "health":
        svc, _, tail = rest.partition("@")
        node, _, by = tail.partition("/")
        return {"kind": kind, "service": svc, "node": node, "checked_by": by}
    return {"kind": kind, "name": rest}


def method_for(series: str) -> tuple | None:
    """The proof method for a series: registry first, then the generic one for its kind. Pure."""
    if series in REGISTRY:
        return REGISTRY[series]
    kind = parse_series(series)["kind"]
    if kind == "slo":
        return ("slo_page", {})
    return None


def sli_kind(sql: str) -> tuple:
    """Which synthetic bad events an SLI query needs -> ('probe', name) | ('bluf', None) | ('jobs', None) |
    ('alerts', None) | (None, None). Pure."""
    import re
    s = " ".join((sql or "").split())
    m = re.search(r"telemetry\.probe_results\b.*?probe\s*=\s*'([^']+)'", s)
    if m:
        return "probe", m.group(1)
    if "scheduler_runs" in s and "morning_brief" in s:
        return "bluf", None
    if "scheduler_runs" in s:
        return "jobs", None
    if "telemetry.events" in s:
        return "alerts", None
    return None, None


def marker(series: str, proof_id: int, summary: str) -> tuple | None:
    """(sql, params) for the Chanos-recognised marker of one passed proof, or None. Pure."""
    p = parse_series(series)
    note = f"{PROOF_TAG} #{proof_id} (sandboxed; marker row, not a reading): {summary}"[:900]
    if p["kind"] == "health":
        return ("INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message, checked_at) "
                f"VALUES (%s, %s, %s, 'ok', %s, now() - interval '{HEALTH_MARKER_BACKDATE}')",
                (p["service"], p["node"], p["checked_by"], note))
    if p["kind"] == "slo":
        return ("INSERT INTO slo_status (name, at, state, burn) "
                f"VALUES (%s, now() - interval '{MARKER_BACKDATE_DAYS} days', 'drill', %s::jsonb) "
                "ON CONFLICT DO NOTHING",
                (p["name"], json.dumps({"chaos_proof": proof_id, "note": note,
                                        "proved_at": datetime.now(timezone.utc).isoformat()})))
    if p["kind"] == "task":
        script = (REGISTRY.get(series, (None, {}))[1] or {}).get("script", "chaos_proof")
        return ("INSERT INTO scheduler_runs (run_id, task_id, task_script, task_group, scheduled_at, started_at, "
                "ended_at, duration_ms, exit_code, status, error_tail) VALUES (%s, %s, %s, 'chaos_proof', "
                f"(extract(epoch FROM now() - interval '{MARKER_BACKDATE_DAYS} days') * 1000)::bigint, "
                f"(extract(epoch FROM now() - interval '{MARKER_BACKDATE_DAYS} days') * 1000)::bigint, "
                f"(extract(epoch FROM now() - interval '{MARKER_BACKDATE_DAYS} days') * 1000)::bigint + 1, "
                "1, 1, 'failure', %s) ON CONFLICT (run_id) DO NOTHING",
                (f"chaos-proof-{proof_id}", p["name"], script, note))
    return None


def closed_port() -> int:
    """A loopback TCP port with nothing listening (bound, then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def rewrite_health_url(health_url, port: int):
    """Point an absolute health URL at 127.0.0.1:<port> keeping its path; a bare path stays a path. Pure."""
    from urllib.parse import urlsplit
    hu = (health_url or "").strip()
    if hu.startswith(("http://", "https://")):
        parts = urlsplit(hu)
        return f"http://127.0.0.1:{port}{parts.path or '/'}"
    return hu or None


def _result(passed: bool, sandbox: str, **evidence) -> dict:
    return {"passed": bool(passed), "sandbox": sandbox, "evidence": evidence}


# ── recording fakes (no database) ─────────────────────────────────────────────

class RecCursor:
    """Records every statement; answers fetches from a script of (substring, rows) pairs."""

    def __init__(self, answers=(), fail_on=()):
        self.sql, self.answers, self.fail_on, self._last = [], list(answers), tuple(fail_on), ""
        self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql
        if any(f in sql for f in self.fail_on) and "SAVEPOINT" not in sql:
            raise RuntimeError(f"sandbox: forced failure on {sql[:40]}")

    def _rows(self):
        for key, rows in self.answers:
            if key in self._last:
                return rows
        return []

    def fetchall(self):
        return list(self._rows())

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def close(self):
        pass

    def inserts(self, table: str) -> list:
        return [p for s, p in self.sql if f"INSERT INTO {table}" in s]


class RecConn:
    def __init__(self, cur: RecCursor):
        self.cur, self.commits, self.rollbacks, self.closed, self.autocommit = cur, 0, 0, False, False
        cur.connection = self

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@contextlib.contextmanager
def http_server(path: str):
    """A loopback HTTP server answering 200 on `path` (the positive control for HTTP checks)."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path.split("?")[0] == path else 404)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _load(name: str):
    """Import a sibling script by file path under a private name (never runs its main)."""
    spec = importlib.util.spec_from_file_location(f"_chaosproof_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── proofs ────────────────────────────────────────────────────────────────────

def prove_mesh_check(series: str, cur=None, **_) -> dict:
    """The mesh agent's own probe says 'down' for a dead endpoint shaped like this service's, and its
    reconcile path writes a 'down' health_checks row for an in-memory decoy registry row."""
    p = parse_series(series)
    health_url = None
    if cur is not None:
        try:
            cur.execute("SELECT health_url FROM service_registry WHERE service_name = %s AND node_name = %s",
                        (p["service"], p["node"]))
            r = cur.fetchone()
            health_url = r[0] if r else None
        except Exception as e:  # noqa: BLE001 — the shape is a nicety; the proof stands without it
            log(f"registry read failed ({e}); proving the TCP shape")
    M = _load("nova_mesh_agent")
    dead = closed_port()
    hu = rewrite_health_url(health_url, dead)
    path = (hu or "").replace(f"http://127.0.0.1:{dead}", "") if hu and hu.startswith("http") else hu
    down_up, _ = M.probe_registry_service("127.0.0.1", dead, hu, set())
    # positive control: the same check against something alive must say up
    if hu:
        with http_server(path or "/") as live:
            ctl_up, _ = M.probe_registry_service("127.0.0.1", live, rewrite_health_url(hu, live), set())
    else:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            ctl_up, _ = M.probe_registry_service("127.0.0.1", srv.getsockname()[1], None, set())
        finally:
            srv.close()
    # reconcile path with a decoy registry row, through a fake psycopg2 (nothing reaches PG)
    rc = RecCursor(answers=[("FROM service_registry",
                             [(f"{p['service']}_chaos_decoy", p["node"], "127.0.0.1", dead, path, 3600.0)])])
    fake_pg = mock.MagicMock()
    fake_pg.connect.return_value = RecConn(rc)
    cfg = {**M.config, "node_name": "chaos-sandbox", "registry_authority": "chaos-sandbox", "pg_dsn": "sandbox"}
    with mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), mock.patch.object(M, "config", cfg), \
            contextlib.redirect_stdout(open(os.devnull, "w")):
        M.reconcile_registry_health()
    rows = rc.inserts("health_checks")
    wrote_down = bool(rows) and rows[0][3] == "down"
    reg = [p_ for s, p_ in rc.sql if "UPDATE service_registry" in s]
    return _result(not down_up and ctl_up and wrote_down, "closed loopback port + in-memory decoy registry row",
                   probe_dead_endpoint_up=down_up, control_up=ctl_up, reconcile_health_row=rows[:1],
                   registry_update=reg[:1], shape=("http" if hu else "tcp"))


def prove_security_organ(series: str, **_) -> dict:
    """The security organ's cycle() writes a 'degraded' heartbeat when UniFi cannot be read."""
    SO = _load("nova_security_organ")
    rc = RecCursor()
    with mock.patch.object(SO.unifi, "_fetch_clients", return_value=None), \
            mock.patch.object(SO.unifi, "_unifi_login", return_value=False), \
            mock.patch.object(SO, "notify", return_value=True), \
            contextlib.redirect_stdout(open(os.devnull, "w")):
        SO.cycle(RecConn(rc))
    hb = [p for s, p in rc.sql if "INSERT INTO health_checks" in s]
    status = hb[0][1] if hb else None
    return _result(status == "degraded", "UniFi fetch forced to fail; recording connection",
                   heartbeat_status=status, heartbeat_detail=(hb[0][2] if hb else None))


def prove_core_liveness(series: str, **_) -> dict:
    """The liveness gate pages and goes 'degraded' with a keystone on a closed port, and 'down' when its
    own check fails."""
    L = _load("nova_core_liveness")
    sent = []
    fresh = [("max(checked_at)", [(5,)]), ("capacity_snapshots", [(5,)]), ("SELECT status", [("up", "now")])]
    rc = RecCursor(answers=fresh)
    with mock.patch.object(L, "notify", side_effect=lambda *a, **k: sent.append((a, k)) or True), \
            mock.patch.object(L, "KEYSTONES", [("chaos decoy", "127.0.0.1", closed_port(), None)]), \
            contextlib.redirect_stdout(open(os.devnull, "w")):
        n = L.run_once(RecConn(rc))
    hb1 = [p for s, p in rc.sql if "INSERT INTO health_checks" in s]
    rc2 = RecCursor(answers=fresh, fail_on=("max(checked_at)",))
    raised = False
    with mock.patch.object(L, "notify", return_value=True), mock.patch.object(L, "KEYSTONES", []), \
            contextlib.redirect_stdout(open(os.devnull, "w")):
        try:
            L.run_once(RecConn(rc2))
        except RuntimeError:
            raised = True
    hb2 = [p for s, p in rc2.sql if "INSERT INTO health_checks" in s]
    s1, s2 = (hb1[0][0] if hb1 else None), (hb2[0][0] if hb2 else None)
    paged = bool(sent) and sent[0][1].get("level") == "critical"
    return _result(n == 1 and paged and s1 == "degraded" and s2 == "down" and raised,
                   "closed loopback keystone; forced check failure; notify stubbed",
                   issues=n, paged_critical=paged, heartbeat_issue=s1, heartbeat_check_failed=s2)


def prove_task_sentinel(series: str, **_) -> dict:
    """The sentinel escalates a scratch task with 6 straight failures, and its own heartbeat goes 'down'
    when it has nothing current to judge or cannot deliver a page."""
    T = _load("nova_task_sentinel")
    now = T._now_ms()
    rows = [("chaos_scratch", now - i * 3_600_000, "failure", 1) for i in range(6)]
    pages = []
    ok_notify = mock.MagicMock(notify=lambda *a, **k: pages.append(k) or True)
    bad_notify = mock.MagicMock(notify=lambda *a, **k: False)
    out = {}
    for label, data, notifier in (("scratch", rows, ok_notify), ("empty", [], ok_notify),
                                  ("undelivered", rows, bad_notify)):
        rc = RecCursor(answers=[("FROM scheduler_runs", data)])
        with mock.patch.object(T, "load_configured_tasks", return_value={"chaos_scratch": "every 1h"}), \
                mock.patch.dict(sys.modules, {"nova_notify": notifier}):
            deg = T.run_once(RecConn(rc))
        hb = [p for s, p in rc.sql if "INSERT INTO health_checks" in s]
        out[label] = {"heartbeat": hb[0][0] if hb else None, "degraded": [d[1]["state"] for d in deg],
                      "queued": any("INSERT INTO claude_queue" in s for s, _ in rc.sql)}
    crit = out["scratch"]["degraded"] == ["critical"] and out["scratch"]["queued"] and \
        any(k.get("level") == "critical" for k in pages)
    passed = crit and out["empty"]["heartbeat"] == "down" and out["undelivered"]["heartbeat"] == "down"
    return _result(passed, "scratch scheduler_runs list; notify stubbed", **out)


def sandbox_env(kind: str, tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("NOVA_OPS_DSN",)}
    if kind == "pg_invalid":
        env["NOVA_PG_HOST"] = SANDBOX_PG_HOST
    elif kind == "empty_home":
        env["HOME"] = tmp
    return env


def prove_task_exit(series: str, argv=None, sandbox_env_kind=None, **kw) -> dict:
    """One sandboxed run of the task's own script must exit non-zero (what the scheduler records as a
    failure), and the sentinel's classifier must call six such runs critical."""
    kind = sandbox_env_kind or kw.get("sandbox_env")
    argv = [sys.executable if a == "{python}" else a for a in (argv or kw.get("argv") or [])]
    with tempfile.TemporaryDirectory(prefix="nova-chaos-proof-") as tmp:
        try:
            r = subprocess.run(argv, cwd=str(HERE), env=sandbox_env(kind, tmp), capture_output=True, text=True,
                               timeout=90, stdin=subprocess.DEVNULL)
            rc, tail = r.returncode, (r.stderr or r.stdout or "").strip()[-300:]
        except subprocess.TimeoutExpired:
            rc, tail = None, "timed out (the scheduler records a timeout as a failure)"
    T = _load("nova_task_sentinel")
    now = T._now_ms()
    h = T.classify_task([{"started_at": now - i * 3_600_000, "status": "failure", "exit_code": rc or 1}
                         for i in range(6)], now_ms=now)
    return _result(rc != 0 and h["state"] == "critical", f"subprocess with {kind}",
                   exit_code=rc, output_tail=tail, sentinel_state_after_6=h["state"])


def _bluf_end(now: datetime) -> datetime:
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    d = (now.astimezone(pt) + timedelta(days=1)).date()
    return datetime(d.year, d.month, d.day, 7, 12, tzinfo=pt).astimezone(timezone.utc)


def _insert_bad(cur, kind: str, probe: str | None, end: datetime) -> int:
    """Synthetic bad events inside [end-1h, end). Caller's transaction; it is always rolled back."""
    n = 0
    for k in range(12):
        t = end - timedelta(minutes=1 + 5 * k)
        if kind == "probe":
            cur.execute("INSERT INTO telemetry.probe_results (ts, probe, ok, latency_ms, detail) "
                        "VALUES (%s, %s, false, 0, %s)", (t, probe, f"{PROOF_TAG} sandbox (rolled back)"))
        elif kind == "jobs":
            ms = int(t.timestamp() * 1000)
            cur.execute("INSERT INTO scheduler_runs (run_id, task_id, task_script, task_group, scheduled_at, "
                        "started_at, ended_at, status, exit_code) VALUES (%s, 'chaos_sandbox', 'none', "
                        "'chaos_proof', %s, %s, %s, 'failure', 1)", (f"chaos-sandbox-{uuid.uuid4().hex}", ms, ms, ms + 1))
        elif kind == "alerts":
            cur.execute("INSERT INTO telemetry.events (ts, source, level, category, title, status, meta) "
                        "VALUES (%s, 'nova_chaos_proofs', 'info', 'chaos', %s, 'error', '{\"test\": true}'::jsonb)",
                        (t, f"{PROOF_TAG} sandbox (rolled back)"))
        else:
            return 0
        n += 1
    return n


def prove_slo_page(series: str, conn=None, now: datetime | None = None, **_) -> dict:
    """The objective's own SLI query + nova_slo's burn/evaluate say 'page' on synthetic bad events, and do
    not without them. One transaction, always rolled back; nothing is committed."""
    import nova_slo as SLO
    name = parse_series(series)["name"]
    now = now or datetime.now(timezone.utc)
    own = conn is None
    if own:
        import psycopg2
        conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"))
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        cur.execute("SET LOCAL lock_timeout = '3s'")
        cur.execute("SELECT sli_sql, target FROM slo_definitions WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            return _result(False, "rolled-back transaction", error=f"no slo_definitions row {name}")
        sql, target = row
        kind, probe = sli_kind(sql)

        def state_at(end):
            burns = {w: round(SLO.burn_rate(*SLO._sli(cur, sql, end - d, end), target), 3)
                     for w, d in SLO.WINDOWS.items()}
            return SLO.evaluate(burns), burns
        if kind == "bluf":
            end = _bluf_end(now)                               # tomorrow 07:12 PT: no delivery exists yet
            bad, bad_burns = state_at(end)
            ms = int((end - timedelta(minutes=7)).timestamp() * 1000)
            cur.execute("INSERT INTO scheduler_runs (run_id, task_id, task_script, task_group, scheduled_at, "
                        "started_at, ended_at, status, exit_code) VALUES (%s, 'morning_brief', 'none', "
                        "'chaos_proof', %s, %s, %s, 'success', 0)", (f"chaos-sandbox-{uuid.uuid4().hex}", ms, ms, ms))
            control, ctl_burns = state_at(end)
            injected = 0
        elif kind:
            end = now + timedelta(minutes=70)                  # windows ending here hold no real rows
            control, ctl_burns = state_at(end)
            injected = _insert_bad(cur, kind, probe, end)
            bad, bad_burns = state_at(end)
        else:
            return _result(False, "rolled-back transaction", error="SLI shape not recognised", sli_sql=sql[:200])
        return _result(bad == "page" and control != "page", "one transaction, always rolled back",
                       sli_kind=kind, probe=probe, injected=injected, state_with_bad=bad, burns_with_bad=bad_burns,
                       state_control=control, burns_control=ctl_burns, window_end=end.isoformat())
    finally:
        try:
            conn.rollback()                                    # ALWAYS: no synthetic row survives
        finally:
            if own:
                conn.close()


METHODS = {"mesh_check": prove_mesh_check, "security_organ": prove_security_organ,
           "core_liveness": prove_core_liveness, "task_sentinel": prove_task_sentinel,
           "task_exit": prove_task_exit, "slo_page": prove_slo_page}


def prove(series: str, cur=None) -> dict:
    """Run one proof. Never raises: an error is a failed proof with the error as evidence."""
    m = method_for(series)
    if not m:
        return {"series": series, "method": None, **_result(False, "none", error="no sandboxed proof: Phase 2")}
    method, args = m
    t = time.monotonic()
    try:
        kw = dict(args)
        if method == "mesh_check":
            kw["cur"] = cur
        res = METHODS[method](series, **kw)
    except Exception as e:  # noqa: BLE001
        res = _result(False, "error", error=f"{type(e).__name__}: {e}")
    res["evidence"]["seconds"] = round(time.monotonic() - t, 2)
    return {"series": series, "method": method, **res}


# ── database ──────────────────────────────────────────────────────────────────

def connect():
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def marker_due(cur, series: str) -> bool:
    """health markers every HEALTH_MARKER_EVERY_DAYS (retention is 30 d); slo/task markers once."""
    kind = parse_series(series)["kind"]
    if kind == "health":
        cur.execute("SELECT 1 FROM chaos_proofs WHERE series = %s AND marker IS NOT NULL "
                    "AND proved_at > now() - make_interval(days => %s) LIMIT 1", (series, HEALTH_MARKER_EVERY_DAYS))
    else:
        cur.execute("SELECT 1 FROM chaos_proofs WHERE series = %s AND marker IS NOT NULL LIMIT 1", (series,))
    return cur.fetchone() is None


def record(cur, res: dict) -> int:
    cur.execute("INSERT INTO chaos_proofs (series, method, sandbox, passed, evidence) VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id", (res["series"], res["method"] or "none", res["sandbox"], res["passed"],
                                 json.dumps(res["evidence"], default=str)))
    pid = cur.fetchone()[0]
    if res["passed"] and marker_due(cur, res["series"]):
        summ = f"{res['method']} in {res['sandbox']}"
        mk = marker(res["series"], pid, summ)
        if mk:
            cur.execute(*mk)
            cur.execute("UPDATE chaos_proofs SET marker = %s WHERE id = %s", (mk[0].split("(")[0].strip(), pid))
    return pid


def chanos_unproven(cur) -> list:
    try:
        cur.execute("SELECT series FROM chanos_filter WHERE month = (SELECT max(month) FROM chanos_filter) "
                    "AND critical AND verdict = 'never_red_unproven' ORDER BY readings DESC")
        return [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def run(series: list, dry: bool) -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        if not dry:
            ensure_schema(cur)
        results, failed = [], 0
        for s in series:
            r = prove(s, cur)
            results.append(r)
            mark = ""
            if not dry:
                r["id"] = record(cur, r)
                mark = f" (#{r['id']})"
            failed += not r["passed"]
            log(f"{'PROVEN' if r['passed'] else 'NOT PROVEN'} {s} via {r['method']}{mark}: "
                f"{json.dumps(r['evidence'], default=str)[:300]}")
        phase2 = [r["series"] for r in results if not r["method"]]
        print(f"\n{len(results) - failed}/{len(results)} proven in a sandbox"
              + (f"; no sandboxed proof (Phase 2 candidates): {', '.join(phase2)}" if phase2 else "")
              + (" [dry-run: nothing written]" if dry else ""))
        return 0 if not failed else 1
    finally:
        conn.close()


def report() -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('chaos_proofs') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("No chaos proofs recorded yet.")
            return 0
        cur.execute("SELECT DISTINCT ON (series) series, proved_at, method, passed, marker FROM chaos_proofs "
                    "ORDER BY series, proved_at DESC")
        for s, at, m, ok, mk in cur.fetchall():
            print(f"{'PROVEN    ' if ok else 'NOT PROVEN'} {s:<55} {at:%Y-%m-%d %H:%M} {m} {mk or ''}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    assert parse_series("health:a@b/c") == {"kind": "health", "service": "a", "node": "b", "checked_by": "c"}
    assert parse_series("slo:chat") == {"kind": "slo", "name": "chat"}
    assert method_for("slo:new_one")[0] == "slo_page" and method_for("probe:x") is None
    assert sli_kind("SELECT .. FROM telemetry.probe_results WHERE probe = 'postgres' AND ts") == ("probe", "postgres")
    assert sli_kind("FROM scheduler_runs r WHERE r.task_id = 'morning_brief'")[0] == "bluf"
    assert sli_kind("FROM scheduler_runs WHERE status IN")[0] == "jobs"
    assert sli_kind("FROM telemetry.events WHERE status")[0] == "alerts" and sli_kind("x") == (None, None)
    sql, prm = marker("health:a@b/c", 7, "x")
    assert "'ok'" in sql and "interval '1 day'" in sql and "drill" in prm[3]
    assert "91 days" in marker("slo:chat", 7, "x")[0] and "'failure'" in marker("task:slo", 7, "x")[0]
    assert rewrite_health_url("http://10.0.0.9:11434/api/version", 5) == "http://127.0.0.1:5/api/version"
    assert rewrite_health_url("/health", 5) == "/health" and rewrite_health_url(None, 5) is None
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--from-chanos", action="store_true")
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.report:
        return report()
    if not (a.run or a.dry_run or a.scheduled or a.from_chanos):
        ap.print_help()
        return 0
    series = a.series or list(REGISTRY)
    if a.from_chanos:
        conn = connect()
        try:
            series = chanos_unproven(conn.cursor()) or series
        finally:
            conn.close()
    return run(series, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
