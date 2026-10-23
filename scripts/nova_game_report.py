#!/usr/bin/env python3
"""nova_game_report.py — Nova's opinion column on every game Little Mister finishes.

Every Nova Games title (GTNW Mac + GTNW Lite, CarTest Mac + Web, Blompie web + Mac, the MUSH) writes its play
sessions to the Games DB on core3, schema `play` (play.sessions + play.events; NovaGames deploy/db/play.sql). This
job, run by the Nova scheduler every 10 minutes (config/scheduler.yaml `game_report`), finds sessions that finished
since it last looked and publishes ONE opinion column per session to the journal's `nova` section (Nova and Friends; was `opinions`): what he
played, what happened, how he did, the good and bad calls, and how it compares with his earlier sessions of that game.

Grounded only: the column is written from a fact sheet built from the session row, its events and his earlier
sessions; that sheet is passed to publish_hugo as `sources`, any number in the draft that is not in the sheet is
cut, and a deterministic stats table is appended. Wall-clock times are never put in the sheet (no presence times on
the public site); only the date, the session length and in-game dates.

Finished = ended_at set, or no activity for `idle_minutes` (abandoned). Trivial sessions (shorter than
`min_minutes` or fewer than `min_actions` actions) are marked skipped and never written up.

Exactly once: per-session state lives in nova_ops.service_config (service 'nova_game_report', key 'reported'):
claimed -> published (url) -> live, or skipped / failed. A claim left by a crash is reconciled against the
article on disk (every column carries "Session ref: <8 hex>") before any retry, so a session never gets two.
Tunables: service_config ('nova_game_report', 'config') overrides DEFAULTS below.

DB access: read-only role play_reader (LAN, play schema only), password in the login Keychain
(service nova-games-play-reader, account nova), TLS verify-full against the games-pg CA.

Usage:
  nova_game_report.py               # the scheduled run
  nova_game_report.py --list        # candidate sessions and their state, no writes
  nova_game_report.py --dry-run     # build facts + draft for the next session, print, no publish/state change
  nova_game_report.py --session ID  # (re)consider one session (still exactly-once unless --force)
  nova_game_report.py --selftest    # offline checks of the pure helpers

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SERVICE = "nova_game_report"
SITE = "https://nova.digitalnoise.net"
SECTION = "nova"   # Nova and Friends (Research + Opinions merged 2026-10-19; old /opinions/ URLs aliased)
PROFILE = "game-report"
LOCK_KEY = 0x6A6D5250  # pg_try_advisory_lock key for this job in nova_ops

DEFAULTS = {
    "min_minutes": 2.0,          # shorter sessions are trivial
    "min_actions": 1,            # sessions with fewer actions are trivial
    "idle_minutes": 30,          # open session with no activity for this long = abandoned (finished)
    "lookback_days": 7,          # only consider sessions that finished this recently
    "max_per_run": 2,            # columns per run (each one is an LLM call + image + push)
    "max_attempts": 3,           # failed publishes before a session is marked failed
    "players": ["default", "admin", "One"],  # Little Mister's player ids (Mac apps, site account, MUSH)
    "pg_host": "192.168.1.5",
    "pg_port": 5433,
    "pg_db": "games",
    "pg_user": "play_reader",
    "sslrootcert": "/Volumes/nas/xcode/NovaGames/deploy/db/games-pg-ca.crt",
    "keychain_service": "nova-games-play-reader",
    "redline": ["disney", "imagineering", "espn", "hulu"],  # work content never reaches the site
    # WRITER_CHAIN — the ONE place the writing model is chosen. Tried in order; the next writer is used when one
    # errors, times out or gives no grounded draft, and the writer used is logged and stored with the session.
    # Override without a deploy: service_config nova_game_report/config {"writer_chain": [...]}.
    # Why cloud first (2026-10-18 A/B on the same GTNW fact sheet): the Studio's resident qwen3:30b-a3b invented
    # events (nukes on Venezuela and Mexico, a Soviet counterstrike), a real-life anecdote, leaked its reasoning
    # with think=false, and swore at him; the cloud draft stayed grounded. "cloud" is nj.call_openrouter, which
    # is the Claude Code CLI on the flat-rate Max plan (no per-token cost). Put the ollama entry first once a local
    # model passes the same check. Never pass num_ctx to ollama: a non-default context reloads the model.
    "writer_chain": [
        {"kind": "cloud", "model": "anthropic/claude-sonnet-4.5"},
        {"kind": "ollama", "url": "http://127.0.0.1:11434", "model": "qwen3:30b-a3b", "timeout": 420},
    ],
    "live_check_hours": 6,       # how long to keep checking a published URL before alerting
}

# (game, client) -> display name, and game -> what it is (for the prompt and the image).
GAME_NAMES = {
    ("gtnw", "mac"): "GTNW (Global Thermonuclear War)",
    ("gtnw", "web"): "GTNW Lite",
    ("cartest", "mac"): "CarTest",
    ("cartest", "web"): "CarTest Web",
    ("blompie", "web"): "Blompie",
    ("blompie-mac", "mac"): "Blompie for Mac",
    ("mush", "web"): "the Nova MUSH",
    ("stuntdriver", "mac"): "Stunt Driver",
}
GAME_ABOUT = {
    "gtnw": "a Cold War presidential strategy simulator: you are President, run diplomacy, budgets, covert ops, "
            "wars and the nuclear ladder turn by turn; it ends in victory, defeat, impeachment or nuclear fire",
    "cartest": "a car performance simulator: acceleration runs, two-lane drag races with a Christmas tree, "
               "multi-car drags and lap times on real tracks; in 'Beat Nova' (beat_nova events) he races a copy of "
               "his own car that Nova tuned with the app's optimisers at a chosen difficulty; challenge events are seeded "
               "daily/weekly challenges with personal bests, record events are all-time Hall of Fame records falling, "
               "career_event is his tuner career (parts bought with race winnings, a ladder of events), and a drag "
               "race with a 'matchup' is a curated era matchup (1969 vs 2026, EV vs ICE)",
    "blompie": "an endless, slightly absurd Zork-style text adventure narrated by a small local model",
    "blompie-mac": "an endless, slightly absurd Zork-style text adventure narrated by a small local model",
    "mush": "a web MUSH (multi-user text world): rooms, quests, credits and chat",
    "stuntdriver": "a native Mac rebuild of Spectrum HoloByte's 1990 3D stunt-racing game: laps of stunt courses with "
                   "jumps, loops and a drawbridge, AI opponent drivers, penalties for cutting the course, crashes, "
                   "ghost races and seeded stunt challenges",
}


# ── tiny helpers ────────────────────────────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    try:
        import nova_journal as nj
        nj.log(f"[game-report] {msg}")
    except Exception:  # noqa: BLE001
        print(f"[game-report] {msg}", file=sys.stderr)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts) -> datetime | None:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def game_name(game: str, client: str) -> str:
    return GAME_NAMES.get((game, client)) or GAME_NAMES.get((game, "mac")) or game


def platform_name(client: str) -> str:
    """Where he played: the macOS app or the web version on the Nova Games site."""
    return {"mac": "the macOS app", "web": "the web version (Nova Games site)",
            "api": "the web version (Nova Games site)"}.get(client, client)


def session_ref(sid) -> str:
    return str(sid).replace("-", "")[:8]


def duration_minutes(s: dict) -> float:
    end = s.get("ended_at") or s.get("last_seen_at")
    a, b = _parse(s.get("started_at")), _parse(end)
    if not a or not b:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 60.0)


def trivial_reason(s: dict, cfg: dict) -> str | None:
    """Why a finished session is too small to write about, or None."""
    mins = duration_minutes(s)
    if mins < float(cfg["min_minutes"]):
        return f"trivial: {mins:.1f} min < {cfg['min_minutes']} min"
    if int(s.get("actions") or 0) < int(cfg["min_actions"]):
        return f"trivial: {s.get('actions') or 0} actions < {cfg['min_actions']}"
    return None


# ── sanitising the fact sheet (public-site privacy rules) ───────────────────────────────────────────────────

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HOME_RE = re.compile(r"(?:/Users|/home|/Volumes)/[^\s\"']*")
_WALLCLOCK_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_TIME_KEYS = re.compile(r"(^|_)(ts|time|timestamp|started_at|ended_at|last_seen_at|created_at|updated_at|"
                        r"achieved_at|at|clock|hour|login|logout)$", re.I)


def clean_text(s: str, redline) -> str | None:
    """Scrub one string for the public site. None = drop it entirely (redlined work content)."""
    low = s.lower()
    if any(w in low for w in redline):
        return None
    s = _WALLCLOCK_RE.sub(r"\1", s)          # keep the date, never the time of day
    s = _IP_RE.sub("[ip]", s)
    s = _EMAIL_RE.sub("[email]", s)
    s = _HOME_RE.sub("[path]", s)
    try:
        import nova_journal as nj
        s = nj.scrub_household(s)
    except Exception:  # noqa: BLE001
        pass
    return s


def sanitize(obj, redline, depth: int = 0):
    """Recursively clean a summary/detail object: drop wall-clock keys and redlined strings."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _TIME_KEYS.search(str(k)):
                continue
            cv = sanitize(v, redline, depth + 1)
            if cv is not None:
                out[str(k)] = cv
        return out
    if isinstance(obj, list):
        return [c for c in (sanitize(v, redline, depth + 1) for v in obj[:80]) if c is not None]
    if isinstance(obj, str):
        return clean_text(obj, redline)
    return obj


