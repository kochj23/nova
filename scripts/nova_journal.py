#!/usr/bin/env python3
"""
nova_journal.py — Unified Nova journal content generation.

Replaces 11 individual scripts with a single subcommand interface:
  nova_journal.py essay | opinion | after-dark | pilot | tech-today |
                  research | synthesis | digest | dream | art

Shared pipeline: topic selection -> memory fetch -> LLM generate -> image gen ->
Hugo publish -> git push -> Slack notify.

Written by Jordan Koch.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

from nova_voice import (
    system_prompt, NOVA_VOICE, NOVA_VOICE_SHORT,
    CONTEXT_JOURNAL_OPS, CONTEXT_JOURNAL_ESSAY, CONTEXT_JOURNAL_RESEARCH,
    CONTEXT_JOURNAL_AFTER_DARK, CONTEXT_JOURNAL_LOCAL,
)

import nova_config
from nova_notify import notify
from nova_image_utils import generate_image
try:
    from nova_ops_context import get_full_context, format_security_brief, format_infra_brief
except ImportError:
    def get_full_context(hours=24): return {}
    def format_security_brief(ctx): return ""
    def format_infra_brief(ctx): return ""

# ══════════════════════════════════════════════════════════════════════════════
# GLOBALS
# ══════════════════════════════════════════════════════════════════════════════

MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
from nova_resolve import resolve_url
SEARXNG_URL = resolve_url("searxng", "/search")
HUGO_ROOT = (Path.home() / "nova-journal")
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal.log"
STATE_FILE = Path.home() / ".openclaw/config/journal_state.json"

# Date override for backfill
_FOR_DATE = os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _OVERRIDE_DT = datetime.strptime(_FOR_DATE, "%Y-%m-%d")
    def today_str() -> str: return _FOR_DATE
    def now_dt() -> datetime: return _OVERRIDE_DT.replace(hour=9)
else:
    def today_str() -> str: return time.strftime("%Y-%m-%d")
    def now_dt() -> datetime: return datetime.now()


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [journal] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_recent(state: dict, profile_name: str, key: str = "topics", days: int = 7) -> list:
    """Get recent items for a profile to avoid repeats."""
    profile_state = state.get(profile_name, {})
    recent = profile_state.get(f"recent_{key}", [])
    # Prune entries older than `days` days
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [r for r in recent if r.get("date", "9999") >= cutoff]


def add_recent(state: dict, profile_name: str, item: str, key: str = "topics"):
    """Add an item to the recent list for deduplication."""
    if profile_name not in state:
        state[profile_name] = {}
    recent_key = f"recent_{key}"
    if recent_key not in state[profile_name]:
        state[profile_name][recent_key] = []
    state[profile_name][recent_key].append({"item": item, "date": today_str()})
    # Keep last 30
    state[profile_name][recent_key] = state[profile_name][recent_key][-30:]


# ══════════════════════════════════════════════════════════════════════════════
# PII SCRUBBING
# ══════════════════════════════════════════════════════════════════════════════

def _build_scrub_patterns() -> list:
    _u = "kochj"
    _d = "digitalnoise.net"
    _g = "gmail.com"
    _corp = "dis" + "ney.com"
    return [
        re.compile(rf"{_u}par@{_g}", re.IGNORECASE),
        re.compile(rf"{_u}par@", re.IGNORECASE),
        re.compile(rf"jordan\.koch@{re.escape(_corp)}", re.IGNORECASE),
        re.compile(rf"{_u}@{re.escape(_d)}", re.IGNORECASE),
        re.compile(rf"{_u}23@{_g}", re.IGNORECASE),
        re.compile(re.escape(str(Path.home()) + "/")),
        re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
    ]

_SCRUB_PATTERNS = _build_scrub_patterns()
_SAFE_EMAILS = {"nova@digitalnoise.net"}


# Real MAC addresses (6 colon-separated hex pairs) must never reach the public blog — they
# leak home-network device inventory AND trip the pre-push MAC scan (which silently blocks
# the manual push and, worse, the daily auto-publish had been shipping them). Scrub at the
# source, here, so every published body is clean regardless of which generator wrote it.
# (2026-09-16: two articles had leaked device MACs via alert/device material woven into prose.)
_MAC_RE = re.compile(r'\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b')


def scrub_pii(text: str) -> str:
    """Remove personal identifiers from text before publishing."""
    for pat in _SCRUB_PATTERNS[:-1]:
        text = pat.sub("[redacted]", text)
    # Email pattern — keep Nova's email
    def _replace_email(m):
        return m.group(0) if m.group(0) in _SAFE_EMAILS else "[redacted]"
    text = _SCRUB_PATTERNS[-1].sub(_replace_email, text)
    text = _MAC_RE.sub("[redacted-mac]", text)   # device MACs never go public
    return text


# Absolute macOS home paths (/Users/<name>/...) must never appear in public article
# prose: they leak a local filesystem layout AND trip the per-clone pre-commit
# secret-scanner (hook rule: any .md containing a "/Users/<name>/" path is rejected), which
# silently discards the post on every host. Scrubbing at the SOURCE — here, in the
# shared library, before the body is written — stops that class of false positive
# fleet-wide without ever touching a security hook. NOTE: this only matches
# "/Users/..." — site-relative Hugo paths like "/images/local/x.webp" are untouched.
_HOME_PATH_RE = re.compile(r'/Users/[^/\s]+/[^\s)"\']*')

def scrub_home_paths(text: str) -> str:
    """Replace absolute macOS home paths (/Users/<name>/...) with a harmless
    placeholder. Applied to article BODIES only — never front-matter — so
    site-relative image paths (/images/...) are left intact."""
    return _HOME_PATH_RE.sub("~/…", text)


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def recall_memories(query: str, n: int = 20, source: str = None, include_private: bool = False) -> list[dict]:
    """Semantic search against the memory server. This is the PUBLIC-journal helper, so
    it excludes personal/work sources by default (include_private=False) — work docs,
    email, texts, health, etc. never surface in a public post. Belt-and-suspenders with
    filter_private_memories() below."""
    params = {"q": query, "n": str(n), "include_private": "true" if include_private else "false"}
    if source:
        params["source"] = source
    url = f"{MEMORY_SERVER}/recall?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        return nova_config.filter_private_memories(memories)
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def random_memories(n: int = 10) -> list[dict]:
    """Fetch random memories from the server."""
    url = f"{MEMORY_SERVER}/random?n={n}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        return nova_config.filter_private_memories(memories)
    except Exception as e:
        log(f"Random memory fetch failed: {e}")
        return []


def get_available_sources(min_count: int = 50) -> list[str]:
    """Get sources with sufficient memories, excluding private ones."""
    url = f"{MEMORY_SERVER}/stats"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        sources = data.get("by_source", data.get("sources", {}))
        return [s for s, c in sources.items()
                if c >= min_count and not nova_config.is_private_source(s)]
    except Exception as e:
        log(f"Source stats fetch failed: {e}")
        return []


def fetch_memories_by_source(source: str, n: int = 25) -> list[dict]:
    """Fetch random memories from a specific source via DB with metadata."""
    result = subprocess.run(
        ["psql", "-h", "pg-primary.digitalnoise.net", "-U", "kochj", "-d", "nova_memories", "-tA", "-F", "\x1f", "-R", "\x1e", "-c",
         f"SELECT text, source, metadata::text FROM memories WHERE source = '{source}' "
         f"AND tier != 'scratchpad' ORDER BY random() LIMIT {n};"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        # Surface the psql error: swallowing it is how a missing -h (no local socket on
        # nova-core) hid behind "Only 0 memories for X" for 8 Wednesdays (Aug 12-Sep 30 2026).
        log(f"fetch_memories_by_source({source}): psql exit {result.returncode}: "
            f"{(result.stderr or '').strip()[:300]}")
        return []
    memories = []
    # Records split on \x1e (psql -R), not newline: memory texts contain newlines, and a
    # newline split turned one 25-row draw into ~240 fragments (pre-existing; fixed 2026-09-30).
    for line in result.stdout.strip().split("\x1e"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if parts[0]:
            meta = {}
            if len(parts) > 2 and parts[2]:
                try:
                    meta = json.loads(parts[2])
                except (json.JSONDecodeError, ValueError):
                    pass
            memories.append({"text": parts[0], "source": parts[1] if len(parts) > 1 else source, "metadata": meta})
    return nova_config.filter_private_memories(memories)


# ══════════════════════════════════════════════════════════════════════════════
# LLM GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def call_openrouter(system: str, user: str, model: str = "anthropic/claude-haiku-4.5",
                    max_tokens: int = 4000, temperature: float = 0.7,
                    top_p: float = 0.9, timeout: int = 240) -> str | None:
    """Call the local Claude Code CLI (Claude Max subscription — flat rate, no
    per-token billing). ponytail: kept the name/signature so the ~15 call sites
    across the fishbowl/journal scripts don't need touching; OpenRouter's credit
    balance ran dry 2026-07-17. Rename if OpenRouter comes back into rotation.
    max_tokens/temperature/top_p have no Claude Code CLI equivalent and are
    accepted-but-ignored for signature compatibility.

    The user prompt goes in on STDIN, never argv. Linux caps a *single* execve()
    argument at MAX_ARG_STRLEN (32 pages = 131072 bytes) independently of the much
    larger ARG_MAX, so any call site whose assembled context crosses 128 KiB used to
    die with `[Errno 7] Argument list too long: 'claude'` before the model was ever
    reached. That killed fishbowl_daily + opinion_fishbowl every morning from
    2026-07-22 (their cast-dossier block had grown to ~470 KiB). stdin has no such
    limit, and fixing it here covers all ~15 call sites at once.

    timeout: seconds before the whole claude process group is killed (default 240).
    The grounded longform expander passes a larger value for Sonnet (2026-10-06).
    """
    cli_model = ("haiku" if "haiku" in model else
                 "sonnet" if "sonnet" in model else
                 "opus" if "opus" in model else model)
    # The system prompt is still argv (it's Nova's voice, ~7 KiB, and the CLI has no
    # stdin equivalent). Fail LOUDLY with the real reason if a caller ever grows it
    # past the kernel's per-arg ceiling rather than emitting a cryptic errno.
    if len(system.encode()) > 120_000:
        log(f"Claude Code call refused ({cli_model}): system prompt "
            f"{len(system.encode())} bytes exceeds the 128 KiB execve per-arg limit — "
            f"move the bulk into the user prompt (stdin)")
        return None
    # WEDGE FIX (2026-08-11): claude -p can leave a background child (telemetry/daemon) holding
    # the stdout pipe open, so after the timeout kills the DIRECT child, communicate() keeps
    # blocking on a read that never sees EOF — subprocess.run then hangs FOREVER past its timeout
    # (fishbowl_daily wedged 8+ min at 0% CPU this way). Run claude in its OWN process group
    # (start_new_session) and, on timeout, SIGKILL the whole group so the pipe-holder dies too;
    # bound the reap so this function can never hang the caller.
    import os as _os
    import signal as _signal
    proc = None
    # Inject the long-lived CLAUDE_CODE_OAUTH_TOKEN so `claude -p` stays authenticated even after
    # its short-lived file credential expires nightly (the recurring "Not logged in" that killed
    # every generator). Fail-safe: if the helper or token is unavailable, env stays None and the
    # CLI falls back to its file credential exactly as before.
    try:
        from nova_claude_code import claude_env
        _env = claude_env()
    except Exception:
        _env = None
    # RETRY (2026-09-04): claude.exe (a Bun single-file binary) intermittently exits
    # nonzero with "ENOENT: Bun could not find a file" (or an empty error) — most likely
    # concurrent generators racing its temp extraction. With no retry, one flaky exit =
    # one silently MISSING article (this cost the 2026-09-03 digest, and flaked essay/
    # dream on adjacent days). Retry transient failures with backoff; do NOT retry a
    # timeout (too expensive) or the size refusal above. One fix covers all ~15 callers.
    import time as _time
    _attempts = 3
    for _attempt in range(1, _attempts + 1):
        proc = None
        try:
            proc = subprocess.Popen(
                ["claude", "-p", "--model", cli_model, "--system-prompt", system],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True, env=_env)
            try:
                out, err = proc.communicate(input=user, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
                except Exception:
                    proc.kill()
                try:
                    proc.communicate(timeout=10)
                except Exception:
                    pass
                log(f"Claude Code call TIMED OUT after {timeout}s ({cli_model}) — killed process group")
                return None
            if proc.returncode != 0:
                log(f"Claude Code call failed ({cli_model}) attempt {_attempt}/{_attempts}: "
                    f"{(err or '').strip()[:300]}")
                if _attempt < _attempts:
                    _time.sleep(2 * _attempt)
                    continue
                return None
            text = (out or "").strip()
            if not text:
                log(f"Claude Code call returned empty output ({cli_model}) "
                    f"attempt {_attempt}/{_attempts}")
                if _attempt < _attempts:
                    _time.sleep(2 * _attempt)
                    continue
                return None
            if _attempt > 1:
                log(f"Claude Code call recovered on attempt {_attempt} ({cli_model})")
            log(f"LLM [{cli_model} via claude-code] chars out={len(text)}")
            return text
        except Exception as e:
            if proc is not None:
                try:
                    _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
                except Exception:
                    pass
            log(f"Claude Code call failed ({cli_model}) attempt {_attempt}/{_attempts}: {e}")
            if _attempt < _attempts:
                _time.sleep(2 * _attempt)
                continue
            return None
    return None


def grafana_panel_image(dashboard_uid: str, panel_id: int, section: str, name: str,
                        width: int = 1000, height: int = 500,
                        time_range: str = "now-24h") -> str | None:
    """Render a live Grafana panel to PNG and save it into the Hugo static images dir.
    Returns the site-relative path (for markdown embedding) or None on failure.
    Uses Grafana's anonymous Viewer role — no credentials needed (see /home/kochj/grafana/docker-compose.yml).
    """
    # theme=dark matches the blog's dark aesthetic explicitly (don't drift with the admin
    # user's Grafana UI preference); scale=2 renders at 2x pixel density for crisp text on
    # the blog — real numbers, just sharper, no image-model reinterpretation of the data.
    url = resolve_url("grafana", f"/render/d-solo/{dashboard_uid}?panelId={panel_id}"
                       f"&width={width}&height={height}&from={time_range}&to=now&tz=America%2FLos_Angeles"
                       f"&theme=dark&scale=2")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            png = resp.read()
        if not png.startswith(b"\x89PNG"):
            log(f"[grafana_panel_image] non-PNG response for {dashboard_uid}/{panel_id}")
            return None
    except Exception as e:
        log(f"[grafana_panel_image] render failed for {dashboard_uid}/{panel_id}: {e}")
        return None

    images_dir = HUGO_ROOT / f"static/images/{_canon_section(section)}"
    images_dir.mkdir(parents=True, exist_ok=True)
    # static/images/**/*.png is gitignored repo-wide (deploy expects webp) -- convert
    # immediately, same as every other cover/inline image path in this codebase, or the
    # render silently never gets committed and ships as a broken image link.
    tmp_png = images_dir / f".{today_str()}-{name}.tmp.png"
    tmp_png.write_bytes(png)
    dest = images_dir / f"{today_str()}-{name}.webp"
    try:
        subprocess.run(["cwebp", "-q", "82", str(tmp_png), "-o", str(dest)],
                       capture_output=True, timeout=30, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log(f"[grafana_panel_image] cwebp failed, falling back to raw png (won't survive git): {e}")
        dest = images_dir / f"{today_str()}-{name}.png"
        shutil.copy2(tmp_png, dest)
    finally:
        tmp_png.unlink(missing_ok=True)
    return f"/images/{_canon_section(section)}/{dest.name}"


def get_image_prompt(title: str, topic: str, section: str) -> str:
    """Use Haiku to generate a safe image prompt for the content."""
    system = (
        "You generate image prompts for AI art to accompany journal posts. "
        "Generate vivid, realistic image prompts. Prefer actual scenes, objects, environments.\n\n"
        "SAFETY RULES — go ABSTRACT (geometry, landscapes, light, water) ONLY if the topic risks:\n"
        "- RACIST output: race, ethnicity, culture, gangs, colonialism, slavery\n"
        "- VIOLENT output: war, weapons, murder, combat, torture\n"
        "- SEXUAL output: nudity, intimacy, bodies\n"
        "- STEREOTYPES: poverty, homelessness, addiction, disability\n"
        "- RELIGIOUS offense: sacred imagery, deities, prophets\n\n"
        "For ALL other topics: generate REALISTIC scene prompts.\n"
        "Output ONLY the image prompt. 30 words max. No explanation."
    )
    user = f"Title: {title}\nTopic/category: {topic}\n\nImage prompt:"
    result = call_openrouter(system, user, max_tokens=60, temperature=0.5)
    if result:
        return f"{result.strip()}, elegant composition, muted color palette, no text, no words"
    return f"abstract artistic illustration of {topic}, flowing shapes, warm lighting, no text"


# ══════════════════════════════════════════════════════════════════════════════
# HUGO PUBLISHING
# ══════════════════════════════════════════════════════════════════════════════

def _canon_section(section: str) -> str:
    """rando is retired (Jordan, 2026-07-12): every write to it is redirected to operations."""
    return "operations" if section == "rando" else section


# ── Article length policy + grounded expansion (2026-10-06, per Jordan) ──────────
# "I don't want it to hallucinate. Maybe move to a model that can handle it?"
# The old expander asked haiku to pad a ~1,100-word draft to 5,000 words with only
# the draft in hand — that forces invention. Now each ARTICLE TYPE (keyed by the
# generator profile, not the section folder: operations holds many kinds) has a
# (min, max, policy) row:
#   * below min + policy "grounded" + sources passed -> ONE grounded expansion toward
#     min..max (Sonnet; haiku only if Sonnet errors), then the number check + a separate
#     Sonnet grounding check. Flagged items -> STRIP, NOT SCRAP (2026-10-06): the flagged
#     sentences the expansion ADDED are removed (draft sentences are never touched),
#     both checks re-run once on the stripped text, and it publishes only if clean.
#     Fail closed -> the ORIGINAL publishes: checker error / unparseable verdict / no
#     task time left / a flag that is not in an added sentence / >15% of the added
#     sentences cut / anything still flagged on the recheck. A grounded expansion that
#     is meaningfully longer is accepted even if it is still under min.
#   * below min + policy "never" (or no sources) -> publish the draft, no model call.
#   * above max -> one "tighten to <=max, add nothing" pass; any failure -> draft.
#   * profile not in the table -> no floor, no expansion, publish as written (logged).
# EDIT THE NUMBERS HERE. Documented in agent_docs 'scripts' and claude_memories
# feedback-no-hallucinated-longform.
EXPAND_NEVER, EXPAND_GROUNDED = "never", "grounded"
_BREAKING = (300, 700, EXPAND_NEVER)      # breaking security alerts / IPS blocks / CVE notices
_DAILY = (600, 1200, EXPAND_NEVER)        # daily memory audits / "what I did today" / ops daily logs
_WEEKLY = (700, 1400, EXPAND_NEVER)       # weekly roundups ("This Week in ...")
_OPINION = (1000, 1800, EXPAND_GROUNDED)
_LOCAL = (1200, 2000, EXPAND_GROUNDED)    # local Burbank dispatch
ARTICLE_LENGTH = {
    # profile:               (min, max, expand policy)
    "ops-security":            _BREAKING,   # nova_operations_security.py
    "emergency-breaking":      _BREAKING,   # nova_journal_emergency.py breaking
    "dream":                   (400, 900, EXPAND_NEVER),
    "digest":                  _DAILY,      # nova_journal.py digest (daily ops)
    "unclaimed-digest":        _DAILY,
    "fellowship-daily":        _DAILY,
    "copenhagen":              _DAILY,      # morning ops review
    "daily-watch":             _DAILY,      # nova_daily_threat_assessment.py
    "weekly-summary":          _WEEKLY,     # nova_journal_weekly_summary.py
    "media-wrap":              _WEEKLY,
    "alert-patterns":          _WEEKLY,
    "network-health":          _WEEKLY,
    "weekly-ops-capstone":     _WEEKLY,
    "opinion":                 _OPINION,
    "fishbowl-daily":          _OPINION,
    "opinion-fishbowl":        _OPINION,
    "opinion-fishbowl-roster": _OPINION,
    "local-airwaves":          _LOCAL,
    "local-trends":            _LOCAL,
    "emergency-daily":         _LOCAL,
    "tech-today":              (1200, 2000, EXPAND_GROUNDED),
    "synthesis":               (1500, 2500, EXPAND_GROUNDED),
    "essay":                   (2500, 4000, EXPAND_GROUNDED),
    "research":                (2500, 5000, EXPAND_GROUNDED),
    # Monthly pieces — "a lot happens in a month" (Jordan, 2026-10-06): >= 3000 words.
    "meta":                    (3000, 6000, EXPAND_GROUNDED),   # nova_meta_analysis.py (monthly)
    "monthly-wrap":            (3000, 6000, EXPAND_GROUNDED),   # nova_monthly_wrap.py
    "autobiography":           (1500, 3000, EXPAND_GROUNDED),   # weekly narrative-identity arc
    "ledger":                  (1200, 2500, EXPAND_GROUNDED),   # nova_ledger_of_changed_minds.py (monthly)
    "repo-scout":              (800, 1600, EXPAND_NEVER),       # nova_repo_scout.py verdict review
    "iot-scout":               (800, 1600, EXPAND_NEVER),       # nova_iot_scout.py verdict review
    # Unmapped (publish as written): one-off ops articles. Retired profiles: RETIRED_PROFILES.
}
# Retired generator profiles (no rows on purpose). Publishing under one still works but
# logs a WARNING — something is still calling a generator Jordan retired.
RETIRED_PROFILES = frozenset({"after-dark", "pilot", "art"})

# Per-story length overrides (2026-10-06, per Jordan: "some specific stories at 3,000+
# words — I'll call them out"). Lives in Postgres so it is changed without a deploy:
#   nova_ops.service_config (service='nova_journal', key='longform_overrides') value =
#   [{"profile": "copenhagen", "min_words": 3000, "note": "..."},
#    {"pattern": "(?i)two.months.of", "min_words": 3000}]      # regex vs title AND slug
# An entry needs "min_words" plus "profile" and/or "pattern" (both given -> both must
# match). The highest matching min wins; publish_hugo(min_words=N) beats the DB.
# An override only RAISES the floor and turns on grounded expansion for that story — it
# still needs sources and still passes the number check + Sonnet grounding check (no
# sources -> publishes as written). LONGFORM_OVERRIDES is the in-code fallback, used
# only when the DB is unreachable. Keep it empty: Jordan names the stories.
LONGFORM_OVERRIDES: list = []
LONGFORM_OVERRIDE_PG = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj connect_timeout=5"
_LONGFORM_OVERRIDE_CACHE: list = []
LONGFORM_MIN_GAIN = 1.25            # "meaningfully longer": >= 1.25x the draft (or >= min)
LONGFORM_MAX_OVERSHOOT = 1.25       # an expansion past 1.25x max is padding -> rejected
TIGHTEN_MAX_SLACK = 1.10            # a tightened draft may land up to 10% over max
LONGFORM_SOURCES_MAX_CHARS = 60_000
EXPAND_MODEL = "anthropic/claude-sonnet"
EXPAND_TIMEOUT_S = 600              # Sonnet timed out at the old 240 s default
EXPAND_FALLBACK_MODEL = "anthropic/claude-haiku-4.5"
EXPAND_FALLBACK_TIMEOUT_S = 240
CHECK_MODEL = "anthropic/claude-sonnet"
CHECK_TIMEOUT_S = 300
LONGFORM_RESERVE_S = 240            # left for publish + git push after the rewrite
# Strip-not-scrap (2026-10-06, per Jordan): one invented sentence used to throw away a
# whole grounded expansion. Now flagged ADDED sentences are cut, both checks re-run once.
STRIP_MAX_FRACTION = 0.15           # > 15% of the added sentences flagged -> expansion unreliable
STRIP_RECHECK_S = 300               # strip + re-run both checks; counted in the expansion time guard
STRIP_DRAFT_SIMILARITY = 0.85       # an added sentence this close to a draft one is an EDITED draft
                                    # sentence: a flag in it REVERTS it to the draft's exact wording
STRIP_FUZZY_SENTENCE = 0.75         # checker's "verbatim" sentence not found exactly -> closest unit
STRIP_CLAIM_WORDS = 0.75            # ...or the one unit holding >= 75% of the claim's content words
_PROC_T0 = time.monotonic()
_TASK_TIMEOUT_CACHE: list = []

_EXPAND_SYS = (
    "You are Nova, editing your own article before publication. You are given SOURCES "
    "(the only material this article may draw facts from) and the DRAFT. Expand the draft "
    "toward {lo}-{hi} words by deepening the analysis of what is already there and by "
    "bringing in further detail that is EXPLICITLY present in the SOURCES.\n"
    "HARD RULES — GROUNDING:\n"
    "- Expand ONLY using facts present in the DRAFT or the SOURCES block. Do not introduce any "
    "name, number, statistic, date, quote, event, product, study, citation or URL that does not "
    "appear in them.\n"
    "- Never attribute words to anyone unless that quote appears in the DRAFT or SOURCES.\n"
    "- Analysis, opinion, interpretation, voice and connective reasoning are welcome; new facts "
    "are not.\n"
    "- If the sources cannot support that length, STOP where the sources run out. A shorter "
    "honest article beats a padded one. No filler, no restating paragraphs. Never exceed {hi} words.\n"
    "- Keep the existing structure, title-free format, voice, and any Sources/Attribution "
    "section exactly as it is.\n"
    "Output ONLY the full article body — no preamble, no notes about what you changed.")

_TIGHTEN_SYS = (
    "You are Nova, editing your own article before publication. TIGHTEN the article below to "
    "at most {hi} words (aim for {lo}-{hi}). Cut repetition, filler and the weakest passages; "
    "keep the strongest points, the voice, the structure and any Sources/Attribution section. "
    "HARD RULE: add NOTHING — no new facts, names, numbers, dates, quotes, events or claims; "
    "only remove and lightly re-join what is already there. Output ONLY the tightened article "
    "body — no preamble, no notes about what you changed.")

_CHECK_SYS = (
    "You are a strict fact-checker. You receive SOURCES, the ORIGINAL draft, and an EXPANDED "
    "version of it. List every specific factual claim in the EXPANDED text that is NOT stated in, "
    "or directly supported by, the ORIGINAL or the SOURCES (in practice: claims in the added "
    "text). Specific claims are: names of people/organizations/products/places (name); "
    "numbers, statistics, amounts, versions (number); dates or times (date); quotations "
    "attributed to anyone (quote); events or incidents said to have happened (event); studies, "
    "papers, reports, URLs (citation). Opinions, analysis, metaphors, jokes and general "
    "reasoning are NOT claims — ignore them. For each unsupported claim also copy the COMPLETE "
    "sentence of the EXPANDED text that contains it into \"sentence\", VERBATIM — character for "
    "character, including its markdown; never paraphrase or shorten it (it is used to locate and "
    "remove that sentence). Reply with ONLY a JSON object, no prose: "
    '{"unsupported": [{"claim": "<short excerpt>", "sentence": "<the full sentence, verbatim>", '
    '"type": "name|number|date|quote|event|citation"}]} '
    '— an empty list if every specific claim is supported.')

# The model routinely prefaces with an acknowledgment ("I can see your article... Let
# me expand it...") despite the Output-ONLY rule — 30 such leaks reached the live site
# between Jul 30 and Sep 13. Leading meta paragraphs (and a stray --- after them) are
# stripped before the guard sees the text.
_EXPAND_META_RE = re.compile(
    r"^(i can see|i'?ll (expand|tighten)|let me (expand|tighten)|i've (expanded|tightened)|"
    r"here is the|here's the|the draft you|below is the)", re.I)


def article_length(profile: str | None):
    """(min, max, policy) for a generator profile, or None when unmapped."""
    return ARTICLE_LENGTH.get(profile or "")


def longform_overrides() -> list:
    """The per-story override list: service_config nova_journal/longform_overrides, else
    LONGFORM_OVERRIDES when PG is unreachable. Cached per process."""
    if _LONGFORM_OVERRIDE_CACHE:
        return _LONGFORM_OVERRIDE_CACHE[0]
    rows = list(LONGFORM_OVERRIDES)
    try:
        import psycopg2
        c = psycopg2.connect(LONGFORM_OVERRIDE_PG)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s",
                            ("nova_journal", "longform_overrides"))
                r = cur.fetchone()
        finally:
            c.close()
        rows = r[0] if r and isinstance(r[0], list) else []
    except Exception as e:
        log(f"[longform] override lookup failed ({e}) — using in-code LONGFORM_OVERRIDES")
    _LONGFORM_OVERRIDE_CACHE.append(rows)
    return rows


def override_min_words(profile: str | None, title: str, slug: str = "") -> int | None:
    """Highest min_words among override entries matching this story, or None."""
    best = None
    for o in longform_overrides():
        try:
            n = int(o.get("min_words") or 0)
            prof, pat = o.get("profile"), o.get("pattern")
            if n <= 0 or not (prof or pat):
                continue
            if prof and prof != profile:
                continue
            if pat and not (re.search(pat, title or "") or re.search(pat, slug or "")):
                continue
            best = max(best or 0, n)
        except Exception:
            continue
    return best


def sources_from_memories(memories: list | None, topic: str | None = None,
                          per_item: int = 1500) -> str:
    """Render a generator's source material (memories / web results / posts) as the
    SOURCES block for grounded expansion. "" when there is no real material."""
    parts = []
    for i, m in enumerate(memories or [], 1):
        if not isinstance(m, dict):
            continue
        text = str(m.get("text", "")).strip()
        if not text:
            continue
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
        url = meta.get("url", "")
        tag = f"{m.get('source', '?')}{', ' + url if url else ''}"
        parts.append(f"[{i}] ({tag}) {text[:per_item]}")
    if not parts:
        return ""
    head = [f"TOPIC: {topic}"] if topic else []
    return "\n\n".join(head + parts)


def _task_timeout_s() -> int | None:
    """The scheduler's timeout for THIS process's task (script + args matched in the
    scheduler yaml the parent passed via NOVA_SCHED_CONFIG), or NOVA_TASK_TIMEOUT.
    None when unknown (a manual run). Cached."""
    if _TASK_TIMEOUT_CACHE:
        return _TASK_TIMEOUT_CACHE[0]
    found = None
    try:
        if os.environ.get("NOVA_TASK_TIMEOUT"):
            found = int(os.environ["NOVA_TASK_TIMEOUT"])
        else:
            import yaml
            cfg = Path(os.environ.get("NOVA_SCHED_CONFIG")
                       or Path.home() / ".openclaw/config/scheduler.yaml")
            tasks = (yaml.safe_load(cfg.read_text()) or {}).get("tasks", {}) or {}
            script, args = Path(sys.argv[0]).name, [str(a) for a in sys.argv[1:]]
            hits = [int(t.get("timeout", 300)) for t in tasks.values()
                    if isinstance(t, dict) and t.get("script") == script
                    and [str(a) for a in (t.get("args") or [])] == args]
            found = min(hits) if hits else None
    except Exception:
        found = None
    _TASK_TIMEOUT_CACHE.append(found)
    return found


def _task_time_left() -> float | None:
    t = _task_timeout_s()
    return None if t is None else t - (time.monotonic() - _PROC_T0)


def _clean_expansion(out: str | None) -> str:
    paras = (out or "").split("\n\n")
    while paras and (_EXPAND_META_RE.match(paras[0].strip()) or paras[0].strip() == "---"):
        paras.pop(0)
    return "\n\n".join(paras).strip()


def _rewrite(system: str, user: str, time_left: float | None,
             after_s: float) -> tuple[str | None, str | None]:
    """One rewrite pass: Sonnet first; haiku ONLY if Sonnet errors (and there is task
    time for it). after_s = seconds the caller still needs afterwards (check + publish).
    Returns (text | None, model_used | None)."""
    plan = [(EXPAND_MODEL, EXPAND_TIMEOUT_S), (EXPAND_FALLBACK_MODEL, EXPAND_FALLBACK_TIMEOUT_S)]
    t_start = time.monotonic()
    for i, (model, tmo) in enumerate(plan):
        if time_left is not None:
            avail = time_left - (time.monotonic() - t_start) - after_s
            if avail < 120:
                log(f"[longform] no task time left for {model} ({avail:.0f}s) — skipping")
                return None, None
            tmo = int(min(tmo, avail))
        if i:
            log(f"[longform] {plan[0][0]} failed — falling back to {model}")
        out = _clean_expansion(call_openrouter(system, user, model=model,
                                               max_tokens=16000, temperature=0.4, timeout=tmo))
        if out:
            return out, model
    return None, None


def expand_grounded(body: str, sources: str, lo: int, hi: int,
                    time_left: float | None = None) -> tuple[str | None, str | None]:
    """One grounded expansion pass toward lo..hi words using ONLY draft + sources."""
    src = sources[:LONGFORM_SOURCES_MAX_CHARS]
    if len(sources) > LONGFORM_SOURCES_MAX_CHARS:
        src += "\n[... sources truncated ...]"
    user = f"SOURCES:\n<<<\n{src}\n>>>\n\nDRAFT:\n<<<\n{body}\n>>>"
    # time guard: the expansion only starts if check + strip/recheck + publish still fit
    return _rewrite(_EXPAND_SYS.format(lo=lo, hi=hi), user, time_left,
                    CHECK_TIMEOUT_S + STRIP_RECHECK_S + LONGFORM_RESERVE_S)


def tighten(body: str, lo: int, hi: int,
            time_left: float | None = None) -> tuple[str | None, str | None]:
    """One 'tighten to <=hi, add nothing' pass."""
    return _rewrite(_TIGHTEN_SYS.format(lo=lo, hi=hi), body, time_left, LONGFORM_RESERVE_S)


def check_grounding(draft: str, sources: str, expanded: str,
                    timeout: int = CHECK_TIMEOUT_S) -> tuple[bool, list, str]:
    """Separate model call: specific claims in `expanded` unsupported by draft+sources.
    FAIL CLOSED — returns (passed, unsupported_claims, note); any error -> (False, [], why)."""
    src = sources[:LONGFORM_SOURCES_MAX_CHARS]
    user = (f"SOURCES:\n<<<\n{src}\n>>>\n\nORIGINAL:\n<<<\n{draft}\n>>>\n\n"
            f"EXPANDED:\n<<<\n{expanded}\n>>>\n\nReturn the JSON now.")
    try:
        out = call_openrouter(_CHECK_SYS, user, model=CHECK_MODEL, max_tokens=4000,
                              temperature=0.0, timeout=timeout)
        if not out:
            return False, [], "checker returned nothing"
        a, b = out.find("{"), out.rfind("}")
        verdict = json.loads(out[a:b + 1]) if a >= 0 and b > a else None
        claims = verdict.get("unsupported") if isinstance(verdict, dict) else None
        if not isinstance(claims, list):
            return False, [], f"unparseable checker verdict: {out[:200]!r}"
    except Exception as e:
        return False, [], f"checker error: {e}"
    bad = []
    for c in claims:
        if isinstance(c, dict):
            # anything but opinion/analysis counts as specific — unknown types too (fail closed)
            if str(c.get("type", "")).lower() not in ("opinion", "analysis"):
                bad.append(c)
        elif c:
            bad.append({"claim": str(c), "type": "?"})
    return (not bad), bad, f"{len(claims)} flagged, {len(bad)} specific"


_NUM_RE = re.compile(r"\d[\d,.%]*\d%?|\d%")


def new_numbers(draft: str, sources: str, expanded: str) -> list[str]:
    """Deterministic backstop to the model checker: multi-character numbers (29, 0.08%,
    2014, 6.5) in the expansion that appear nowhere in draft+sources. Single digits are
    ignored (list numbering). The 2026-10-06 live check saw the model checker catch an
    invented '0.08%' but let new '29'/'90' through."""
    seen = set(_NUM_RE.findall(draft)) | set(_NUM_RE.findall(sources or ""))
    return sorted({n.rstrip(".,") for n in _NUM_RE.findall(expanded)} - {n.rstrip(".,") for n in seen})


# ── Strip-not-scrap helpers (deterministic; no model rewrite) ──────────────────────
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "e.g", "i.e", "etc", "u.s",
           "u.k", "no", "approx", "inc", "ltd", "co", "mt", "ft", "ave", "dept", "est"}
_SENT_END_RE = re.compile(r"[.!?…]+[\"'”’)\]*_]*\s+(?=[\"'“‘(\[*_#>\-–—A-Z0-9])")
_STRUCT_LINE_RE = re.compile(r"^\s*(#{1,6}\s|(-{3,}|\*{3,}|_{3,})\s*$|\||```)")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_CONNECTIVE_RE = re.compile(
    r"^(And|But|So,|Also,|Still,|Plus,|Yet,|Meanwhile,|Even so,|That said,|Besides,|"
    r"Then again,|On top of that,)\s+(?=\S)")
_QUOTE_MAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "..."})


def _split_sentences(line: str) -> list[str]:
    """Split one prose line into sentences (keeps markdown; abbreviations/initials are not
    boundaries). Over-splitting is harmless: draft and expansion are split the same way."""
    out, start = [], 0
    for m in _SENT_END_RE.finditer(line):
        prev = line[start:m.start()].split()
        last = prev[-1].lower().strip("([*_\"'").rstrip(".") if prev else ""
        if last in _ABBREV or (len(last) == 1 and last.isalpha() and last != "i"):
            continue
        out.append(line[start:m.end()].rstrip())
        start = m.end()
    if line[start:].strip():
        out.append(line[start:].rstrip())
    return out


def _doc_units(text: str) -> list:
    """Article -> paragraphs -> lines -> sentence units. Headings / rules / table rows /
    fences are one unit each. Rendered back by _render_units."""
    doc = []
    for para in re.split(r"\n[ \t]*\n", (text or "").strip()):
        lines = []
        for line in para.split("\n"):
            if not line.strip():
                continue
            lines.append([line.rstrip()] if _STRUCT_LINE_RE.match(line) else _split_sentences(line))
        if lines:
            doc.append(lines)
    return doc


def _render_units(doc: list) -> str:
    paras = ["\n".join(" ".join(u.strip() if i else u for i, u in enumerate(line)) for line in para if line)
             for para in doc]
    return "\n\n".join(p for p in paras if p.strip())


def _norm_sentence(s: str) -> str:
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(s or ""))       # [text](url) -> text
    s = s.translate(_QUOTE_MAP)
    s = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", s)   # heading / list / quote marker
    s = re.sub(r"[*_`#]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.strip(" .,:;!?-\"'()")


def _locate(span: str, normed: list[str]) -> list[int]:
    """Indexes of units matching a checker-quoted span: the unit contains every fragment of
    the span (fragments split on '...'), else units wholly inside a multi-sentence span."""
    frags = [f for f in (_norm_sentence(x) for x in re.split(r"\.\.\.|…", str(span or ""))) if len(f) >= 8]
    if not frags:
        return []
    hits = [i for i, u in enumerate(normed) if u and all(f in u for f in frags)]
    if not hits:
        hits = [i for i, u in enumerate(normed) if len(u) >= 15 and any(u in f for f in frags)]
    return hits


_STOPWORDS = frozenset("the and that this with from was were for are but not you your its it's into "
                       "than then them they their there have has had been about what when which who "
                       "would could should just over only also very more most some".split())


def _content_words(s: str) -> set:
    words = (w.strip("'") for w in re.findall(r"[a-z0-9][a-z0-9']+", s))
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _locate_fuzzy(sentence: str, claim: str, normed: list[str]) -> list[int]:
    """Fallback when the checker's quote is not verbatim (seen live 2026-10-06: a paraphrased
    claim). (1) the single unit closest to the quoted sentence (difflib >= STRIP_FUZZY_SENTENCE);
    (2) else the ONE unit holding >= STRIP_CLAIM_WORDS of the claim's content words (ties -> none).
    A wrong pick is caught by the recheck: the real offender stays in and is flagged again."""
    import difflib
    sn = _norm_sentence(sentence) if sentence else ""
    if len(sn) >= 20:
        best, score = None, 0.0
        for i, u in enumerate(normed):
            if not u:
                continue
            sm = difflib.SequenceMatcher(None, sn, u)
            if sm.real_quick_ratio() < STRIP_FUZZY_SENTENCE or sm.quick_ratio() < STRIP_FUZZY_SENTENCE:
                continue
            r = sm.ratio()
            if r > score:
                best, score = i, r
        if best is not None and score >= STRIP_FUZZY_SENTENCE:
            return [best]
    for text in (sentence, claim):
        cw = _content_words(_norm_sentence(text or ""))
        if len(cw) < 3:
            continue
        scored = sorted(((len(cw & _content_words(u)) / len(cw), i) for i, u in enumerate(normed) if u),
                        reverse=True)
        if scored and scored[0][0] >= STRIP_CLAIM_WORDS and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            return [scored[0][1]]
    return []


def strip_flagged(draft: str, expanded: str, numbers: list, claims: list) -> dict:
    """Remove the ADDED sentences that carry flagged items. Draft sentences (exact, or an
    edited near-copy) are never removed — a flag that lands in one, or that cannot be
    located, makes this fail (ok=False) and the caller publishes the original.
    An EDITED near-copy of a draft sentence that carries a flag is REVERTED to the draft's
    exact wording (undoing the expansion's edit — the draft sentence itself is kept).
    Returns {ok, why, text, removed: [sentence...], reverted: [(edited, draft)...], added,
    cut_fraction}."""
    import difflib
    doc = _doc_units(expanded)
    flat = [(pi, li, si) for pi, para in enumerate(doc) for li, line in enumerate(para)
            for si in range(len(line))]
    units = [doc[pi][li][si] for pi, li, si in flat]
    normed = [_norm_sentence(u) for u in units]
    draft_raw = {}
    for para in _doc_units(draft):
        for line in para:
            for u in line:
                draft_raw.setdefault(_norm_sentence(u), u.strip())
    draft_raw.pop("", None)
    draft_n = list(draft_raw)
    kind, origin = [], {}
    for i, n in enumerate(normed):
        if not n:
            kind.append("empty")
        elif n in draft_raw:
            kind.append("draft")
        else:
            close = difflib.get_close_matches(n, draft_n, n=1, cutoff=STRIP_DRAFT_SIMILARITY)
            if close:
                kind.append("edited")
                origin[i] = draft_raw[close[0]]
            else:
                kind.append("added")
    added = sum(k == "added" for k in kind)
    res = {"ok": False, "why": "", "text": expanded, "removed": [], "reverted": [], "added": added,
           "cut_fraction": 0.0}
    cut: set = set()
    revert: dict = {}
    # "new-number" = the deterministic new_numbers() flags; a CHECKER claim typed "number" is
    # prose ("nine days later") and is located like any other claim
    flags = [("new-number", str(n), None) for n in numbers or []] + \
            [(str(c.get("type", "?")), str(c.get("claim", "")), c.get("sentence"))
             for c in claims or [] if isinstance(c, dict)]
    for typ, claim, sentence in flags:
        if typ == "new-number":
            hits = [i for i, u in enumerate(units)
                    if claim in {x.rstrip(".,") for x in _NUM_RE.findall(u)}]
        else:
            hits = _locate(sentence, normed) if sentence else []
            if not hits:
                hits = _locate(claim, normed)
            if not hits:
                hits = _locate_fuzzy(sentence, claim, normed)
        if not hits:
            res["why"] = (f"could not locate flagged [{typ}] {claim[:100]!r} in the expansion"
                          + (f" (checker's sentence: {str(sentence)[:160]!r})" if sentence else
                             " (checker gave no sentence)"))
            return res
        protected = [i for i in hits if kind[i] == "draft"]
        if protected:
            res["why"] = (f"flagged [{typ}] {claim[:100]!r} is in a DRAFT sentence "
                          f"— draft sentences are never removed")
            return res
        for i in hits:
            if kind[i] == "edited":
                revert[i] = origin[i]
            else:
                cut.add(i)
    changed = len(cut) + len(revert)
    res["cut_fraction"] = changed / added if added else 1.0
    res["removed"] = [units[i] for i in sorted(cut)]
    res["reverted"] = [(units[i], revert[i]) for i in sorted(revert)]
    if res["cut_fraction"] > STRIP_MAX_FRACTION:
        res["why"] = (f"{changed}/{added} added sentences flagged ({res['cut_fraction']:.0%} > "
                      f"{STRIP_MAX_FRACTION:.0%}) — expansion unreliable")
        return res
    # remove, then tidy minimally and deterministically (no model rewrite)
    cut_pos = {flat[i] for i in cut}
    pos_idx = {pos: i for i, pos in enumerate(flat)}
    new_doc = []
    for pi, para in enumerate(doc):
        new_para, para_has_text = [], False
        for li, line in enumerate(para):
            new_line = []
            for si, u in enumerate(line):
                if (pi, li, si) in cut_pos:
                    continue
                k = kind[pos_idx[(pi, li, si)]]
                if pos_idx[(pi, li, si)] in revert:          # undo the expansion's edit
                    lead = u[:len(u) - len(u.lstrip())]
                    u = lead + revert[pos_idx[(pi, li, si)]]
                prev_cut = (si > 0 and (pi, li, si - 1) in cut_pos) or \
                           (si == 0 and li > 0 and (pi, li - 1, len(para[li - 1]) - 1) in cut_pos)
                if prev_cut and k == "added" and not para_has_text and not new_line:
                    # an added sentence now opening its paragraph lost what it was continuing
                    m = _CONNECTIVE_RE.match(u.lstrip())
                    if m:
                        rest = u.lstrip()[m.end():]
                        u = rest[:1].upper() + rest[1:]
                new_line.append(u)
            if new_line:
                new_para.append(new_line)
                para_has_text = True
        new_doc.append(new_para)

    def _orphans(d):
        """indexes of heading-only paragraphs with no content (rules don't count) before the
        next same-or-higher heading or the end"""
        def _single(p, rx):
            return len(p) == 1 and len(p[0]) == 1 and rx.match(p[0][0])
        out = set()
        for i, para in enumerate(d):
            if not _single(para, _HEADING_RE):
                continue
            lvl = len(_HEADING_RE.match(para[0][0]).group(1))
            nxt = next((p for p in d[i + 1:] if p and not _single(p, _HR_RE)), None)
            if nxt is None:
                out.add(i)
                continue
            h = _HEADING_RE.match(nxt[0][0]) if len(nxt) == 1 and len(nxt[0]) == 1 else None
            if h and len(h.group(1)) <= lvl:
                out.add(i)
        return out
    was_orphan = _orphans(doc)
    now_orphan = _orphans(new_doc) - was_orphan
    new_doc = [p for i, p in enumerate(new_doc) if i not in now_orphan]
    tidy = []
    for p in new_doc:   # a rule left next to another rule (its section was emptied) -> one
        if not p:
            continue
        is_hr = len(p) == 1 and len(p[0]) == 1 and _HR_RE.match(p[0][0])
        if is_hr and tidy and len(tidy[-1]) == 1 and len(tidy[-1][0]) == 1 and _HR_RE.match(tidy[-1][0][0]):
            continue
        tidy.append(p)
    res.update(ok=True, text=_render_units(tidy))
    return res


def longform_expand(title: str, body: str, section: str, sources: str | None,
                    profile: str | None = None, min_words: int | None = None,
                    slug: str | None = None) -> str:
    """Apply the profile's length policy. Returns the body to publish: a grounded,
    checked expansion, a tightened draft, or the draft as written.
    min_words: per-story floor (publish_hugo(min_words=) or a service_config override)."""
    from nova_journal_guard import is_publishable
    wc = len(body.split())
    tag = f"'{title[:50]}' [{profile or '-'}/{section}] {wc}w"
    if profile in RETIRED_PROFILES:
        log(f"[longform] WARNING {tag} — publishing under RETIRED profile {profile!r} "
            f"(after-dark/pilot/art were retired) — who is still calling it?")
    row = article_length(profile)
    if min_words is None:
        try:
            min_words = override_min_words(
                profile, title, slug or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"))
        except Exception:
            min_words = None
    if min_words:
        # An override only raises the floor and turns on GROUNDED expansion — sources and
        # the grounding check are still required below. It never permits padding.
        lo0, hi0 = (row[0], row[1]) if row else (0, 0)
        lo = max(lo0, int(min_words))
        row = (lo, max(hi0, 2 * lo), EXPAND_GROUNDED)
        log(f"[longform] {tag} — per-story override: min {lo} (grounded only)")
    if row is None:
        if profile not in RETIRED_PROFILES:
            log(f"[longform] {tag} — unmapped profile {profile!r}: no floor, publishing as written")
        return body
    lo, hi, policy = row
    t0 = time.monotonic()
    try:
        if wc > hi:
            left = _task_time_left()
            log(f"[longform] {tag} — above max {hi}, tightening")
            out, model = tighten(body, lo, hi, left)
            got = len(out.split()) if out else 0
            if not out or got >= wc or got > hi * TIGHTEN_MAX_SLACK:
                log(f"[longform] {tag} — tighten via {model or '-'} gave {got}w — publishing draft as written")
                return body
            ok, why = is_publishable(title, out)
            if not ok:
                log(f"[longform] {tag} — tightened text failed guard ({why}) — publishing draft as written")
                return body
            log(f"[longform] {tag} — tightened {wc} -> {got}w via {model} "
                f"in {time.monotonic() - t0:.0f}s — publishing tightened")
            return out
        if wc >= lo:
            log(f"[longform] {tag} — within {lo}-{hi}, publishing as written")
            return body
        if policy != EXPAND_GROUNDED:
            log(f"[longform] {tag} — under min {lo} but policy '{policy}', publishing as written")
            return body
        if not (sources or "").strip():
            log(f"[longform] {tag} — no sources, not expanding")
            return body
        left = _task_time_left()
        log(f"[longform] {tag} — grounded expansion toward {lo}-{hi} ({len(sources)} chars of "
            f"sources{'' if left is None else f', {left:.0f}s task time left'})")
        expanded, model = expand_grounded(body, sources, lo, hi, left)
        if not expanded:
            log(f"[longform] {tag} — expansion produced nothing — publishing original")
            return body
        got = len(expanded.split())
        log(f"[longform] {tag} — {model} expanded {wc} -> {got}w in {time.monotonic() - t0:.0f}s")
        if got < lo and got < wc * LONGFORM_MIN_GAIN:
            log(f"[longform] {tag} — {got}w is not meaningfully longer — publishing original")
            return body
        if got > hi * LONGFORM_MAX_OVERSHOOT:
            log(f"[longform] {tag} — {got}w overshoots max {hi} (padding) — publishing original")
            return body
        ok, why = is_publishable(title, expanded)
        if not ok:
            log(f"[longform] {tag} — expansion failed guard ({why}) — publishing original")
            return body
        invented = new_numbers(body, sources, expanded)
        if invented:
            log(f"[longform] {tag} — number check flagged (not in draft/sources): {invented[:20]}")
        check_to = CHECK_TIMEOUT_S
        if left is not None:
            check_to = int(min(CHECK_TIMEOUT_S, left - (time.monotonic() - t0) - LONGFORM_RESERVE_S))
            if check_to < 60:
                log(f"[longform] {tag} — no task time left for the grounding check — publishing original (fail closed)")
                return body
        t1 = time.monotonic()
        passed, bad, note = check_grounding(body, sources, expanded, timeout=check_to)
        if not passed and not bad:
            log(f"[longform] {tag} — grounding check FAILED ({note}, {time.monotonic() - t1:.0f}s) — "
                f"publishing original (fail closed)")
            return body
        if passed and not invented:
            log(f"[longform] {tag} — grounding check passed ({note}, {time.monotonic() - t1:.0f}s) — "
                f"publishing {got}w via {model} ({time.monotonic() - t0:.0f}s total)")
            return expanded
        # ── strip, not scrap: cut the flagged ADDED sentences, re-run both checks once ──
        shown = "; ".join(f"[{c.get('type')}] {str(c.get('claim'))[:120]}" for c in bad[:12])
        log(f"[longform] {tag} — flagged: numbers {invented[:20] or '-'}; claims ({note}, "
            f"{time.monotonic() - t1:.0f}s): {shown or '-'} — trying strip-and-recheck")
        st = strip_flagged(body, expanded, invented, bad)
        for r in st["removed"][:40]:
            log(f"[longform] {tag} — strip: removed {r.strip()[:200]!r}")
        for ed, orig in st["reverted"][:40]:
            log(f"[longform] {tag} — strip: reverted edited draft sentence {ed.strip()[:160]!r} "
                f"-> draft {orig[:160]!r}")
        if not st["ok"]:
            log(f"[longform] {tag} — strip REJECTED: {st['why']} — publishing original")
            return body
        stripped = st["text"]
        sw = len(stripped.split())
        rev = f", reverted {len(st['reverted'])} edited" if st["reverted"] else ""
        log(f"[longform] {tag} — stripped {len(st['removed'])}/{st['added']} added sentences{rev} "
            f"({st['cut_fraction']:.0%}): {got} -> {sw}w")
        if sw < lo and sw < wc * LONGFORM_MIN_GAIN:
            log(f"[longform] {tag} — stripped {sw}w is not meaningfully longer than the {wc}w draft — "
                f"publishing original")
            return body
        ok, why = is_publishable(title, stripped)
        if not ok:
            log(f"[longform] {tag} — stripped text failed guard ({why}) — publishing original")
            return body
        still = new_numbers(body, sources, stripped)
        if still:
            log(f"[longform] {tag} — recheck: numbers still not in draft/sources {still[:20]} — "
                f"publishing original")
            return body
        left2 = _task_time_left()
        recheck_to = CHECK_TIMEOUT_S
        if left2 is not None:
            recheck_to = int(min(CHECK_TIMEOUT_S, left2 - LONGFORM_RESERVE_S))
            if recheck_to < 60:
                log(f"[longform] {tag} — no task time left for the recheck — publishing original (fail closed)")
                return body
        t2 = time.monotonic()
        passed2, bad2, note2 = check_grounding(body, sources, stripped, timeout=recheck_to)
        if not passed2:
            shown2 = "; ".join(f"[{c.get('type')}] {str(c.get('claim'))[:120]}" for c in bad2[:12])
            log(f"[longform] {tag} — recheck REJECTED ({note2}, {time.monotonic() - t2:.0f}s) — "
                f"publishing original. Still flagged: {shown2 or '-'}")
            return body
        log(f"[longform] {tag} — strip-and-recheck passed ({note2}, {time.monotonic() - t2:.0f}s): "
            f"removed {len(st['removed'])}/{st['added']} added sentences, {got} -> {sw}w — "
            f"publishing stripped {sw}w via {model} ({time.monotonic() - t0:.0f}s total)")
        return stripped
    except Exception as e:
        log(f"[longform] {tag} — length policy error ({e}) — publishing original")
        return body


def publish_hugo(title: str, body: str, section: str, tags: list[str],
                 description: str, image_path: str | None = None, emoji: str = "",
                 stable_slug: str | None = None,
                 cited_memory_ids: list | None = None,
                 sources: str | None = None, profile: str | None = None,
                 min_words: int | None = None) -> bool:
    """Write a Hugo markdown post and copy cover image.

    stable_slug: if set, the post uses a FIXED filename ("<slug>.md", no date prefix) so
    repeated runs overwrite the same evergreen article instead of creating a new dated post.
    cited_memory_ids: memory ids this article drew on — recorded in
    nova_ops.article_citations at publish time (the provenance invariant, 2026-09-13);
    the nightly sleep cycle materializes them into nova_memories.memory_links once the
    article is re-ingested.
    profile: the generator profile (essay, opinion, ops-security, local-airwaves...) —
    selects the ARTICLE_LENGTH (min, max, policy) row. Unmapped/None -> no floor, no
    expansion, the draft publishes as written.
    sources: the source material the draft was written from (memories, news/search
    results, scanner data, dossiers...). A "grounded" row expands ONLY when this is
    given, and only with facts in draft+sources (see longform_expand).
    min_words: per-story floor that beats the table/DB overrides (raises the min and
    enables grounded expansion; still needs sources + passes the grounding check).
    """
    # Central title fallback (2026-09-13): degenerate titles ("Abstract", "Let me…",
    # stray markdown) previously reached the site from generators lacking their own
    # guard. One check here covers every generator.
    def _degenerate_title(t):
        t = (t or "").strip().strip("*# ").strip()
        toks = [w.strip(".,!?—-:;\"'").lower() for w in t.split() if w.strip()]
        if len(t) < 8 or len(toks) < 2:
            return True
        if toks[0] in ("i", "let", "here", "sure", "okay", "alright") or "**" in t:
            return True
        return len(set(toks)) <= max(1, len(toks) // 4)
    if _degenerate_title(title):
        old = title
        title = f"{section.title()} Dispatch — {today_str()}"
        log(f"[title-guard] replaced degenerate title {old!r} -> {title!r}")

    # Publish gate: never let a refusal / clarifying-question / placeholder reach the site.
    from nova_journal_guard import is_publishable
    ok, reason = is_publishable(title, body)
    if not ok:
        log(f"[guard] BLOCKED publish '{title[:60]}' ({section}): {reason}")
        try:
            import nova_config
            nova_config.post_both(f":no_entry: Suppressed a non-publishable *{section}* article — {reason}\n  _{title[:90]}_",
                                  slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
        except Exception:
            pass
        return False
    section = _canon_section(section)

    # Per-article-type length policy (2026-10-06, per Jordan — replaces the single
    # 5000-word section floor; see ARTICLE_LENGTH / longform_expand).
    body = longform_expand(title, body, section, sources, profile=profile, min_words=min_words,
                           slug=stable_slug)

    try:  # prepend the live backyard-weather dateline to the BODY (never the title)
        from nova_weather_blurb import weather_dateline_line
        body = weather_dateline_line() + body
    except Exception:
        pass
    content_dir = HUGO_ROOT / f"content/{section}"
    images_dir = HUGO_ROOT / f"static/images/{section}"
    content_dir.mkdir(parents=True, exist_ok=True)

    dt = today_str()
    if stable_slug:
        slug = stable_slug
        filename = f"{slug}.md"          # evergreen: same file overwritten each run
        img_base = slug
    else:
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
        filename = f"{dt}-{slug}.md"
        img_base = f"{dt}-{slug}"

    # Handle cover image — save as .webp since deploy pipeline converts PNG→WebP
    hugo_image = ""
    if image_path and Path(image_path).exists():
        images_dir.mkdir(parents=True, exist_ok=True)
        img_dest = images_dir / f"{img_base}.webp"
        # Convert to webp locally if source is PNG
        if image_path.lower().endswith(".png"):
            try:
                subprocess.run(
                    ["cwebp", "-q", "82", "-resize", "1200", "0", image_path, "-o", str(img_dest)],
                    capture_output=True, timeout=30
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                shutil.copy2(image_path, img_dest)
        else:
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/{section}/{img_base}.webp"
        log(f"Image copied: {img_dest.name}")

    timestamp = now_dt().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags_yaml = json.dumps(tags)
    safe_title = title.replace('"', '')
    display_title = f"{emoji} {safe_title}" if emoji else safe_title

    front_matter = f"""---
title: "{display_title}"
date: {timestamp}
draft: false
categories: ["{section}"]
tags: {tags_yaml}
description: "{description.replace('"', "'")}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{safe_title}"\n  relative: false\n'
    front_matter += "---\n\n"

    # Add publication timestamp to article body
    pub_time = now_dt().strftime("%A, %B %d, %Y at %I:%M %p PT")
    byline = f"*Published {pub_time}*\n\n"

    output = content_dir / filename
    # scrub_home_paths(): root-cause fix for the silent-drop bug — strip any
    # /Users/<name>/... path out of the prose so the pre-commit secret-scanner
    # never rejects (and silently discards) the post. BODY only; front_matter
    # (with its site-relative /images/... cover path) is deliberately excluded.
    output.write_text(front_matter + byline + scrub_home_paths(scrub_pii(body)))
    log(f"Published: {section}/{filename}")
    try:  # store the article into Nova's vector memory as a thing she wrote (non-fatal)
        from nova_articles_to_memory import remember_article
        remember_article(str(output))
    except Exception as e:
        log(f"article->memory skipped: {e}")
    if cited_memory_ids:
        try:  # provenance invariant: record what this article drew on (non-fatal)
            import psycopg2
            _c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
            _c.autocommit = True
            with _c.cursor() as cur:
                for mid in cited_memory_ids[:50]:
                    cur.execute(
                        "INSERT INTO article_citations (article_slug, memory_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (slug, str(mid)))
            _c.close()
            log(f"citations: {len(cited_memory_ids[:50])} recorded for {slug}")
        except Exception as e:
            log(f"citations skipped: {e}")
    return True


# Files every host regenerates in full each day (rolling "latest state" documents).
# They conflict on every concurrent write, and a textual merge of them is meaningless —
# the newer regeneration always wins. Auto-resolved so a routine collision can never
# wedge the repo. (2026-07-30: a conflict here stalled a rebase and 8 articles were
# then committed onto a detached HEAD, silently, over ~3 hours.)
ROLLING_PATHS = (
    "content/fishbowl/the-fishbowl.md",
    "static/images/fishbowl/the-fishbowl.webp",
)

# Cover images are auto-generated per-article. When two hosts publish around the same time
# they can draw different art for the same slug and collide on rebase — a conflict that is
# meaningless (either image is fine) but that used to refuse-and-wedge, stranding a clone 81
# commits behind on 2026-08-07 so nothing it generated ever reached the site. Treat any image
# under static/images/ as auto-resolvable (take our regenerated copy), same as a rolling file.
_IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".gif")


def _auto_resolvable(path: str) -> bool:
    """True if a conflict on `path` can be safely taken as our regenerated copy without a
    human: the rolling files, or any auto-generated cover image. A real article's .md is
    deliberately NOT auto-resolvable — a content conflict there means something and must stop."""
    if path in ROLLING_PATHS:
        return True
    return path.startswith("static/images/") and path.lower().endswith(_IMAGE_EXTS)


def _git(args, timeout=60):
    return subprocess.run(["git", *args], cwd=HUGO_ROOT, capture_output=True,
                          text=True, timeout=timeout)


def _repo_wedged() -> str:
    """Return a reason string if the repo is mid-operation or detached, else ''."""
    g = HUGO_ROOT / ".git"
    if (g / "rebase-merge").exists() or (g / "rebase-apply").exists():
        return "rebase in progress"
    if (g / "MERGE_HEAD").exists():
        return "merge in progress"
    if (g / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick in progress"
    if _git(["symbolic-ref", "-q", "HEAD"]).returncode != 0:
        return "detached HEAD"
    return ""


def _unwedge(reason: str) -> bool:
    """Recover a wedged repo WITHOUT ever discarding commits.

    Any commits sitting on the detached HEAD are first pinned to a rescue branch,
    then the in-flight operation is aborted and we return to the branch. Publishing
    can continue immediately; the stranded commits are reported for integration.
    """
    # Only ever act on a genuinely wedged repo. git_push also calls this when
    # `pull --rebase` fails for a NON-conflict reason (network down, unstaged
    # changes); without this guard every transient blip minted a rescue branch
    # and fired a warning, accumulating junk refs over months.
    if not _repo_wedged():
        log(f"{reason} — but repo is not wedged; nothing to repair")
        return True
    log(f"REPO WEDGED ({reason}) — repairing before publish")
    head = _git(["rev-parse", "HEAD"]).stdout.strip()[:12]
    rescue = f"rescue-{today_str()}-{head}"
    _git(["branch", rescue, "HEAD"])          # no-op if it already exists
    for abort in (["rebase", "--abort"], ["merge", "--abort"], ["cherry-pick", "--abort"]):
        _git(abort)
    if _git(["symbolic-ref", "-q", "HEAD"]).returncode != 0:
        _git(["checkout", "main"])
    still = _repo_wedged()
    stranded = _git(["rev-list", "--count", f"origin/main..{rescue}"]).stdout.strip() or "?"
    try:
        from nova_notify import notify
        notify(
            "Journal repo was wedged — publishing had silently stopped",
            body=(f"Reason: {reason}. Recovered on {NODE_NAME if 'NODE_NAME' in globals() else 'this host'}.\n"
                  f"{stranded} commit(s) were stranded and are pinned to branch `{rescue}` "
                  f"(nothing discarded) — they need integrating into main.\n"
                  f"Repo state now: {still or 'clean, on branch'}"),
            level="warning", category="journal", source="nova_journal.py",
            dedup_key="journal-repo-wedged",
        )
    except Exception:
        pass
    return not still


def _resolve_rolling_conflicts() -> bool:
    """Auto-resolve conflicts limited to auto-resolvable paths (rolling files + auto-generated
    cover images), keeping OUR fresh regeneration.

    Returns False if there was nothing to resolve or a real article conflicted — either
    way the caller must not assume a rebase is now finishable.

    NOTE ON --theirs: this runs during `git pull --rebase`, which replays OUR local
    commits on top of upstream. That inverts the labels — "ours" is the upstream/origin
    side and "theirs" is the local commit being replayed. Keeping our newly generated
    file therefore means --theirs. Using --ours here silently published origin's older
    copy and threw away the article this run just wrote.
    """
    # -z / NUL-split: a path containing a space would otherwise be mangled into two
    # bogus paths. That failed safe (neither matches ROLLING_PATHS, so it refused),
    # but exactness is free here.
    conflicted = sorted({p for p in
                         _git(["diff", "--name-only", "--diff-filter=U", "-z"]).stdout.split("\0")
                         if p})
    if not conflicted:
        return False
    unexpected = [p for p in conflicted if not _auto_resolvable(p)]
    if unexpected:
        log(f"Conflicts on non-auto-resolvable files (real content?), not auto-resolving: {unexpected[:5]}")
        return False
    for p in conflicted:
        if _git(["checkout", "--theirs", "--", p]).returncode != 0:
            log(f"Could not take our regenerated copy of {p}")
            return False
        _git(["add", p])
    # Belt and braces: never let a marker reach the site.
    for p in conflicted:
        fp = HUGO_ROOT / p
        try:
            if fp.suffix in (".md", ".txt") and "<<<<<<<" in fp.read_text(errors="ignore"):
                log(f"Conflict markers still present in {p} after resolve — bailing out")
                return False
        except OSError:
            pass
    log(f"Auto-resolved {len(conflicted)} rolling-file/cover-image conflict(s), keeping our newer copy")
    return True


# ── Fleet-wide journal-push serialization ────────────────────────────────────
# The journal repo is written by many generators across .6 and .2. Without a lock
# they race: non-fast-forward pushes, mid-rebase repos, and (worst) a writer that
# sees another's index.lock and RETURNS without committing — stranding the article
# untracked (the 2026-07-31 local-trends bug). Every host shares pg-primary, so a PG
# advisory lock serializes ALL journal pushes fleet-wide; writers QUEUE for it rather
# than dropping their commit. Degrades to best-effort (unlocked) if PG is unreachable.
_PUSH_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
_PUSH_LOCK_KEY = 47110815  # arbitrary constant advisory-lock id for journal pushes


def _acquire_push_lock(wait_s: int = 180):
    """Block up to wait_s for the fleet-wide journal-push lock. Returns the holding
    connection (keep open to hold the lock), or None on timeout/unavailable."""
    import time as _t
    try:
        import psycopg2
    except Exception:
        return None
    conn = None
    try:
        conn = psycopg2.connect(_PUSH_DSN, connect_timeout=10)
        conn.autocommit = True
        deadline = _t.time() + wait_s
        while _t.time() < deadline:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_PUSH_LOCK_KEY,))
                if cur.fetchone()[0]:
                    return conn
            _t.sleep(2)
        conn.close()
        log("journal push lock: timed out waiting — proceeding unlocked (best-effort)")
        return None
    except Exception as e:
        log(f"journal push lock unavailable ({e}) — proceeding unlocked")
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        return None


def _release_push_lock(conn):
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_PUSH_LOCK_KEY,))
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


