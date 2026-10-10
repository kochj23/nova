#!/usr/bin/env python3
"""nova_earth_boxes.py — THE EARTH-BOX COUNT: retirement has to stick.

From Stoker's Dracula: the Count can rest only in his own earth, and he has shipped fifty
boxes of it to London. The hunters trace every box from the carriers' waybills and sterilise
each one by daylight, counting down, until he flees England with a single box left. Poe adds
the bell rope: the narrator of "The Premature Burial" has his family vault made to open from
within and a bell hung over the tomb, its rope run through a hole in the coffin to the
corpse's hand (checked against the Gutenberg text). Usher adds the rule that a sign of life
from the tomb is never kept quiet. Nova's version: when something is retired she lists every
place it could rise from, counts those boxes down to zero, and listens for any actor that
keeps acting on the buried thing (Matheson's Captain Ross, re-running his last plan).

Minimal first version:
  * burials: hand-seeded with the four subagents retired 2026-10-08 (lookout, analyst,
    librarian, coder); `--bury NAME` adds more. Each burial carries an epoch from a PG
    sequence (the future Ross gate's fencing token) and the content hash of its script.
  * box sweep: every launchd plist (file name and body) and the user crontab on the Studio,
    scheduler.yaml, Big Brother's SUBAGENTS restart list, and on nova-core scheduler-core.yaml,
    the crontab and the systemd unit list. Each place that still names a burial is one box.
  * Ross loop: Big Brother "Subagent X stale -> Restarted" lines in nova.jsonl, per buried
    name per day, since the burial.
Each remaining box and each day with restarts is filed to claude_queue (deduplicated).

MERGED into nova_yellow_eye.py on 2026-10-09 (merge M5) as `--burials` / `--bury`: this module
keeps the logic, table, sequence and queue session; its CLI is a thin wrapper that forwards there.

CLI:     --run [--dry-run]   --bury NAME [--kind K] [--host H] [--by WHO]   --selftest
         (= nova_yellow_eye.py --burials [--dry-run] / --bury NAME ...)
Tables:  earth_box_burials (+ sequence earth_box_epoch). No service_config keys.
Schedule: daily 05:20 (`nova_earth_boxes.py --run`, or `nova_yellow_eye.py --burials`).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import nova_watch_common as W  # noqa: E402

OPENCLAW = Path.home() / ".openclaw"
PLIST_DIRS = (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchDaemons"))
SCHEDULER_YAML = OPENCLAW / "config/scheduler.yaml"
BIG_BROTHER = SCRIPTS / "nova_big_brother.py"
NOVA_LOGS = (OPENCLAW / "logs/nova.jsonl", OPENCLAW / "logs/nova.jsonl.1")
NOVA_CORE = "nova-core.digitalnoise.net"
REMOTE_CMD = ("echo '### scheduler-core.yaml'; cat ~/.openclaw/config/scheduler-core.yaml; "
              "echo '### crontab'; crontab -l 2>/dev/null; "
              "echo '### systemd'; systemctl list-unit-files --no-pager --plain 2>/dev/null; "
              "systemctl --user list-unit-files --no-pager --plain 2>/dev/null; true")
QUEUE_SESSION = "nova-earth-boxes"
BOX_RESURFACE_DAYS = 7
SEED_HOST = "studio"
SEED = [(n, "subagent", "2026-10-08T00:00:00-07:00", "claude (Big Brother fix)")
        for n in ("lookout", "analyst", "librarian", "coder")]
RESTART_RX = re.compile(r"(?i)subagent (\w[\w-]*) stale\W+restarted|restarted subagent (\w[\w-]*)")

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS earth_box_epoch;
CREATE TABLE IF NOT EXISTS earth_box_burials (
  id serial PRIMARY KEY, name text NOT NULL, host text NOT NULL, kind text NOT NULL,
  content_hash text, retired_at timestamptz NOT NULL DEFAULT now(),
  epoch bigint NOT NULL DEFAULT nextval('earth_box_epoch'), retired_by text,
  UNIQUE (name, host, kind));
"""


