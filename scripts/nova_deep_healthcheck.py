#!/usr/bin/env python3
"""nova_deep_healthcheck.py — daily 08:00 FUNCTIONAL health check + auto-fix.

Jordan's principle (2026-09-15): "up but not functional isn't up." A port answering
is not health. Yesterday Plex was 'up' with zero libraries because its NAS mounts were
dead — a basic check called that healthy. This checks whether each subsystem actually
DOES ITS JOB end-to-end, fixes what's safely fixable, and reports honestly to Slack:
what's working, what it FIXED, and what still needs a human.

Every fix passes a hard redline guard (no purchases, deletes, DB-primary surgery,
reboots, network/DNS destruction, exfiltration). Anything it can't safely fix is
escalated, not silently swallowed. --dry-run reports without fixing.

MERGED 2026-10-09 (organ audit M4): this now runs as `nova_selfcheck.py --deep`. main() is a thin
wrapper that delegates there. The check functions, run_checks(), format_report() and write_log()
stay here and are what selfcheck --deep calls; the redline guard / dry-run gate is now
nova_selfcheck.fix_gate (safe_fix delegates to it). deep_healthcheck_log is still written (nova_affect
reads it); each check also lands in selfcheck_runs as deep-*. Report: #nova-alerts when anything
was fixed or broken, else #nova-digest (was #nova-warning, mirrored to Discord).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_selfcheck as _sc  # noqa: E402  — the one fix gate (redline + --dry-run)

MEMSRV = "http://memory-server.digitalnoise.net:18790"
GATEWAY = "http://127.0.0.1:18792"
PLEX_HOST = "192.168.1.2"
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
_REDLINE = _sc._REDLINE


def log(m): print(f"[deep-hc {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def sh(cmd, timeout=45):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 124, "", str(e))


def ssh(host, cmd, timeout=45):
    return sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"kochj@{host}", cmd], timeout)


def get(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), r.status
    except Exception as e:
        return str(e), 0


def keychain(svc):
    r = sh(["security", "find-generic-password", "-a", "nova", "-s", svc, "-w"], 10)
    return r.stdout.strip()


def result(name, ok, detail, fixed=False, fix_detail="", needs_human=False):
    return {"name": name, "ok": ok, "detail": detail, "fixed": fixed,
            "fix_detail": fix_detail, "needs_human": needs_human}


def safe_fix(desc, fn):
    """Run a fix only if it passes the redline guard and we're not in dry-run (nova_selfcheck.fix_gate)."""
    return _sc.fix_gate(desc, fn)


# ── FUNCTIONAL CHECKS ─────────────────────────────────────────────────────────

def check_plex():
    tok = keychain("nova-plex-token")
    body, st = get(f"http://{PLEX_HOST}:32400/library/sections?X-Plex-Token={tok}", 10)
    if st != 200:
        return result("plex", False, f"Plex not responding on {PLEX_HOST}:32400 (http {st})", needs_human=True)
    sizes = [int(x) for x in re.findall(r'size="(\d+)"', body)]
    keys = re.findall(r'key="(\d+)"', body)
    # functional: does at least one library actually contain items?
    total = 0
    for k in keys[:15]:
        b2, s2 = get(f"http://{PLEX_HOST}:32400/library/sections/{k}/all?X-Plex-Token={tok}&X-Plex-Container-Size=0", 10)
        m = re.search(r'totalSize="(\d+)"|size="(\d+)"', b2)
        if m:
            total += int(m.group(1) or m.group(2) or 0)
    if not keys:
        # up but NO libraries — the exact "up but not functional" case
        def fx():
            # most likely the .2 media mounts are dead — remount + refresh
            ssh(PLEX_HOST, "sudo -n systemctl restart 'mnt-*.automount' 2>/dev/null; "
                           "ls /mnt/nas >/dev/null 2>&1; ls /external* >/dev/null 2>&1")
            for k in re.findall(r'key="(\d+)"', get(f'http://{PLEX_HOST}:32400/library/sections?X-Plex-Token={tok}')[0]):
                get(f"http://{PLEX_HOST}:32400/library/sections/{k}/refresh?X-Plex-Token={tok}")
            return True, "remounted .2 media + triggered Plex library refresh"
        ok, fd = safe_fix("plex: remount .2 media and refresh libraries", fx)
        return result("plex", False, "Plex UP but has ZERO libraries (mounts likely dead)", fixed=ok, fix_detail=fd, needs_human=not ok)
    if total == 0:
        def fx():
            ssh(PLEX_HOST, "sudo -n systemctl restart 'mnt-*.automount' 2>/dev/null; true")
            for k in keys:
                get(f"http://{PLEX_HOST}:32400/library/sections/{k}/refresh?X-Plex-Token={tok}")
            return True, f"triggered refresh on {len(keys)} sections (were empty)"
        ok, fd = safe_fix("plex: refresh empty libraries", fx)
        return result("plex", False, f"Plex UP, {len(keys)} libraries but 0 items total (empty!)", fixed=ok, fix_detail=fd, needs_human=not ok)
    return result("plex", True, f"{len(keys)} libraries, {total} items")


