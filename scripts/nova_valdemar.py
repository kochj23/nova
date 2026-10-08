#!/usr/bin/env python3
"""nova_valdemar.py — VALDEMAR REGISTER: everything Nova holds artificially alive.

From Poe's "The Facts in the Case of M. Valdemar": the narrator mesmerises his dying friend
in articulo mortis and holds him there for nearly seven months. Asked how he feels, the
tongue answers "I have been sleeping—and now—now—I am dead." When the trance is finally
broken, the body rots away under the narrator's hands in less than a minute. The hold was
never free: it only postponed, and the longer it ran, the worse the release. Nova's version:
every workaround she is holding in suspension (a .bak fallback, a disabled-but-installed
launchd job, a learned-normal alert baseline that quiets a signature, a model pinned in
memory with keep_alive forever) is entered in one register with its age, so the old ones are
looked at before they become Valdemar.

Minimal first version (Studio only):
  * bak            `*.bak*` files in scripts/, config/, LaunchAgents, LaunchDaemons (age = mtime)
  * disabled_plist launchd labels marked disabled whose plist is still installed, plus
                   `*.plist.disabled*` files and LaunchAgents/_disabled/* (age = mtime)
  * suppression    learned_baselines rows (signatures triage treats as known-normal; age = created_at)
  * ollama_pin     models in Ollama /api/ps whose expiry is more than a year out (keep_alive -1;
                   age = first time this register saw it)
`--run` upserts one row per hold and marks holds no longer found as released. `--oldest`
prints the ten oldest live holds and files them as one claude_queue item per month.

CLI:     --run [--dry-run]   --oldest [--dry-run]   --selftest
Tables:  valdemar_holds. No service_config keys.
Schedule: weekly Wednesday 04:15 (`--run`); monthly on the 1st 04:20 (`--oldest`).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

HOST = "studio"
OPENCLAW = Path.home() / ".openclaw"
AGENTS = Path.home() / "Library/LaunchAgents"
DAEMONS = Path("/Library/LaunchDaemons")
BAK_DIRS = (OPENCLAW / "scripts", OPENCLAW / "config", AGENTS, DAEMONS)
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"
PIN_HORIZON = timedelta(days=365)
QUEUE_SESSION = "nova-valdemar"
KINDS = ("bak", "disabled_plist", "suppression", "ollama_pin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS valdemar_holds (
  id serial PRIMARY KEY, host text NOT NULL, kind text NOT NULL, name text NOT NULL,
  held_since timestamptz, first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(), released_at timestamptz, detail jsonb,
  UNIQUE (host, kind, name));
"""