def log(m: str) -> None:
    print(f"[earth-boxes {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


def script_hash(name: str) -> str | None:
    p = SCRIPTS / f"nova_agent_{name}.py"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


# ── burials ─────────────────────────────────────────────────────────────────

def bury(cur, name, kind, host, retired_at=None, by=None) -> None:
    cur.execute("INSERT INTO earth_box_burials (name, host, kind, content_hash, retired_at, retired_by) "
                "VALUES (%s,%s,%s,%s,coalesce(%s::timestamptz, now()),%s) "
                "ON CONFLICT (name, host, kind) DO NOTHING",
                (name, host, kind, script_hash(name) if kind == "subagent" else None, retired_at, by))


def burials(cur) -> list:
    """Every burial; the hand seed when the table does not exist yet (a dry run before the first run)."""
    exists = _q(cur, "SELECT to_regclass('earth_box_burials')")
    if not exists or exists[0][0] is None:
        return [{"name": n, "host": SEED_HOST, "kind": k, "retired_at": datetime.fromisoformat(t), "epoch": None}
                for n, k, t, _ in SEED]
    return [{"name": r[0], "host": r[1], "kind": r[2], "retired_at": r[3], "epoch": r[4]}
            for r in _q(cur, "SELECT name, host, kind, retired_at, epoch FROM earth_box_burials ORDER BY id")]


# ── footholds (each source is (host, place, text)) ──────────────────────────

def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _crontab() -> str:
    try:
        return subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log(f"crontab -l failed: {e}")
        return ""


def restart_list(src: str) -> str:
    """Big Brother's SUBAGENTS = [...] contents (its restart list), or ''."""
    m = re.search(r"^SUBAGENTS\s*=\s*\[([^\]]*)\]", src, re.M)
    return m.group(1) if m else ""


def local_sources(plist_dirs=PLIST_DIRS, read=None, crontab=None) -> list:
    """`read`/`crontab` let nova_yellow_eye's shared Scan supply files it has already read."""
    read, crontab = read or _read, crontab or _crontab
    out = [(SEED_HOST, str(p), p.name + "\n" + read(p))
           for d in plist_dirs if d.is_dir() for p in sorted(d.glob("*.plist"))]
    out += [(SEED_HOST, "crontab", crontab()), (SEED_HOST, str(SCHEDULER_YAML), read(SCHEDULER_YAML)),
            (SEED_HOST, "big_brother SUBAGENTS", restart_list(read(BIG_BROTHER)))]
    return out


def _ssh(host: str, cmd: str) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, cmd],
                       capture_output=True, text=True, timeout=40)
    return r.stdout if r.returncode == 0 else ""


