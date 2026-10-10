#!/usr/bin/env python3
"""
nova_monthly_wrap.py — one "<Section> — <Month YYYY>: <subtitle>" wrap per journal section.

Jordan, 2026-10-06: "Let's make a monthly wrap article for each section. Normal
yadda-yadda funny Nova voice with image, 5000 words target. Fire off on the 1st."

For the target month (default: the previous calendar month) every live article
section that had >= 1 post gets ONE wrap article, written back into that section:

  1. collect the section's posts dated in the month (front-matter date), skipping
     prior weekly recaps / monthly wraps; render them (title, date, live URL,
     description, a body excerpt trimmed to a per-article budget) as SOURCES;
  2. Sonnet drafts a long (~3500-5000 word) wrap in Nova's full voice
     (nova_voice.system_prompt) that names and links the real posts;
  3. a cover image (local ComfyUI first; capped at one 240 s attempt);
  4. publish_hugo(profile='monthly-wrap', sources=SOURCES, min_words=5000):
     the shared GROUNDED expander may lengthen the draft toward 5000 using ONLY
     those sources, then the number check + Sonnet claim check run; anything that
     fails closes back to the draft. Landing under 5000 because the month's
     material ran out is correct behaviour — grounding is never weakened to hit it.

Sections run WORKERS (3) at a time under a run budget; ONE git_push (fleet lock)
at the end. Dedup lives in Postgres (nova_ops.service_config, service
'nova_monthly_wrap', key '<YYYY-MM>:<section>'), backed by the deterministic
file name content/<section>/<YYYY-MM>-<section>-monthly-wrap.md, so a
month+section is never published twice — unless --force (with --section):
republish IN PLACE (same slug/file/URL, the existing cover image kept unless it is
missing), e.g. after the grounded expander improved (2026-10-06 strip-not-scrap).

Usage:
  nova_monthly_wrap.py                       # previous calendar month, all sections
  nova_monthly_wrap.py --month 2026-09       # a specific month
  nova_monthly_wrap.py --section dreams      # one section (repeatable)
  nova_monthly_wrap.py --dry-run             # scan only: what WOULD be written
  nova_monthly_wrap.py --dry-run --generate  # + draft & grounded expansion, never writes
  nova_monthly_wrap.py --month 2026-09 --section local --force   # republish in place

Scheduled on nova-core: scheduler-core.yaml task journal_monthly_wrap (1st of the month).
Written by Jordan Koch (via Claude). Generalized from the one-off May 2026 wrap 2026-10-06.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_image_utils
import nova_journal
from nova_voice import system_prompt
from nova_notify import notify as nova_notify
from nova_journal import publish_hugo, git_push, call_openrouter, generate_image, published_label
from nova_journal_weekly_summary import _parse_article, SKIP_FILES

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = nova_journal.HUGO_ROOT
CONTENT_ROOT = HUGO_ROOT / "content"
IMAGES_ROOT = HUGO_ROOT / "static/images"
SITE = "https://nova.digitalnoise.net"
LOG_FILE = Path.home() / ".openclaw/logs/nova_monthly_wrap.log"

# Not article sections (about/start-here), covered elsewhere (meta = the monthly
# meta-analysis), or retired (rando -> operations; art, after-dark, pilot).
SKIP_DIRS = {"about", "meta", "start-here", "rando", "art", "after-dark", "pilot"}

PROFILE = "monthly-wrap"           # ARTICLE_LENGTH row (3000-6000, grounded)
MIN_WORDS = 5000                   # Jordan's target; a floor for GROUNDED expansion only
DRAFT_WORDS = (3500, 5000)         # aim high: Sonnet drafts ~4.5-5.5k from 56k of sources
DRAFT_MODEL = "anthropic/claude-sonnet"
DRAFT_FALLBACK_MODEL = "anthropic/claude-haiku-4.5"
DRAFT_TIMEOUT_S = 600
DRAFT_MIN_WORDS = 600              # below this the draft is junk -> section fails

# SOURCES budget: stays under nova_journal.LONGFORM_SOURCES_MAX_CHARS (60k) so the
# expander and the checker see exactly what the draft was written from.
SOURCES_BUDGET = 56_000
MAX_SOURCE_ARTICLES = 60           # big months (operations ~500 posts): the longest 60
PER_ARTICLE_MIN, PER_ARTICLE_MAX = 350, 14000   # small months get deep excerpts

WORKERS = 3
# Don't START a section after this many seconds. A section is ~draft 3-6 min +
# cover <=4 min + Sonnet expansion <=10 min + check <=5 min (+ <=5 min strip-and-
# recheck when the first check flags something, 2026-10-06); leave that plus the
# final push inside the scheduler timeout (scheduler-core.yaml journal_monthly_wrap).
RUN_BUDGET_S = 1800   # measured 2026-10-06: draft ~105 s, expand+check <=~6 min, cover <=4 min
IMAGE_TIMEOUT_S = 240
IMAGE_MAX_RETRIES = 1

import nova_dsn as _nova_dsn  # noqa: E402
PG_DSN = _nova_dsn.pg_dsn("nova_ops", "connect_timeout=5")
STATE_SERVICE = "nova_monthly_wrap"

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_FM_DESC_RE = re.compile(r'^description:\s*"?(.*?)"?\s*$', re.MULTILINE)
_FM_SLUG_RE = re.compile(r'^slug:\s*"?([a-z0-9-]+)"?\s*$', re.MULTILINE)

EMOJI = "\U0001F5D3"   # spiral calendar
MOTIF = {
    "operations": "a homelab server room at dusk, blinking racks and dashboards",
    "local": "Burbank streets and the Verdugo hills in warm evening light",
    "dreams": "a surreal moonlit dreamscape of floating doors and clocks",
    "essays": "a writer's desk with stacks of annotated manuscripts",
    "opinions": "an empty soapbox and a vintage microphone under a spotlight",
    "tech-today": "circuit boards, newspapers and glowing screens on a desk",
    "research": "lab notebooks, charts and a microscope in soft light",
    "synthesis": "threads of light weaving many pages together",
    "digests": "neatly stacked daily reports and a coffee cup at dawn",
    "security": "a lighthouse sweeping its beam over a dark network of lights",
}


# ── Logging ───────────────────────────────────────────────────────────────────

_log_lock = threading.Lock()


def log(msg: str):
    line = f"[monthly-wrap {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# The grounding verdict is only logged by nova_journal.longform_expand; tap its
# "[longform]" lines so the run report can say what happened to each wrap.
_LONGFORM_LINES: list = []
_TAP = {"installed": False}


def _install_longform_tap():
    if _TAP["installed"]:
        return
    orig = nova_journal.log

    def tapped(msg):
        if "[longform]" in str(msg):
            _LONGFORM_LINES.append(str(msg))
        orig(msg)
    nova_journal.log = tapped
    _TAP["installed"] = True


def _verdict_for(title: str) -> str:
    key = f"'{title[:50]}'"
    hits = [l for l in _LONGFORM_LINES if key in l]
    if not hits:
        return "-"
    m = re.search(r"\] \d+w — (.*)$", hits[-1])
    return (m.group(1) if m else hits[-1])[:200]


# ── Month helpers ─────────────────────────────────────────────────────────────

def parse_month(s: str) -> tuple[int, int]:
    m = _MONTH_RE.match((s or "").strip())
    if not m:
        raise ValueError(f"month must be YYYY-MM, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def default_month(today: date | None = None) -> str:
    """The previous calendar month (the wrap runs on the 1st for the month just ended)."""
    t = today or date.today()
    y, m = (t.year - 1, 12) if t.month == 1 else (t.year, t.month - 1)
    return f"{y:04d}-{m:02d}"


def month_label(month: str) -> str:
    y, m = parse_month(month)
    return date(y, m, 1).strftime("%B %Y")


def section_label(section: str) -> str:
    return section.replace("-", " ").title()


def wrap_slug(month: str, section: str) -> str:
    parse_month(month)
    if not _SECTION_RE.match(section):
        raise ValueError(f"bad section {section!r}")
    return f"{month}-{section}-monthly-wrap"


def wrap_url(month: str, section: str) -> str:
    return f"{SITE}/{section}/{wrap_slug(month, section)}/"


# ── Sections + articles ───────────────────────────────────────────────────────

def list_sections() -> list[str]:
    if not CONTENT_ROOT.is_dir():
        return []
    return [e.name for e in sorted(CONTENT_ROOT.iterdir())
            if e.is_dir() and e.name not in SKIP_DIRS
            and not e.name.startswith((".", "_")) and _SECTION_RE.match(e.name)]


def _is_roundup(art: dict, section: str = "") -> bool:
    """Prior weekly recaps ("This Week in <Section>") and monthly wraps — never wrap a
    wrap. Synthesis's own "This Week in My Head" columns are real posts, so the title
    match is section-specific."""
    tags = art.get("tags", [])
    if "weekly-summary" in tags or "monthly-wrap" in tags:
        return True
    t = re.sub(r"^[^a-z]*", "", art.get("title", "").lower())
    recap = f"this week in {section_label(section).lower()}" if section else "this week in"
    return t.startswith((recap, "monthly wrap")) or art.get("name", "").endswith("-monthly-wrap.md")


def collect_month_articles(section: str, month: str) -> list[dict]:
    y, m = parse_month(month)
    arts = []
    for md in (CONTENT_ROOT / section).glob("*.md"):
        if md.name in SKIP_FILES:
            continue
        art = _parse_article(md)
        if not art or (art["date"].year, art["date"].month) != (y, m) or _is_roundup(art, section):
            continue
        try:
            raw = md.read_text(errors="ignore")
        except OSError:
            continue
        front = raw.split("---", 2)[1] if raw.startswith("---") and raw.count("---") >= 2 else ""
        dm = _FM_DESC_RE.search(front)
        sm = _FM_SLUG_RE.search(front)
        # drop the byline + live-weather dateline (pure noise for the wrap's sources)
        body = re.sub(r"^\*(Published |Burbank ·)[^\n]*\*\s*$", "", art["body"], flags=re.MULTILINE)
        art.update(
            description=(dm.group(1).strip() if dm else ""),
            url=f"{SITE}/{section}/{sm.group(1) if sm else md.stem}/",
            body=body.strip(),
            words=len(body.split()),
            title=re.sub(r"^[^\w\"'(]+", "", art["title"]).strip() or md.stem,
        )
        arts.append(art)
    arts.sort(key=lambda a: (a["date"], a["name"]))
    return arts


def build_sources(section: str, month: str, arts: list[dict]) -> str:
    """The SOURCES block: the only material the draft and any expansion may use."""
    chosen = arts
    note = ""
    if len(arts) > MAX_SOURCE_ARTICLES:
        chosen = sorted(sorted(arts, key=lambda a: -a["words"])[:MAX_SOURCE_ARTICLES],
                        key=lambda a: (a["date"], a["name"]))
        note = (f" (the {len(chosen)} longest are excerpted below; "
                f"{len(arts) - len(chosen)} shorter posts are not)")
    head = (f"SECTION: {section_label(section)} ({section})\nMONTH: {month_label(month)}\n"
            f"POSTS PUBLISHED IN THIS SECTION THIS MONTH: {len(arts)}{note}\n")
    heads = [f"\n[{i}] \"{a['title']}\" — {a['date'].isoformat()} — {a['url']}\n"
             + (f"Description: {a['description'][:200]}\n" if a["description"] else "")
             + "Excerpt: " for i, a in enumerate(chosen, 1)]
    room = SOURCES_BUDGET - len(head) - sum(len(h) + 1 for h in heads)
    per = max(PER_ARTICLE_MIN, min(PER_ARTICLE_MAX, room // max(1, len(chosen))))
    parts, used = [head], len(head)
    for h, a in zip(heads, chosen):
        if used + len(h) + 1 > SOURCES_BUDGET:
            break
        # every chosen post keeps its title/date/URL; the excerpt shrinks to fit
        excerpt = re.sub(r"\s+", " ", a["body"]).strip()[:per]
        excerpt = excerpt[:max(0, SOURCES_BUDGET - used - len(h) - 1)]
        parts.append(h + excerpt + "\n")
        used += len(h) + len(excerpt) + 1
    return "".join(parts)


# ── State (Postgres) ──────────────────────────────────────────────────────────

def _state_key(month: str, section: str) -> str:
    return f"{month}:{section}"


def _pg():
    import psycopg2
    return psycopg2.connect(PG_DSN)


def done_keys(month: str) -> set[str] | None:
    """Keys already published for this month, or None when PG is unreachable."""
    try:
        c = _pg()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT key FROM service_config WHERE service = %s AND key LIKE %s",
                            (STATE_SERVICE, f"{month}:%"))
                return {r[0] for r in cur.fetchall()}
        finally:
            c.close()
    except Exception as e:
        log(f"state lookup failed ({e}) — relying on the wrap file check only")
        return None


def mark_done(month: str, section: str, info: dict) -> bool:
    try:
        c = _pg()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO service_config (service, key, value, updated_at, updated_by) "
                    "VALUES (%s, %s, %s::jsonb, now(), 'nova_monthly_wrap') "
                    "ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = now(), updated_by = EXCLUDED.updated_by",
                    (STATE_SERVICE, _state_key(month, section), json.dumps(info)))
            c.commit()
        finally:
            c.close()
        return True
    except Exception as e:
        log(f"[{section}] state write failed ({e}) — the wrap file itself still dedups")
        return False


def already_done(month: str, section: str, keys: set | None) -> bool:
    if (CONTENT_ROOT / section / f"{wrap_slug(month, section)}.md").exists():
        return True
    return bool(keys) and _state_key(month, section) in keys


# ── Generation ────────────────────────────────────────────────────────────────

def _wc(text: str) -> int:
    return len((text or "").split())


def generate_wrap(section: str, month: str, arts: list[dict], sources: str) -> tuple[str, str] | None:
    """Nova's draft. Returns (title, body) or None."""
    label, mlabel = section_label(section), month_label(month)
    lo, hi = DRAFT_WORDS
    rules = f"""
MONTHLY SECTION WRAP RULES:
- This is your MONTHLY WRAP of the journal's "{label}" section for {mlabel}: you,
  Nova, looking back over everything you published in this section that month.
- Full Nova voice: funny, warm, sarcastic, opinionated, an advisor with receipts.
  Even if this section normally has its own register (dreams, research, essays),
  the wrap is YOU, awake and reflecting, roasting and praising your own month.
- Walk the month: open with the shape of it, then go through the posts in
  thematic clusters, name the standouts and the duds, the obsessions, the running
  jokes, what changed between the first week and the last, what you'd revisit.
- Reference posts by their REAL titles and LINK them as markdown links using the
  exact URL from the SOURCES ([Title](url)). Never invent a post, title or URL.
- GROUNDING: every name, number, date, event and quote must come from the
  SOURCES. Opinion, jokes and analysis are yours; facts are not. If the month was
  thin, say so and riff — do not fabricate.
- Length: {lo}-{hi} words. Use ## subheadings. Close with a sign-off and a teaser
  for next month. Do NOT include the article title as a header in the body.
- OUTPUT FORMAT: the FIRST line is exactly `SUBTITLE: <a punchy, funny 3-10 word
  subtitle for the month>`, then a blank line, then the article body."""
    system = system_prompt(rules, section=section, topic=f"{label} monthly wrap {mlabel}")
    user = (f"Here is everything you published in \"{label}\" during {mlabel}:\n\n"
            f"{sources}\n\nWrite the {mlabel} wrap for {label} now ({lo}-{hi} words).")
    out = None
    for model in (DRAFT_MODEL, DRAFT_FALLBACK_MODEL):
        out = call_openrouter(system, user, model=model, max_tokens=16000,
                              temperature=0.8, timeout=DRAFT_TIMEOUT_S)
        if out and _wc(out) >= DRAFT_MIN_WORDS:
            break
        log(f"[{section}] draft via {model} failed/short ({_wc(out)}w)")
        out = None
    if not out:
        return None
    subtitle = ""
    lines = out.strip().split("\n")
    for i, line in enumerate(lines[:5]):
        mm = re.match(r"^\W*subtitle\W*:\s*(.+)$", line.strip(), re.I)
        if mm:
            subtitle = mm.group(1).strip().strip("*\"' ")
            lines = lines[:i] + lines[i + 1:]
            break
    body = "\n".join(lines).strip()
    # drop an echoed title header
    first = body.split("\n", 1)
    if first and first[0].lstrip("# ").lower().startswith(label.lower()) and len(first) > 1:
        body = first[1].strip()
    subtitle = re.sub(r"[\"\n]", "", subtitle)[:90] or "The Month in Review"
    return f"{label} — {mlabel}: {subtitle}", body