def check_nas_mounts():
    bad = []
    for m in ("/Volumes/nas", "/Volumes/external"):
        r = sh(["/bin/ls", "-1", m], 8)
        entries = [x for x in r.stdout.splitlines() if x and not x.startswith(".")]
        if r.returncode != 0 or len(entries) == 0:
            bad.append(m)
    if not bad:
        return result("nas_mounts", True, "/Volumes/nas + /Volumes/external readable & populated")
    def fx():
        r = sh(["/opt/homebrew/bin/python3", str(Path(__file__).parent / "nova_mac_share_mount.py")], 90)
        okall = all(len([x for x in sh(["/bin/ls", "-1", m], 8).stdout.splitlines() if x and not x.startswith(".")]) > 0 for m in bad)
        return okall, f"ran mac_share_mount; remounted={okall}"
    ok, fd = safe_fix("nas: remount dead/empty shares", fx)
    return result("nas_mounts", False, f"dead/empty: {', '.join(bad)}", fixed=ok, fix_detail=fd, needs_human=not ok)


def check_postgres():
    try:
        import psycopg2
        c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=8)
        c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT NOT pg_is_in_recovery()"); writable = cur.fetchone()[0]
        if not _sc.DRY:  # the write probe is the one PG write a report-only run skips
            cur.execute("CREATE TABLE IF NOT EXISTS _hc_probe(t timestamptz); INSERT INTO _hc_probe VALUES(now()); DELETE FROM _hc_probe WHERE t < now()-interval '1 day'")
        cur.execute("SELECT count(*) FROM pg_stat_replication"); standbys = cur.fetchone()[0]
        c.close()
        if writable and standbys >= 1:
            return result("postgres", True, f"primary writable, {standbys} standby(s) streaming")
        return result("postgres", False, f"writable={writable}, standbys={standbys}", needs_human=True)
    except Exception as e:
        return result("postgres", False, f"write/read test FAILED: {e}", needs_human=True)


def check_memory():
    body, st = get(f"{MEMSRV}/stats", 8)
    if st != 200:
        needs = True
    else:
        # functional: does a real recall return anything?
        rb, rs = get(f"{MEMSRV}/recall?q=nova%20memory%20test&n=1", 10)
        try:
            got = len(json.loads(rb).get("memories", []))
        except Exception:
            got = 0
        if got >= 1:
            cnt = json.loads(body).get("count", "?")
            return result("memory", True, f"recall returns results; {cnt} memories")
        needs = True
    def fx():
        ssh("192.168.1.2", "sudo -n systemctl restart nova-memory-server")
        import time; time.sleep(6)
        rb, rs = get(f"{MEMSRV}/recall?q=test&n=1", 10)
        try:
            ok2 = len(json.loads(rb).get("memories", [])) >= 1
        except Exception:
            ok2 = False
        return ok2, "restarted memory server" + (" (recall back)" if ok2 else " (still failing)")
    ok, fd = safe_fix("memory: restart server (recall not returning)", fx)
    return result("memory", False, "memory server up but recall returns nothing", fixed=ok, fix_detail=fd, needs_human=not ok)


