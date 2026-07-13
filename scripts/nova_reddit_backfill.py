#!/usr/bin/env python3
"""nova_reddit_backfill.py <subreddit> <vector> — one-time DEEP ingest of a subreddit's
posts + comments into Nova's memory (source=<vector>), verbatim.

Paginates reddit's public .json across new/hot/top/controversial/rising (no auth),
dedupes by post id, fetches each post's comment tree. "Everything available" = as deep
as reddit serves (~1000 per listing). Complements the ongoing nova_reddit_ingest.py.
"""
import json
import sys
import time
import urllib.request

MEMORY_URL = "http://192.168.1.6:18790/remember"
UA = "nova-fishbowl-backfill/1.0"
SLEEP = 1.6
PAGES_PER_SORT = 11
CHUNK = 1500


def log(m):
    print(f"[reddit-backfill] {m}", flush=True)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == 3:
                log(f"GET failed {url[:80]}: {e}"); return None
            time.sleep(6)
    return None


def remember(text, meta):
    payload = json.dumps({"text": text, "source": meta["vector"], "tier": "long_term",
                          "metadata": {**meta, "privacy": "private"}}).encode()
    req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception:
        return False


def comments_text(sub, pid):
    d = get(f"https://www.reddit.com/r/{sub}/comments/{pid}.json?limit=200&raw_json=1")
    if not d or len(d) < 2:
        return ""
    out = []

    def walk(children):
        for c in children:
            k = c.get("data", {})
            if k.get("body"):
                out.append(f"u/{k.get('author','?')}: {k['body']}")
            repl = k.get("replies")
            if isinstance(repl, dict):
                walk(repl.get("data", {}).get("children", []))
    walk(d[1].get("data", {}).get("children", []))
    return "\n".join(out)


def main():
    arg = sys.argv[1].rstrip("/")
    if "/r/" in arg:
        sub = arg.split("/r/")[1].split("/")[0]
    elif arg.startswith("r/"):
        sub = arg[2:]
    else:
        sub = arg.split("/")[-1]
    vector = sys.argv[2]
    seen = set(); total = 0
    log(f"deep backfill r/{sub} -> source={vector}")
    for sort in ["new", "hot", "top", "controversial", "rising"]:
        after = None; pages = 0
        while pages < PAGES_PER_SORT:
            t = "&t=all" if sort in ("top", "controversial") else ""
            url = (f"https://www.reddit.com/r/{sub}/{sort}.json?limit=100&raw_json=1{t}"
                   + (f"&after={after}" if after else ""))
            d = get(url); time.sleep(SLEEP)
            if not d:
                break
            children = d.get("data", {}).get("children", [])
            if not children:
                break
            for p in children:
                k = p.get("data", {}); pid = k.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                title = k.get("title", ""); body_self = k.get("selftext", "") or ""
                author = k.get("author", "?"); score = k.get("score", "")
                ct = comments_text(sub, pid); time.sleep(SLEEP)
                body = f"[r/{sub} post by u/{author} ({score} pts)] {title}\n{body_self}".strip()
                if ct:
                    body += "\n\n--- comments ---\n" + ct
                meta = {"vector": vector, "type": "reddit", "subreddit": sub,
                        "post_id": pid, "title": title[:200], "author": "fishbowl"}
                for i in range(0, max(1, len(body)), CHUNK):
                    remember(body[i:i + CHUNK], {**meta, "idx": i // CHUNK})
                total += 1
                if total % 25 == 0:
                    log(f"...{total} posts ingested ({len(seen)} unique)")
            after = d.get("data", {}).get("after")
            pages += 1
            if not after:
                break
    log(f"DONE: {total} posts ingested from r/{sub}")


if __name__ == "__main__":
    main()
