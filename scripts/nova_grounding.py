#!/usr/bin/env python3
"""nova_grounding.py — the "Grounded in N memories · M sources" line at the foot of every article.

Jordan (2026-10-19): every published article carries a small grounding line. N is the memories the
article cited (publish_hugo's cited_memory_ids, the rows it records in nova_ops.article_citations);
M is the source items in the SOURCES block the generator handed publish_hugo (the "[n] (source, url)"
items sources_from_memories renders, else the distinct public URLs in it). The line is a <details>
summary: clicking it expands the de-identified list of what the article stood on.

Public-journal privacy rules apply to every label: no IPs, household names, home paths, camera names,
presence/sleep, nothing about Jordan's employer, no "OpenClaw". A memory from a private source, a
private-tagged memory, or an operational stream (cameras, presence, scanner, mail...) is never itemised —
it is counted in one generic line. No citations and no sources -> no line at all (no "0 memories").

publish_hugo calls footer(cited_memory_ids, sources) — one call, fail-open. --backfill [--days 30]
adds the line to recent articles that have article_citations rows (conservative: skips rolling
evergreen pages and anything already carrying the line). --dry-run reports without writing.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MARKER = 'class="nova-grounding"'
MAX_ITEMS = 40
HUGO_ROOT = Path.home() / "nova-journal"
# Rolling pages regenerated in full every day: they get the line on their next publish; never backfilled.
ROLLING_SLUGS = {"the-fishbowl", "daily-watch"}

_ITEM_RX = re.compile(r"^\[(\d+)\]\s*\(([^)\n]*)\)", re.M)
_URL_RX = re.compile(r"https?://[^\s)\]>\"']+")
_IP_RX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){3,7}\b", re.I)
_PRIVATE_HOST_RX = re.compile(r"(^|\.)(local|lan|internal|home|localhost)$|^(localhost|127\.|10\.|192\.168\.|172\.)", re.I)
_INTERNAL_DOMAIN = "digitalnoise.net"
_PUBLIC_OWN_HOSTS = {"nova.digitalnoise.net"}
# Operational streams: never itemised (they describe the house, its people, or its radios).
_OPS_RX = re.compile(r"camera|protect|frame_vision_cam|presence|sleep|health|imessage|email|mail|calendar|"
                     r"location|scanner|p25|adsb|fishbowl_presence|sensor|telemetry|syslog|alert|home_|"
                     r"hue|lutron|unifi|network|security_cam|personal|work|quarantine:", re.I)
_TITLE_KEYS = ("show", "title", "book", "series", "channel_name", "feed")
_WORK_TYPES_OK = {"tv_transcript", "tvshow_transcript", "comedy_transcript", "movie_screenplay", "screenplay",
                  "movie_script", "film_documentary_transcript", "course_lecture", "book", "wikipedia_book",
                  "wikipedia", "encyclopedia", "article", "blog_post", "gov_rss", "web_page", "youtube_transcript",
                  "video_transcript", "plot", "work_lessons", "reddit", ""}


def _scrub(text: str) -> str:
    """Every label passes the journal's own scrubbers plus IP / OpenClaw / home-path removal."""
    t = str(text or "")
    try:
        import nova_journal as nj
        t = nj.scrub_home_paths(nj.scrub_pii(t))
    except Exception:  # noqa: BLE001 — fall back to the local rules below
        t = re.sub(r"/Users/[^/\s]+/\S*", "", t)
    t = _IP_RX.sub("", t)
    t = re.sub(r"open\s*claw", "Nova", t, flags=re.I)
    t = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]", "", t)          # YouTube video ids
    t = re.sub(r"\.(ts|mkv|mp4|m4v|avi|mov|md|txt|pdf)\b", "", t)
    return re.sub(r"\s+", " ", t).strip(" -·,")


def _blocked(text: str) -> bool:
    try:
        import nova_config
        return bool(nova_config._contains_blocked_content(text))
    except Exception:  # noqa: BLE001 — cannot check the employer list: treat as blocked (fail closed)
        return True


