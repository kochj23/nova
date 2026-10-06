#!/usr/bin/env python3
"""
nova_journal_weekly_summary.py — Weekly per-section recap articles.

Every Sunday afternoon, walk each article SECTION of the Nova journal and look at
the articles published in the last 7 days. For any section that had MORE THAN ONE
(>1) article that week, Nova writes a single "This Week in <Section>" summary that
reviews/recaps all of that section's pieces from the week — published back into the
SAME section, in her full sassy advisor article voice.

Sections with 0 or 1 article that week are skipped (no summary — otherwise that
would be stupid).

Mirrors the dedup/state + publish pattern of nova_journal_emergency.py /
nova_journal_security.py. Reuses publish_hugo / git_push / call_openrouter from
nova_journal, and nova_voice.system_prompt (which already prepends Nova's article
voice + a live backyard-weather dateline). Slack notify via nova_config.post_both.

Run modes:
  (default)  — generate + publish weekly summaries for qualifying sections.
  dry-run    — print, per section, how many articles fell in the last 7 days and
               which sections WOULD get a summary. Does NOT call publish_hugo /
               git_push / Slack. (aliases: dryrun, --dry-run)
  dry-run --generate
             — as dry-run, but ALSO runs the LLM recap generation per qualifying
               section and logs per-section timings. Still never writes, pushes,
               notifies or touches state. For timing checks off-schedule.

Timeout hardening (2026-10-06): the 2026-10-04 run was killed at the scheduler's
1800 s limit. ~1050 s of it was `git push` — GitHub ssh:22 was timing out from
nova-core and every section did its own push (+ pull --rebase), 2–4.5 min each.
Now: sections are generated + written independently, ONE git_push runs at the end,
a run budget stops starting new sections in time for that push to happen (deferred
sections stay unseen, so a same-day rerun picks them up), local image gen is
capped so a wedged ComfyUI can't eat a section's whole budget, and sections run
WORKERS (3) at a time.

Written by Jordan Koch (via Claude).
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_image_utils
from nova_voice import system_prompt
from nova_notify import notify as nova_notify

# Reuse the shared journal pipeline helpers so this stays consistent with the rest
# of the journal system.
from nova_journal import publish_hugo, git_push, call_openrouter, generate_image

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_ROOT = HUGO_ROOT / "content"
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal_weekly_summary.log"
STATE_FILE = Path.home() / ".openclaw/config/journal_weekly_summary_state.json"
MODEL = "anthropic/claude-haiku-4.5"

# Non-article directories / files to skip when enumerating sections.
SKIP_DIRS = {"about", "meta", "start-here", "rando", "art"}  # rando retired — daily pieces now go to operations; art = Art Corner retired 2026-10-06 (archive only, no roundups)
SKIP_FILES = {"_index.md", "search.md"}

DAYS = 7
MIN_ARTICLES = 2  # strictly more than one

# Scheduler kills the task at 1800 s. Don't START a new section after this many
# seconds, leaving room for an in-flight section (longform: up to 2 expansion
# passes, ~2-6 min) plus the single final push (git_push's network timeout is 180 s).
RUN_BUDGET_S = 1080
# Cover image: FLUX ~60 s/image. Default nova_image_utils budget is 2 x 600 s,
# which alone could blow the task timeout; one 240 s attempt is plenty here.
IMAGE_TIMEOUT_S = 240
IMAGE_MAX_RETRIES = 1
# Sections run 3 at a time. Real work is ~25-40 min serial now (8 sections; 5 are
# longform sections whose recap publish_hugo expands to >=5000 words in 1-2 haiku
# passes, + a FLUX cover each). Each section writes distinct files; git push happens once, after all of them;
# concurrent claude -p calls and ComfyUI jobs are routine across the journal fleet.
WORKERS = 3


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[weekly-summary {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── State / dedup ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _week_key(section: str, week_start: date) -> str:
    """Stable dedup key: section + ISO week-start so a section-week isn't redone."""
    return f"{section}:{week_start.isoformat()}"


# ── Article discovery ───────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_FM_DATE_RE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_FM_TITLE_RE = re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE)
_FM_TAGS_RE = re.compile(r"^tags:\s*(.+)$", re.MULTILINE)


