#!/usr/bin/env python3
"""nova_leader_wrapper.py — SPOF plan Phase 3: PG-advisory-lock leader election around any long-running service.

    nova_leader_wrapper.py --name nova-scheduler-core -- /path/python nova_scheduler.py

Whoever holds pg_advisory_lock(hashtext(name)) on the nova_ops primary runs the real command as a child;
everyone else warm-stands by, retrying every RETRY_S, and writes a heartbeat row to leader_standby so the
board shows who is leader and who is waiting. The lock lives in one session: if that PG connection dies the
lock is gone, so the wrapper then kills its child (two leaders are worse than none). SIGTERM/SIGHUP are
forwarded to the child; the wrapper exits with the child's code (systemd Restart= brings it back as a
candidate). State file semantics of the wrapped service are its own business (scheduler: copy its
*_state.json to the standby so a takeover does not fire every interval task at once).
--selftest (needs PG): proves a second candidate cannot take the lock while the first holds it.
Approved by Jordan 2026-10-04 (essay 2026-10-03 §XIV, Phase 3).
"""
import nova_dsn as _nova_dsn  # noqa: E402
import os, sys, time, signal, socket, subprocess
import psycopg2

DSN = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops", "connect_timeout=8"))
NODE = socket.gethostname().split(".")[0]
RETRY_S = 10            # standby: how often to try for the lock
BEAT_S = 30             # heartbeat / liveness check cadence
DDL = """CREATE TABLE IF NOT EXISTS leader_standby (
  name text NOT NULL, node text NOT NULL, role text NOT NULL, pid int, ts timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (name, node))"""
BEAT = ("INSERT INTO leader_standby (name, node, role, pid, ts) VALUES (%s,%s,%s,%s,now()) "
        "ON CONFLICT (name, node) DO UPDATE SET role=EXCLUDED.role, pid=EXCLUDED.pid, ts=now() WHERE leader_standby.name=EXCLUDED.name")

def log(m): print(f"[leader {NODE}] {m}", flush=True)

def parse(argv):
    """Pure: ['--name', N, '--', cmd...] -> (N, [cmd...])."""
    if "--" not in argv or "--name" not in argv:
        raise SystemExit(__doc__)
    i = argv.index("--")
    return argv[argv.index("--name") + 1], argv[i + 1:]

def connect():
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur: cur.execute(DDL)
    return c

def try_lock(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,)); return cur.fetchone()[0]

def beat(conn, name, role, pid=None):
    try:
        with conn.cursor() as cur: cur.execute(BEAT, (name, NODE, role, pid)); return True
    except Exception as e:  # noqa: BLE001
        log(f"heartbeat failed: {e}"); return False

def run_leader(conn, name, cmd):
    child = subprocess.Popen(cmd)
    def fwd(sig, _f):
        log(f"forwarding signal {sig} to child {child.pid}"); child.send_signal(sig)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, fwd)
    log(f"LEADER — started child {child.pid}: {' '.join(cmd)}")
    beat(conn, name, "leader", child.pid)
    last = time.time()
    while child.poll() is None:
        time.sleep(1)
        if time.time() - last >= BEAT_S:
            last = time.time()
            if not beat(conn, name, "leader", child.pid):       # lock session is gone -> we are no longer the leader
                log("lost the PG session that holds the lock — stopping child")
                child.terminate()
                try: child.wait(20)
                except subprocess.TimeoutExpired: child.kill()
                return 75                                        # EX_TEMPFAIL: systemd restarts us as a candidate
    log(f"child exited {child.returncode}")
    return child.returncode

def main():
    name, cmd = parse(sys.argv[1:])
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            if try_lock(conn, name):
                rc = run_leader(conn, name, cmd)
                try:
                    with conn.cursor() as cur: cur.execute("SELECT pg_advisory_unlock_all()")
                    beat(conn, name, "standby")
                except Exception:  # noqa: BLE001
                    pass
                sys.exit(rc)
            beat(conn, name, "standby")
            time.sleep(RETRY_S)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            log(f"PG unavailable ({e.__class__.__name__}) — retrying in {RETRY_S}s")
            try: conn and conn.close()
            except Exception: pass
            conn = None; time.sleep(RETRY_S)

def selftest():
    assert parse(["--name", "x", "--", "sleep", "1"]) == ("x", ["sleep", "1"])
    a, b = connect(), connect()
    assert try_lock(a, "selftest-lock") is True
    assert try_lock(b, "selftest-lock") is False, "second session must NOT get the lock"
    a.close()                                          # session gone -> lock released
    time.sleep(0.2)
    assert try_lock(b, "selftest-lock") is True
    with b.cursor() as cur: cur.execute("SELECT pg_advisory_unlock_all()")
    b.close(); print("selftest ok")

if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