def log(m: str) -> None:
    print(f"[valdemar {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


def _mtime(p: Path):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def hold(kind, name, since, **detail) -> dict:
    return {"kind": kind, "name": name, "held_since": since, "detail": detail}


# ── discovery ───────────────────────────────────────────────────────────────

def baks(dirs=BAK_DIRS) -> list:
    return [hold("bak", str(p), _mtime(p)) for d in dirs if d.is_dir() for p in sorted(d.glob("*.bak*"))]


def disabled_labels(text: str) -> set:
    """Labels marked disabled in `launchctl print-disabled` output."""
    return set(re.findall(r'"([^"]+)"\s*=>\s*(?:disabled|true)', text))


def _print_disabled(domain: str) -> str:
    try:
        return subprocess.run(["launchctl", "print-disabled", domain], capture_output=True,
                              text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log(f"launchctl print-disabled {domain} failed: {e}")
        return ""


def disabled_plists(agents=AGENTS, daemons=DAEMONS, gui_text=None, sys_text=None) -> list:
    out = []
    for d, text in ((agents, gui_text if gui_text is not None else _print_disabled(f"gui/{os.getuid()}")),
                    (daemons, sys_text if sys_text is not None else _print_disabled("system"))):
        for label in sorted(disabled_labels(text)):
            p = d / f"{label}.plist"
            if p.is_file():
                out.append(hold("disabled_plist", str(p), _mtime(p), how="launchctl disabled, still installed"))
        if d.is_dir():
            out += [hold("disabled_plist", str(p), _mtime(p), how="renamed .disabled")
                    for p in sorted(d.glob("*.plist.disabled*"))]
            out += [hold("disabled_plist", str(p), _mtime(p), how="moved to _disabled")
                    for p in sorted((d / "_disabled").glob("*")) if p.is_file()]
    return out


def suppressions(cur) -> list:
    return [hold("suppression", f"learned_baselines:{sig}", created, source=src)
            for sig, src, created in _q(cur, "SELECT signature, source, created_at FROM learned_baselines")]


def _fetch_ps() -> list:
    with urllib.request.urlopen(OLLAMA_PS, timeout=10) as r:
        return json.load(r).get("models") or []


def pinned(models: list, now: datetime) -> list:
    """Models whose expiry is beyond PIN_HORIZON (keep_alive -1 shows as a date centuries out)."""
    out = []
    for m in models:
        try:
            exp = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", m.get("expires_at") or ""))
        except ValueError:
            continue
        if exp - now > PIN_HORIZON:
            out.append(hold("ollama_pin", m["name"], None, expires_at=m["expires_at"], size_vram=m.get("size_vram")))
    return out


def ollama_pins(_sleep=None) -> list | None:
    """None when Ollama could not be read (the kind is then skipped, never marked released)."""
    models = W.retry(_fetch_ps, attempts=3, delay=2.0, tag="valdemar", _sleep=_sleep)
    return None if models is False else pinned(models, datetime.now(timezone.utc))


def discover(cur) -> tuple:
    """(holds, kinds actually scanned)."""
    holds = baks() + disabled_plists() + suppressions(cur)
    pins = ollama_pins()
    if pins is None:
        log("ollama unreachable; pins skipped this run")
    return holds + (pins or []), [k for k in KINDS if k != "ollama_pin" or pins is not None]


# ── register ────────────────────────────────────────────────────────────────

def age_days(h: dict, now: datetime) -> float:
    since = h.get("held_since") or h.get("first_seen") or now
    return (now - since).total_seconds() / 86400


def oldest(holds: list, now: datetime, n: int = 10) -> list:
    return sorted(holds, key=lambda h: -age_days(h, now))[:n]


def write(cur, holds: list, kinds: list, ts) -> int:
    ensure_schema(cur)
    for h in holds:
        cur.execute("INSERT INTO valdemar_holds (host, kind, name, held_since, first_seen, last_seen, detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (host, kind, name) DO UPDATE SET "
                    "last_seen=EXCLUDED.last_seen, held_since=EXCLUDED.held_since, detail=EXCLUDED.detail, "
                    "released_at=NULL",
                    (HOST, h["kind"], h["name"], h["held_since"], ts, ts, json.dumps(h["detail"], default=str)))
    cur.execute("UPDATE valdemar_holds SET released_at=%s WHERE host=%s AND kind = ANY(%s) AND last_seen < %s "
                "AND released_at IS NULL", (ts, HOST, list(kinds), ts))
    return cur.rowcount


def live_holds(cur) -> list | None:
    """Live holds from the table, or None when it does not exist yet."""
    exists = _q(cur, "SELECT to_regclass('valdemar_holds')")
    if not exists or exists[0][0] is None:
        return None
    return [{"kind": r[0], "name": r[1], "held_since": r[2], "first_seen": r[3]}
            for r in _q(cur, "SELECT kind, name, held_since, first_seen FROM valdemar_holds "
                             "WHERE host=%s AND released_at IS NULL", (HOST,))]


def run(dry: bool = False) -> list:
    ts = datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        holds, kinds = discover(cur)
        counts = {k: sum(h["kind"] == k for h in holds) for k in kinds}
        log(f"{'DRY RUN ' if dry else ''}{len(holds)} holds {counts}")
        for h in oldest(holds, ts, len(holds)) if dry else []:
            print(f"  {age_days(h, ts):7.1f}d  {h['kind']:<14} {h['name']}")
        if not dry:
            log(f"registered {len(holds)}; {write(cur, holds, kinds, ts)} released since last run")
        return holds
    finally:
        conn.close()


def seven_months(dry: bool = False) -> list:
    """The ten oldest live holds; filed once per month to claude_queue."""
    now = datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        holds = live_holds(cur)
        if holds is None:   # register not built yet (dry run before the first --run)
            holds = discover(cur)[0]
        top = oldest(holds, now)
        lines = [f"{age_days(h, now):.0f}d  {h['kind']}  {h['name']}" for h in top]
        print("\n".join(lines) or "no holds")
        if not dry and top:
            desc = f"Valdemar Register: the ten oldest holds, {now:%Y-%m}"
            cur.execute("SELECT 1 FROM claude_queue WHERE description=%s LIMIT 1", (desc,))
            if not cur.fetchone():
                cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                            "VALUES (%s,'pending',4,%s,%s)",
                            (QUEUE_SESSION, desc, "Renew each with fresh evidence or release it gradually:\n"
                             + "\n".join(lines)))
                log("filed to claude_queue")
        return top
    finally:
        conn.close()


def selftest() -> int:
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert disabled_labels('"com.a" => disabled\n"com.b" => enabled\n"com.c" => true') == {"com.a", "com.c"}
    p = pinned([{"name": "nova:latest", "expires_at": "2319-01-18T15:02:06.752196807-08:00"},
                {"name": "qwen3:8b", "expires_at": "2026-10-08T16:35:42.762326-07:00"},
                {"name": "bad", "expires_at": None}], now)
    assert [h["name"] for h in p] == ["nova:latest"], p
    hs = [hold("bak", "a", now - timedelta(days=200)), hold("bak", "b", now - timedelta(days=2)),
          {"kind": "ollama_pin", "name": "c", "held_since": None, "first_seen": now - timedelta(days=30)}]
    assert [h["name"] for h in oldest(hs, now, 2)] == ["a", "c"]
    assert round(age_days(hs[0], now)) == 200 and age_days(hold("x", "y", None), now) == 0
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="discover holds and update the register")
    ap.add_argument("--oldest", action="store_true", help="the ten oldest live holds (files them monthly)")
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.oldest:
        seven_months(dry=a.dry_run)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