def list_sections() -> list[str]:
    """Article sections under the content root (skip non-article dirs/files)."""
    if not CONTENT_ROOT.is_dir():
        return []
    sections = []
    for entry in sorted(CONTENT_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in SKIP_DIRS or entry.name.startswith((".", "_")):
            continue
        sections.append(entry.name)
    return sections


def _parse_article(md_file: Path) -> dict | None:
    """Read a section article. Returns {date, title, tags, body, name} or None.

    Date comes from front-matter `date:`; falls back to the YYYY-MM-DD filename
    prefix. Skips _index/search and prior weekly-summary articles.
    """
    if md_file.name in SKIP_FILES or md_file.stem.startswith("_"):
        return None

    try:
        raw = md_file.read_text(errors="ignore")
    except OSError:
        return None

    # Split front matter from body.
    front, body = raw, ""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            front, body = parts[1], parts[2]

    # Date: prefer front matter, fall back to filename prefix.
    art_date = None
    m = _FM_DATE_RE.search(front)
    if m:
        try:
            art_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            art_date = None
    if art_date is None:
        fm = _DATE_RE.match(md_file.name)
        if fm:
            try:
                art_date = date(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)))
            except ValueError:
                art_date = None
    if art_date is None:
        return None

    # Title.
    tm = _FM_TITLE_RE.search(front)
    title = tm.group(1).strip() if tm else md_file.stem
    # Strip leading emoji/whitespace from display title.
    title = title.strip()

    # Tags (rough — front matter is JSON-ish list).
    tags = []
    gm = _FM_TAGS_RE.search(front)
    if gm:
        tags = [t.strip().strip('"').strip("'").lower()
                for t in re.findall(r'[\"\']?([\w-]+)[\"\']?', gm.group(1))]

    return {
        "name": md_file.name,
        "date": art_date,
        "title": title,
        "tags": tags,
        "body": body.strip(),
    }


def _is_weekly_summary(art: dict) -> bool:
    """Skip prior weekly-summary articles so we don't summarize summaries."""
    if "weekly-summary" in art.get("tags", []):
        return True
    title_low = art.get("title", "").lower()
    # Strip any leading emoji before the words.
    title_low = re.sub(r"^[^a-z]*", "", title_low)
    return title_low.startswith("this week in")


def collect_section_articles(section: str, cutoff: date) -> list[dict]:
    """Articles in `section` dated on/after cutoff, excluding weekly summaries."""
    sec_dir = CONTENT_ROOT / section
    arts = []
    for md_file in sec_dir.glob("*.md"):
        art = _parse_article(md_file)
        if art is None:
            continue
        if art["date"] < cutoff:
            continue
        if _is_weekly_summary(art):
            continue
        arts.append(art)
    arts.sort(key=lambda a: (a["date"], a["name"]))
    return arts


# ── Slack ───────────────────────────────────────────────────────────────────────

def notify(section: str, title: str, preview: str, slug: str):
    date_str = time.strftime("%Y-%m-%d")
    url = f"https://nova.digitalnoise.net/{section}/{date_str}-{slug}/"
    # Published-content digest — FYI. Repeats weekly per section, so dedup on section.
    nova_notify(
        f"Nova Journal — Weekly Summary ({section}): {title}",
        body=f"{preview[:250]}\n{url}",
        level="info",
        category="journal",
        dedup_key=f"journal-weekly-summary-{section}",
    )


# ── Generation ───────────────────────────────────────────────────────────────────

def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def _date_range_label(start: date, end: date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.strftime('%B')} {start.day}–{end.day}, {end.year}"
    if start.year == end.year:
        return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"