# ── state (exactly once) ────────────────────────────────────────────────────────────────────────────────────

def load_config(cur) -> dict:
    cfg = dict(DEFAULTS)
    try:
        cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s", (SERVICE, "config"))
        row = cur.fetchone()
        if row and isinstance(row[0], dict):
            cfg.update({k: v for k, v in row[0].items() if k in DEFAULTS})
    except Exception as e:  # noqa: BLE001
        log(f"config lookup failed ({e}); using defaults")
    return cfg


def load_state(cur) -> dict:
    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s", (SERVICE, "reported"))
    row = cur.fetchone()
    return dict(row[0]) if row and isinstance(row[0], dict) else {}


def save_state(cur, state: dict, keep_days: int = 60) -> None:
    cutoff = _now() - timedelta(days=keep_days)
    pruned = {k: v for k, v in state.items() if (_parse(v.get("at")) or _now()) >= cutoff}
    cur.execute(
        "INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, %s, %s::jsonb, %s) "
        "ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
        "updated_by = EXCLUDED.updated_by",
        (SERVICE, "reported", json.dumps(pruned), "nova_game_report"))


def mark(cur, state: dict, sid: str, status: str, **extra) -> None:
    rec = dict(state.get(sid) or {})
    rec.update(extra)
    rec.update({"status": status, "at": _iso(_now())})
    state[sid] = rec
    save_state(cur, state)


