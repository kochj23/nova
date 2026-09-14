#!/usr/bin/env python3
"""nova_reddit_rss_ingest.py [sub[,sub...] vector] — ingest subreddits via Reddit RSS.

Reddit IP-blocks the .json API here (403); the .rss feeds return 200, so we crawl those.
With no args, crawls the full SUBS map (each sub -> its own vector); args override for testing.
Light-touch to dodge 429s: hot+new feeds only, comments skipped on the first seed and capped
after, 22s between requests with 429 backoff, and a single-instance lock so runs never overlap
(overlapping crawls were what triggered the throttling). Dedupes by post id in PG
(reddit_rss_seen). RSS serves only the recent window, so a scheduled re-run keeps it current.
Slack (#nova-info) posts only on real activity — no per-run "started/blocked/0-new" banners.
"""
import fcntl
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
DELAY = 22          # seconds between Reddit requests (avoid 429)
CHUNK = 1500
SORTS = [("", "")]  # hot feed only — one request per sub keeps us well under Reddit's RSS throttle
COMMENT_CAP = 3     # max comment-fetches per sub per run (0 on first seed) — bounds request volume
ROTATE_BATCH = 4    # non-fishbowl subs crawled per run; rotate the rest so each run stays tiny
DEADLINE_S = 750    # bail cleanly before the scheduler's 900s timeout kills us mid-run
START_TS = time.time()

# 2026-08-24: 43 consecutive scheduler timeouts. Root cause: on 429 the old code slept
# 60s and retried IN-RUN (up to 5x), blowing the 900s budget, and the next run hammered
# Reddit again while still throttled — which kept the throttle hot for days. New shape:
# first 429 aborts the whole pass and persists a cooldown (honoring Retry-After, growing
# 15m -> 4h across consecutive throttled runs); runs during cooldown exit immediately.


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after

# subreddit -> vector. Restored from the retired .json ingester (Reddit now 403-blocks that API);
# RSS is the only working path. Per-sub vector so each lands in the same place it did before.
FISHBOWL_SUBS = ["TheTpGentleman", "WatchesCirclejerk"]   # active drama — crawled EVERY run
SUBS = {
    "burbank": "burbank", "glendale": "local", "Sovereigncitizen": "reddit",
    "SipsTea": "reddit", "lazerpig": "reddit", "vibecoding": "reddit",
    "3Dprinting": "reddit", "avesLA": "socal_rave", "CarPlay": "automotive",
    "chaoticgood": "reddit", "ClaudeCode": "reddit",
    "TheTpGentleman": "fishbowl", "WatchesCirclejerk": "fishbowl",
}
STATE_FILE = os.path.join(tempfile.gettempdir(), "nova_reddit_rss.offset")


def rotating_targets():
    """Fishbowl subs every run + a rotating window of the others, so each run makes only a
    handful of requests. Persists the rotation offset so successive runs cover everything."""
    others = [s for s in SUBS if s not in FISHBOWL_SUBS]
    try:
        off = int(open(STATE_FILE).read().strip()) % len(others)
    except Exception:
        off = 0
    window = (others * 2)[off:off + ROTATE_BATCH]      # wrap around the end
    try:
        open(STATE_FILE, "w").write(str((off + ROTATE_BATCH) % len(others)))
    except Exception:
        pass
    picked = FISHBOWL_SUBS + window
    return {s: SUBS[s] for s in picked}


def log(m):
    print(f"[reddit-rss] {m}", flush=True)