def check_gateway():
    body, st = get(f"{GATEWAY}/health", 6)
    if st != 200:
        needs = True
    else:
        # functional: does a real chat turn come back non-empty?
        try:
            req = urllib.request.Request(f"{GATEWAY}/api/chat", method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"message": "healthcheck: reply with one word.", "session_id": "gw2:hc:probe", "agent_id": "chat"}).encode())
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            if resp.get("ok") and (resp.get("response") or "").strip():
                return result("gateway", True, "chat pipeline returns a real response")
        except Exception:
            pass
        needs = True
    def fx():
        sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/net.digitalnoise.nova-gateway-v2"])
        import time; time.sleep(10)
        b2, s2 = get(f"{GATEWAY}/health", 6)
        return s2 == 200, "restarted gateway" + (" (health back)" if s2 == 200 else " (still down)")
    ok, fd = safe_fix("gateway: restart (chat not responding)", fx)
    return result("gateway", False, "gateway up but chat pipeline not returning", fixed=ok, fix_detail=fd, needs_human=not ok)


def check_inference():
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST", headers={"Content-Type": "application/json"},
                data=json.dumps({"model": "qwen3:8b", "stream": False, "think": False, "options": {"num_predict": 5}, "messages": [{"role": "user", "content": "hi"}]}).encode())
            with urllib.request.urlopen(req, timeout=60) as r:
                if json.load(r).get("message", {}).get("content"):
                    return result("inference", True, f"model answered via {node.split('//')[1]}")
        except Exception:
            continue
    return result("inference", False, "NO inference node answered a live prompt", needs_human=True)


def check_dns():
    r = sh(["dscacheutil", "-q", "host", "-a", "name", "pg-primary.digitalnoise.net"], 8)
    ip = ""
    for ln in r.stdout.splitlines():
        if "ip_address" in ln:
            ip = ln.split(":")[-1].strip(); break
    if ip == "192.168.1.2":
        return result("dns", True, f"pg-primary → {ip} (correct primary)")
    def fx():
        sh(["sudo", "-n", "dscacheutil", "-flushcache"]); sh(["sudo", "-n", "killall", "-HUP", "mDNSResponder"])
        sh(["/opt/homebrew/bin/python3", str(Path(__file__).parent / "nova_dns_sync.py")], 60)
        return True, "flushed cache + re-ran dns_sync"
    ok, fd = safe_fix("dns: pg-primary resolves wrong", fx)
    return result("dns", False, f"pg-primary → {ip or 'unresolved'} (expected .2)", fixed=ok, fix_detail=fd, needs_human=not ok)


def check_journal():
    body, st = get("https://nova.digitalnoise.net/operations/index.xml", 12)
    if st == 200 and "<item>" in body:
        return result("journal", True, "operations feed live with recent items")
    return result("journal", False, f"journal feed not serving (http {st})", needs_human=True)


def check_scheduler():
    try:
        import psycopg2
        c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=8)
        cur = c.cursor()
        cur.execute("SELECT exit_code, count(*) FROM scheduler_runs WHERE started_at/1000 > extract(epoch from now())-3600 GROUP BY 1")
        rows = dict(cur.fetchall()); c.close()
        ok = rows.get(0, 0); fail = sum(v for k, v in rows.items() if k not in (0, None))
        if ok > 0 and fail < ok:
            return result("scheduler", True, f"last hour: {ok} ok / {fail} fail")
        return result("scheduler", False, f"last hour: {ok} ok / {fail} fail (unhealthy ratio)", needs_human=True)
    except Exception as e:
        return result("scheduler", False, f"cannot read scheduler_runs: {e}", needs_human=True)