def _private_source(source: str) -> bool:
    try:
        import nova_config
        return bool(nova_config.is_private_source(source or ""))
    except Exception:  # noqa: BLE001 — fail closed
        return True


def friendly_source(source: str) -> str:
    s = (source or "memory").split(":")[-1].replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else "Memory"


def public_url(url: str) -> str | None:
    """The URL when it is safe to show on the public site, else None."""
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or _IP_RX.fullmatch(host) or _PRIVATE_HOST_RX.search(host):
        return None
    if host.endswith(_INTERNAL_DOMAIN) and host not in _PUBLIC_OWN_HOSTS:
        return None
    if _blocked(url) or re.search(r"open\s*claw", url, re.I):
        return None
    return url.split("#")[0]


def parse_source_items(sources: str | None) -> list[dict]:
    """The SOURCES block's items: [{'source','url'}]. "[n] (source, url)" items first; when the block
    is free text, its distinct URLs. Order kept, duplicates collapsed."""
    text = sources or ""
    items, seen = [], set()
    for _n, tag in _ITEM_RX.findall(text):
        src, _, url = tag.partition(",")
        key = (src.strip(), url.strip())
        if key in seen:
            continue
        seen.add(key)
        items.append({"source": src.strip(), "url": url.strip()})
    if items:
        return items
    for url in _URL_RX.findall(text):
        url = url.rstrip(".,;:")
        if url in seen:
            continue
        seen.add(url)
        items.append({"source": "web", "url": url})
    return items


def source_label(item: dict) -> str | None:
    """Markdown line for one source item, or None when it must not be shown."""
    src = item.get("source") or ""
    if src != "web" and (_private_source(src) or _OPS_RX.search(src)):
        return None
    url = public_url(item.get("url") or "") if item.get("url") else None
    if url:
        host = urlparse(url).hostname or url
        host = host[4:] if host.startswith("www.") else host
        label = f"[{_scrub(host)}]({url})"
        return f"{_scrub(friendly_source(src))} · {label}" if src and src != "web" else label
    if src == "web":
        return None
    return _scrub(friendly_source(src))


def memory_label(source: str, md: dict | None) -> str | None:
    """De-identified label for a cited memory, or None (not itemised)."""
    md = md if isinstance(md, dict) else {}
    if _private_source(source) or _OPS_RX.search(source or ""):
        return None
    if str(md.get("privacy") or "").lower() == "private" or md.get("no_content_generation") or md.get("camera"):
        return None
    if str(md.get("type") or "") not in _WORK_TYPES_OK:
        return friendly_source(source)
    title = ""
    for k in _TITLE_KEYS:
        if md.get(k):
            title = str(md[k])
            break
    if md.get("show") and md.get("title") and str(md["title"]) != str(md["show"]):
        title = str(md["title"]) if str(md["show"]) in str(md["title"]) else f"{md['show']} — {md['title']}"
    if not title and md.get("video"):
        title = str(md["video"])
    url = public_url(str(md.get("url") or "")) if md.get("url") else None
    label = friendly_source(source) + (f" · {title}" if title else "")
    label = _scrub(label)[:140]
    if not label or _blocked(label):
        return None
    if url:
        label += f" ([link]({url}))"
    return label


def _fetch_memories(ids: list[str]) -> list[tuple[str, dict]]:
    """[(source, metadata)] for the cited ids. Fail-open: [] on any error."""
    if not ids:
        return []
    try:
        import psycopg2
        import nova_dsn
        c = psycopg2.connect(nova_dsn.pg_dsn("nova_memories", "connect_timeout=5"))
        try:
            with c.cursor() as cur:
                cur.execute("SELECT source, metadata FROM memories WHERE id = ANY(%s)", (list(ids),))
                return [(r[0], r[1] if isinstance(r[1], dict) else {}) for r in cur.fetchall()]
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return []


