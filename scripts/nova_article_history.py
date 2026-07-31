#!/usr/bin/env python3
"""nova_article_history.py — a column's own rolling memory of what it just published.

Article generators kept rehashing the same observations/jokes/topics day after day
because each run started blind. This gives a generator the last N days of ITS OWN
published articles (title + one-line gist) as prompt context, with instructions to
stop repeating and instead name the PATTERNS/TRENDS across the window and report on
those.

    recent_articles_context("operations")   # -> prompt block, or "" if no history

CLI:  python3 nova_article_history.py operations
"""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

_FM_RE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$")


def _content_dir(section: str):
    """Locate the Hugo content dir for a section, robust across hosts."""
    candidates = [
        Path.home() / "nova-journal" / "content" / section,
        Path("/nova/nova-journal/content") / section,
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # last resort: any nova-journal/content/<section> under $HOME
    home = Path.home()
    for p in home.glob(f"**/nova-journal/content/{section}"):
        if p.is_dir():
            return p
    return None


def _parse_frontmatter(text: str) -> dict:
    """Pull the simple key: value pairs out of the leading --- ... --- block."""
    fm = {}
    if not text.startswith("---"):
        return fm
    end = text.find("\n---", 3)
    if end == -1:
        return fm
    for line in text[3:end].splitlines():
        m = _FM_RE.match(line)
        if m:
            k, v = m.group(1).lower(), m.group(2).strip().strip('"').strip("'")
            if k not in fm:
                fm[k] = v
    return fm


def _first_sentence(text: str, limit: int = 140) -> str:
    """First real sentence of the body (after frontmatter), trimmed."""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
    body = re.sub(r"[#>*_`\-]", " ", body)                 # strip md markers
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""
    cut = body[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 40 else cut).strip()


def _article_date(path: Path, fm: dict):
    """Prefer the YYYY-MM-DD filename prefix; fall back to frontmatter date."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", path.name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    d = fm.get("date", "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def recent_articles_context(section: str, days: int = 14, max_items: int = 40) -> str:
    """Last `days` of this section's articles as a prompt block. "" if none found."""
    d = _content_dir(section)
    if not d:
        return ""
    cutoff = datetime.now() - timedelta(days=days)
    items = []
    for path in d.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        adate = _article_date(path, fm)
        if not adate or adate < cutoff:
            continue
        title = fm.get("title") or path.stem
        # strip emoji/markdown noise from titles for a clean list
        title = re.sub(r"[*_`]", "", title).strip()
        gist = fm.get("description") or _first_sentence(text)
        items.append((adate, title, gist))
    if not items:
        return ""
    items.sort(key=lambda t: t[0], reverse=True)
    items = items[:max_items]
    lines = "\n".join(
        f"- {a.strftime('%Y-%m-%d')}: \"{t}\"" + (f" — {g}" if g else "")
        for a, t, g in items
    )
    return (
        f"YOUR LAST {days} DAYS IN THIS COLUMN — you already published these. Do NOT rehash "
        f"the same topics, jokes, or observations; the reader has seen them. Look ACROSS these "
        f"two weeks, name the real PATTERNS / TRENDS / recurring threads, and report on THOSE. "
        f"Fresh angles only; callbacks are fine when they add genuine insight.\n\n" + lines + "\n"
    )


if __name__ == "__main__":
    import sys
    sec = sys.argv[1] if len(sys.argv) > 1 else "operations"
    out = recent_articles_context(sec)
    print(out if out else f"(no articles in the last 14 days for section '{sec}')")