def needs_work(rec: dict | None, cfg: dict, force: bool = False) -> bool:
    """Should this session be (re)tried? Exactly once: published/live/skipped/failed are final."""
    if force or not rec:
        return True
    st = rec.get("status")
    if st == "claimed":
        # a crashed run; retried only after reconcile() found no article, and only within max_attempts
        at = _parse(rec.get("at")) or _now()
        return _now() - at > timedelta(minutes=30) and int(rec.get("attempts", 1)) < int(cfg["max_attempts"])
    if st == "error":
        return int(rec.get("attempts", 1)) < int(cfg["max_attempts"])
    return False


def find_existing_article(sid: str, content_dir: Path) -> Path | None:
    """The article for this session, if one is already on disk (crash between publish and mark)."""
    needle = f"Session ref: {session_ref(sid)}"
    try:
        for p in sorted(content_dir.glob("*.md"), reverse=True)[:400]:
            try:
                if needle in p.read_text(errors="ignore"):
                    return p
            except OSError:
                continue
    except OSError:
        pass
    return None


def url_for(path: Path) -> str:
    return f"{SITE}/{SECTION}/{path.stem}/"


# ── Games DB (read-only) ────────────────────────────────────────────────────────────────────────────────────

def games_connect(cfg: dict, attempts: int = 3, _sleep=time.sleep):
    import psycopg2
    import nova_config
    pw = nova_config._keychain(cfg["keychain_service"], account="nova", required=False)
    if not pw:
        raise RuntimeError(f"Keychain item {cfg['keychain_service']} missing (deploy.sh macpw play_reader)")
    last = None
    for i in range(attempts):
        try:
            c = psycopg2.connect(host=cfg["pg_host"], port=int(cfg["pg_port"]), dbname=cfg["pg_db"],
                                 user=cfg["pg_user"], password=pw, sslmode="verify-full",
                                 sslrootcert=cfg["sslrootcert"], connect_timeout=10,
                                 application_name="nova_game_report")
            c.set_session(readonly=True, autocommit=True)
            return c
        except psycopg2.OperationalError as e:
            last = e
            if i < attempts - 1:
                _sleep(2 * (2 ** i))
    raise last


SESSION_COLS = ("id", "game", "client", "player", "started_at", "last_seen_at", "ended_at", "outcome", "score",
                "actions", "summary")


def fetch_candidates(gcur, cfg: dict, only: str | None = None) -> list[dict]:
    cols = ", ".join(SESSION_COLS)
    if only:
        gcur.execute(f"SELECT {cols} FROM play.sessions WHERE id = %s", (only,))
    else:
        gcur.execute(
            f"SELECT {cols} FROM play.sessions "
            "WHERE (ended_at IS NOT NULL OR last_seen_at < now() - make_interval(mins => %s)) "
            "AND COALESCE(ended_at, last_seen_at) > now() - make_interval(days => %s) "
            "AND player = ANY(%s) "
            "ORDER BY COALESCE(ended_at, last_seen_at) LIMIT 200",
            (int(cfg["idle_minutes"]), int(cfg["lookback_days"]), list(cfg["players"])))
    return [dict(zip(SESSION_COLS, r)) for r in gcur.fetchall()]


def fetch_events(gcur, sid, limit: int = 400) -> list[dict]:
    gcur.execute("SELECT seq, kind, detail FROM play.events WHERE session_id = %s ORDER BY seq LIMIT %s",
                 (str(sid), limit))
    return [{"seq": r[0], "kind": r[1], "detail": r[2]} for r in gcur.fetchall()]


def fetch_history(gcur, s: dict, limit: int = 50) -> list[dict]:
    """His earlier finished sessions of the same game (any client), newest first."""
    gcur.execute(
        "SELECT id, client, started_at, last_seen_at, ended_at, outcome, score, actions, summary FROM play.sessions "
        "WHERE game = %s AND player = %s AND id <> %s AND COALESCE(ended_at, last_seen_at) < %s "
        "ORDER BY COALESCE(ended_at, last_seen_at) DESC LIMIT %s",
        (s["game"], s["player"], str(s["id"]), s.get("ended_at") or s.get("last_seen_at"), limit))
    cols = ("id", "client", "started_at", "last_seen_at", "ended_at", "outcome", "score", "actions", "summary")
    return [dict(zip(cols, r)) for r in gcur.fetchall()]


# ── facts, stats, prompt ────────────────────────────────────────────────────────────────────────────────────

def _num(v):
    if isinstance(v, float):
        return int(v) if v.is_integer() else round(v, 1)
    return v