def render(n_memories: int, mem_rows: list[tuple[str, dict]], items: list[dict]) -> str:
    """The footer block. "" when there is nothing to stand on."""
    m = len(items)
    if n_memories <= 0 and m <= 0:
        return ""
    parts = []
    if n_memories > 0:
        parts.append(f"{n_memories} memor{'y' if n_memories == 1 else 'ies'}")
    if m > 0:
        parts.append(f"{m} source{'' if m == 1 else 's'}")
    summary = "Grounded in " + " · ".join(parts)
    lines = []
    if mem_rows:
        counted = Counter()
        hidden = 0
        for src, md in mem_rows:
            lab = memory_label(src, md)
            if lab:
                counted[lab] += 1
            else:
                hidden += 1
        if counted or hidden:
            lines.append("**Memories**\n")
            for lab, k in list(counted.most_common())[:MAX_ITEMS]:
                lines.append(f"- {lab}" + (f" ×{k}" if k > 1 else ""))
            if len(counted) > MAX_ITEMS:
                lines.append(f"- …and {len(counted) - MAX_ITEMS} more")
            if hidden:
                lines.append(f"- {hidden} operational or private memor{'y' if hidden == 1 else 'ies'} (not itemised)")
            lines.append("")
    if items:
        shown, hidden = [], 0
        for it in items:
            lab = source_label(it)
            if lab and lab not in shown:
                shown.append(lab)
            elif not lab:
                hidden += 1
        lines.append("**Sources**\n")
        for lab in shown[:MAX_ITEMS]:
            lines.append(f"- {lab}")
        if len(shown) > MAX_ITEMS:
            lines.append(f"- …and {len(shown) - MAX_ITEMS} more")
        if hidden:
            lines.append(f"- {hidden} operational or private item{'' if hidden == 1 else 's'} (not itemised)")
    inner = "\n".join(lines).strip()
    return (f'\n\n<div {MARKER} id="grounding">\n<details><summary><small>{summary}</small></summary>\n\n'
            f"{inner}\n\n</details>\n</div>\n")


def footer(cited_memory_ids: list | None, sources: str | None) -> str:
    """publish_hugo's hook. Never raises; "" on any failure or when there is nothing to show."""
    try:
        ids = list(dict.fromkeys(str(i) for i in (cited_memory_ids or [])[:50] if i))
        items = parse_source_items(sources)
        if not ids and not items:
            return ""
        return render(len(ids), _fetch_memories(ids), items)
    except Exception:  # noqa: BLE001
        return ""


def add_to_body(body: str, cited_memory_ids: list | None, sources: str | None) -> str:
    if MARKER in (body or ""):
        return body
    return (body or "").rstrip() + footer(cited_memory_ids, sources)


# ─────────────────────────── backfill ───────────────────────────
def find_article(slug: str, root: Path = HUGO_ROOT) -> Path | None:
    hits = sorted((root / "content").glob(f"*/*{slug}.md"))
    hits = [h for h in hits if h.stem == slug or re.fullmatch(r"\d{4}-\d{2}-\d{2}-" + re.escape(slug), h.stem)]
    return hits[-1] if len(hits) == 1 else None


def backfill(days: int = 30, dry_run: bool = False, root: Path = HUGO_ROOT) -> list[str]:
    """Append the line to recent articles with article_citations rows. Returns the changed paths."""
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=5"))
    with c.cursor() as cur:
        cur.execute("SELECT article_slug, array_agg(memory_id) FROM article_citations "
                    "WHERE created_at > now() - make_interval(days => %s) GROUP BY 1", (int(days),))
        rows = cur.fetchall()
    c.close()
    changed = []
    for slug, ids in rows:
        if slug in ROLLING_SLUGS:
            continue
        path = find_article(slug, root)
        if not path:
            print(f"[grounding] no unique article file for {slug} — skipped")
            continue
        text = path.read_text()
        if MARKER in text:
            continue
        block = render(len(ids), _fetch_memories(ids), [])
        if not block:
            continue
        print(f"[grounding] {'would add' if dry_run else 'adding'} line to {path.relative_to(root)} ({len(ids)} memories)")
        if not dry_run:
            path.write_text(text.rstrip() + block)
        changed.append(str(path))
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true", help="add the line to recent cited articles")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.backfill:
        out = backfill(a.days, a.dry_run)
        print(f"[grounding] {len(out)} article(s) {'would change' if a.dry_run else 'changed'}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