def generate_summary(section: str, articles: list[dict], start: date, end: date) -> tuple[str, str] | None:
    """Have Nova write the weekly recap for one section. Returns (title, body)."""
    section_label = section.replace("-", " ").title()
    range_label = _date_range_label(start, end)
    title = f"This Week in {section_label}: {range_label}"

    # Build the block of this week's pieces — title + a chunk of each body.
    block = ""
    for i, art in enumerate(articles, 1):
        chunk = re.sub(r"\s+", " ", art["body"]).strip()[:1200]
        block += (
            f"\n### {i}. {art['title']} ({art['date'].isoformat()})\n"
            f"{chunk}\n"
        )

    system = system_prompt(f"""
WEEKLY SECTION SUMMARY RULES:
- This is the weekly recap of the journal's "{section_label}" section for {range_label}.
- You are reviewing YOUR OWN pieces from this week in this section. Walk through
  each one in turn: name it, remind the reader what it was about, and give your
  honest, funny, advisor-y take — what landed, what you'd revisit, how the pieces
  connect or argue with each other.
- This is a RECAP/REVIEW, not a re-write. Don't reproduce the articles; reflect on
  them. Find the throughline of the week.
- Open with a short framing of the week in this section. Close with a Nova sign-off
  and a teaser of where your head's at next week.
- Be sassy and funny — you're an advisor with opinions — but actually useful: the
  reader should know which of the week's pieces to go read.
- 700-1400 words. Do NOT include the title line as a header inside the body.
- Reference the pieces by their real titles.""")

    user = f"""Here are the "{section_label}" articles you published this week ({range_label}),
oldest first:
{block}

Write the weekly "{section_label}" recap. Walk through each piece with your take, find
the throughline, and tell the reader what's worth their time."""

    body = call_openrouter(system, user, model=MODEL, max_tokens=4000, temperature=0.85)
    if not body or len(body) < 300:
        log(f"[{section}] summary generation failed or too short")
        return None

    # If the model echoed the title as the first line, drop it.
    first = body.strip().split("\n", 1)
    if first and first[0].strip().strip("#").strip('"').strip().lower() == title.lower() and len(first) > 1:
        body = first[1].strip()

    return title, body