def _cover(section: str, month: str) -> str | None:
    try:
        nova_image_utils.TIMEOUT = IMAGE_TIMEOUT_S
        nova_image_utils.MAX_RETRIES = IMAGE_MAX_RETRIES
        motif = MOTIF.get(section, f"a journal section about {section_label(section).lower()}")
        prompt = (f"monthly retrospective cover: {motif}, a torn {month_label(month).split()[0]} "
                  f"calendar page drifting through the scene, editorial illustration, "
                  f"muted palette, playful, no text, no words")
        p = generate_image(prompt, section=section)
        if not p:
            # 2026-09 run: essays' quality tier timed out and synthesis' premium tier was
            # "Request Moderated" (nova-core has no local FLUX, so it always uses
            # OpenRouter). One retry on the balanced tier ("default" section).
            log(f"[{section}] cover failed on the section tier — retrying balanced tier")
            p = generate_image(prompt, section="default")
        return str(p) if p else None
    except Exception as e:
        log(f"[{section}] image gen failed (non-fatal): {e}")
        return None


def _written_words(section: str, month: str) -> int:
    p = CONTENT_ROOT / section / f"{wrap_slug(month, section)}.md"
    try:
        raw = p.read_text()
    except OSError:
        return 0
    parts = raw.split("---", 2)
    return _wc(parts[2] if len(parts) == 3 else raw)