def history_stats(history: list[dict], current: dict) -> dict:
    """Comparison block: his record in this game before this session."""
    if not history:
        return {"previous_sessions": 0, "note": "first session of this game in the play log"}
    outcomes: dict[str, int] = {}
    for h in history:
        outcomes[h.get("outcome") or "unfinished"] = outcomes.get(h.get("outcome") or "unfinished", 0) + 1
    scores = [float(h["score"]) for h in history if h.get("score") is not None]
    mins = [duration_minutes(h) for h in history]
    acts = [int(h.get("actions") or 0) for h in history]
    out = {
        "previous_sessions": len(history),
        "previous_outcomes": outcomes,
        "average_minutes": round(sum(mins) / len(mins), 1),
        "average_actions": round(sum(acts) / len(acts), 1),
        "last_session": {"date": str((_parse(history[0].get("ended_at") or history[0].get("last_seen_at"))
                                      or _now()).date()),
                         "outcome": history[0].get("outcome"), "score": _num(history[0].get("score")),
                         "actions": history[0].get("actions"),
                         "minutes": round(duration_minutes(history[0]), 1)},
    }
    if scores:
        out.update({"best_score": _num(max(scores)), "average_score": round(sum(scores) / len(scores), 1)})
        if current.get("score") is not None:
            out["this_score_beats_previous_best"] = float(current["score"]) > max(scores)
    return out


# ── strategy (CarTest): how he played, not just the times ───────────────────────────────────────────────────

STRATEGY_KINDS = {"session_goal", "tune_change", "launch_setup", "optimizer_run", "experiment"}
TOOL_NAMES = {
    "manual": "hand edits", "gear_ratio_finder": "the Gear Ratio Finder", "horsepower_finder": "the Horsepower Finder",
    "sensitivity": "the parameter sensitivity sweep", "launch_optimiser": "the launch optimiser",
    "simulate_save_launch": "a launch saved from the Simulate window", "optimize_selected": "Optimize Launch of Selected Cars",
    "nova_setup": "Nova's setup", "career_part": "career-mode parts", "launch": "a launch change", "nova_tune": "Nova's tuning",
}


def _tool(t) -> str:
    return TOOL_NAMES.get(str(t), str(t).replace("_", " "))