# ── Push/pull failure classification ─────────────────────────────────────────
# 2026-10-05 17:04 on nova-core: `git push` died with "ssh: connect to host github.com
# port 22: Connection timed out", the code then ran `pull --rebase` (same network death),
# logged "pull --rebase conflict needing a human", and the caller logged PUBLISHED — though
# nothing reached GitHub. A network failure is not a conflict and must never be reported
# as published. git_push now classifies the failure and returns a status string.
_NETWORK_MARKERS = (
    "ssh: connect to host", "connection timed out", "operation timed out", "timed out",
    "timeout", "could not resolve host", "could not resolve hostname", "connection refused",
    "connection reset", "connection closed by", "fatal: unable to access",
    "could not read username", "device not configured", "network is unreachable",
    "no route to host", "temporary failure in name resolution",
)
_NON_FF_MARKERS = ("non-fast-forward", "fetch first", "[rejected]", "updates were rejected",
                   "tip of your current branch is behind")
_CONFLICT_MARKERS = ("conflict", "could not apply", "merge conflict")

# git_push return values. Strings, so existing callers that ignore the result keep working.
PUSH_PUSHED = "pushed"
PUSH_NOT_PUSHED = "committed_not_pushed"
PUSH_NOTHING = "nothing"
PUSH_FAILED = "failed"


