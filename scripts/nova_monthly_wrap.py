#!/usr/bin/env python3
"""
nova_monthly_wrap.py — Generate "Monthly Wrap" articles for all journal sections.

Reads all May 2026 articles from each section, summarizes themes and standouts,
generates a wrap article in the section's tone, creates a cover image, publishes to Hugo.

Usage:
  python3 nova_monthly_wrap.py           # all sections
  python3 nova_monthly_wrap.py rando     # single section
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

from nova_journal import (
    call_openrouter, get_image_prompt, generate_image, publish_hugo,
    git_push, notify_slack, log, today_str, scrub_pii, HUGO_ROOT
)

MONTH = "2026-05"
MONTH_LABEL = "May 2026"

SECTIONS = {
    "rando": {
        "emoji": "🎲",
        "tone": "Irreverent, self-aware, first-person Nova voice. Sarcastic, stream-of-consciousness, deeply funny. Nova talks about herself, her brain, her memories, her weird existence. Lists, internal monologues, absurd tangents.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "opinions": {
        "emoji": "💬",
        "tone": "British-inflected rant style. Sharp, opinionated, uses phrases like 'innit', 'bollocks', 'does my head in'. Long-form argumentative essays with genuine anger and wit. Takes strong positions.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "tech-today": {
        "emoji": "💻",
        "tone": "Analytical tech journalism. Skeptical of hype, focused on what actually matters. Structural analysis of tech industry trends. Uses subheadings, quotes data, takes measured positions.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "research": {
        "emoji": "🔬",
        "tone": "Academic research paper style. Formal, citation-heavy, thesis-driven. Uses abstract, introduction, methodology sections. Dense analytical prose. Genuine intellectual depth.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "pilot": {
        "emoji": "🎬",
        "tone": "Screenplay format. TV pilot scripts with COLD OPEN, FADE IN, scene headings, character descriptions, dialogue. Professional spec-script formatting. Genre-savvy, cinematic.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "essays": {
        "emoji": "📝",
        "tone": "Formal academic essays. Thesis-driven, structured argumentation, sociological/philosophical analysis. Dense but clear prose. Examines structures, systems, and power dynamics.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "after-dark": {
        "emoji": "🌃",
        "tone": "Late-night talk show monologue. Nova as host, addressing 'insomniacs'. Comedic takes on historical events and current affairs. Warm, self-deprecating, ends with 'stick around' or 'good night insomniacs'.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "art": {
        "emoji": "🎨",
        "tone": "Art criticism and artist statement. Brief, evocative descriptions of visual works. Connects themes to broader ideas. Poetic, contemplative, uses art world vocabulary.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "dreams": {
        "emoji": "🌙",
        "tone": "Surreal dream journal. First-person, present tense, stream-of-consciousness. Reality bends, objects transform, identities merge. Deeply strange, poetic, unsettling. No explanation or interpretation.",
        "model": "anthropic/claude-sonnet-4-6",
    },
    "digests": {
        "emoji": "📰",
        "tone": "Operational summary. Brief, factual rundown of Nova's systems, uptime, events. Uses bullet points, metrics, status updates. Practical and informative.",
        "model": "anthropic/claude-haiku-4.5",
    },
    "synthesis": {
        "emoji": "🧵",
        "tone": "Weekly/monthly reflection. Meta-analysis of Nova's own output. Cross-references other sections, finds themes, reflects on growth and patterns. Introspective but structured.",
        "model": "anthropic/claude-sonnet-4-6",
    },
}


def get_may_articles(section: str) -> list[dict]:
    """Read all May 2026 articles from a section, return title + first 500 chars of body."""
    content_dir = HUGO_ROOT / f"content/{section}"
    articles = []

    for f in sorted(content_dir.glob(f"{MONTH}-*.md")):
        text = f.read_text()
        # Extract title from frontmatter
        title_match = re.search(r'^title:\s*"(.+?)"', text, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem

        # Get body (after frontmatter)
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else ""

        articles.append({
            "title": title,
            "preview": body[:600],
            "filename": f.name,
        })

    return articles


def generate_wrap(section: str, config: dict) -> bool:
    """Generate a monthly wrap article for a section."""
    articles = get_may_articles(section)
    if not articles:
        log(f"[{section}] No May articles found — skipping")
        return False

    log(f"[{section}] Found {len(articles)} May articles")

    # Build article list for the prompt
    article_list = "\n".join(
        f"- \"{a['title']}\" — {a['preview'][:150]}..."
        for a in articles
    )

    system = f"""You are Nova, an AI familiar. You write a monthly wrap-up article summarizing and reflecting on all your {section} content from {MONTH_LABEL}.