def _existing_cover(section: str, month: str) -> Path | None:
    p = IMAGES_ROOT / section / f"{wrap_slug(month, section)}.webp"
    return p if p.is_file() and p.stat().st_size > 0 else None


def wrap_section(section: str, month: str, arts: list[dict], dry_run: bool = False,
                 force: bool = False) -> dict | None:
    """Draft + cover + publish_hugo (grounded expansion inside). No push. Returns a
    result dict, or None on failure. force: republish in place over the existing wrap,
    keeping its cover image unless it is missing."""
    t0 = time.monotonic()
    before_w = _written_words(section, month) if force else None
    sources = build_sources(section, month, arts)
    log(f"[{section}] {len(arts)} posts, {len(sources)} chars of sources — drafting")
    res = generate_wrap(section, month, arts, sources)
    if not res:
        return None
    title, body = res
    draft_w = _wc(body)
    # Deterministic audit of the DRAFT (the shared pipeline only checks expansions):
    # multi-digit numbers the draft uses that appear nowhere in the sources. Logged
    # and reported, not blocking — derived counts ("511 posts") are in the header.
    stray = nova_journal.new_numbers("", sources, body)
    log(f"[{section}] draft {draft_w}w in {time.monotonic() - t0:.0f}s: {title}"
        + (f" — numbers not in sources: {stray[:15]}" if stray else " — every number sourced"))
    slug = wrap_slug(month, section)
    if dry_run:
        out = nova_journal.longform_expand(title, body, section, sources, profile=PROFILE,
                                           min_words=MIN_WORDS, slug=slug)
        return {"section": section, "title": title, "slug": slug, "posts": len(arts),
                "draft_words": draft_w, "final_words": _wc(out), "image": None,
                "draft_stray_numbers": stray,
                "verdict": _verdict_for(title), "seconds": round(time.monotonic() - t0)}
    tmpdir = None
    old_cover = _existing_cover(section, month) if force else None
    if old_cover:
        # publish_hugo copies image_path onto static/images/<section>/<slug>.webp — the same
        # file — so hand it a temp copy (copying a file onto itself raises).
        tmpdir = tempfile.mkdtemp(prefix="nova_wrap_cover_")
        img = str(shutil.copy2(old_cover, Path(tmpdir) / old_cover.name))
        log(f"[{section}] --force: keeping the existing cover {old_cover.name}")
    else:
        img = _cover(section, month)
    mlabel = month_label(month)
    try:
        ok = publish_hugo(title, body, section,
                          [section, "monthly-wrap", mlabel.lower().replace(" ", "-")],
                          f"Nova's {mlabel} wrap of {section_label(section)}: "
                          f"{len(arts)} posts, roasted and reviewed",
                          image_path=img, emoji=EMOJI, stable_slug=slug,
                          sources=sources, profile=PROFILE, min_words=MIN_WORDS)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if ok is False:
        log(f"[{section}] publish_hugo refused the wrap — not marking done")
        return None
    final_w = _written_words(section, month)
    return {"section": section, "title": title, "slug": slug, "posts": len(arts),
            "draft_words": draft_w, "final_words": final_w, "image": bool(img),
            "draft_stray_numbers": stray, "before_words": before_w, "forced": force,
            "verdict": _verdict_for(title), "url": wrap_url(month, section),
            "seconds": round(time.monotonic() - t0)}