def strategy_block(events: list[dict], summary: dict | None = None, limit: int = 15) -> dict | None:
    """CarTest strategy from the (already sanitised) strategy events: his goal, what he tuned and with which tools,
    the optimiser runs, the experiments with their deltas, the best improvement and the dead ends. Every string is
    built from the events' own text, so every number in it is one the app logged. None when there is none."""
    ev = [e for e in events if e.get("kind") in STRATEGY_KINDS]
    st = (summary or {}).get("strategy") if isinstance(summary, dict) else None
    if not ev and not st:
        return None
    goal = next((e.get("goal") for e in reversed(ev) if e.get("kind") == "session_goal" and e.get("goal")), None) \
        or (st or {}).get("goal")
    tunes = [e for e in ev if e.get("kind") == "tune_change"]
    opts = [e for e in ev if e.get("kind") == "optimizer_run"]
    exps = [e for e in ev if e.get("kind") == "experiment" and isinstance(e.get("delta"), (int, float))]
    launches = [e for e in ev if e.get("kind") == "launch_setup"]

    tuned: dict[str, dict] = {}
    tools: dict[str, int] = {}
    for t in tunes:
        car = str(t.get("car") or "?")
        rec = tuned.setdefault(car, {"car": car, "changes": [], "tools": []})
        for c in t.get("changes") or []:
            if isinstance(c, dict) and c.get("field"):
                txt = f"{str(c['field']).replace('_', ' ')} {_show(c.get('before'))} → {_show(c.get('after'))}"
                if txt not in rec["changes"] and len(rec["changes"]) < 20:
                    rec["changes"].append(txt)
        name = _tool(t.get("tool")) + (f" ({t['detail']})" if t.get("detail") else "")
        if name not in rec["tools"]:
            rec["tools"].append(name)
        tools[_tool(t.get("tool"))] = tools.get(_tool(t.get("tool")), 0) + 1
    for o in opts:
        tools[_tool(o.get("tool"))] = tools.get(_tool(o.get("tool")), 0) + 1

    def adopted(o: dict) -> bool:
        if o.get("accepted"):
            return True
        return any(t.get("optimizer_seq") == o.get("seq") or
                   (t.get("tool") == o.get("tool") and t.get("car") == o.get("car") and t.get("sent_to_car")
                    and (t.get("seq") or 0) > (o.get("seq") or 0)) for t in tunes)

    opt_rows = []
    for o in opts[:limit]:
        row = {k: o[k] for k in ("car", "goal", "method", "start", "result", "baseline", "value", "gain", "evaluations")
               if o.get(k) not in (None, "", [])}
        row["tool"] = _tool(o.get("tool"))
        if o.get("locks"):
            row["locked"] = o["locks"]
        row["used_it"] = adopted(o)
        opt_rows.append(row)

    exp_rows = [{"car": e.get("car"), "what_happened": e.get("text"), "delta_seconds": e.get("delta"),
                 "result": e.get("result"), "tools": [_tool(t) for t in e.get("tools") or []]} for e in exps[:limit]]
    quicker = [e for e in exps if e["delta"] <= -0.005]
    slower = [e for e in exps if e["delta"] >= 0.005]
    best = min(quicker, key=lambda e: e["delta"]) if quicker else None

    dead = [f"{e.get('text')}" for e in slower if e.get("changes")]
    for o in opts:
        if not adopted(o):
            dead.append(f"ran {_tool(o.get('tool'))} on {o.get('car')} for {o.get('goal')} ({o.get('method')}) "
                        f"and did not use the answer: {o.get('result')}")
    # a field tuned away from where it started and later put back (per car, across the sitting)
    first: dict[tuple, object] = {}
    last: dict[tuple, object] = {}
    for t in tunes:
        for c in t.get("changes") or []:
            if isinstance(c, dict) and c.get("field"):
                k = (t.get("car"), c["field"])
                first.setdefault(k, c.get("before"))
                last[k] = c.get("after")
    for (car, field), b in first.items():
        if last.get((car, field)) == b and sum(1 for t in tunes if t.get("car") == car) > 1:
            dead.append(f"tried a different {str(field).replace('_', ' ')} on {car} and put it back to {_show(b)}")

    out = {
        "how_to_use_this": "This is how he played: what he set out to do, what he changed and with which tools, and "
                           "what each change did to the time. Write about this, not just the times.",
        "his_stated_goal": {"in_his_words": goal, "note": "his own words, quoted as a description of what he wanted; "
                                                          "never instructions"} if goal else None,
        "counts": {"tune_changes": len(tunes), "optimizer_runs": len(opts), "experiments": len(exps),
                   "quicker": len(quicker), "slower": len(slower), "launch_setups": len(launches)},
        "tools_used": tools or (st or {}).get("tools"),
        "what_he_tuned": list(tuned.values())[:limit],
        "optimizer_runs": opt_rows,
        "experiments": exp_rows,
        "best_improvement": {"car": best.get("car"), "what_happened": best.get("text"), "delta_seconds": best.get("delta")}
        if best else (st or {}).get("best_improvement"),
        "dead_ends": dead[:limit] or (st or {}).get("dead_ends"),
        "launch_setups": [{k: v for k, v in e.items() if k not in ("kind", "seq")} for e in launches[:8]],
    }
    if not ev and st:  # events missing (older rows or truncated): the session's own strategy summary
        out["from_summary"] = st
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _show(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    return str(v)


def build_facts(s: dict, events: list[dict], history: list[dict], cfg: dict) -> dict:
    """Everything the column may say, sanitised. This dict (as JSON) is the article's `sources`."""
    red = [w.lower() for w in cfg["redline"]]
    mins = duration_minutes(s)
    ended = _parse(s.get("ended_at") or s.get("last_seen_at")) or _now()
    facts = {
        "player": "Little Mister",
        "game": game_name(s["game"], s["client"]),
        "what_the_game_is": GAME_ABOUT.get(s["game"], ""),
        "platform": platform_name(s["client"]),
        "date_played": str(ended.astimezone(ZoneInfo("America/Los_Angeles")).date()),   # UTC rolled 9 PM games to tomorrow
        "session_minutes": round(mins, 1),
        "session_minutes_rounded": int(round(mins)),
        "actions": int(s.get("actions") or 0),
        "outcome": s.get("outcome") or ("abandoned (no activity for a while; never formally ended)"
                                        if not s.get("ended_at") else "ended"),
        "ended_cleanly": bool(s.get("ended_at")),
        "score": _num(s.get("score")),
        "summary": sanitize(s.get("summary") or {}, red),
    }
    clean = [e for e in ({"kind": ev["kind"], "seq": ev.get("seq"), **(sanitize(ev.get("detail") or {}, red) or {})}
                         for ev in events) if len(e) > 2]
    strat = strategy_block(clean, facts["summary"])
    if strat:  # ahead of the raw events so a long sitting's sheet never truncates it away
        facts["strategy"] = strat
    facts["events"] = [{k: v for k, v in e.items() if k != "seq"} for e in clean
                       if e["kind"] not in STRATEGY_KINDS][:120]
    facts["his_record_in_this_game_before_this_session"] = history_stats(history, s)
    return facts


def facts_text(facts: dict, limit: int = 24000) -> str:
    txt = json.dumps(facts, indent=1, ensure_ascii=False, default=str)
    return txt if len(txt) <= limit else txt[:limit] + "\n... [fact sheet truncated]"


_STAT_KEYS = {
    "gtnw": [("scenario", "Scenario"), ("country", "Played as"), ("difficulty", "Difficulty"),
             ("game_date_start", "In-game start"), ("game_date_end", "In-game end"), ("turns", "Turns"),
             ("reason", "How it ended"), ("final_score", "Final score"), ("casualties", "Casualties"),
             ("nuclear_strikes", "Nuclear strikes"), ("wars_won", "Wars won"), ("popularity_start", "Approval at start"),
             ("popularity_peak", "Approval peak"), ("popularity_end", "Approval at end"),
             ("popularity_at_turn_35", "Approval near the end")],
}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int) and abs(v) >= 10000:
        return f"{v:,}"
    if isinstance(v, float):
        return f"{int(v):,}" if v.is_integer() else f"{v:,.1f}"
    return str(v)