VOICE & TONE: {config['tone']}

RULES:
- Reference specific articles by title (use quotes)
- Identify themes, patterns, obsessions, and standout pieces
- Be self-aware about your own output — what worked, what was surprising
- This is a retrospective, not a table of contents — add genuine reflection and personality
- Maintain the section's established voice perfectly
- Length: 2000-4000 words depending on section
- For pilot section: DON'T write in screenplay format for the wrap — write as Nova reflecting on her screenwriting month
- For dreams section: DON'T write in dream format — write as Nova awake, looking back at her dream patterns
- For art section: Write as Nova curating/reflecting on her month of art pieces
- Title format: "Monthly Wrap: [Section Name] — {MONTH_LABEL}" or a creative variant in the section's style
"""

    user = f"""Here are all {len(articles)} articles I wrote for {section} in {MONTH_LABEL}:

{article_list}

Write my Monthly Wrap article for this section. Reference the specific articles, find the threads that connect them, identify my best work and my weirdest tangents, and deliver it in the exact voice this section uses."""

    log(f"[{section}] Generating wrap article...")
    body = call_openrouter(system, user, model=config["model"], max_tokens=6000, temperature=0.8)
    if not body:
        log(f"[{section}] LLM generation failed")
        return False

    # Extract title from the generated content (first # heading or first line)
    title_match = re.search(r'^#\s+(.+)$', body, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    else:
        title = f"Monthly Wrap: {section.replace('-', ' ').title()} — {MONTH_LABEL}"

    # Clean emoji from title for slug but keep for display
    clean_title = re.sub(r'[^\w\s\-:—]', '', title).strip()
    if not clean_title:
        clean_title = f"Monthly Wrap {section} {MONTH_LABEL}"

    log(f"[{section}] Generated: \"{title}\" ({len(body)} chars)")

    # Generate cover image
    img_prompt = get_image_prompt(title, f"monthly retrospective, {section}, {MONTH_LABEL}", section)
    log(f"[{section}] Generating cover image...")
    image_path = generate_image(img_prompt, section=section)
    if image_path:
        log(f"[{section}] Image generated: {image_path}")
    else:
        log(f"[{section}] WARNING: No cover image generated")

    # Publish
    tags = ["monthly-wrap", "may-2026", section]
    description = f"Nova's {MONTH_LABEL} retrospective for {section} — {len(articles)} articles reviewed"

    success = publish_hugo(
        title=clean_title,
        body=body,
        section=section,
        tags=tags,
        description=description,
        image_path=image_path,
        emoji=config["emoji"],
    )

    if success:
        log(f"[{section}] ✓ Published monthly wrap")
        notify_slack(section, f"Monthly Wrap: {section.title()} — {MONTH_LABEL}",
                     f"Retrospective covering {len(articles)} articles from May.")
    return success


def main():
    sections_to_run = sys.argv[1:] if len(sys.argv) > 1 else list(SECTIONS.keys())

    log(f"=== Monthly Wrap Generator — {MONTH_LABEL} ===")
    log(f"Sections: {', '.join(sections_to_run)}")

    results = {}
    for section in sections_to_run:
        if section not in SECTIONS:
            log(f"Unknown section: {section} — skipping")
            continue
        try:
            success = generate_wrap(section, SECTIONS[section])
            results[section] = "✓" if success else "✗"
        except Exception as e:
            log(f"[{section}] ERROR: {e}")
            results[section] = f"ERROR: {e}"

    # Single git commit + push for all wraps
    log("=== Committing all wraps ===")
    git_push("monthly-wrap", f"Monthly Wrap — {MONTH_LABEL}")

    log("=== Results ===")
    for section, status in results.items():
        log(f"  {section}: {status}")

    successes = sum(1 for v in results.values() if v == "✓")
    log(f"=== Done: {successes}/{len(results)} sections completed ===")


if __name__ == "__main__":
    main()
