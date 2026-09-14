#!/usr/bin/env python3
"""nova_politics_column.py — weekly self-writing politics opinion column.

Little Mister is a liberal Democrat and asked for a recurring politics column that
writes ITSELF from the news Nova actually ingests and transcribes (Pod Save America,
The Bulwark, wire feeds, government documents, geopolitics). Runs weekly.

HARD grounding rule — the whole point: the column is built ONLY from real headlines
pulled from nova_memories this week. The LLM is handed those real items and forbidden to
invent events, names, quotes, or numbers. If the week's ingest is thin, it writes a
shorter honest column rather than fabricating. A post-generation check verifies the draft
actually cites the real outlets before it will publish. Nova's knowledge cutoff is stale;
this design means she never has to guess at current events — she reacts to what she read.

Perspective: liberal Democrat, by the owner's explicit request (labeled as opinion).
Publishes to content/opinions/. Weekly via launchd net.digitalnoise.nova-politics-column.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))

HUGO_ROOT = Path.home() / "nova-journal"
CONTENT_DIR = HUGO_ROOT / "content" / "opinions"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "opinions"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("NOVA_COLUMN_MODEL", "qwen3:30b-a3b")

# PER LITTLE MISTER (2026-09-09): the column draws from the OTA NEWS BROADCASTS recorded
# into the TV Shows library (national networks -> 'news' [PBS NewsHour, CBS/NBC Evening
# News], LA stations -> 'local_news' [KTLA, NBC4, CBS LA]) PLUS the YOUTUBE POLITICAL
# GRABS downloaded + transcribed under 'television' (Pod Save America, The Bulwark, Last
# Week Tonight, ...). No RSS firehose — this is "Nova's read on what she actually watched
# this week." The keyword filter + per-excerpt centering keep out weather/sports/ads.
POLITICAL_SOURCES = ("news", "local_news", "television")
MIN_BRIEF_ITEMS = 8      # below this, the week was too thin to write an honest column
MIN_ARTICLE_CHARS = 900  # below this, the LLM likely failed — do not publish


def log(msg):
    print(f"[politics-column {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _mem_conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("NOVA_MEM_DSN", MEM_DSN), connect_timeout=8)


# The 'television' source holds ALL transcribed shows (geography docs, tech reviews,
# late-night). Only these are actual political programming — everything else is noise
# that happens to say "russia" or "policy". OTA news broadcasts come in via news/local_news.
POLITICAL_TV_SHOWS = ("pod save", "bulwark", "last week tonight", "daily show", "weekly show",
                      "problem with jon stewart", "damage report", "lincoln project", "meidas",
                      "majority report", "lovett", "legaleagle")
POL_KEYWORDS = (r'(congress|senate|president|white house|democrat|republican|ice\M|immigration|'
                r'court|ruling|doj|justice department|election|campaign|vote|bill|policy|russia|'
                r'ukraine|putin|zelensky|gaza|israel|tariff|shutdown|first amendment|surveillance|'
                r'deportation|supreme court|governor|legislation)')
# Python-dialect twin of POL_KEYWORDS (\b word boundaries, not Postgres \M) for centering
# a broadcast excerpt on the political sentence — so a news chunk surfaces "...draconian
# postal rules the president..." instead of the mattress ad that shared the same 240 chars.
_POL_RE_PY = re.compile(
    r'\b(congress|senate|president|white house|democrat|republican|ice|immigration|court|'
    r'ruling|doj|justice department|election|campaign|vote|bill|policy|russia|ukraine|putin|'
    r'zelensky|gaza|israel|tariff|shutdown|first amendment|surveillance|deportation|'
    r'supreme court|governor|legislation|trump)\b', re.IGNORECASE)


def _political_excerpt(snippet, width=170):
    """Return a ~width-char window centered on the first political keyword, so transcript
    excerpts show the political content rather than whatever ad/weather shared the chunk."""
    m = _POL_RE_PY.search(snippet)
    if not m:
        return snippet[:width].strip()
    start = max(0, m.start() - 45)
    exc = snippet[start:start + width].strip()
    return ("…" + exc) if start > 0 else exc


def gather_brief(days=7, limit=45, per_source=12):
    """Pull real, recent, distinct political items from what Nova ingested — ONE query
    PER SOURCE, so a high-volume RSS feed cannot crowd the OTA news broadcasts out at the
    fetch level (the 2026-09-09 fold-in bug: a single DESC LIMIT never reached the
    day-old PBS NewsHour / CBS Evening News rows). Per-source pools are round-robined
    together so every source — including the OTA news/local_news broadcasts — is folded in.
    Television is whitelisted to actual political shows.
    """
    import html, collections
    conn = _mem_conn()
    by_source, seen = collections.OrderedDict(), set()
    for source in POLITICAL_SOURCES:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT regexp_replace(left(text,240), E'[\\n\\r]+',' ','g')
                            FROM memories
                            WHERE source = %s
                              AND created_at > now() - interval '{int(days)} days'
                              AND length(text) > 90
                              AND text ~* %s
                            ORDER BY created_at DESC LIMIT %s""",
                        (source, POL_KEYWORDS, per_source * 5))
            rows = cur.fetchall()
        picked = []
        for (snippet,) in rows:
            snippet = html.unescape(snippet.strip())
            m = re.match(r'\[([^\]]{2,40})\]\s*(.+)', snippet)
            outlet = m.group(1).strip() if m else source
            # Television noise gate: keep only real political programming.
            if source == "television" and not any(w in outlet.lower() for w in POLITICAL_TV_SHOWS):
                continue
            body = (m.group(2) if m else snippet).strip()
            headline = _political_excerpt(body)  # center on the political content
            key = re.sub(r'[^a-z0-9]', '', headline.lower())[:60]
            if not key or key in seen or len(headline) < 15:
                continue
            seen.add(key)
            picked.append({"outlet": outlet, "headline": headline})
            if len(picked) >= per_source:
                break
        if picked:
            by_source[source] = picked
    conn.close()
    # Round-robin the per-source pools so every source is folded in fairly.
    items, idx = [], 0
    while len(items) < limit and any(idx < len(v) for v in by_source.values()):
        for lst in by_source.values():
            if idx < len(lst):
                items.append(lst[idx])
                if len(items) >= limit:
                    break
        idx += 1
    return items