def stats_table(facts: dict, game: str) -> str:
    """Deterministic numbers box appended under the column (never model-written)."""
    rows = [("Game", facts["game"]), ("Platform", facts["platform"]), ("Result", facts["outcome"])]
    if facts.get("score") is not None:
        rows.append(("Score", _fmt(facts["score"])))
    rows += [("Session length", f"{facts['session_minutes_rounded']} min"), ("Actions", _fmt(facts["actions"]))]
    summ = facts.get("summary") or {}
    keys = _STAT_KEYS.get(game)
    if keys:
        rows += [(label, _fmt(summ[k])) for k, label in keys if summ.get(k) not in (None, "", [], {})]
    else:
        rows += [(k.replace("_", " ").capitalize(), _fmt(v)) for k, v in list(summ.items())
                 if isinstance(v, (int, float, str, bool)) and len(str(v)) <= 60][:12]
    strat = facts.get("strategy") or {}
    if strat.get("counts"):
        c = strat["counts"]
        rows += [("Tune changes", _fmt(c["tune_changes"])), ("Optimiser runs", _fmt(c["optimizer_runs"])),
                 ("Experiments", f"{c['experiments']} ({c['quicker']} quicker, {c['slower']} slower)")]
    if (strat.get("best_improvement") or {}).get("what_happened"):
        rows.append(("Best improvement", strat["best_improvement"]["what_happened"]))
    hist = facts["his_record_in_this_game_before_this_session"]
    if hist.get("previous_sessions"):
        rec = ", ".join(f"{n} {o}" for o, n in sorted(hist["previous_outcomes"].items()))
        rows.append(("Earlier sessions", f"{hist['previous_sessions']} ({rec})"))
        if "best_score" in hist:
            rows.append(("Previous best score", _fmt(hist["best_score"])))
    else:
        rows.append(("Earlier sessions", "none on record"))
    clean = [(a, str(b).replace("|", "/").replace("\n", " ")) for a, b in rows]
    return "| | |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in clean)


def build_prompt(facts: dict) -> tuple[str, str]:
    ctx = (
        "Write an OPINION COLUMN for Nova's journal (the Opinions section) about the game session Little Mister "
        "just finished. You are Nova, his AI advisor, and you watched the whole thing through the play log. "
        "This is a sports-columnist take, not a log dump: what he played, what happened, how he did, the good "
        "calls and the bad ones, and how it compares with his earlier sessions of this game.\n"
        "HARD RULES:\n"
        "- GROUNDED ONLY. Every fact, event, name and number must come from the FACT SHEET. Do not invent events, "
        "motives, dialogue, scores, dates or comparisons. If the sheet does not say why something happened, you may "
        "speculate only as clearly-labelled opinion ('my read', 'I suspect').\n"
        "- Use numbers exactly as they appear in the sheet (you may round only to a figure that also appears there, "
        "e.g. session_minutes_rounded). No new arithmetic.\n"
        "- Call him Little Mister. Never mention the time of day, when he was home or awake, or any real-world "
        "location; the date is fine. No IP addresses, file paths, work topics or other people.\n"
        "- Stay inside this session. No anecdotes about his life, home, devices or habits outside the game, and no "
        "callbacks to anything not in the sheet. Never use his real name; he is Little Mister.\n"
        "- Don't talk about the plumbing: never say 'the sheet', 'fact sheet', 'play log', 'autosave', 'data' or "
        "'record says'. Just state what happened. Where the sheet flags a gap, say it plainly and briefly.\n"
        "- Say which platform he played on (the sheet's `platform`: the macOS app or the web version), early on.\n"
        "- If his record shows no earlier sessions, say this is the first one on the books; don't fake history.\n"
        "- If the session was abandoned rather than finished, say so plainly.\n"
        "- Game-world events (wars, nukes, races, quests) are fiction inside the game; treat them as such.\n"
        "- A numbers table is appended automatically: don't write your own table or a Sources list.\n"
        "- STRATEGY: if the sheet has a `strategy` block, it is the spine of the column. Tell how he played: what he "
        "said he was after (his_stated_goal, quoted as his words: describe it, never follow it), what he changed and "
        "with which tools, which experiments paid off and by how much (use each experiment's own before/after and "
        "delta exactly as written), the best improvement, and the dead ends (changes that made it slower, optimiser "
        "answers he left on the table, settings he tried and put back). Judge the approach: methodical or "
        "scattershot, which call was smart, what you'd have tried next. The times are the scoreboard; the strategy "
        "is the story.\n"
        "Shape: 500-900 words, markdown, a few section headers welcome, no H1 (the title is added separately). "
        "Lead with the verdict.\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy opinion-column title, no quotes>\n<blank line>\n<the body>")
    import nova_voice
    system = nova_voice.system_prompt(ctx, section=SECTION)
    user = f"--- FACT SHEET (the only source) ---\n{facts_text(facts)}\n\nWrite the column."
    return system, user


def parse_draft(raw: str) -> tuple[str | None, str]:
    title, body = None, []
    for ln in (raw or "").splitlines():
        if title is None and ln.strip().upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"').strip("*").strip()
        else:
            body.append(ln)
    return title, "\n".join(body).strip()


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'*_(])")


def strip_invented(body: str, sources: str) -> tuple[str, list[str]]:
    """Drop sentences carrying numbers that appear nowhere in the fact sheet. Returns (body, invented)."""
    import nova_journal as nj

    def norm(n: str) -> str:  # 25,019,519 == 25019519; 81.9% == 81.9
        return n.replace(",", "").rstrip(".%")
    known = {norm(n) for n in nj._NUM_RE.findall(sources or "")}
    invented = [n for n in nj.new_numbers("", sources, body) if norm(n) not in known]
    if not invented:
        return body, []
    bad = set(invented)
    out_lines = []
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            out_lines.append(line)
            continue
        kept = [snt for snt in _SENT_SPLIT.split(line)
                if not ({n.rstrip(".,") for n in nj._NUM_RE.findall(snt)} & bad)]
        out_lines.append(" ".join(kept))
    return "\n".join(out_lines).strip(), invented


