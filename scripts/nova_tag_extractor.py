"""
nova_tag_extractor.py — Extract 3-6 meaningful tags from post content.

Used by all journal generation scripts to auto-tag posts at generation time.
Tags are stored in Hugo frontmatter and indexed by Fuse.js for search.

Written by Jordan Koch.
"""

import json
import re
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3-coder:30b"

# Category-aware seed tags — ensure category-specific vocab always appears
CATEGORY_SEEDS = {
    "dreams":    ["dream", "memory", "subconscious"],
    "essays":    ["analysis", "culture", "society"],
    "opinions":  ["tech", "culture", "commentary"],
    "tech-today": ["technology", "AI", "infrastructure"],
    "research":  ["research", "academic", "history"],
    "after-dark": ["history", "comedy", "monologue"],
    "art":       ["art", "generative", "visual"],
    "digests":   ["weekly", "reflection", "digest"],
    "synthesis": ["synthesis", "weekly", "reflection"],
    "meta":      ["meta-analysis", "self-reflection", "patterns"],
}

# Stopwords — never emit these as tags
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "nova", "jordan", "i", "my", "me", "we", "our", "this", "that", "it",
    "be", "is", "are", "was", "were", "been", "has", "have", "had", "do",
    "does", "did", "will", "would", "could", "should", "may", "might", "must",
    "more", "most", "very", "quite", "rather", "also", "just", "so", "then",
    "than", "when", "where", "which", "who", "what", "how", "why",
}


def extract_tags(title: str, content: str, category: str = "", n: int = 5) -> list[str]:
    """
    Extract n meaningful tags from title + content.

    First tries keyword frequency + category seeds (fast, no LLM).
    Falls back to LLM if content is complex.
    Returns lowercase, no-stopword tags.
    """
    tags = _extract_keyword_tags(title, content, category, n)
    if len(tags) >= 3:
        return tags[:n]
    # LLM fallback for complex content
    return _extract_llm_tags(title, content[:800], category, n)


def _extract_keyword_tags(title: str, content: str, category: str, n: int) -> list[str]:
    """Fast keyword extraction without LLM."""
    text = (title + " " + content[:2000]).lower()
    # Remove markdown syntax, URLs, punctuation
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[#*`\[\]()_~>|]', ' ', text)
    text = re.sub(r'[^\w\s-]', ' ', text)

    words = text.split()
    # Count 1-2 word phrases, skip stopwords
    freq: dict = {}
    for i, w in enumerate(words):
        if len(w) > 3 and w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
        # Bigrams
        if i < len(words) - 1:
            b = f"{w}-{words[i+1]}"
            if len(b) > 7 and w not in STOPWORDS and words[i+1] not in STOPWORDS:
                freq[b] = freq.get(b, 0) + 0.5

    # Sort by frequency, take top N
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    tags = [w for w, _ in sorted_words if len(w) > 3][:n * 2]

    # Add category seeds
    seeds = CATEGORY_SEEDS.get(category, [])
    result = []
    for seed in seeds:
        if seed not in result:
            result.append(seed)
    for tag in tags:
        if tag not in result and tag not in STOPWORDS:
            result.append(tag)
        if len(result) >= n:
            break

    return result[:n]


def _extract_llm_tags(title: str, content: str, category: str, n: int) -> list[str]:
    """LLM-based tag extraction for complex content."""
    seeds = ", ".join(CATEGORY_SEEDS.get(category, []))
    prompt = (
        f"Extract exactly {n} short tags (1-3 words each) for this {category} post. "
        f"Return ONLY a JSON array of strings, nothing else. "
        f"Include some of these if relevant: [{seeds}]. "
        f"Avoid generic words. Use specific topic words.\n\n"
        f"Title: {title}\n\nContent:\n{content[:600]}"
    )
    try:
        payload = json.dumps({
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 80},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        text = json.loads(resp.read()).get("response", "")
        # Parse JSON array from response
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            tags = json.loads(match.group())
            clean = [str(t).lower().strip().replace(" ", "-")
                     for t in tags if t and str(t).lower().strip() not in STOPWORDS]
            return clean[:n]
    except Exception:
        pass
    return CATEGORY_SEEDS.get(category, ["journal"])[:n]