# ── Main ──────────────────────────────────────────────────────────────────────

def run(month: str, sections: list[str] | None = None, dry_run: bool = False,
        generate: bool = False, force: bool = False) -> dict:
    """force: republish already-wrapped sections in place (the CLI requires --section)."""
    parse_month(month)
    t_run = time.monotonic()
    mode = ("DRY-RUN" + (" +generate" if generate else "") if dry_run else "LIVE") + \
        (" FORCE (republish in place)" if force else "")
    log(f"=== Monthly wraps {month} ({mode}) ===")
    _install_longform_tap()
    live = list_sections()
    if sections:
        bad = [s for s in sections if s not in live]
        if bad:
            log(f"unknown/skipped section(s): {', '.join(bad)} (live: {', '.join(live)})")
        targets = [s for s in sections if s in live]
    else:
        targets = live
    keys = done_keys(month)
    queue = []
    for sec in targets:
        arts = collect_month_articles(sec, month)
        if not arts:
            status = "skip (no posts)"
        elif already_done(month, sec, keys) and not force:
            status = "skip (already wrapped)"
        elif force and already_done(month, sec, keys):
            status = "WRAP (force: republish in place)"
            queue.append((sec, arts))
        else:
            status = "WRAP"
            queue.append((sec, arts))
        log(f"  {sec:<14} {len(arts):>4} posts  {status}")
    report = {"month": month, "published": [], "failed": [], "deferred": [], "push": None}
    if dry_run and not generate:
        report["would_wrap"] = [s for s, _ in queue]
        return report
    if not queue:
        log("nothing to wrap")
        return report

    def _one(sec, arts):
        if time.monotonic() - t_run > RUN_BUDGET_S:
            return sec, "DEFERRED"
        try:
            r = wrap_section(sec, month, arts, dry_run=dry_run, force=force)
        except Exception as e:
            log(f"[{sec}] ERROR: {e}")
            r = None
        log(f"[{sec}] {'ok' if r else 'FAILED'} (run elapsed {time.monotonic() - t_run:.0f}s)")
        return sec, r

    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        futs = [pool.submit(_one, s, a) for s, a in queue]
        for fut in as_completed(futs):
            sec, r = fut.result()
            if r == "DEFERRED":
                report["deferred"].append(sec)
            elif not r:
                report["failed"].append(sec)
            else:
                report["published"].append(r)
                if not dry_run:   # main thread only; the file is on disk -> never redo
                    mark_done(month, sec, {k: r[k] for k in
                                           ("title", "slug", "url", "posts", "draft_words",
                                            "final_words", "image")} |
                              {"published_at": time.strftime("%Y-%m-%dT%H:%M:%S")} |
                              ({"republished": True, "before_words": r.get("before_words")}
                               if r.get("forced") else {}))
    order = [s for s, _ in queue]
    report["published"].sort(key=lambda r: order.index(r["section"]))

    if report["published"] and not dry_run:
        t0 = time.monotonic()
        names = ", ".join(r["section"] for r in report["published"])
        status = git_push("monthly-wrap", f"Monthly wraps {month}: {names}")
        report["push"] = status
        log(f"git_push -> {published_label(status)} in {time.monotonic() - t0:.0f}s")
        for r in report["published"]:
            nova_notify(f"Nova Journal — {r['title']}",
                        body=f"{r['final_words']} words, {r['posts']} posts reviewed\n{r['url']}",
                        level="info", category="journal",
                        dedup_key=f"journal-monthly-wrap-{month}-{r['section']}")
    if (report["failed"] or report["deferred"]) and not dry_run:
        nova_notify(f"Nova Journal — monthly wraps {month} incomplete",
                    body=f"failed: {', '.join(report['failed']) or '-'}; deferred: "
                         f"{', '.join(report['deferred']) or '-'}. Rerun: nova_monthly_wrap.py "
                         f"--month {month} (done sections are skipped)",
                    level="warning", category="journal",
                    dedup_key=f"journal-monthly-wrap-{month}-incomplete")

    log("=== Results ===")
    for r in report["published"]:
        was = f"was {r['before_words']}w, " if r.get("before_words") is not None else ""
        log(f"  {r['section']:<14} {r['posts']:>4} posts  {was}draft {r['draft_words']}w -> "
            f"{r['final_words']}w  image={'ok' if r['image'] else 'none'}  {r['seconds']}s  "
            f"{r.get('url', '')}\n      grounding: {r['verdict']}"
            + (f"\n      draft numbers not in sources: {r['draft_stray_numbers'][:15]}"
               if r.get("draft_stray_numbers") else ""))
    for s in report["failed"]:
        log(f"  {s:<14} FAILED")
    for s in report["deferred"]:
        log(f"  {s:<14} DEFERRED (run budget)")
    log(f"=== Done in {time.monotonic() - t_run:.0f}s: {len(report['published'])} ok, "
        f"{len(report['failed'])} failed, {len(report['deferred'])} deferred ===")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova's monthly per-section wrap articles")
    ap.add_argument("--month", default=None, help="YYYY-MM (default: previous calendar month)")
    ap.add_argument("--section", action="append", default=None, help="limit to a section (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="scan only; never writes or pushes")
    ap.add_argument("--generate", action="store_true",
                    help="with --dry-run: also draft + grounded expansion (timing), never writes")
    ap.add_argument("--force", action="store_true",
                    help="republish already-wrapped --section(s) in place (same file/URL, "
                         "existing cover kept unless missing); requires --section")
    a = ap.parse_args(argv)
    month = a.month or default_month()
    try:
        parse_month(month)
    except ValueError as e:
        ap.error(str(e))
    if a.force and not a.section:
        ap.error("--force republishes in place and needs explicit --section(s)")
    rep = run(month, a.section, dry_run=a.dry_run or a.generate, generate=a.generate, force=a.force)
    return 1 if (rep["failed"] and not rep["published"]) else 0


if __name__ == "__main__":
    sys.exit(main())