def slack(m):
    try:
        nova_config.post_both(m, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        log(f"slack failed: {e}")


def _db():
    c = psycopg2.connect(DSN); c.autocommit = True
    return c


def ensure(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS reddit_rss_seen ("
                "subreddit text, post_id text, seen_at timestamptz DEFAULT now(), "
                "PRIMARY KEY (subreddit, post_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS reddit_rss_state ("
                "key text PRIMARY KEY, value text, updated_at timestamptz DEFAULT now())")


def get_state(cur, key, default=""):
    cur.execute("SELECT value FROM reddit_rss_state WHERE key=%s", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_state(cur, key, value):
    cur.execute("INSERT INTO reddit_rss_state (key, value, updated_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                (key, str(value)))


class Deadline(Exception):
    """Raised inside a crawl when the run budget is exhausted — caller exits cleanly."""


def check_deadline():
    if time.time() - START_TS > DEADLINE_S:
        raise Deadline()


def fetch(url):
    """GET a Reddit RSS URL. Raises RateLimited on 429 — no in-run retry: more requests
    while throttled just extend the throttle, and the sleeps blow the scheduler budget.
    Raises Deadline when the run budget is spent — the between-subs check alone let a
    slow sub (comments + retries) started at 740s run past the scheduler's 900s axe."""
    check_deadline()
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/atom+xml,application/xml,text/xml"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                raise RateLimited(int(ra) if ra and ra.isdigit() else 0)
            log(f"HTTP {e.code} on {url[:70]}"); return None
        except Exception:
            time.sleep(10)
    return None


def parse_entries(xml):
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def g(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", e, re.S)
            return html.unescape(re.sub("<[^>]+>", "", m.group(1))).strip() if m else ""
        cm = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
        content = re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", cm.group(1)))).strip() if cm else ""
        out.append({"id": g("id"), "title": g("title"), "author": g("author"),
                    "pub": g("published"), "content": content})
    return out


def remember(text, meta):
    check_deadline()   # chunk loops can be long; a mid-post abort just re-crawls the post next run
    payload = json.dumps({"text": text, "source": meta["vector"], "tier": "long_term",
                          "metadata": {**meta, "privacy": "private"}}).encode()
    req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception:
        return False


def comments(sub, short_id):
    xml = fetch(f"https://www.reddit.com/r/{sub}/comments/{short_id}/.rss")
    time.sleep(DELAY)
    if not xml:
        return ""
    return "\n".join(f"u/{e['author'].replace('/u/','')}: {e['content']}"
                     for e in parse_entries(xml) if e["content"])


def crawl_sub(cur, sub, vector):
    """Crawl one subreddit via RSS. Returns (new_count, sample_body, was_first_seed).

    Comments are the heaviest cost (one 22s request each), so we skip them entirely on the
    first seed (dozens of posts) and cap them at COMMENT_CAP on incremental runs."""
    cur.execute("SELECT post_id FROM reddit_rss_seen WHERE subreddit=%s", (sub,))
    seen = {r[0] for r in cur.fetchall()}
    first_seed = len(seen) == 0
    cm_budget = 0 if first_seed else COMMENT_CAP
    new, sample = 0, ""
    for sort, q in SORTS:
        path = f"/{sort}" if sort else ""
        xml = fetch(f"https://www.reddit.com/r/{sub}{path}/.rss{q}")
        time.sleep(DELAY)
        if not xml:
            continue
        for p in parse_entries(xml):
            pid = p["id"]
            if not pid or pid in seen:
                continue
            seen.add(pid)
            cm = ""
            if cm_budget > 0:
                cm = comments(sub, pid.replace("t3_", ""))
                cm_budget -= 1
            body = f"[r/{sub} post by {p['author']}] {p['title']}\n{p['content']}".strip()
            if cm:
                body += "\n\n--- comments ---\n" + cm
            meta = {"vector": vector, "type": "reddit", "subreddit": sub,
                    "post_id": pid, "title": p["title"][:200], "author": "fishbowl"}
            for i in range(0, max(1, len(body)), CHUNK):
                remember(body[i:i + CHUNK], {**meta, "idx": i // CHUNK})
            cur.execute("INSERT INTO reddit_rss_seen (subreddit,post_id) VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", (sub, pid))
            new += 1
            if not sample:
                sample = body[:600].strip()
    return new, sample, first_seed


def main():
    # No args → crawl the full configured SUBS map. Args override for testing: "<sub[,sub]> <vector>".
    if len(sys.argv) >= 3:
        targets = {s.split("/r/")[-1].split("/")[0].replace("r/", "").strip("/"): sys.argv[2]
                   for s in sys.argv[1].split(",") if s.strip()}
    else:
        targets = rotating_targets()   # fishbowl every run + a rotating batch of the others

    # Single-instance lock: overlapping runs double the request rate and are exactly what
    # triggered the 429s. If another crawl holds it, skip this pass rather than pile on.
    lf = open(os.path.join(tempfile.gettempdir(), "nova_reddit_rss.lock"), "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another crawl is already running — skipping this pass"); return

    conn = _db(); cur = conn.cursor(); ensure(cur)

    # Respect an active rate-limit cooldown: exit clean immediately (a cooled-down skip is
    # correct behavior, not a failure — the scheduler timeout alarms were the old spiral).
    cooldown_until = float(get_state(cur, "cooldown_until", "0") or 0)
    if time.time() < cooldown_until:
        mins = int((cooldown_until - time.time()) / 60)
        log(f"rate-limit cooldown active for another {mins}m — skipping this pass")
        conn.close()
        return

    seeded = []
    incr = []             # (sub, new) — general incremental activity, rolled up into ONE ping
    fishbowl_pings = []   # fishbowl keeps its own sampled per-sub pings (the drama IS the point)
    for sub, vector in targets.items():
        if time.time() - START_TS > DEADLINE_S:
            log("deadline reached — stopping cleanly (rotation offset resumes next run)")
            break
        try:
            new, sample, was_seed = crawl_sub(cur, sub, vector)
        except Deadline:
            log("deadline reached mid-crawl — stopping cleanly (rotation offset resumes next run)")
            break
        except RateLimited as rl:
            # First 429 ends the pass. Cooldown = max(Retry-After, 15m), doubling across
            # consecutive throttled runs up to 4h; a clean run resets the streak.
            streak = int(get_state(cur, "throttle_streak", "0") or 0) + 1
            cool = max(rl.retry_after, min(900 * (2 ** (streak - 1)), 14400))
            set_state(cur, "throttle_streak", streak)
            set_state(cur, "cooldown_until", time.time() + cool)
            log(f"429 from Reddit — aborting pass, cooldown {cool // 60}m (streak {streak})")
            conn.close()
            return
        except Exception as e:
            log(f"r/{sub}: error {e}"); continue
        # Slack only on real activity. First-seed batch → one summary. Incremental new posts →
        # a single per-run ROLLUP (not a per-sub ping storm — that was a top nova-feed noise
        # source, and the posts are already ingested to the vector; the Slack line is pure FYI).
        # Fishbowl is the exception: it keeps its sampled per-sub ping. No "0 new" noise.
        if was_seed:
            seeded.append(f"r/{sub}→{vector} ({new})")
            log(f"r/{sub}: SEEDED {new} posts (comments skipped)")
        elif new:
            if vector == "fishbowl":
                msg = f":mag: *Reddit RSS* — r/{sub} → {vector}: {new} new post(s)."
                if sample:
                    msg += f"\n*Sample:*\n> {sample}…"
                fishbowl_pings.append(msg)
            else:
                incr.append((sub, new))
            log(f"r/{sub}: {new} new")
        else:
            log(f"r/{sub}: no new posts")
    # Clean pass (no 429): reset the throttle streak so future cooldowns start small again.
    set_state(cur, "throttle_streak", 0)
    set_state(cur, "cooldown_until", 0)

    if incr:
        total = sum(n for _, n in incr)
        subs = ", ".join(f"r/{s} ({n})" for s, n in sorted(incr, key=lambda x: -x[1]))
        slack(f":mag: *Reddit RSS* — {total} new post(s) across {len(incr)} sub(s): {subs}")
    for m in fishbowl_pings:
        slack(m)
    if seeded:
        slack(":seedling: *Reddit RSS restored* — first-seed pass ingested: " + ", ".join(seeded))
    conn.close()


if __name__ == "__main__":
    main()