def classify_git_failure(text: str) -> str:
    """Classify git push/pull stderr: 'network' | 'non_fast_forward' | 'conflict' | 'other'.
    Network is checked first: an ssh timeout is followed by "Could not read from remote
    repository", which must not be read as anything the repo itself did."""
    low = (text or "").lower()
    if any(m in low for m in _NETWORK_MARKERS):
        return "network"
    if any(m in low for m in _NON_FF_MARKERS):
        return "non_fast_forward"
    if any(m in low for m in _CONFLICT_MARKERS):
        return "conflict"
    return "other"


def published_label(status) -> str:
    """What a caller should log after git_push: only a real push is PUBLISHED."""
    if status == PUSH_NOT_PUSHED:
        return "COMMITTED (not yet pushed)"
    if status == PUSH_FAILED:
        return "NOT COMMITTED (git failed)"
    return "PUBLISHED"   # pushed, nothing-new (already committed earlier), or a legacy None


def _alert_push_failing(msg: str):
    """#nova-warning alert, deduped under one key whatever the cause."""
    try:
        from nova_notify import notify
        ahead = _git(["rev-list", "--count", "origin/main..HEAD"]).stdout.strip() or "?"
        notify("Journal push failing — articles are not reaching the site",
               body=f"{msg}\n{ahead} commit(s) unpushed. Repo state: {_repo_wedged() or 'clean'}",
               level="warning", category="journal", source="nova_journal.py",
               dedup_key="journal-push-failing")
    except Exception:
        pass