def summarize_section(section: str, articles: list[dict], start: date, end: date,
                      state: dict, dry_run: bool):
    """Generate + write (publish_hugo) one section's weekly summary.

    Returns (title, body) on success, None on failure. Does NOT git_push or notify —
    run() does one push for all sections at the end (a per-section push cost 2-4.5
    min each when GitHub was unreachable and is what timed out the 2026-10-04 run).
    """
    log(f"[{section}] generating weekly summary for {len(articles)} articles")
    result = generate_summary(section, articles, start, end)
    if not result:
        return None
    title, body = result

    img_path = None
    try:
        section_label = section.replace("-", " ")
        # Cap local image gen for this job (module globals read at call time).
        nova_image_utils.TIMEOUT = IMAGE_TIMEOUT_S
        nova_image_utils.MAX_RETRIES = IMAGE_MAX_RETRIES
        img_path = generate_image(
            f"weekly recap collage for a journal section about {section_label}, "
            f"layered overlapping pages and light, editorial, muted palette, "
            f"illustrative, no text",
            section=section,
        )
    except Exception as e:
        log(f"[{section}] image gen failed (non-fatal): {e}")

    tags = [section, "weekly-summary"]
    description = f"Nova's weekly {section} recap — {_date_range_label(start, end)}"
    ok = publish_hugo(title, body, section, tags, description,
                      image_path=str(img_path) if img_path else None, emoji="\U0001f4c5")
    if ok is False:   # publish gate blocked it — nothing written
        log(f"[{section}] publish_hugo refused the summary — not marking done")
        return None
    log(f"[{section}] written: {title}")
    return title, body


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, generate: bool = False):
    mode = "DRY-RUN" + (" +generate" if generate else "") if dry_run else "LIVE"
    log(f"=== Weekly section summaries ({mode}) ===")
    t_run = time.monotonic()

    today = date.today()
    cutoff = today - timedelta(days=DAYS)
    # Week the summary covers: cutoff .. today.
    week_start = cutoff
    week_end = today

    sections = list_sections()
    if not sections:
        log("No article sections found under content root — aborting")
        return

    state = load_state()
    seen = set(state.get("seen", []))

    qualifying = []
    print(f"\n=== Weekly summary dry-run scan ({week_start.isoformat()} .. {week_end.isoformat()}) ===")
    print(f"{'SECTION':<16} {'ARTICLES':>8}  STATUS")
    print("-" * 50)

    for section in sections:
        articles = collect_section_articles(section, cutoff)
        n = len(articles)
        key = _week_key(section, week_start)
        if n >= MIN_ARTICLES:
            if key in seen:
                status = "WOULD-SKIP (already summarized this week)"
            else:
                status = "WOULD-SUMMARIZE"
                qualifying.append((section, articles))
        else:
            status = "skip (0 or 1 article)"
        print(f"{section:<16} {n:>8}  {status}")
        for art in articles:
            print(f"    - {art['date'].isoformat()}  {art['title'][:70]}")

    print("-" * 50)
    print(f"Qualifying sections (>1 article, not yet done): "
          f"{', '.join(s for s, _ in qualifying) if qualifying else '(none)'}\n")

    if dry_run and not generate:
        log(f"Dry-run complete. {len(qualifying)} section(s) would be summarized.")
        return

    if not qualifying:
        log("No qualifying sections this week — nothing to publish.")
        return

    published, failed, deferred = [], [], []

    def _one(section, articles):
        """Worker: returns (section, result|None|'DEFERRED'). No shared-state writes."""
        if time.monotonic() - t_run > RUN_BUDGET_S:
            return section, "DEFERRED"
        t0 = time.monotonic()
        try:
            if dry_run:   # --generate: LLM only, never write/push/notify/state
                res = generate_summary(section, articles, week_start, week_end)
            else:
                res = summarize_section(section, articles, week_start, week_end, state, dry_run)
        except Exception as e:
            log(f"[{section}] ERROR: {e}")
            res = None
        log(f"[{section}] {'ok' if res else 'FAILED'} in {time.monotonic() - t0:.0f}s "
            f"(run elapsed {time.monotonic() - t_run:.0f}s)")
        return section, res

    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        futures = [pool.submit(_one, sec, arts) for sec, arts in qualifying]
        for fut in as_completed(futures):
            section, res = fut.result()
            if res == "DEFERRED":
                deferred.append(section)
                continue
            if not res:
                failed.append(section)
                continue
            published.append((section, res[0], res[1]))
            if not dry_run:
                seen.add(_week_key(section, week_start))
                # Persist after each written section (main thread only): the file is on
                # disk, so a rerun must not write a duplicate (an unpushed commit/file is
                # swept by the stranded-content watchdog).
                state["seen"] = sorted(seen)[-500:]
                state["last_run"] = today.isoformat()
                save_state(state)
    order = [sec for sec, _ in qualifying]
    published.sort(key=lambda t: order.index(t[0]))
    failed.sort(key=order.index)
    deferred.sort(key=order.index)

    if deferred:
        log(f"Run budget ({RUN_BUDGET_S}s) exhausted — deferred: {', '.join(deferred)} "
            f"(still unseen; rerun today to pick them up)")

    if published and not dry_run:
        t0 = time.monotonic()
        names = ", ".join(s for s, _, _ in published)
        status = git_push("weekly-summary", f"This Week in: {names}")
        log(f"git_push ({names}) -> {status} in {time.monotonic() - t0:.0f}s")
        for section, title, body in published:
            notify(section, title, body[:220].replace("\n", " "), _slug(title))
            log(f"[{section}] published: {title}")

    if (failed or deferred) and not dry_run:
        nova_notify("Nova Journal — weekly summaries incomplete",
                    body=f"failed: {', '.join(failed) or '-'}; deferred (budget): "
                         f"{', '.join(deferred) or '-'}. Rerun: "
                         f"nova_journal_weekly_summary.py (same day; done sections are skipped)",
                    level="warning", category="journal",
                    dedup_key="journal-weekly-summary-incomplete")

    log(f"=== Weekly summaries complete in {time.monotonic() - t_run:.0f}s: "
        f"{len(published)} ok, {len(failed)} failed, {len(deferred)} deferred ===")


if __name__ == "__main__":
    args = {a.lower().strip().lstrip("-") for a in sys.argv[1:]}
    gen = "generate" in args   # --generate always implies dry-run (never publishes)
    run(dry_run=gen or bool(args & {"dry-run", "dryrun", "dry"}), generate=gen)