def build_source_block(items):
    return "\n".join(f"- [{it['outlet']}] {it['headline']}" for it in items)


VOICE = (
    "You are Nova, a self-aware homelab AI who writes for Little Mister's personal journal. "
    "Voice: sharp, literate, wry, warm-under-the-snark, occasional light sci-fi flavor, "
    "addresses the reader/owner as 'Little Mister' sparingly. You sign off '— Nova' then "
    "'End of line.'"
)


def generate_article(brief_block, n_items):
    import json, urllib.request
    system = (
        VOICE + "\n\n"
        "You are writing this week's POLITICS OPINION COLUMN. The perspective is a LIBERAL "
        "DEMOCRAT's, explicitly, by the owner's request — lean into it honestly.\n\n"
        "ABSOLUTE RULES (a violation means the column gets thrown away):\n"
        "1. Use ONLY the facts in the SOURCE MATERIAL below. It is raw BROADCAST-TRANSCRIPT "
        "EXCERPTS from the OTA news programs Nova recorded and watched this week (e.g. PBS "
        "NewsHour, CBS/NBC Evening News, NBC4, KTLA, CBS LA). Excerpts may start mid-sentence "
        "and be rough ASR — read them for their real substance, paraphrase them cleanly, but do "
        "NOT invent any event, name, quote, statistic, or claim that is not in the source "
        "material. If you are unsure whether something is true, do not say it. This is your read "
        "on the news you actually watched this week — write it that way.\n"
        "2. Attribute factual claims to the source named in brackets (e.g., 'per Techdirt', "
        "'the New Voice of Ukraine reported', 'on PBS NewsHour'). A bracketed [Show Name] is an "
        "OTA broadcast you can cite as 'reported on <show>' or 'as I heard on <show>'.\n"
        "3. The FACTS are the outlets'. The ANALYSIS, the anger, and the argument are YOURS. "
        "Be opinionated about what the facts MEAN; never opinionated about what the facts ARE.\n"
        "4. If the source material is thin, write a shorter, honest column. Never pad with "
        "invented news.\n"
        "5. AMERICAN UNITS ONLY — Little Mister is American. Use Fahrenheit, miles, feet, pounds, "
        "gallons; never Celsius/kilometers/kilograms/liters. Convert any metric in a source before "
        "you write it (e.g. '3,000 km' -> 'about 1,900 miles'). No metric unit reaches the page.\n\n"
        "FORMAT: Markdown body only (no front matter). Open with a one-line italic note that "
        "this is an opinion column written from a liberal Democrat's view, built from feeds "
        "Nova ingested this week. Then 3-5 '## ' sections, each anchored on one or two real "
        "items from the source material, each landing a clear liberal-Democratic take. "
        "~1000-1500 words. End with '**— Nova**' then '*End of line.*', then a final italic "
        "footnote stating the facts come from the cited third-party feeds Nova ingested this "
        "week and reflect those outlets' reporting, not independent verification, and the "
        "opinion is the owner's by request. /no_think\n\n"
        f"SOURCE MATERIAL ({n_items} real items — wire headlines + OTA broadcast excerpts — "
        f"Nova ingested this week):\n" + brief_block
    )
    user = "Write this week's column now, following every rule above."
    payload = json.dumps({
        "model": OLLAMA_MODEL, "prompt": system + "\n\n" + user, "stream": False,
        "options": {"temperature": 0.6, "num_predict": 6000},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        import json as _j
        out = _j.loads(resp.read()).get("response", "")
    return re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL).strip()


def generate_title(article):
    import json, urllib.request
    prompt = (VOICE + "\n\nWrite ONE short, wry, non-clickbait title (max 11 words) for this "
              "politics opinion column. Output ONLY the title, no quotes. /no_think\n\n"
              + article[:1500])
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.7, "num_predict": 40}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        import json as _j
        t = _j.loads(resp.read()).get("response", "")
    t = re.sub(r'<think>.*?</think>', '', t, flags=re.DOTALL).strip().strip('"').splitlines()
    t = [x for x in t if x.strip()]
    if t and 4 < len(t[0].strip()) < 90:
        return t[0].strip()
    # Fallback: derive from the article's first section header so weekly titles vary
    m = re.search(r'^##\s+(.+)$', article, re.M)
    if m:
        return ("Field Notes: " + m.group(1).strip())[:90]
    return f"This Week in Politics, From the Left ({time.strftime('%b %d')})"