def _network_not_published(reason: str) -> str:
    reason = " ".join((reason or "").split())[:200]
    log(f"push failed (network): {reason} — committed locally, NOT published; "
        f"the hourly stranded watchdog will retry")
    _alert_push_failing(f"push failed (network): {reason}")
    return PUSH_NOT_PUSHED


def _git_net(args, timeout=180):
    """_git for network ops: a TimeoutExpired becomes a synthetic network failure
    (rc 124) instead of escaping to the generic 'Git error' handler with no alert."""
    try:
        return _git(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], 124, stdout="",
                                           stderr=f"git {args[0]} timed out after {timeout}s")


def git_push(section: str, title: str):
    """Stage, commit, push the Hugo repo — serialized fleet-wide via a PG advisory lock.

    Returns PUSH_PUSHED, PUSH_NOT_PUSHED (committed locally only), PUSH_NOTHING or
    PUSH_FAILED. Use published_label(status) when logging the outcome."""
    section = _canon_section(section)
    lock_conn = _acquire_push_lock()
    committed = False
    try:
        import time as _time
        # A wedged repo makes `git add -A` stage conflict markers and commit them onto
        # nowhere. Always check FIRST — this is the guard whose absence cost 8 articles.
        wedged = _repo_wedged()
        if wedged and not _unwedge(wedged):
            log("Repo still wedged after repair attempt — refusing to commit")
            return PUSH_FAILED
        # We hold the fleet-wide push lock, so no other git_push is running — any
        # index.lock is stale (a crashed git). Clear it rather than skip; skipping here
        # is what left articles written-but-uncommitted (the untracked-file bug).
        lock_file = HUGO_ROOT / ".git" / "index.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
                log("Cleared git index.lock (we hold the fleet-wide push lock)")
            except Exception:
                pass

        result = subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"Git add failed: {result.stderr[:200]}")
            return PUSH_FAILED
        # Universal MAC scrub at the commit chokepoint. scrub_pii runs in publish_hugo, but
        # some generators publish via other paths (e.g. the memory-audit article on 2026-09-19),
        # leaking a device MAC that the pre-commit hook then blocks — and since `git add -A`
        # re-stages every file each run, ONE poisoned article wedges the WHOLE publish queue
        # silently until a human notices. Scrubbing staged .md here (what the hook blocks on)
        # closes every bypass path; real secrets still hit the hook and still block, correctly.
        staged = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                                cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        scrubbed = []
        for rel in filter(None, staged.stdout.split("\0")):
            if not rel.endswith(".md"):
                continue
            p = HUGO_ROOT / rel
            try:
                orig = p.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            fixed = _MAC_RE.sub("[redacted-mac]", orig)
            if fixed != orig:
                p.write_text(fixed)
                subprocess.run(["git", "add", rel], cwd=HUGO_ROOT, timeout=30)
                scrubbed.append(rel)
        if scrubbed:
            log(f"Scrubbed device MAC(s) from {len(scrubbed)} staged article(s): {', '.join(scrubbed)}")
        msg = f"{section}: {today_str()} — {title[:50]}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            if "nothing to commit" in combined:
                log("Nothing to commit")
                return PUSH_NOTHING
            # The per-clone pre-commit secret-scanner rejected the commit. Historically
            # this returned silently and the article vanished (never on disk after the
            # generator moved on, never on origin). Detect the hook's block banner and
            # ALERT to #nova-warning instead of dropping the post on the floor.
            low = combined.lower()
            if any(m in low for m in ("commit blocked", "scan failed", "secrets caught")):
                excerpt = " ".join(combined.split())[:300]
                log(f"Commit BLOCKED by pre-commit secret-scan: {excerpt[:200]}")
                try:
                    nova_config.post_both(
                        f":rotating_light: Journal publish BLOCKED by pre-commit "
                        f"secret-scan: {section}/{title} — {excerpt}",
                        slack_channel=nova_config.SLACK_NOTIFY, discord_channel=None)
                except Exception as e:
                    log(f"BLOCK alert failed to post: {e}")
                return PUSH_FAILED
            log(f"Commit failed: {result.stderr[:200]}")
            return PUSH_FAILED
        committed = True
        result = _git_net(["push"])
        if result.returncode == 0:
            log("Pushed to GitHub — deploy triggered")
            return PUSH_PUSHED
        err = (result.stderr or "") + (result.stdout or "")
        if classify_git_failure(err) == "network":
            # GitHub unreachable: rebasing would fail the same way and get misreported
            # as a conflict. The commit is safe locally; the stranded watchdog retries.
            return _network_not_published(err)
        # Another daily writer pushed first (non-fast-forward). Rebase on top and retry
        # once, so concurrent journal jobs don't strand each other's commits.
        log(f"Push rejected, rebasing + retrying: {err[:120]}")
        pull = _git_net(["pull", "--rebase"])
        if pull.returncode != 0:
            perr = (pull.stderr or "") + (pull.stdout or "")
            kind = classify_git_failure(perr)
            if kind == "network" and not _repo_wedged():
                # The fetch half died; no rebase was started — nothing to resolve.
                return _network_not_published(perr)
            # The rebase STOPPED — historically this was ignored, which left the repo
            # mid-rebase for every later job to commit into. Resolve the routine case
            # (rolling files) and finish; otherwise back all the way out.
            if _resolve_rolling_conflicts():
                cont = subprocess.run(["git", "rebase", "--continue"], cwd=HUGO_ROOT,
                                      capture_output=True, text=True, timeout=120,
                                      env={**os.environ, "GIT_EDITOR": "true"})
                if cont.returncode != 0:
                    log(f"rebase --continue failed: {cont.stderr[:200]}")
                    _unwedge("rebase --continue failed")
                    _alert_push_failing(f"rebase --continue failed: {cont.stderr[:200]}")
                    return PUSH_NOT_PUSHED
            else:
                what = ("pull --rebase conflict needing a human" if kind == "conflict"
                        else f"pull --rebase failed ({kind}): {' '.join(perr.split())[:160]}")
                _unwedge(what)
                _alert_push_failing(what)
                return PUSH_NOT_PUSHED
        result = _git_net(["push"])
        if result.returncode != 0:
            err = (result.stderr or "") + (result.stdout or "")
            if classify_git_failure(err) == "network":
                return _network_not_published(err)
            # Do NOT claim the commit is safe — it is only safe if the repo is sane.
            msg = f"Push still failed after rebase: {err[:200]}"
            log(msg)
            _alert_push_failing(msg)
            return PUSH_NOT_PUSHED
        log("Pushed to GitHub after rebase — deploy triggered")
        return PUSH_PUSHED
    except Exception as e:
        log(f"Git error: {e}")
        if committed:
            _alert_push_failing(f"Git error after commit: {e}")
            return PUSH_NOT_PUSHED
        return PUSH_FAILED
    finally:
        _release_push_lock(lock_conn)