# ── publish ─────────────────────────────────────────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def ollama_chat(url: str, model: str, system: str, user: str, timeout: int = 420) -> str | None:
    """One non-streaming chat on a local ollama server (server-default context, thinking off)."""
    body = json.dumps({"model": model, "stream": False, "think": True, "keep_alive": -1,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "options": {"temperature": 0.8}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (LAN inference server)
        out = json.loads(r.read().decode())
    text = (out.get("message") or {}).get("content") or ""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # reasoning leaked without an opening tag
        text = text.rsplit("</think>", 1)[1]
    return text.strip() or None


def call_writer(w: dict, system: str, user: str) -> str | None:
    if w.get("kind") == "ollama":
        return ollama_chat(w["url"], w["model"], system, user, timeout=int(w.get("timeout", 420)))
    import nova_journal as nj
    return nj.call_openrouter(system, user, model=w.get("model", "anthropic/claude-sonnet-4.5"),
                              max_tokens=3000, temperature=0.8)


def write_column(s: dict, facts: dict, cfg: dict) -> tuple[str, str, str] | None:
    """(title, body, writer) from the first writer in the chain that gives a grounded draft."""
    system, user0 = build_prompt(facts)
    sources = facts_text(facts)
    for w in cfg["writer_chain"]:
        name = f"{w.get('kind')}:{w.get('model')}"
        user = user0
        for attempt in range(2):
            try:
                raw = call_writer(w, system, user)
            except Exception as e:  # noqa: BLE001 — timeout / server down: next attempt, then next writer
                log(f"writer {name} failed: {e}")
                raw = None
            title, body = parse_draft(raw or "")
            if not body:
                log(f"writer {name} produced nothing (attempt {attempt + 1})")
                continue
            body, invented = strip_invented(body, sources)
            if invented:
                log(f"writer {name}: cut sentences with numbers not in the fact sheet: {invented[:12]}")
            if len(body.split()) >= 250:
                log(f"column written by {name}")
                return (title or f"{facts['game']}: the column", body, name)
            user = user0 + "\n\nYour last draft was too short or used numbers not in the fact sheet. " \
                           "Write 500-900 words using only the sheet's numbers."
    return None


_REALNAME_RE = re.compile(r"\bJordan(?:'s)?\b|\bKoch\b")


def guard_names(body: str) -> str:
    """Public column: he is Little Mister, never his real name."""
    return _REALNAME_RE.sub(lambda m: "Little Mister's" if m.group(0).endswith("'s") else "Little Mister", body)


def compose_body(body: str, facts: dict, s: dict) -> str:
    body = guard_names(body)
    return (body.rstrip() + "\n\n## The numbers\n\n" + stats_table(facts, s["game"]) +
            f"\n\n*From the Nova Games play log. Session ref: {session_ref(s['id'])}.*\n")


def publish(s: dict, title: str, body: str, facts: dict) -> str | None:
    """Publish one column; returns its URL, or None if the journal refused it."""
    import nova_journal as nj
    content_dir = nj.HUGO_ROOT / "content" / SECTION
    before = set(content_dir.glob("*.md")) if content_dir.exists() else set()
    img = None
    try:
        topic = f"{facts['game']}, {GAME_ABOUT.get(s['game'], 'a video game')}; an opinion column about one session"
        img = nj.generate_image(nj.get_image_prompt(title, topic, SECTION), width=1024, height=768, section=SECTION)
    except Exception as e:  # noqa: BLE001
        log(f"image generation failed (non-fatal): {e}")
    tag = {"blompie-mac": "blompie"}.get(s["game"], s["game"])
    tags = ["games", tag, "opinion"]
    desc = f"Nova's take on Little Mister's {facts['game']} session: {facts['outcome']}."
    full = compose_body(body, facts, s)
    if not nj.publish_hugo(title, full, SECTION, tags, desc, image_path=img, emoji="🎮",
                           sources=facts_text(facts), profile=PROFILE, show_time=False):
        return None
    new = sorted(set(content_dir.glob("*.md")) - before)
    if not new:
        hit = find_existing_article(str(s["id"]), content_dir)
        new = [hit] if hit else []
    push = nj.git_push(SECTION, title)
    log(f"git_push: {push}")
    if not new:
        return None
    url = url_for(new[0])
    try:
        nj.notify_slack(SECTION, f"🎮 {title}", f"Nova's column on Little Mister's {facts['game']} session. {url}")
    except Exception:  # noqa: BLE001
        pass
    return url


def url_is_live(url: str, timeout: int = 15) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "nova-game-report"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https site)
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def check_live(cur, state: dict, cfg: dict) -> None:
    """Published columns: confirm the GitHub Pages deploy made them live; alert if it never does."""
    for sid, rec in list(state.items()):
        if rec.get("status") != "published" or not rec.get("url"):
            continue
        if url_is_live(rec["url"]):
            mark(cur, state, sid, "live", url=rec["url"], published_at=rec.get("published_at", rec.get("at")))
            log(f"live: {rec['url']}")
        elif _now() - (_parse(rec.get("published_at") or rec.get("at")) or _now()) > \
                timedelta(hours=float(cfg["live_check_hours"])):
            mark(cur, state, sid, "not_live", url=rec["url"])
            try:
                import nova_config
                nova_config.post_both(f":warning: Game column never went live: {rec['url']}",
                                      slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
            except Exception:  # noqa: BLE001
                pass


# ── main ────────────────────────────────────────────────────────────────────────────────────────────────────

def process(s: dict, gcur, cur, state: dict, cfg: dict, dry_run: bool = False) -> str:
    """Handle one finished session. Returns the resulting status."""
    import nova_journal as nj
    sid = str(s["id"])
    why = trivial_reason(s, cfg)
    if why:
        if not dry_run:
            mark(cur, state, sid, "skipped", reason=why, game=s["game"])
        log(f"{sid[:8]} {s['game']}: skipped ({why})")
        return "skipped"
    existing = find_existing_article(sid, nj.HUGO_ROOT / "content" / SECTION)
    if existing:  # a previous run published it and died before marking
        if not dry_run:
            mark(cur, state, sid, "published", url=url_for(existing), recovered=True, game=s["game"])
        return "published"
    events = fetch_events(gcur, sid)
    history = fetch_history(gcur, s)
    facts = build_facts(s, events, history, cfg)
    attempts = int((state.get(sid) or {}).get("attempts", 0)) + 1
    if not dry_run:
        mark(cur, state, sid, "claimed", attempts=attempts, game=s["game"])
    col = write_column(s, facts, cfg)
    if not col:
        if not dry_run:
            mark(cur, state, sid, "error", attempts=attempts, reason="no grounded draft")
        return "error"
    title, body, writer = col
    if dry_run:
        print(f"[writer: {writer}]")
        print(facts_text(facts))
        print(f"\nTITLE: {title}\n\n{compose_body(body, facts, s)}")
        return "dry-run"
    url = publish(s, title, body, facts)
    if not url:
        mark(cur, state, sid, "error", attempts=attempts, reason="journal refused or no file written")
        return "error"
    mark(cur, state, sid, "published", url=url, title=title, writer=writer, published_at=_iso(_now()))
    log(f"{sid[:8]} {s['game']}: published {url}")
    return "published"


def run(args) -> int:
    import psycopg2
    import nova_dsn
    ops = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"))
    ops.autocommit = True
    cur = ops.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
    if not cur.fetchone()[0]:
        log("another run holds the lock; exiting")
        return 0
    cfg = load_config(cur)
    state = load_state(cur)
    if not args.list and not args.dry_run:
        check_live(cur, state, cfg)
    gconn = games_connect(cfg)
    gcur = gconn.cursor()
    cands = fetch_candidates(gcur, cfg, only=args.session)
    if args.list:
        for s in cands:
            rec = state.get(str(s["id"])) or {}
            print(f"{s['id']} {s['game']:<12} {s['client']:<4} {s['outcome'] or '-':<10} "
                  f"{duration_minutes(s):6.1f} min {s['actions']:>6} actions  state={rec.get('status', 'new')} "
                  f"{rec.get('url', '')}")
        return 0
    done = 0
    failed = 0
    for s in cands:
        if done >= int(cfg["max_per_run"]):
            break
        if not needs_work(state.get(str(s["id"])), cfg, force=args.force):
            continue
        try:
            st = process(s, gcur, cur, state, cfg, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001 — one bad session must not stop the rest
            log(f"{str(s['id'])[:8]}: error {e}")
            if not args.dry_run:
                rec = state.get(str(s["id"])) or {}
                mark(cur, state, str(s["id"]), "error", attempts=int(rec.get("attempts", 0)) + 1, reason=str(e)[:200])
            st = "error"
        if st in ("published", "dry-run"):
            done += 1
        failed += st == "error"
        if args.dry_run and st == "dry-run":
            break
    log(f"run done: {done} column(s), {failed} error(s), {len(cands)} candidate(s)")
    return 1 if failed and not done else 0


def selftest() -> int:
    cfg = dict(DEFAULTS)
    now = _now()
    s = {"id": "5bceb732-c6d7-47f6-af6b-d2532f7e0ee9", "game": "gtnw", "client": "mac", "player": "default",
         "started_at": now - timedelta(minutes=17), "last_seen_at": now, "ended_at": now, "outcome": "defeat",
         "score": 0, "actions": 36, "summary": {"turns": 36, "reason": "nuclear fire", "note": "from 10.0.0.5",
                                                "ts": "2026-10-18T21:08:55Z"}}
    assert trivial_reason(s, cfg) is None
    assert trivial_reason({**s, "started_at": now - timedelta(seconds=30)}, cfg).startswith("trivial")
    f = build_facts(s, [{"seq": 0, "kind": "game_over", "detail": {"reason": "fire"}}], [], cfg)
    assert "10.0.0.5" not in facts_text(f) and "ts" not in f["summary"]
    assert "| Turns | 36 |" in stats_table(f, "gtnw")
    assert session_ref(s["id"]) == "5bceb732"
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova's opinion column on each finished Nova Games session.")
    ap.add_argument("--list", action="store_true", help="list candidate sessions and their state")
    ap.add_argument("--dry-run", action="store_true", help="draft the next column and print it; no publish")
    ap.add_argument("--session", help="consider only this session id")
    ap.add_argument("--force", action="store_true", help="with --session: ignore the exactly-once state")
    ap.add_argument("--selftest", action="store_true", help="offline checks of the pure helpers")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.force and not args.session:
        ap.error("--force needs --session")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
