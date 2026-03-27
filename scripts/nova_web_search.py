#!/usr/bin/env python3
"""
nova_web_search.py — Simple web search for Nova.

Uses DuckDuckGo (no API key) to look things up before forming opinions.
Returns a summary suitable for injecting into LLM context.

Written by Jordan Koch.
"""

import json
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)

    def get_text(self):
        return " ".join(self.text_parts)


def ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return results. Falls back to Lite HTML scraper."""
    # Try instant answer API first
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
        "q": query, "format": "json", "no_html": "1",
        "skip_disambig": "1", "no_redirect": "1"
    })
    results = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Nova/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", query),
                "snippet": data["AbstractText"][:400],
                "url": data.get("AbstractURL", "")
            })
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "snippet": topic.get("Text", "")[:300],
                    "url": topic.get("FirstURL", "")
                })
    except Exception:
        pass

    # Fallback: scrape DuckDuckGo Lite for more specific queries
    if not results:
        try:
            import re
            lite_url = "https://lite.duckduckgo.com/lite?" + urllib.parse.urlencode({"q": query})
            req = urllib.request.Request(lite_url, headers={"User-Agent": "Mozilla/5.0 Nova/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read().decode("utf-8", errors="ignore")
            # DDG Lite results come in groups of 3 <td>s: title, snippet, url
            tds = re.findall(r'<td[^>]*>(.*?)</td>', html, re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', td).strip().replace('&nbsp;', ' ').strip()
                     for td in tds]
            cells = [c for c in cells if len(c) > 10]
            # Every 3 non-empty cells = one result (title, snippet, url)
            i = 0
            while i + 2 < len(cells) and len(results) < max_results:
                title   = cells[i][:80]
                snippet = cells[i+1][:300] if i+1 < len(cells) else ""
                url_    = cells[i+2][:200] if i+2 < len(cells) else ""
                if snippet:
                    results.append({"title": title, "snippet": snippet, "url": url_})
                i += 3
        except Exception as e:
            results = [{"title": "Search error", "snippet": str(e), "url": ""}]

    return results[:max_results]


def fetch_page_summary(url: str, max_chars: int = 800) -> str:
    """Fetch a URL and return a plain-text summary."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 Nova/1.0",
            "Accept": "text/html"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        parser = TextExtractor()
        parser.feed(html)
        text = parser.get_text()
        # Collapse whitespace
        import re
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"(Could not fetch: {e})"


def search_summary(query: str, fetch_top: bool = False) -> str:
    """
    Search for query and return a formatted summary for LLM context.
    If fetch_top=True, also fetches the top result page.
    """
    results = ddg_search(query)
    if not results:
        return f"No search results found for: {query}"

    lines = [f"Search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet'][:250]}")
        if r["url"]:
            lines.append(f"   {r['url']}")
        lines.append("")

    if fetch_top and results[0].get("url"):
        page_text = fetch_page_summary(results[0]["url"])
        if page_text:
            lines.append(f"Top result content:\n{page_text}")

    return "\n".join(lines)


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "test"
    print(search_summary(query, fetch_top="--fetch" in sys.argv))