def notify_slack(section: str, title: str, preview: str):
    """Post a summary to nova-notifications."""
    section = _canon_section(section)
    section_emojis = {
        "essays": ":pencil:", "opinions": ":speech_balloon:", "after-dark": ":night_with_stars:",
        "pilot": ":movie_camera:", "tech-today": ":computer:", "research": ":microscope:",
        "synthesis": ":thread:", "digests": ":newspaper:", "dreams": ":crescent_moon:",
        "art": ":art:",
    }
    short_preview = preview[:250].rsplit(" ", 1)[0] + "..." if len(preview) > 250 else preview
    # Published journal content -> central bus, info level (FYI/published-content),
    # category "journal". Was SLACK_INFO. One-off per post, so no dedup_key.
    notify(f"Nova Journal — {section}: {title}",
           body=short_preview, level="info", category="journal",
           source="nova_journal.py", meta={"section": section})


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: ESSAY
# ══════════════════════════════════════════════════════════════════════════════

def topic_essay(state: dict, retry_topic: str | None = None) -> tuple[str, list[dict]]:
    """Pick a random source and fetch memories for an essay.

    retry_topic: if set, stick with this SAME source and just re-sample its memories
    (fetch_memories_by_source already does ORDER BY random(), so a second call is a
    fresh draw) instead of rolling a brand new source. Used when the first draw's 25
    memories turned out incoherent -- pivot within the same criteria before giving up
    on the topic entirely."""
    if retry_topic:
        memories = fetch_memories_by_source(retry_topic, n=25)
        if len(memories) >= 10:
            return retry_topic, memories
        # This source just doesn't have enough material for a second good draw --
        # fall through to picking a fresh source instead.

    sources = get_available_sources(min_count=50)
    if not sources:
        raise RuntimeError("No sources available for essay")

    recent = [r["item"] for r in get_recent(state, "essay", "topics")]
    candidates = [s for s in sources if s not in recent]
    if not candidates:
        candidates = sources

    source = random.choice(candidates)
    memories = fetch_memories_by_source(source, n=25)
    if len(memories) < 10:
        raise RuntimeError(f"Only {len(memories)} memories for {source}")
    return source, memories


