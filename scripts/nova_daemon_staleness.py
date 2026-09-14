#!/usr/bin/env python3
"""nova_daemon_staleness.py — flag nova daemons running STALE on-disk code.

WHY THIS EXISTS:
A launchd daemon loads its script into a long-lived process. When you edit the
script but forget to restart the job, the process keeps running the OLD code — the
edit on disk is a lie about what's actually executing. This is silent and can persist
for days: an ops article found nova-scheduler-core with on-disk code 127h newer than
the running process. Nothing noticed.

WHAT IT DOES:
For each managed nova launchd job (labels net.digitalnoise.* / com.nova.* /
com.digitalnoise.nova.*) that currently has a live PID, it compares:
  - the mtime of the job's main script (from the plist ProgramArguments), against
  - the process start time (now - `ps -o etimes`).
If the on-disk script is newer than the running process by more than GRACE_S
(default 10 min), it NOTIFYs (nova_notify, category 'stale-code', deduped per label)
that the daemon is running stale code and should be restarted.

IT DOES NOT RESTART ANYTHING. Restarting a daemon is the owner's call (it may be
mid-work, or the edit may be intentional-but-not-yet-ready). This reports only.

Jobs with no live PID (StartInterval/on-demand jobs not currently executing) have no
running process to be stale, so they are skipped. The GRACE_S window absorbs the
normal ordering where a freshly-restarted process starts a moment after the file it
loads was written.

Cadence: every 30m via launchd (net.digitalnoise.nova-daemon-staleness, StartInterval
1800). Never raises out of run_once.

Written by Jordan Koch.
"""
from __future__ import annotations
import os
import sys
import glob
import plistlib
import subprocess
from typing import Optional

LAUNCHAGENTS = os.path.expanduser("~/Library/LaunchAgents")
GRACE_S = 600  # 10 min: on-disk newer than the process by more than this -> stale
MANAGED_PREFIXES = ("net.digitalnoise.", "com.nova.", "com.digitalnoise.nova.")
# interpreters that are never themselves "the script" — skip to find the real target
_INTERPRETERS = ("python", "python3", "zsh", "bash", "sh", "ruby", "node", "perl", "env")
SESSION_ID = "nova-daemon-staleness"


def _now_s() -> float:
    import time
    return time.time()


def label_is_managed(label: str) -> bool:
    """True for the nova-owned launchd namespaces we watch."""
    return bool(label) and label.startswith(MANAGED_PREFIXES)


def script_path_from_args(args: list) -> Optional[str]:
    """Pick the daemon's main script out of ProgramArguments.

    Skips the interpreter (python3/zsh/...) and any leading -flags, returning the
    first real script path (.py/.sh/... or an absolute path under the home scripts
    dir). Falls back to the last argument if nothing matches."""
    if not args:
        return None
    cand = None
    for a in args:
        if not isinstance(a, str) or not a:
            continue
        base = os.path.basename(a).lower()
        if base in _INTERPRETERS or a.startswith("-"):
            continue
        if a.endswith((".py", ".sh", ".rb", ".js", ".pl")) or a.startswith("/"):
            cand = a
            break
    if cand is None:
        # last resort: last non-flag, non-interpreter arg (a bare interpreter has no script)
        for a in reversed(args):
            if (isinstance(a, str) and a and not a.startswith("-")
                    and os.path.basename(a).lower() not in _INTERPRETERS):
                cand = a
                break
    return cand


def parse_plist(path: str) -> Optional[dict]:
    """Return {'label', 'script'} for a plist, or None if unusable."""
    try:
        with open(path, "rb") as f:
            d = plistlib.load(f)
    except Exception:
        return None
    label = d.get("Label")
    script = script_path_from_args(d.get("ProgramArguments") or [])
    if not label or not script:
        return None
    return {"label": label, "script": script}


def discover_daemons(launchagents: str = LAUNCHAGENTS) -> list:
    """All managed nova daemons found in ~/Library/LaunchAgents (label+script)."""
    out = []
    for path in sorted(glob.glob(os.path.join(launchagents, "*.plist"))):
        info = parse_plist(path)
        if info and label_is_managed(info["label"]):
            info["plist"] = path
            out.append(info)
    return out