def split_sections(host: str, text: str) -> list:
    """'### name' markers -> [(host, name, body)]."""
    parts = re.split(r"^### (.+)$", text, flags=re.M)
    return [(host, parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def remote_sources(_sleep=None) -> list | None:
    """nova-core's scheduler-core.yaml, crontab and unit list; None when unreachable (skipped, said so)."""
    text = W.retry(_ssh, NOVA_CORE, REMOTE_CMD, attempts=3, delay=3.0, tag="earth-boxes", _sleep=_sleep)
    return split_sections("nova-core", text) if text else None


def name_rx(name: str):
    n = re.escape(name)
    return re.compile(rf"(?:nova_agent_|agent-){n}\b|[\"']{n}[\"']")


def boxes(buried: list, sources: list) -> list:
    """One box per (burial, host, place) whose text still names the burial, with the first line."""
    out = []
    for b in buried:
        rx = name_rx(b["name"])
        for host, place, text in sources:
            line = next((ln.strip() for ln in text.splitlines() if rx.search(ln)), None)
            if line is not None:
                out.append({"name": b["name"], "kind": b["kind"], "host": host, "place": place,
                            "line": line[:200]})
    return out


# ── Ross loop: Big Brother restarts of buried names ─────────────────────────

def restarts(buried: list, files=NOVA_LOGS) -> dict:
    """{(name, 'YYYY-MM-DD'): count} of Big Brother restart lines after each burial."""
    since = {b["name"].lower(): b["retired_at"] for b in buried}
    out: dict = {}
    for p in files:
        try:
            f = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if "big-brother" not in line or "estart" not in line:
                    continue
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"])
                except Exception:  # noqa: BLE001 — torn or foreign line
                    continue
                m = RESTART_RX.search(e.get("msg") or "")
                if e.get("source") != "big-brother" or not m:
                    continue
                name = (m.group(1) or m.group(2)).lower()
                if name in since and ts >= since[name]:
                    key = (name, ts.date().isoformat())
                    out[key] = out.get(key, 0) + 1
    return out


# ── filing ──────────────────────────────────────────────────────────────────

def file_item(cur, desc: str, context: str, days: int | None) -> int | None:
    """One claude_queue row per description (per `days` window, or ever). Returns the new id or None."""
    if days is None:
        cur.execute("SELECT id FROM claude_queue WHERE description=%s LIMIT 1", (desc,))
    else:
        cur.execute("SELECT id FROM claude_queue WHERE description=%s AND created_at > now() - make_interval(days => %s) "
                    "LIMIT 1", (desc, days))
    if cur.fetchone():
        return None
    # claude_queue.session_id is a foreign key: register the organ's session first (failed 2026-10-09 05:20).
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "VALUES (%s,'pending',3,%s,%s) RETURNING id", (QUEUE_SESSION, desc, context[:3000]))
    return cur.fetchone()[0]


def findings(bx: list, rs: dict) -> list:
    """(description, context, dedup_days) for each box and each Ross-loop day."""
    out = [(f"Earth-Box Count: buried {b['kind']} '{b['name']}' still named on {b['host']} in {b['place']}",
            f"Line: {b['line']}\nRemove the reference (or record an exhumation) so the retirement sticks.",
            BOX_RESURFACE_DAYS) for b in bx]
    out += [(f"Earth-Box Count: Big Brother restarted buried subagent '{n}' on {d}",
             f"{c} 'Restarted' lines in nova.jsonl for {n} on {d} (UTC) after its burial. Ross loop: "
             "Big Brother keeps acting on a retired target; check the running daemon's restart list.",
             None) for (n, d), c in sorted(rs.items())]
    return out


def run(dry: bool = False, sources: list | None = None) -> dict:
    """The count. `sources` = local_sources() already read (nova_yellow_eye's Scan)."""
    conn = W.connect()
    try:
        cur = conn.cursor()
        if not dry:
            ensure_schema(cur)
            for n, k, t, by in SEED:
                bury(cur, n, k, SEED_HOST, t, by)
        buried = burials(cur)
        sources = local_sources() if sources is None else sources
        remote = remote_sources()
        if remote is None:
            log(f"{NOVA_CORE} unreachable; its boxes not counted this run")
        bx = boxes(buried, sources + (remote or []))
        rs = restarts(buried)
        log(f"{'DRY RUN ' if dry else ''}{len(buried)} burials; {len(sources) + len(remote or [])} places "
            f"searched; {len(bx)} boxes remain; {sum(rs.values())} Big Brother restarts of buried names")
        for b in buried:
            left = [x for x in bx if x["name"] == b["name"]]
            days = {d: c for (n, d), c in rs.items() if n == b["name"].lower()}
            print(f"  {b['name']:<12} epoch={b['epoch'] or '-':<4} boxes={len(left)} restarts={days or 0}")
            for x in left:
                print(f"      box: {x['host']} {x['place']}: {x['line'][:100]}")
        filed = [] if dry else [i for d, c, k in findings(bx, rs) if (i := file_item(cur, d, c, k))]
        if not dry:
            log(f"filed {len(filed)} claude_queue item(s)")
        return {"burials": buried, "boxes": bx, "restarts": rs, "filed": filed}
    finally:
        conn.close()


def selftest() -> int:
    b = [{"name": "coder", "kind": "subagent", "retired_at": datetime.fromisoformat("2026-10-08T00:00:00+00:00")}]
    src = [("studio", "a.plist", "<string>com.nova.agent-coder</string>"),
           ("studio", "y", "model: qwen-coder\n  coder-ish"),
           ("studio", "bb", restart_list('x\nSUBAGENTS = ["sentinel", "coder"]\n')),
           ("studio", "bb2", restart_list('SUBAGENTS = ["sentinel"]'))]
    got = boxes(b, src)
    assert [g["place"] for g in got] == ["a.plist", "bb"], got
    assert split_sections("h", "### a\nx\n### b\ny\n") == [("h", "a", "\nx\n"), ("h", "b", "\ny\n")]
    m = RESTART_RX.search("[warning] Subagent coder stale → Restarted via subagent_ctl.sh")
    assert m and m.group(1) == "coder"
    assert RESTART_RX.search("Restarted subagent lookout").group(2) == "lookout"
    assert not RESTART_RX.search("Suppressed (escalation tier): Subagent coder stale/missing")
    f = findings(got, {("coder", "2026-10-08"): 5})
    assert len(f) == 3 and f[-1][2] is None and "5 'Restarted'" in f[-1][1]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    """Thin wrapper (merge M5): the run lives in nova_yellow_eye --burials / --bury."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="= nova_yellow_eye.py --burials")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the count, write nothing")
    ap.add_argument("--bury", metavar="NAME", help="= nova_yellow_eye.py --bury NAME")
    ap.add_argument("--kind", default="subagent")
    ap.add_argument("--host", default=SEED_HOST)
    ap.add_argument("--by", default="jordan")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.bury:
        fwd = ["--bury", a.bury, "--kind", a.kind, "--host", a.host, "--by", a.by]
    elif a.run:
        fwd = ["--burials"] + (["--dry-run"] if a.dry_run else [])
    else:
        ap.print_help()
        return 0
    log(f"merged into nova_yellow_eye on 2026-10-09: running nova_yellow_eye {' '.join(fwd)}")
    import nova_yellow_eye
    return nova_yellow_eye.main(fwd)


if __name__ == "__main__":
    sys.modules.setdefault("nova_earth_boxes", sys.modules[__name__])   # one module object
    sys.exit(main())