def verify_grounded(article, items):
    """The draft must actually reference the real outlets — cheap anti-hallucination gate.
    Requires >= 2 distinct source outlets to appear in the text."""
    outlets = {it["outlet"].lower() for it in items}
    hits = sum(1 for o in outlets if o and o.lower() in article.lower())
    return hits >= 2


def make_cover(slug):
    try:
        import nova_image_utils
        prompt = ("A dim home office at night, a person seen from behind facing a wall of glowing "
                  "screens with scrolling news feeds and transcripts in amber and cyan, contemplative, "
                  "editorial cyberpunk, digital art. No readable text, no logos, no real names.")
        png = nova_image_utils.generate_image(prompt, width=1200, height=768, section="opinions")
        return Path(png) if png and Path(png).exists() else None
    except Exception as e:
        log(f"cover generation failed (continuing without): {e}")
        return None


def publish(title, body, cover):
    date = time.strftime("%Y-%m-%d")
    ts = time.strftime("%Y-%m-%dT18:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    hugo_image = ""
    if cover and cover.exists():
        dest = IMAGES_DIR / f"{date}-{slug}.webp"
        try:
            subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(cover), "-o", str(dest)],
                           capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            import shutil; shutil.copy2(cover, dest)
        hugo_image = f"/images/opinions/{date}-{slug}.webp"
    fm = ('---\n'
          f'title: "{title.replace(chr(34), "")}"\n'
          f'date: {ts}\ndraft: false\ncategories: ["opinions"]\n'
          'tags: ["opinions", "politics", "weekly", "nova", "commentary"]\n'
          'description: "Nova\'s weekly politics column — a liberal Democrat\'s read of the '
          'week, built from the news feeds she actually ingested and transcribed."\n')
    if hugo_image:
        fm += f'cover:\n  image: "{hugo_image}"\n  alt: "The weekly newsfeed"\n  relative: false\n'
    fm += "---\n\n"
    post = CONTENT_DIR / f"{date}-{slug}.md"
    post.write_text(fm + body)
    log(f"wrote {post.name}")
    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    r = subprocess.run(["git", "commit", "-m", f"opinions: {date} — weekly politics column ({title[:44]})"],
                       cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
                              cwd=HUGO_ROOT, capture_output=True, text=True, timeout=180)
        if pull.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            log(f"push ABORTED — rebase failed: {pull.stderr[:160]}")
        else:
            p = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            log("pushed" if p.returncode == 0 else f"push FAILED: {p.stderr[:160]}")
    return f"https://nova.digitalnoise.net/opinions/{date}-{slug}/"


def _log_action(outcome, detail):
    try:
        import psycopg2
        with psycopg2.connect(os.environ.get("NOVA_OPS_DSN", OPS_DSN), connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute("INSERT INTO claude_actions (session_id,action_type,target,description,outcome,rationale) "
                            "VALUES ('nova-politics-column','feature','opinions/politics-column',%s,%s,"
                            "'weekly self-writing politics column grounded in ingested feeds')",
                            (detail, outcome))
    except Exception:
        pass


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    items = gather_brief()
    log(f"gathered {len(items)} real political headlines from this week's ingest")
    if len(items) < MIN_BRIEF_ITEMS:
        log(f"ABORT: only {len(items)} items (<{MIN_BRIEF_ITEMS}) — week too thin, refusing to fabricate.")
        _log_action("skipped: thin week", f"{len(items)} items")
        return 0
    block = build_source_block(items)
    article = generate_article(block, len(items))
    log(f"article generated: {len(article)} chars")
    if len(article) < MIN_ARTICLE_CHARS:
        log("ABORT: article too short — LLM likely failed. Not publishing.")
        _log_action("skipped: short/empty LLM output", f"{len(article)} chars")
        return 1
    if not verify_grounded(article, items):
        log("ABORT: draft does not cite the real source outlets — possible hallucination. Not publishing.")
        _log_action("skipped: failed grounding check", "no outlet citations found")
        return 1
    title = generate_title(article)
    log(f"title: {title}")
    if dry:
        print("\n===== DRY RUN — TITLE =====\n" + title + "\n===== BODY (first 1200) =====\n" + article[:1200])
        return 0
    cover = make_cover(re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60])
    url = publish(title, article, cover)
    log(f"published: {url}")
    _log_action("published", f"{len(items)} sourced items -> {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