def launchctl_pid(label: str) -> Optional[int]:
    """PID of a loaded job, or None if not loaded / not currently running."""
    try:
        r = subprocess.run(["launchctl", "list", label],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith('"PID"'):
            # '"PID" = 22008;'
            try:
                return int(s.split("=")[1].strip().rstrip(";").strip())
            except Exception:
                return None
    return None


def parse_etime(s: str) -> Optional[int]:
    """Parse BSD `ps -o etime` elapsed time ([[DD-]HH:]MM:SS) to seconds. macOS ps
    has no `etimes`, so we parse the formatted `etime` field instead — locale-free."""
    s = (s or "").strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return None
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
    elif len(parts) == 2:
        h, m, sec = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, sec = 0, 0, parts[0]
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + sec


def process_start_s(pid: int, now_s: Optional[float] = None) -> Optional[float]:
    """Epoch start time of a process, derived from `ps -o etime` (elapsed time)."""
    now_s = now_s if now_s is not None else _now_s()
    try:
        r = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    elapsed = parse_etime(out)
    return None if elapsed is None else now_s - elapsed


def is_stale(script_mtime_s: float, proc_start_s: float, grace_s: int = GRACE_S) -> bool:
    """True if on-disk script is newer than the running process by more than grace."""
    return (script_mtime_s - proc_start_s) > grace_s


def check_daemon(info: dict, now_s: Optional[float] = None,
                 pid_fn=launchctl_pid, start_fn=process_start_s,
                 mtime_fn=None) -> Optional[dict]:
    """Evaluate one daemon. Returns a stale-report dict if (and only if) it is
    running stale code, else None (not loaded, no script on disk, or up to date)."""
    now_s = now_s if now_s is not None else _now_s()
    mtime_fn = mtime_fn or os.path.getmtime
    pid = pid_fn(info["label"])
    if not pid:
        return None  # not currently running -> nothing to be stale
    try:
        mtime = mtime_fn(info["script"])
    except OSError:
        return None  # script path in plist doesn't exist on disk -> can't judge
    started = start_fn(pid, now_s) if _accepts_two(start_fn) else start_fn(pid)
    if started is None:
        return None
    if not is_stale(mtime, started, GRACE_S):
        return None
    return {
        "label": info["label"],
        "script": info["script"],
        "pid": pid,
        "script_mtime_s": mtime,
        "proc_start_s": started,
        "newer_by_s": round(mtime - started, 1),
        "newer_by_h": round((mtime - started) / 3600.0, 2),
    }


def _accepts_two(fn) -> bool:
    """process_start_s takes (pid, now_s); test doubles may take (pid) only."""
    try:
        import inspect
        return len(inspect.signature(fn).parameters) >= 2
    except (TypeError, ValueError):
        return True


def _notify_stale(rep: dict) -> None:
    try:
        from nova_notify import notify
    except Exception:
        def notify(*a, **k):
            return False
    notify(
        f"Daemon '{rep['label']}' is running STALE code",
        body=(f"On-disk {os.path.basename(rep['script'])} is {rep['newer_by_h']}h newer "
              f"than the running process (pid {rep['pid']}). Restart to pick up the edit: "
              f"launchctl kickstart -k gui/$(id -u)/{rep['label']}"),
        level="warning", category="stale-code", source="nova_daemon_staleness",
        dedup_key=f"stale-code:{rep['label']}", meta={"dedup_window_s": 21600})


def ensure_session(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (SESSION_ID,))
        conn.commit()
    except Exception:
        conn.rollback()


def _log_action(conn, checked: int, stale: list) -> None:
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_act")
        try:
            cur.execute(
                """INSERT INTO claude_actions (session_id, action_type, target, description, outcome)
                   VALUES (%s,'staleness-check','launchd',%s,%s)""",
                (SESSION_ID,
                 f"checked {checked} nova daemon(s); {len(stale)} running stale code: "
                 + ",".join(r["label"] for r in stale[:20]),
                 f"stale={len(stale)}"))
            cur.execute("RELEASE SAVEPOINT sp_act")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_act")
    conn.commit()


def run_once(conn=None) -> list:
    """Sweep all managed daemons, notify (report-only) the stale ones, log. Returns
    the list of stale reports. `conn` optional so the sweep works without a DB."""
    daemons = discover_daemons()
    stale = []
    for info in daemons:
        rep = check_daemon(info)
        if rep:
            stale.append(rep)
            _notify_stale(rep)
    if conn is not None:
        ensure_session(conn)
        _log_action(conn, len(daemons), stale)
    return stale


def main() -> int:
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(DSN)
    except Exception:
        conn = None  # DB optional; the check + notify still work
    try:
        stale = run_once(conn)
    except Exception as e:
        print(f"daemon_staleness: sweep failed: {e}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    if not stale:
        print("daemon_staleness: all daemons running current code")
        return 0
    print(f"daemon_staleness: {len(stale)} daemon(s) running STALE code:")
    for r in stale:
        print(f"  {r['label']:45} on-disk {r['newer_by_h']}h newer than pid {r['pid']}")
    return 0


DSN = "host=localhost dbname=nova_ops user=kochj"

if __name__ == "__main__":
    sys.exit(main())
