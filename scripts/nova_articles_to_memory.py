#!/usr/bin/env python3
"""nova_articles_to_memory.py — store every journal article Nova has written into
her own vector memory as a thing SHE wrote, with the full article text.

Why a filesystem reconciler (not just a publish_hugo hook): several publishers
(dream_deliver, nova_art_corner, nova_after_dark, …) write their own .md and
bypass publish_hugo, so the only place that sees *every* article is the content
tree itself. Walking it covers backfill AND going-forward in one mechanism.

- source   = 'nova_articles'  (so recall can find "things I've written")
- text     = the whole article (title + body) — the article itself, not chunks
- metadata = {author:'nova', type:'article', section, title, slug, date, tags, path, url}
- dedup    = skip articles whose content path is already stored; the /remember
             service also text_hash-dedups server-side, so re-runs are safe.

Runs under launchd: the Hugo content lives on /Volumes/Data (needs Full Disk
Access). Embedding + insert happen in the memory service (.6:18790/remember).

Usage:
  nova_articles_to_memory.py            # backfill / daily reconcile (all sections)
  nova_articles_to_memory.py <file.md>  # store one article (the publish_hugo hook path)
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

import psycopg2

HUGO_CONTENT = (Path.home() / "nova-journal" / "content")
MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"
SOURCE = "nova_articles"
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_memories")


def log(m):
    print(f"[articles->mem] {m}", flush=True)


def _base_url():
    """Best-effort site baseURL from the Hugo config (for a clickable url in metadata)."""
    root = HUGO_CONTENT.parent
    for name in ("hugo.toml", "config.toml", "hugo.yaml", "config.yaml", "hugo.yml", "config.yml"):
        f = root / name
        try:
            if f.exists():
                m = re.search(r'baseURL\s*[:=]\s*["\']?([^"\'\n]+)', f.read_text(errors="ignore"))
                if m:
                    return m.group(1).strip().rstrip("/")
        except Exception:
            pass
    return None


def parse_md(path: Path):
    """Split Hugo front matter from body; pull title/date/description/tags."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if not raw.lstrip().startswith("---"):
        return None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    fm, body = parts[1], parts[2].strip()
    body = re.sub(r'^\*Published[^\n]*\*\s*', '', body).strip()  # drop the byline line
    if not body or len(body) < 80:
        return None

    def field(key):
        m = re.search(rf'^{key}:\s*(.+)$', fm, re.M)
        return m.group(1).strip().strip('"').strip("'") if m else None

    tags = []
    tm = re.search(r'^tags:\s*(\[.*\])', fm, re.M)
    if tm:
        try:
            tags = json.loads(tm.group(1))
        except Exception:
            tags = []
    return {
        "title": field("title") or path.stem,
        "date": field("date"),
        "description": field("description"),
        "tags": tags,
        "body": body,
    }


def _post(text: str, meta: dict) -> bool:
    payload = json.dumps({
        "text": text, "source": SOURCE, "tier": "long_term",
        "metadata": {**meta, "privacy": "public"},
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL + "?async=1", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20):
        return True


def _store(path: Path, base: str | None) -> bool:
    art = parse_md(path)
    if not art:
        return False
    try:
        rel = str(path.relative_to(HUGO_CONTENT))
    except ValueError:
        rel = path.name
    section = path.parent.name
    slug = path.stem
    meta = {
        "author": "nova", "type": "article", "section": section,
        "title": art["title"], "slug": slug, "date": art["date"],
        "tags": art["tags"], "path": rel,
        "stored_by": "nova_articles_to_memory.py",
    }
    if base:
        clean = re.sub(r'^\d{4}-\d{2}-\d{2}-', '', slug)  # strip the date prefix Hugo slugs carry
        meta["url"] = f"{base}/{section}/{clean}/"
    text = f"{art['title']}\n\n{art['body']}"
    try:
        return _post(text, meta)
    except Exception as e:
        log(f"store failed {rel}: {e}")
        return False


def remember_article(path) -> bool:
    """Store a single article (used as the publish_hugo hook). Relies on the
    service's text_hash dedup, so re-publishing the same text is a no-op."""
    return _store(Path(path), _base_url())


def existing_paths(cur) -> set:
    cur.execute(
        "SELECT metadata->>'path' FROM memories "
        "WHERE source=%s AND metadata->>'path' IS NOT NULL", (SOURCE,))
    return {r[0] for r in cur.fetchall()}


def remember_all() -> tuple[int, int]:
    if not HUGO_CONTENT.exists():
        log(f"content dir not found: {HUGO_CONTENT} (FDA? run under launchd)")
        return 0, 0
    base = _base_url()
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    seen = existing_paths(cur)
    files = [f for f in sorted(HUGO_CONTENT.rglob("*.md")) if not f.name.startswith("_index")]
    added = skipped = 0
    for f in files:
        rel = str(f.relative_to(HUGO_CONTENT))
        if rel in seen:
            skipped += 1
            continue
        if _store(f, base):
            added += 1
            if added % 50 == 0:
                log(f"...{added} stored")
        else:
            skipped += 1
    conn.close()
    log(f"done: {added} stored, {skipped} skipped/seen, {len(files)} md files, "
        f"{len(seen)} already in memory")
    return added, skipped


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        ok = remember_article(args[0])
        print("stored" if ok else "skipped/failed")
        sys.exit(0 if ok else 1)
    remember_all()