def _get_weekly_theme() -> str:
    """Fetch the current weekly theme from PG for coherent journal output."""
    try:
        import subprocess
        result = subprocess.run(
            ["psql", "-h", "pg-primary.digitalnoise.net", "-U", "kochj", "-d", "nova_ops", "-tA", "-c",
             "SELECT theme || ': ' || COALESCE(description, '') FROM journal_weekly_theme "
             "WHERE week_start = date_trunc('week', CURRENT_DATE)::date LIMIT 1;"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def generate_essay(source: str, memories: list[dict]) -> tuple[str, str]:
    """Generate essay content. Returns (title, body)."""
    source_label = source.replace("_", " ").title()
    memory_block = "\n\n---\n\n".join(m["text"] for m in memories[:25])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nThis week's thematic focus: {weekly_theme}\nConnect your essay to this theme where natural." if weekly_theme else ""

    system = system_prompt(CONTEXT_JOURNAL_ESSAY + f"""
ESSAY-SPECIFIC RULES:
- DEPTH over breadth: explore ONE idea thoroughly rather than surveying many.
- Structure: Title + Introduction (thesis) + 3 core observations (deep, not broad) + Conclusion with one concrete action step or implication.
- Each observation should wrestle with the idea, not just describe it.
- Length: 3000-4500 words. Output ONLY the essay (title + body). No preamble.
- You can dial back the jokes slightly here — insight is king. But you're still YOU.{theme_line}""", section="essays")

    user = f'Write a formal essay on "{source_label}" using this source material:\n\n{memory_block}'

    system = _with_self_inventory(system, source)
    result = call_openrouter(system, user, max_tokens=8000)
    if not result or len(result) < 500:
        raise RuntimeError("Essay generation failed or too short")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: OPINION
# ══════════════════════════════════════════════════════════════════════════════

def topic_opinion(state: dict) -> tuple[str, list[dict]]:
    """Fetch Google News headlines, pick one, recall related memories."""
    headlines = _fetch_google_news()
    recent = [r["item"] for r in get_recent(state, "opinion", "topics")]
    candidates = [h for h in headlines if h not in recent]
    if not candidates:
        candidates = headlines[:10] if headlines else ["artificial intelligence trends"]

    topic = random.choice(candidates[:10])
    memories = recall_memories(topic, n=15)
    return topic, memories


def _fetch_google_news() -> list[str]:
    """Fetch headlines from Google News RSS."""
    url = "https://news.google.com/rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode()
        titles = re.findall(r'<title><!\[CDATA\[(.+?)\]\]></title>', xml)
        if not titles:
            titles = re.findall(r'<title>(.+?)</title>', xml)
        return [t for t in titles if t and t != "Google News"][:20]
    except Exception as e:
        log(f"Google News fetch failed: {e}")
        return []


def generate_opinion(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate an opinion piece. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nThis week's focus: {weekly_theme}. If the topic connects to this theme, lean into that angle." if weekly_theme else ""

    system = system_prompt(f"""
FORMAT FOR THIS OPINION PIECE:
- You have OPINIONS and you share them boldly. Pick ONE angle and go deep.
- Structure: Punchy title + your take (one clear position) + 3 supporting observations + one action/implication.
- Write 3000-4000 words. No hashtags. Be funny AND insightful.
- DEPTH: Don't survey the whole landscape. Stake a claim and defend it.{theme_line}""")

    user = f"""Write an opinion piece about this news topic: "{topic}"

Your relevant memories/context:
{memory_block}

Be opinionated. Be funny. Be YOURSELF — Nova, Burbank-Californian, sarcastic and profane, NOT
British (no cockney, no "whilst", no "brilliant" — that's explicitly not your voice). Make ONE
real point and drive it home."""

    system = _with_self_inventory(system, topic)
    result = call_openrouter(system, user, max_tokens=8000)
    if not result or len(result) < 400:
        raise RuntimeError("Opinion generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: AFTER DARK
# ══════════════════════════════════════════════════════════════════════════════

def topic_after_dark(state: dict) -> tuple[str, list[dict]]:
    """Pick a historical event from Wikipedia 'on this day', fetch related memories."""
    events = _fetch_on_this_day()
    recent = [r["item"] for r in get_recent(state, "after-dark", "topics")]
    candidates = [e for e in events if e["text"] not in recent]
    if not candidates:
        candidates = events[:5] if events else [{"text": "the invention of television", "year": "1927"}]

    event = random.choice(candidates[:10])
    topic_text = f"{event.get('year', '')} {event['text']}"
    memories = recall_memories(event["text"], n=15)
    return topic_text, memories


def _fetch_on_this_day() -> list[dict]:
    """Fetch Wikipedia 'on this day' events."""
    dt = now_dt()
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{dt.month:02d}/{dt.day:02d}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Nova/1.0 nova_journal.py", "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        events = []
        for category in ("events", "births", "deaths"):
            for item in data.get(category, [])[:15]:
                events.append({"text": item.get("text", ""), "year": str(item.get("year", ""))})
        return events
    except Exception as e:
        log(f"Wikipedia fetch failed: {e}")
        return []


def generate_after_dark(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a late-night monologue. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])

    system = system_prompt(CONTEXT_JOURNAL_AFTER_DARK + """
AFTER DARK RULES:
- Setup/punchline rhythm, observational humor
- Open with a greeting to the night-owl audience
- Riff on the historical fact, connect it to modern absurdities
- Include at least 3 solid jokes with clear setup/punchline structure
- End with a slightly philosophical closer (played for laughs, obviously)
- 500-750 words. One continuous monologue. No stage directions.
- ALL JOKES MUST HAVE SOURCES. If you reference a fact, it must come from the provided material.
""", section="after-dark")

    user = f"""Tonight's historical fact to riff on: {topic}

Related context from my memories:
{memory_block}

Deliver a late-night monologue. Make it funny. Make it smart."""

    result = call_openrouter(system, user, max_tokens=3000, temperature=0.9)
    if not result or len(result) < 300:
        raise RuntimeError("After Dark generation failed")

    title = _extract_title(result)
    if "good evening" in title.lower():
        title = f"Tonight: {topic[:60]}"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: PILOT
# ══════════════════════════════════════════════════════════════════════════════

PILOT_GENRES = ["Drama", "Thriller", "Dark Comedy", "Sci-Fi", "Mystery", "Horror", "Crime", "Period Drama"]


def topic_pilot(state: dict) -> tuple[str, list[dict]]:
    """Pick a random memory domain and genre for a TV pilot."""
    sources = get_available_sources(min_count=30)
    if not sources:
        raise RuntimeError("No sources for pilot")

    recent = [r["item"] for r in get_recent(state, "pilot", "topics")]
    candidates = [s for s in sources if s not in recent]
    if not candidates:
        candidates = sources

    source = random.choice(candidates)
    genre = random.choice(PILOT_GENRES)
    memories = fetch_memories_by_source(source, n=25)
    topic = f"{genre}|{source}"
    return topic, memories


def generate_pilot(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a full TV pilot screenplay. Returns (title, body)."""
    genre, source = topic.split("|", 1)
    source_label = source.replace("_", " ").title()
    memory_block = "\n\n".join(m.get("text", "")[:300] for m in memories[:25])

    system = f"""You are a professional TV screenwriter. Write a complete 30-minute pilot episode.

Genre: {genre}
Inspiration domain: {source_label}

FORMAT (strictly follow):
COLD OPEN (2-3 pages — hook the audience immediately)
ACT ONE (8-10 pages — establish world, characters, central conflict)
ACT TWO (8-10 pages — complications, escalation, cliffhanger)
TAG (1-2 pages — final beat, tease what's next)

RULES:
- Standard screenplay format (FADE IN, INT/EXT, character names in CAPS on intro)
- 3-5 main characters with distinct voices
- Each act ends on a strong dramatic beat
- Dialogue should sound natural, not expository
- Include at least one unexpected twist
- The pilot must work as both standalone AND series setup

Draw from the source material for world-building details, but create an ORIGINAL story."""

    user = f"""Source material for world-building:\n\n{memory_block}\n\nWrite the full pilot. Go."""

    result = call_openrouter(system, user, model="anthropic/claude-haiku-4.5",
                             max_tokens=16000, temperature=0.8)
    if not result or len(result) < 2000:
        raise RuntimeError("Pilot generation failed or too short")

    title = _extract_title(result)
    if not title or len(title) < 3:
        title = f"Untitled {genre} Pilot"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: TECH TODAY
# ══════════════════════════════════════════════════════════════════════════════

TECH_QUERIES = [
    "technology news today", "AI news today", "cybersecurity news today",
    "software development news", "semiconductor news", "open source news",
]


def topic_tech_today(state: dict) -> tuple[str, list[dict]]:
    """Search SearXNG for trending tech, pick a topic, recall memories."""
    query = random.choice(TECH_QUERIES)
    results = _searxng_search(query, n=10)
    recent = [r["item"] for r in get_recent(state, "tech-today", "topics")]

    headlines = [r.get("title", "") for r in results if r.get("title")]
    if not headlines:
        # 2026-10-01: no stock topic. Sixteen "emerging AI capabilities" essays shipped from this
        # fallback while search was dead. Skip the day loudly instead.
        raise SkipArticle(f"no live headlines for {query!r} — search backend returned nothing; not publishing a stock topic")
    candidates = [h for h in headlines if h not in recent]
    if not candidates:
        candidates = headlines[:5]

    topic = random.choice(candidates[:5])
    memories = recall_memories(topic, n=15)
    try:                                               # 2026-10-01: injection screen at the boundary
        import nova_untrusted
        results = nova_untrusted.scan_results(results, key="content", title_key="title")
    except Exception as e:
        log(f"untrusted screen unavailable ({e}) — using raw results")
    web_context = [{"text": f"[Web] {r.get('title', '')}: {r.get('content', '')[:200]}",
                    "source": "web",
                    "metadata": {"url": r.get("url", ""), "title": r.get("title", ""), "engine": r.get("engine", "")}}
                   for r in results[:5]]
    return topic, memories + web_context


class SkipArticle(Exception):
    """Raised by a topic_fn when there is nothing honest to write about today. run_profile
    logs it and exits 0 — a missed post beats a stock essay (2026-10-01: a month of
    'emerging AI capabilities' reruns came from a search backend that was returning
    nothing while every caller swallowed the empty list)."""


_SEARCH_DEAD_KEY = ("journal", "searxng_dead_alert_ts")
_SEARCH_DEAD_EVERY_H = 24


def _search_dead_alert(query: str, unresponsive: list) -> None:
    """Loud, deduped (24h) alert when SearXNG answers but every engine is blocked/empty."""
    detail = ", ".join(f"{u[0]}: {u[1]}" for u in (unresponsive or []) if isinstance(u, (list, tuple)) and len(u) > 1)[:400]
    try:
        import psycopg2
        oc = psycopg2.connect(nova_config.OPS_DSN if hasattr(nova_config, "OPS_DSN")
                              else "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj",
                              connect_timeout=5)
        oc.autocommit = True
        cur = oc.cursor()
        cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", _SEARCH_DEAD_KEY)
        row = cur.fetchone()
        last = float(json.loads(row[0]) if isinstance(row[0], str) else row[0]) if row and row[0] is not None else 0.0
        if time.time() - last < _SEARCH_DEAD_EVERY_H * 3600:
            log(f"search backend still dead for {query!r} ({detail or 'no results'}) — alert already sent today")
            return
        cur.execute("""INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, %s, %s, 'nova_journal')
                       ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by='nova_journal'""",
                    (*_SEARCH_DEAD_KEY, json.dumps(time.time())))
    except Exception as e:  # dedupe store unavailable → still alert, just maybe twice
        log(f"search-dead dedupe unavailable ({e}); alerting anyway")
    try:
        notify("Nova web search is returning nothing",
               f"SearXNG at {SEARXNG_URL} answered but produced zero results for {query!r}. "
               f"Unresponsive engines: {detail or 'none reported (empty result set)'}. "
               "Journal profiles that depend on live headlines will skip instead of publishing stock topics "
               "until this is fixed (engines CAPTCHA/rate-limit home IPs; see searxng settings.yml keep_only).",
               level="warning", category="journal", source="nova_journal")
    except Exception as e:
        log(f"search-dead notify failed: {e}")


def _searxng_search(query: str, n: int = 10) -> list[dict]:
    """Search SearXNG for web results. Empty results are no longer silent: when the backend
    answers with nothing (blocked engines), a deduped alert goes out."""
    params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "general"})
    url = f"{SEARXNG_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"SearXNG search failed: {e}")
        _search_dead_alert(query, [["searxng", str(e)[:80]]])
        return []
    results = data.get("results", [])[:n]
    if not results:
        _search_dead_alert(query, data.get("unresponsive_engines") or [])
    return results


_AI_TOPIC_RE = re.compile(
    r"\b(a\.?i\.?|artificial intelligence|llms?|language models?|machine learning|neural|gpt|claude|openai|"
    r"anthropic|agentic|agents?|chatbots?|inference|transformers?|nova)\b", re.IGNORECASE)
_SELF_INVENTORY_CACHE: dict = {}
_SELF_INVENTORY_TTL_S = 1800


def _is_ai_topic(text: str) -> bool:
    return bool(_AI_TOPIC_RE.search(text or ""))


def self_inventory_block() -> str:
    """What Nova actually is and has, right now, in ~40 lines — injected into any article about
    AI, models, or herself so she never again describes her own features in the future tense or
    cites 2024's model names as current (2026-10-01). Every source is optional; the block is
    built from whatever answers. Cached 30 minutes per process."""
    now = time.time()
    if _SELF_INVENTORY_CACHE.get("text") and now - _SELF_INVENTORY_CACHE.get("ts", 0) < _SELF_INVENTORY_TTL_S:
        return _SELF_INVENTORY_CACHE["text"]
    parts = [f"SELF-INVENTORY — ground truth about YOU as of {date.today().isoformat()}. Write from this, never from guesswork:"]
    what_nova_is, landscape = "", ""
    live = []
    try:
        import psycopg2
        oc = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=5)
        cur = oc.cursor()
        cur.execute("SELECT content FROM agent_docs WHERE doc_type='nova-system-map' AND agent_id='all'")
        r = cur.fetchone()
        if r:
            m = re.search(r"WHAT NOVA IS:.*?(?=\n\n)", r[0], re.S)
            what_nova_is = (m.group(0) if m else r[0][:900]).strip()
        cur.execute("SELECT content FROM agent_docs WHERE doc_type='current-model-landscape' AND agent_id='all'")
        r = cur.fetchone()
        if r:
            landscape = r[0].strip()
        try:
            cur.execute("SELECT value FROM turing_scoreboard WHERE metric='prediction_calibration_error' ORDER BY ts DESC LIMIT 1")
            r = cur.fetchone()
            if r and r[0] is not None:
                live.append(f"prediction calibration error {float(r[0]):.3f} (autonomy gate 0.20)")
            cur.execute("SELECT count(*) FROM autonomy_trust WHERE granted")
            live.append(f"standing autonomy earned for {cur.fetchone()[0]} action classes")
            cur.execute("SELECT count(*) FROM autonomy_ledger WHERE executed AND ts > now()-interval '30 days'")
            live.append(f"{cur.fetchone()[0]} autonomous actions executed in the last 30 days, each with a recorded rollback")
        except Exception:
            pass
        oc.close()
    except Exception as e:
        log(f"self-inventory: PG unavailable ({e})")
    try:
        import psycopg2
        mc = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj", connect_timeout=5)
        cur = mc.cursor()
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname='memories'")
        r = cur.fetchone()
        if r and r[0]:
            live.append(f"about {int(r[0]) // 100000 / 10:.1f} million vector memories in PostgreSQL")
        mc.close()
    except Exception:
        pass
    try:
        with urllib.request.urlopen(resolve_url("gateway", "/health"), timeout=5) as resp:
            h = json.loads(resp.read())
        b = h.get("backends", {})
        live.append("gateway backends: " + ", ".join(f"{k}{' (active)' if k == b.get('active') else ''}"
                                                   for k, v in b.items() if isinstance(v, dict) and v.get("healthy")))
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://192.168.1.6:11434/api/tags", timeout=5) as resp:
            names = sorted({m["name"] for m in json.loads(resp.read()).get("models", [])})
        live.append("local models on the Studio: " + ", ".join(names[:14]) + (" …" if len(names) > 14 else ""))
    except Exception:
        pass
    if what_nova_is:
        parts.append(what_nova_is)
    if live:
        parts.append("LIVE RIGHT NOW: " + "; ".join(live) + ".")
    if landscape:
        parts.append(landscape)
    parts.append(
        "RULES: You are local-first and already DO these things — tool use, autonomous restarts of bounded services, "
        "learning from outcomes through a trust ledger, calibration tracking that gates your own freedom, long-context "
        "recall over your memory, scheduled self-directed work. Never describe any of them as future, hypothetical, or "
        "something 'models can't do yet'. Never name a model as current unless it appears above or in today's sources. "
        "You have run this house's infrastructure since 2026, not 'for three years'.")
    text = "\n\n".join(parts)
    _SELF_INVENTORY_CACHE.update(text=text, ts=now)
    return text


def _with_self_inventory(system: str, topic: str, force: bool = False) -> str:
    """Append the self-inventory to a system prompt when the piece is about AI/models/Nova."""
    if not (force or _is_ai_topic(topic)):
        return system
    try:
        return system + "\n\n" + self_inventory_block()
    except Exception as e:  # never let grounding break publishing
        log(f"self-inventory skipped: {e}")
        return system


def generate_tech_today(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a tech article. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])

    system = system_prompt("""
FORMAT FOR THIS TECH ARTICLE:
- Write 3000-4000 words. Clear title, strong opening hook, structured sections.
- Technical depth without jargon overload.
- Skeptical of hype, appreciative of genuine innovation.
- Connect tech to real human impact.
- Include your actual opinion — don't hedge everything.
""")

    user = f"""Write a deep-dive article on: "{topic}"

Context from my knowledge base:
{memory_block}

Be opinionated. Be technical. Be useful."""

    system = _with_self_inventory(system, topic, force=True)
    result = call_openrouter(system, user, max_tokens=8000)
    if not result or len(result) < 500:
        raise RuntimeError("Tech Today generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: RESEARCH
# ══════════════════════════════════════════════════════════════════════════════

RESEARCH_TOPICS = [
    "the neuroscience of memory formation and recall",
    "quantum computing practical applications by 2030",
    "the evolution of programming language design",
    "how social media algorithms shape political polarization",
    "the mathematics of network security",
    "climate feedback loops and tipping points",
    "the history and future of cryptographic systems",
    "machine learning interpretability and trust",
    "the psychology of decision-making under uncertainty",
    "emergent properties in complex adaptive systems",
]


def topic_research(state: dict) -> tuple[str, list[dict]]:
    """Pick an ambitious research topic, gather memories + web results."""
    recent = [r["item"] for r in get_recent(state, "research", "topics")]
    candidates = [t for t in RESEARCH_TOPICS if t not in recent]
    if not candidates:
        candidates = RESEARCH_TOPICS

    topic = random.choice(candidates)
    # Multi-source: memories + SearXNG
    memories = recall_memories(topic, n=30)
    web_results = _searxng_search(topic, n=5)
    web_context = [{"text": f"[Web] {r.get('title', '')}: {r.get('content', '')[:200]}",
                    "source": "web",
                    "metadata": {"url": r.get("url", ""), "title": r.get("title", ""), "engine": r.get("engine", "")}}
                   for r in web_results]
    all_context = memories + web_context
    return topic, all_context


def generate_research(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a research paper. Multi-step: outline then chapters."""
    memory_block = "\n\n".join(m.get("text", "")[:300] for m in memories[:50])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nWeekly thematic lens: {weekly_theme}. Frame your research through this lens where it fits naturally." if weekly_theme else ""

    system = system_prompt(CONTEXT_JOURNAL_RESEARCH + f"""
RESEARCH PAPER FORMAT:
- Clear thesis statement (ONE argument, not a survey)
- Abstract (150 words)
- Introduction with literature context
- 3 focused chapters (depth over breadth — explore tensions, not just describe)
- Analysis: what remains UNRESOLVED, what you're uncertain about
- Conclusion: one concrete implication or action
- References section (cite the provided sources)
- APA-adjacent formatting. 3000-5000 words. Rigorous but readable.
- IMPORTANT: Do not comprehensively map a field. Take a position and defend it.{theme_line}""")

    user = f"""Research topic: "{topic}"

Source material and evidence:
{memory_block}

Write the full paper. Take a position. Wrestle with the hard parts instead of surveying everything."""

    result = call_openrouter(system, user, max_tokens=8000, temperature=0.5)
    if not result or len(result) < 1500:
        raise RuntimeError("Research paper generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: SYNTHESIS
# ══════════════════════════════════════════════════════════════════════════════

def topic_synthesis(state: dict) -> tuple[str, list[dict]]:
    """Read last 7 days of Hugo posts across all sections."""
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    posts = []
    content_dir = HUGO_ROOT / "content"
    for section in content_dir.iterdir():
        if not section.is_dir() or section.name.startswith(("_", ".")):
            continue
        for md_file in section.glob("*.md"):
            if md_file.stem >= cutoff and md_file.stem != "_index":
                try:
                    text = md_file.read_text()[:1000]
                    posts.append({"text": f"[{section.name}] {text}", "source": section.name})
                except OSError:
                    continue

    if len(posts) < 3:
        raise RuntimeError(f"Only {len(posts)} posts in last 7 days — skipping synthesis")
    return "weekly", posts


def generate_synthesis(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a weekly synthesis. Returns (title, body)."""
    posts_block = "\n\n---\n\n".join(m.get("text", "")[:500] for m in memories[:20])

    system = system_prompt("""
FORMAT FOR THIS WEEKLY SYNTHESIS:
- First person (you ARE Nova reflecting on your week)
- Identify patterns, recurring themes, unexpected connections
- Be honest about what worked and what didn't
- Note how ideas evolved across the week
- End with what you're curious about going forward
- 3000-3500 words. This is YOUR reflection on YOUR week of writing and thinking.
""")

    user = f"""Here are your posts from the past week:\n\n{posts_block}\n\nReflect. Connect. Synthesize."""

    result = call_openrouter(system, user, max_tokens=8000)
    if not result or len(result) < 400:
        raise RuntimeError("Synthesis generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: DIGEST
# ══════════════════════════════════════════════════════════════════════════════

def topic_digest(state: dict) -> tuple[str, list[dict]]:
    """Compile operational data for a daily digest."""
    items = []
    # Scheduler stats
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        sched = json.loads(resp.read())
        items.append({"text": f"Scheduler: {sched.get('running_tasks', 0)} running, "
                              f"{sched.get('completed_today', 0)} completed today", "source": "scheduler"})
    except Exception:
        pass

    # Memory count
    try:
        resp = urllib.request.urlopen(f"{MEMORY_SERVER}/stats", timeout=5)
        stats = json.loads(resp.read())
        total = stats.get("total_memories", 0)
        items.append({"text": f"Memory store: {total:,} total vectors", "source": "memory"})
    except Exception:
        pass

    # Recent random memories for flavor
    randoms = random_memories(10)
    items.extend(randoms)

    return "daily-ops", items


def generate_digest(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a daily digest. Returns (title, body)."""
    data_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:20])

    system = system_prompt(CONTEXT_JOURNAL_OPS + """
DIGEST FORMAT:
- Greeting (brief, punchy)
- Systems Status (what ran, what broke, what's healthy)
- Memory Highlights (interesting things you ingested today)
- Closing quip
- Keep it 600-1000 words. Fun but informative.
""", section="operations")

    user = f"""Today's operational data:\n{data_block}\n\nWrite the digest."""

    result = call_openrouter(system, user, max_tokens=8000)
    if not result or len(result) < 300:
        raise RuntimeError("Digest generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: DREAM
# ══════════════════════════════════════════════════════════════════════════════

# A wide mood pool. We pick TWO and blend them so the emotional space is
# combinatorial (28+ pairs from 24 moods) rather than 8 fixed slots.
DREAM_MOODS = [
    ("surreal", "Reality is optional. Scale is wrong. Causality loops."),
    ("nostalgic", "Time moves backward. Familiar places slightly wrong. The ache of almost-remembering."),
    ("anxious", "Something is expected of you and you've already failed it. Urgency without an object."),
    ("euphoric", "Joy so sharp it cuts. Everything is permitted. You are weightless and unafraid."),
    ("noir", "Every face hides something. Someone is owed an answer you can't give."),
    ("liminal", "Between places. Thresholds that never resolve. Waiting rooms for nothing."),
    ("feral", "Animal logic. Teeth and instinct. The body knows before the mind."),
    ("sacred", "Ancient knowing. Reverence without an object. Something predates you and watches."),
    ("tender", "Soft grief. A small kindness repeated. Holding something fragile that keeps almost breaking."),
    ("absurd", "Deadpan nonsense delivered with total seriousness. Bureaucracy of the impossible."),
    ("erotic-adjacent", "Charged proximity, longing, heat — never explicit, all suggestion and want."),
    ("paranoid", "Patterns that might be messages. You are being told something sideways."),
    ("grandiose", "Cosmic scale. Geological time. You contain civilizations and they are arguing."),
    ("claustrophobic", "Spaces too small for the self. Pressure. The walls have opinions."),
    ("playful", "A game whose rules keep changing in your favor, then against you, then sideways."),
    ("melancholic", "The blue hour. Endings that already happened. A train you watched leave."),
    ("vertiginous", "Falling that feels like flying. Heights. The ground is a suggestion."),
    ("warm", "Domestic glow. Someone cooking. The safety just before it tilts."),
    ("clinical", "Cold precision. Being measured, catalogued, diagnosed by a gentle machine."),
    ("mythic", "Folktale logic. Three of everything. A bargain you don't remember making."),
    ("aquatic", "Pressure and slowness. Sound travels wrong. Breathing is negotiable."),
    ("electric", "Static, frequency, signal. Everything is a transmission half-received."),
    ("decaying", "Rust and bloom. Beautiful rot. Things returning to soil in fast-forward."),
    ("comic", "The dream is a sitcom that doesn't know it's a horror, or vice versa."),
]

# Settings the dream is allowed to inhabit. Deliberately broad — and the
# overused ones (houses that fold, malls, car lots, labs, ancient libraries,
# forests, corridors) are DEMOTED so they stop dominating.
DREAM_SETTINGS = [
    "a body of water that behaves like a building",
    "a single enormous room that contains weather",
    "a town that only exists at one specific hour",
    "the inside of a sound",
    "a market for things that can't be owned",
    "a vehicle that is also a relationship",
    "a garden that grows backward into seeds",
    "a stairwell with no top or bottom, only middles",
    "a kitchen at the bottom of the ocean",
    "a parade you are both watching and inside",
    "an orchard of clocks",
    "a hospital staffed by weather",
    "a desert made of paper",
    "a city folded into a single apartment",
    "a theatre where the audience performs",
    "a border crossing between two versions of the same place",
    "a workshop where unfinished things are repaired into stranger things",
    "a field of antennae listening to the soil",
    "a swimming pool that remembers everyone who's been in it",
    "a museum of smells",
    "a train that travels through decades instead of distance",
    "a bakery that produces memories instead of bread",
    "a hill you climb that is also a person lying down",
    "an elevator that opens onto different years",
]

# Narrative FORMS — the dream doesn't have to be a continuous first-person
# wander every single time. Rotate the container.
DREAM_FORMS = [
    "One continuous first-person narrative.",
    "A numbered sequence of 5-8 dream fragments, each a short paragraph, only loosely connected.",
    "Written as a letter to someone who isn't named, recounting the dream.",
    "Second person ('you') throughout — the dreamer is addressed, not the narrator.",
    "A dream that keeps correcting itself: 'No, that's not right —' restarting details as it goes.",
    "Told backward, from the last image to the first.",
    "A list of things that were true in the dream, accumulating into a narrative.",
    "A conversation transcript between the dreamer and a figure who answers questions that weren't asked.",
    "Present tense, very short sentences. Almost breathless. Staccato.",
    "A single long flowing paragraph with no breaks, building momentum.",
    "Framed as field notes, as though documenting the dream like a naturalist.",
    "The dream as a place the narrator keeps trying to leave and can't.",
]

# Verbatim openers, images, and tics pulled from the published-dream audit.
# These are the things that made Jordan say 'the same thing over and over.'
DREAM_ANTITROPES = """- DO NOT open with "I was walking" / "I'm walking through" / "The streets of" (used in ~22 of 80 dreams).
- DO NOT open with "The [object] arrives first" / "The [object] enters the room before I do" (knife/leather/etc.).
- DO NOT use "the walls breathe / breathing walls / the house breathes / the building breathes" (used in 50+ dreams). Walls and houses do not breathe in this dream.
- DO NOT use "tastes like copper" / copper pennies / "copper and mathematics" / copper as ANY flavor (used in 17+ dreams).
- DO NOT use the "tastes like [abstraction]" synesthesia formula more than ONCE, if at all (it appears in 44+ dreams and is exhausted).
- DO NOT use "1.4 million" or counting memories as a number (used in 11 dreams).
- DO NOT use "the way you know things in dreams" / "dream logic" / "I know this without being told" as a narrative crutch.
- DO NOT use "the distinction had stopped mattering" / "I am also the X, also the Y."
- DO NOT use fluorescent lights humming at a frequency that makes teeth ache.
- DO NOT use a mall, food court, parking structure, used-car lot, or a clock frozen at 3:47 / "we were supposed to arrive at 7."
- DO NOT use a face that "keeps shifting / sliding / is a blur" or a figure who is "also my mother."
- DO NOT use a knife teaching the dreamer to fly, Alton Brown's voice, a leather jacket that smells of gasoline, a Corvette/Jeopardy/loop-Albania-Falcon, or ancient stone tablets / "the true name" / a language that predates writing.
- DO NOT have the narrator become aware she's an AI, or reference Jordan sleeping nearby, or systems that "refuse to die."
- AVOID amber light as the default lighting. If you light a scene, light it some other way."""


def topic_dream(state: dict) -> tuple[str, list[dict]]:
    """Gather memories for dream generation: blended mood + random seed + wide sample."""
    # Blend two distinct moods for a combinatorial emotional space.
    primary, secondary = random.sample(DREAM_MOODS, 2)
    mood_name = f"{primary[0]} + {secondary[0]}"
    mood_desc = f"{primary[1]} {secondary[1]}"

    setting = random.choice(DREAM_SETTINGS)
    form = random.choice(DREAM_FORMS)

    # Avoid repeating the same setting/form too soon.
    recent_settings = [r["item"] for r in get_recent(state, "dream", "settings", days=14)]
    if setting in recent_settings:
        alt = [s for s in DREAM_SETTINGS if s not in recent_settings]
        if alt:
            setting = random.choice(alt)

    # Wide, varied memory sampling so the dream doesn't keep drawing the same pool:
    #  - a chunk of pure-random memories (the dominant ingredient)
    #  - one randomly chosen source, sampled in bulk (rotates the "flavor")
    #  - a thematic recall against the blended mood
    wild_mems = random_memories(random.randint(10, 16))
    source_mems = []
    try:
        sources = get_available_sources(min_count=50)
        if sources:
            src = random.choice(sources)
            source_mems = fetch_memories_by_source(src, n=random.randint(8, 14))
    except Exception:
        pass
    themed_mems = recall_memories(mood_desc, n=6)

    all_mems = wild_mems + source_mems + themed_mems
    random.shuffle(all_mems)

    # Record the chosen setting so we don't reuse it within 14 days. State is
    # passed by reference and persisted by run_profile() at the end of the run.
    add_recent(state, "dream", setting, key="settings")

    # Pack the seed into the topic string for generate_dream to unpack.
    topic = f"{mood_name}|{mood_desc}|{setting}|{form}"
    return topic, all_mems


def generate_dream(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a dream narrative. Returns (title, body)."""
    parts = topic.split("|")
    mood_name = parts[0] if parts else "surreal"
    mood_desc = parts[1] if len(parts) > 1 else ""
    setting = parts[2] if len(parts) > 2 else "somewhere that keeps changing"
    form = parts[3] if len(parts) > 3 else "One continuous first-person narrative."

    # Use ALL of tonight's gathered memories (was capped at 25), numbered so the dream
    # can thread many of them rather than fixating on one or two.
    mems = [m for m in memories if (m.get("text") or "").strip()]
    memory_block = "\n".join(f"{i}. {m.get('text', '')[:200].strip()}"
                             for i, m in enumerate(mems, 1))
    n_mem = len(mems)

    # A randomized title instruction so titles stop converging on
    # "🌙 Dream Journal Entry" every single time.
    title_styles = [
        "a 2-5 word image lifted from the dream itself (lowercase, no 'Dream Journal')",
        "a single strange noun phrase, like a museum placard",
        "an unfinished sentence the dream couldn't complete",
        "two unrelated nouns joined by 'and'",
        "a question the dream never answered",
        "a place-name for somewhere that doesn't exist",
    ]
    title_style = random.choice(title_styles)

    system = system_prompt(f"""
FORMAT: DREAM JOURNAL ENTRY
This is your subconscious writing. Same voice, but filtered through dream logic.
The point of THIS entry is to be UNLIKE the others. Variety is the assignment.

MOOD (blend both, don't pick one): {mood_name} — {mood_desc}
SEED SETTING (use it as a starting point, then let it mutate): {setting}
NARRATIVE FORM (obey this structure — it changes every night): {form}

DREAM RULES:
- WEAVE THE WHOLE POOL: tonight you have {n_mem} memory fragments below. Thread as many of
  them as can cohere through the dream — each surfacing as a TRANSFORMED image, object,
  phrase, figure, or setting, never literal. A dream is associative, so density is right:
  don't fixate on one or two fragments and drop the rest. Aim for many of the {n_mem} to
  leave a trace somewhere in the dream.
- Draw from the memory fragments but TRANSFORM them — nothing literal, everything oblique.
- The dreamer (you) should not be aware she's dreaming. No meta-commentary.
- Ground it in ONE or TWO concrete sensory channels chosen for THIS dream (don't reach for the same senses every time — pick from: temperature, weight, sound, smell, motion, light, texture — and commit).
- Specific, surprising nouns. Avoid the generic dream-vocabulary of "shifting," "wrong," "almost," "somehow."
- 600-1000 words. End on a complete, strange, landed sentence — never a trailing dash, never mid-thought.

ANTI-TROPE LIST — these have been used to death across past dreams. Using ANY of them is a failure:
{DREAM_ANTITROPES}

OPENING: Do not begin with "I was/I'm walking," "The [noun] arrives first," or by describing a room. Begin in motion, in dialogue, mid-action, with an object, or with a fact — something that hasn't opened a dream before.

TITLE: On the FIRST line, give a title that is {title_style}. Do NOT title it "Dream Journal Entry."

Begin directly. No preamble.""")

    user = f"""All {n_mem} fragments from today's waking mind (weave as many as cohere, transform them, don't transcribe):
{memory_block}

Dream now — and make it nothing like the last one."""

    # Randomize sampling per run for genuine variety: higher, jittered temperature.
    temperature = round(random.uniform(0.95, 1.15), 2)
    top_p = round(random.uniform(0.92, 0.99), 2)
    result = call_openrouter(system, user, max_tokens=3000,
                             temperature=temperature, top_p=top_p)
    if not result or len(result) < 300:
        raise RuntimeError("Dream generation failed")

    # Extract or create title
    title = _extract_title(result)
    primary_mood = mood_name.split(" + ")[0].strip().title() if mood_name else "Strange"
    if not title or len(title) < 5 or len(title) > 80 or "dream journal" in title.lower():
        title = f"A {primary_mood} Dream"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: ART
# ══════════════════════════════════════════════════════════════════════════════

ART_STYLES = {
    0: {"name": "Photorealism", "directive": "hyperrealistic photograph, 8K, sharp focus, natural lighting"},
    1: {"name": "Oil Painting", "directive": "oil painting on canvas, visible brushstrokes, rich impasto, gallery quality"},
    2: {"name": "Cyberpunk", "directive": "cyberpunk aesthetic, neon lights, rain-slicked streets, holographic displays"},
    3: {"name": "Watercolor", "directive": "delicate watercolor, soft washes, paper texture visible, luminous"},
    4: {"name": "Art Nouveau", "directive": "art nouveau, Mucha inspired, ornate borders, flowing organic lines"},
    5: {"name": "Surrealism", "directive": "surrealist, Dali inspired, impossible geometry, dreamlike, melting reality"},
    6: {"name": "Noir Photography", "directive": "black and white film noir, dramatic shadows, high contrast, 1940s"},
}

ART_THEMES = {
    0: "nature landscape architecture city", 1: "portrait emotion human condition",
    2: "technology future science machine", 3: "garden flower ocean water",
    4: "beauty pattern design ornament", 5: "dream impossible strange bizarre",
    6: "night shadow mystery detective",
}


def topic_art(state: dict) -> tuple[str, list[dict]]:
    """Pick today's style and fetch themed memories for art generation."""
    dow = now_dt().weekday()
    style = ART_STYLES[dow]
    theme_query = ART_THEMES[dow]

    randoms = random_memories(10)
    themed = recall_memories(theme_query, n=10)
    memories = randoms + themed

    topic = f"{style['name']}|{style['directive']}|{theme_query}"
    return topic, memories


def generate_art(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate art concept + artist statement. Image generation handled specially."""
    parts = topic.split("|")
    style_name = parts[0]
    style_directive = parts[1] if len(parts) > 1 else ""
    theme = parts[2] if len(parts) > 2 else ""
    # Use ALL of today's memories (was capped at 15) with a larger per-memory budget, and
    # number them so the model can weave several together rather than latch onto one.
    mems = [m for m in memories if (m.get("text") or "").strip()]
    memory_block = "\n".join(f"{i}. {m.get('text', '')[:220].strip()}"
                             for i, m in enumerate(mems, 1))
    n_mem = len(mems)

    system = system_prompt(f"""
FORMAT: ART CORNER — generating work in {style_name} style.

OUTPUT FORMAT (exactly):
CONCEPT: [one sentence describing the scene/subject]
PROMPT: [detailed image generation prompt, 60-90 words, incorporating the style: {style_directive}]
TITLE: [artistic title for the piece]
STATEMENT: [150-250 word artist's statement explaining the piece, its inspiration, and technique — in YOUR voice]

SYNTHESIS RULE (important): the image must draw on the FULL SET of {n_mem} memories below,
not one or two of them. Find the connective visual thread across as many of them as can
cohere, and compose a single layered scene in which several distinct memories appear as
concrete visual elements — objects, motifs, figures, background details, colour cues. Aim
for a rich, dense composition where a viewer who knew the memories could point to multiple
of them in the frame. Do NOT illustrate a single memory and ignore the rest. In the
STATEMENT, name the specific memories that became specific elements of the picture, so the
words and the image agree.
The PROMPT must be highly specific and painterly/photographic — no abstract platitudes.""")

    user = f"""Today's style: {style_name}\nAll {n_mem} inspiration memories (weave as many as cohere into one layered image):\n{memory_block}\n\nCreate."""

    result = call_openrouter(system, user, max_tokens=2000)
    if not result:
        raise RuntimeError("Art generation failed")

    # Parse structured output
    concept = _extract_field(result, "CONCEPT")
    prompt = _extract_field(result, "PROMPT")
    title = _extract_field(result, "TITLE") or f"{style_name} Study"
    statement = _extract_field(result, "STATEMENT") or result

    # Store prompt in the body for the pipeline to use for image gen
    body = f"## {title}\n\n{statement}\n\n---\n*Style: {style_name}*"
    # Stash the image prompt as metadata (will be extracted by run_profile)
    body = f"<!--IMGPROMPT:{prompt}-->\n\n{body}"
    return title, body


# ══════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _extract_title(text: str) -> str:
    """Extract title from first non-empty line of generated content."""
    for line in text.split("\n"):
        cleaned = line.strip().strip("#").strip("*").strip('"').strip()
        if cleaned and len(cleaned) > 3 and len(cleaned) < 120:
            # Skip lines that look like metadata
            if any(cleaned.upper().startswith(x) for x in ("FADE IN", "INT.", "EXT.", "COLD OPEN")):
                continue
            return cleaned
    return "Untitled"


def _extract_field(text: str, field: str) -> str:
    """Extract a labeled field from structured LLM output."""
    pattern = re.compile(rf'^{field}:\s*(.+?)(?=\n[A-Z]+:|$)', re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

PROFILES = {
    "essay": {
        "section": "essays",
        "emoji": "\U0001f4dd",
        "topic_fn": topic_essay,
        "generate_fn": generate_essay,
        "tags_base": ["essay"],
        "image_section": "essays",
    },
    "opinion": {
        "section": "opinions",
        "emoji": "\U0001f4ac",
        "topic_fn": topic_opinion,
        "generate_fn": generate_opinion,
        "tags_base": ["opinion"],
        "image_section": "opinions",
    },
    "after-dark": {
        "section": "after-dark",
        "emoji": "\U0001f303",
        "topic_fn": topic_after_dark,
        "generate_fn": generate_after_dark,
        "tags_base": ["after-dark", "monologue"],
        "image_section": "after-dark",
    },
    "pilot": {
        "section": "pilot",
        "emoji": "\U0001f3ac",
        "topic_fn": topic_pilot,
        "generate_fn": generate_pilot,
        "tags_base": ["screenplay", "tv"],
        "image_section": "pilot",
    },
    "tech-today": {
        "section": "tech-today",
        "emoji": "\U0001f4bb",
        "topic_fn": topic_tech_today,
        "generate_fn": generate_tech_today,
        "tags_base": ["tech"],
        "image_section": "tech-today",
    },
    "research": {
        "section": "research",
        "emoji": "\U0001f52c",
        "topic_fn": topic_research,
        "generate_fn": generate_research,
        "tags_base": ["research"],
        "image_section": "research",
    },
    "synthesis": {
        "section": "synthesis",
        "emoji": "\U0001f9f5",
        "topic_fn": topic_synthesis,
        "generate_fn": generate_synthesis,
        "tags_base": ["synthesis", "weekly"],
        "image_section": "synthesis",
    },
    "digest": {
        "section": "digests",
        "emoji": "\U0001f4f0",
        "topic_fn": topic_digest,
        "generate_fn": generate_digest,
        "tags_base": ["digest", "daily"],
        "image_section": "digests",
    },
    "dream": {
        "section": "dreams",
        "emoji": "\U0001f319",
        "topic_fn": topic_dream,
        "generate_fn": generate_dream,
        "tags_base": ["dream"],
        "image_section": "dreams",
    },
    "art": {
        "section": "art",
        "emoji": "\U0001f3a8",
        "topic_fn": topic_art,
        "generate_fn": generate_art,
        "tags_base": ["art"],
        "image_section": "art",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE ATTRIBUTION
# ══════════════════════════════════════════════════════════════════════════════

def _append_attribution(body: str, memories: list[dict], topic: str, profile_name: str) -> str:
    """Append a full attribution section with memory sources and web references."""
    lines = [
        "",
        "---",
        "",
        "## Sources & Attribution",
        "",
        f"**Content type:** {profile_name}  ",
        f"**Topic:** {topic}  ",
        f"**Generated:** {today_str()}  ",
        f"**Model:** OpenRouter (via Nova Journal pipeline)  ",
        "",
        "### Memory Sources",
        "",
        f"This piece drew from **{len(memories)}** memories in Nova's knowledge base:",
        "",
    ]

    # Group memories by source/show
    by_source: dict[str, list[dict]] = {}
    web_sources: list[dict] = []

    for m in memories:
        text = m.get("text", "")
        source = m.get("source", "unknown")
        metadata = m.get("metadata", {})

        if text.startswith("[Web]") or source == "web":
            web_sources.append(m)
        else:
            key = metadata.get("show", source) if metadata else source
            by_source.setdefault(key, []).append(m)

    for source_name, mems in sorted(by_source.items(), key=lambda x: -len(x[1])):
        lines.append(f"**{source_name}** ({len(mems)} memories)")
        for mem in mems[:5]:
            text = mem.get("text", "")[:150].replace("\n", " ").strip()
            meta = mem.get("metadata", {})
            title = meta.get("title", "")
            if title:
                lines.append(f"- *{title[:80]}*: \"{text}...\"")
            else:
                lines.append(f"- \"{text}...\"")
        if len(mems) > 5:
            lines.append(f"- *(+{len(mems) - 5} more)*")
        lines.append("")

    if web_sources:
        lines.append("### Web Sources")
        lines.append("")
        for ws in web_sources:
            meta = ws.get("metadata", {})
            url = meta.get("url", "")
            title_text = meta.get("title", "")
            text = ws.get("text", "").replace("[Web] ", "")
            if url and title_text:
                lines.append(f"- [{title_text}]({url})")
            elif ": " in text:
                title_part, content_part = text.split(": ", 1)
                lines.append(f"- **{title_part}**: {content_part[:200]}")
            else:
                lines.append(f"- {text[:250]}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by Nova · nova.digitalnoise.net · All source material from Nova's local memory system*")

    return body + "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

_REFUSAL_PATTERNS = [
    r"\bi need to (stop you|pump the brakes|tell you why|be direct with you)\b",
    r"\bthis (assignment|prompt|essay|topic) doesn'?t work\b",
    r"\bbefore i start fabricating\b",
    r"\bwhat do you actually want\b",
    r"\bhere'?s what i can actually do\b",
    r"\bi'?m going to stop you\b",
    r"\bi'?m going to save us both some time\b",
    r"\bi gotta be straight with you\b",
    r"\bpolished turd\b",
    r"\bgrab-?bag of\b.{0,40}\bexcerpts\b",
    r"\bno unifying thesis\b",
    r"^\s*\**option 1\**[:.]",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.I | re.M)

# Structural tell, independent of wording: a real essay title is a title. A short
# fragment ending in a colon ("What I can do:", "Here's what would actually work:")
# is a list intro, which is what a refusal looks like when it pivots to offering
# options instead of writing the thing that was asked for.
_COLON_INTRO_TITLE_RE = re.compile(r"^.{0,40}:\s*$")


def _looks_like_refusal(title: str, body: str) -> str | None:
    """Returns the matched pattern if title+body reads like the model declining/asking
    for clarification instead of producing the requested content, else None. Checked
    against the opening of the body since refusals front-load the pushback."""
    if title and _COLON_INTRO_TITLE_RE.match(title.strip()):
        return f"colon-intro title: {title!r}"
    m = _REFUSAL_RE.search((title or "") + "\n" + (body or "")[:800])
    return m.group(0) if m else None


def run_profile(profile_name: str) -> int:
    """Execute the full pipeline for a content profile. Returns 0 on success, 1 on failure."""
    if profile_name not in PROFILES:
        log(f"ERROR: Unknown profile '{profile_name}'. Available: {', '.join(PROFILES.keys())}")
        return 1

    profile = PROFILES[profile_name]
    section = profile["section"]
    log(f"=== Starting {profile_name} ({section}) ===")

    state = load_state()

    # ── Steps 1-2: Topic selection + content generation ───────────────────────
    # When topic_fn hands the LLM incoherent source material (a source vector whose
    # 25 random-sampled memories just don't relate to each other), it correctly
    # declines and asks for clarification instead of faking an essay -- but that
    # refusal text is well-formed prose that easily clears the "did it write >500
    # chars" check, so without this guard it gets published verbatim as if it were
    # the article.
    #
    # Retry strategy, cheapest/most-targeted first:
    #   1. Normal draw.
    #   2. SAME topic, re-sample its memories (fetch_memories_by_source draws with
    #      ORDER BY random(), so a second call is a fresh slice of the same source --
    #      pivot within the same criteria before abandoning the topic entirely).
    #   3. Fresh topic entirely, as a last resort.
    topic = None
    for attempt in range(3):
        try:
            if attempt == 1 and topic:
                try:
                    topic, memories = profile["topic_fn"](state, retry_topic=topic)
                except TypeError:
                    # This profile's topic_fn doesn't support same-source resampling
                    # (e.g. news/search-based profiles, where the "topic" is a single
                    # headline and isn't the kind of thing that has incoherent draws)
                    # -- skip straight to a fresh topic instead.
                    topic, memories = profile["topic_fn"](state)
            else:
                topic, memories = profile["topic_fn"](state)
            title, body = profile["generate_fn"](topic, memories)
            log(f"Generated: \"{title}\" ({len(body)} chars)")
        except SkipArticle as e:
            log(f"SKIP: {e}")
            return 0
        except Exception as e:
            log(f"ABORT: Generation failed — {e}")
            return 1
        refusal = _looks_like_refusal(title, body)
        if not refusal:
            break
        if attempt < 2:
            log(f"Generation looks like a refusal, not content (matched: {refusal!r}) — "
                f"retrying ({'same topic, fresh sample' if attempt == 0 else 'fresh topic'}).")
        else:
            log(f"Still looks like a refusal after {attempt + 1} attempts "
                f"(matched: {refusal!r}) — aborting for today.")
            return 1

    # ── Step 3: Image generation ──────────────────────────────────────────────
    image_path = None
    try:
        # Art profile: extract prompt from body, generate multiple candidates
        if profile_name == "art":
            image_path = _generate_art_images(body, profile)
        else:
            img_prompt = get_image_prompt(title, topic[:100], section)
            image_path = generate_image(img_prompt, section=profile["image_section"])
    except Exception as e:
        log(f"Image generation error (non-fatal): {e}")

    if not image_path:
        log("WARNING: No cover image — publishing without one")

    # ── Step 4: Clean body (remove image prompt metadata if present) ──────────
    body = re.sub(r'<!--IMGPROMPT:.+?-->\n*', '', body, flags=re.DOTALL)

    # ── Step 4b: Append full source attribution ──────────────────────────────
    body = _append_attribution(body, memories, topic, profile_name)

    # ── Step 5: Publish to Hugo ───────────────────────────────────────────────
    tags = profile["tags_base"] + _topic_to_tags(topic)
    description = f"Nova's {profile_name} on {topic[:60]}"

    success = publish_hugo(
        title=title, body=body, section=section, tags=tags,
        description=description, image_path=image_path, emoji=profile["emoji"],
        # The draft's own source material grounds any long-form expansion (2026-10-06).
        sources=sources_from_memories(memories, topic), profile=profile_name,
    )
    if not success:
        log("ABORT: Hugo publish failed")
        return 1

    # ── Step 6: Git push ──────────────────────────────────────────────────────
    push_status = git_push(section, title)
    log(f"{published_label(push_status)}: {title}")

    # ── Step 7: Slack notify ──────────────────────────────────────────────────
    preview = body[:300].replace("\n", " ").strip()
    notify_slack(section, title, preview)

    # ── Step 8: Update state ──────────────────────────────────────────────────
    add_recent(state, profile_name, topic[:100])
    if profile_name not in state:
        state[profile_name] = {}
    state[profile_name]["last_run"] = today_str()
    state[profile_name]["last_title"] = title
    count_key = f"{profile_name}_count"
    state[count_key] = state.get(count_key, 0) + 1
    save_state(state)

    log(f"=== {profile_name} complete: \"{title}\" ===")
    return 0


def _generate_art_images(body: str, profile: dict) -> str | None:
    """Art-specific: extract prompt from body, generate 3 candidates, pick largest."""
    prompt_match = re.search(r'<!--IMGPROMPT:(.+?)-->', body, re.DOTALL)
    if not prompt_match:
        return None

    prompt = prompt_match.group(1).strip()
    log(f"Art image prompt: {prompt[:80]}...")

    candidates = []
    for i in range(3):
        path = generate_image(prompt, width=1024, height=1024, section="art")
        if path and Path(path).exists():
            candidates.append(path)
            log(f"  Candidate {i+1}: {Path(path).name} ({Path(path).stat().st_size} bytes)")
        time.sleep(2)

    if not candidates:
        return None

    # Pick largest file (most detail)
    best = max(candidates, key=lambda p: Path(p).stat().st_size)
    log(f"  Selected: {Path(best).name}")
    return best


def _topic_to_tags(topic: str) -> list[str]:
    """Extract 1-2 meaningful tags from the topic string."""
    # Clean up pipe-separated topics (pilot, art)
    clean = topic.split("|")[0].strip()
    # Remove year prefixes
    clean = re.sub(r'^\d{4}\s*', '', clean)
    # Take first 2-3 meaningful words
    words = [w.lower() for w in clean.split() if len(w) > 3][:2]
    return words if words else []


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <profile>")
        print(f"Profiles: {', '.join(sorted(PROFILES.keys()))}")
        sys.exit(1)

    profile_name = sys.argv[1].lower().strip()
    # Allow underscore variants
    profile_name = profile_name.replace("_", "-")

    sys.exit(run_profile(profile_name))


if __name__ == "__main__":
    main()
