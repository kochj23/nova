"""
nova_cross_linker.py — Find semantically related posts for cross-linking.

Given a post's text, searches the published Hugo content directory for
semantically similar posts from OTHER categories using vector recall.
Returns a list of {url, title, category} dicts for the "Connected threads" footer.

Used by nova_publish_journal.py at publish time — fully automated.

Written by Jordan Koch.
"""

import json
import re
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

MEMORY_SERVER = "http://127.0.0.1:18790"
HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
BASE_URL = "https://nova.digitalnoise.net"

# Category to URL prefix mapping
CATEGORY_URLS = {
    "dreams": "/dreams/",
    "essays": "/essays/",
    "opinions": "/opinions/",
    "tech-today": "/tech-today/",
    "after-dark": "/after-dark/",
    "art": "/art/",
    "research": "/research/",
    "digests": "/digests/",
    "synthesis": "/synthesis/",
    "meta": "/meta/",
}

# Category emoji for display
CATEGORY_EMOJI = {
    "dreams": "🌙",
    "essays": "📝",
    "opinions": "💬",
    "tech-today": "⚡",
    "after-dark": "🌃",
    "art": "🎨",
    "research": "📄",
    "digests": "📋",
    "synthesis": "✨",
    "meta": "🔮",
}


def _recall(query: str, n: int = 20) -> list[dict]:
    """Semantic recall from memory server."""
    try:
        q = urllib.parse.quote(query[:500])
        url = f"{MEMORY_SERVER}/recall?q={q}&n={n}&source=journal_published"
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        return data if isinstance(data, list) else data.get("memories", [])
    except Exception:
        return []


def _get_published_posts() -> dict:
    """
    Build a lookup of all published posts: text_fragment → {url, title, category}.
    Uses Hugo content directory as the source of truth.
    """
    posts = {}
    for category_dir in HUGO_ROOT.glob("content/*/"):
        category = category_dir.name
        if category in ("about", "search", "start-here", "_index.md"):
            continue
        for md_file in category_dir.glob("*.md"):
            if md_file.name.startswith("_"):
                continue
            try:
                content = md_file.read_text(errors="replace")
                # Extract title from frontmatter
                title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
                if not title_match:
                    continue
                title = title_match.group(1).strip()
                # Build URL slug: content/{category}/{slug}.md → /{category}/{slug}/
                slug = md_file.stem
                url = f"/{category}/{slug}/"
                # Use first 200 chars of body as lookup key
                body_start = content.find("---\n", 3)
                body = content[body_start + 4:].strip()[:200] if body_start > 0 else content[:200]
                posts[body[:50]] = {
                    "url": url,
                    "title": title,
                    "category": category,
                    "slug": slug,
                }
            except Exception:
                continue
    return posts


def find_related(text: str, current_category: str, current_slug: str,
                  max_results: int = 4, min_score: float = 0.72) -> list[dict]:
    """
    Find posts from OTHER categories related to the given text.

    Returns list of {url, title, category} dicts, max max_results.
    """
    published = _get_published_posts()
    if not published:
        return []

    # Query memory server for similar content
    memories = _recall(text[:500], n=30)

    related = []
    seen_slugs = {current_slug}

    for mem in memories:
        score = mem.get("score", 0)
        if score < min_score:
            continue

        mem_text = mem.get("text", "")[:50]
        # Look up which post this memory fragment came from
        for key, post in published.items():
            if post["category"] == current_category:
                continue  # Only cross-category links
            if post["slug"] in seen_slugs:
                continue
            # Simple text overlap check
            if (key[:30] in mem_text or mem_text[:30] in key or
                    _title_overlap(mem.get("text", ""), post["title"])):
                seen_slugs.add(post["slug"])
                emoji = CATEGORY_EMOJI.get(post["category"], "→")
                related.append({
                    "url": post["url"],
                    "title": post["title"],
                    "category": f"{emoji} {post['category'].replace('-', ' ').title()}",
                    "score": score,
                })
                if len(related) >= max_results:
                    break
        if len(related) >= max_results:
            break

    # Sort by score descending
    related.sort(key=lambda x: -x.get("score", 0))
    # Remove score from output (internal only)
    return [{"url": r["url"], "title": r["title"], "category": r["category"]}
            for r in related[:max_results]]


def _title_overlap(text: str, title: str) -> bool:
    """Check if significant title words appear in text."""
    title_words = {w.lower() for w in re.findall(r'\w{4,}', title)
                   if w.lower() not in {"with", "from", "that", "this", "have", "been",
                                         "will", "what", "when", "where", "which", "nova"}}
    if not title_words:
        return False
    text_lower = text.lower()
    matches = sum(1 for w in title_words if w in text_lower)
    return matches >= min(2, len(title_words))


def format_related_frontmatter(related: list[dict]) -> str:
    """Format related posts as YAML frontmatter block."""
    if not related:
        return ""
    lines = ["related:"]
    for r in related:
        title_escaped = r["title"].replace('"', '\\"')
        lines.append(f'  - title: "{title_escaped}"')
        lines.append(f'    url: "{r["url"]}"')
        lines.append(f'    category: "{r["category"]}"')
    return "\n".join(lines)