def check_awakening():
    """Are Nova's new organs actually beating?"""
    try:
        import psycopg2
        c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj", connect_timeout=8)
        cur = c.cursor()
        cur.execute("SELECT source, count(*) FROM memories WHERE created_at > now()-interval '26 hours' "
                    "AND source IN ('unclaimed','episodic','research','association','conversation') GROUP BY 1")
        d = dict(cur.fetchall()); c.close()
        quiet = [s for s in ("unclaimed", "episodic") if d.get(s, 0) == 0]
        if not quiet:
            return result("awakening", True, "organs beating: " + ", ".join(f"{k}={v}" for k, v in d.items()))
        return result("awakening", False, f"quiet organs (no output 26h): {', '.join(quiet)}", needs_human=True)
    except Exception as e:
        return result("awakening", False, f"cannot check organs: {e}", needs_human=True)


CHECKS = [check_postgres, check_nas_mounts, check_plex, check_memory, check_gateway,
          check_inference, check_dns, check_journal, check_scheduler, check_awakening]


def run_checks():
    """Run every functional check; a crashing check becomes a needs-human result."""
    results = []
    for chk in CHECKS:
        try:
            results.append(chk())
        except Exception as e:
            results.append(result(chk.__name__, False, f"check crashed: {e}", needs_human=True))
    return results


def format_report(results):
    healthy = [r for r in results if r["ok"]]
    fixed = [r for r in results if not r["ok"] and r["fixed"]]
    broken = [r for r in results if not r["ok"] and not r["fixed"]]

    lines = [f":stethoscope: *Nova daily deep check — {datetime.now().strftime('%a %H:%M')}*",
             f"*{len(healthy)}/{len(results)} functional*" + ("  :white_check_mark:" if not broken else "")]
    if fixed:
        lines.append("\n:wrench: *Fixed:*")
        lines += [f"  • {r['name']}: {r['detail']} → _{r['fix_detail']}_" for r in fixed]
    if broken:
        lines.append("\n:rotating_light: *Needs you:*")
        lines += [f"  • {r['name']}: {r['detail']}" + (f" (fix tried: {r['fix_detail']})" if r['fix_detail'] else "") for r in broken]
    if not fixed and not broken:
        lines.append("Everything up AND functional — mounts populated, recall returns, chat replies, writes land.")
    return "\n".join(lines)


def write_log(results):
    """Audit row in deep_healthcheck_log (unchanged table; nova_affect.signal_infra reads it)."""
    healthy = [r for r in results if r["ok"]]
    fixed = [r for r in results if not r["ok"] and r["fixed"]]
    broken = [r for r in results if not r["ok"] and not r["fixed"]]
    try:
        import psycopg2
        c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=8)
        c.autocommit = True; cur = c.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS deep_healthcheck_log (id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), healthy int, fixed int, broken int, detail jsonb)")
        cur.execute("INSERT INTO deep_healthcheck_log (healthy, fixed, broken, detail) VALUES (%s,%s,%s,%s)",
                    (len(healthy), len(fixed), len(broken), json.dumps(results)))
        c.close()
    except Exception as e:
        log(f"audit write failed: {e}")


def main(argv=None):
    """Thin wrapper: merged into nova_selfcheck.py --deep on 2026-10-09."""
    ap = argparse.ArgumentParser(description="Merged into nova_selfcheck.py --deep (2026-10-09).")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    _sc.log("nova_deep_healthcheck.py was merged into nova_selfcheck.py --deep on 2026-10-09 — delegating")
    return _sc.main(["--deep"] + (["--dry-run"] if a.dry_run else []))


if __name__ == "__main__":
    sys.modules.setdefault("nova_deep_healthcheck", sys.modules[__name__])  # selfcheck --deep imports it: reuse this copy
    sys.exit(main())
