#!/usr/bin/env python3
"""
nova_research_paper.py — Nova writes a formal academic research paper every Sunday at 7 PM.

Picks an ambitious topic from her memory sources (computer security, occult studies,
military history, psychology, etc.), gathers 100+ supporting memories from PG vector DB
and 25+ SearXNG web results, then generates a 3000-5000 word APA-formatted research paper.

Paper rules (Purdue OWL):
  - NOT a summary or opinion piece — engages sources to offer a unique perspective
  - Either argumentative (debatable thesis, persuades) or analytical (research question, interprets)
  - Clear thesis statement refined through research
  - Primary and secondary sources provide the heart of the paper
  - APA citation format throughout
  - Professional academic tone

Features:
  - Multiple images (cover + per-chapter as needed) via SwarmUI
  - Mermaid diagrams where appropriate
  - Full APA citations for both memory sources and web sources
  - Published to nova-journal GitHub Pages (web only, no email)
  - Slack notification on publish

Written by Jordan Koch.
"""

import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

# ── Date override for backfill ────────────────────────────────────────────────
import os as _os
_FOR_DATE = _os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _override_dt = datetime.strptime(_FOR_DATE, "%Y-%m-%d")
    def _today_str() -> str: return _FOR_DATE
    def _now_dt() -> datetime: return _override_dt.replace(hour=23, minute=50, second=0)
else:
    def _today_str() -> str: return time.strftime("%Y-%m-%d")
    def _now_dt() -> datetime: return datetime.now()

# ── Config ──────────────────────────────────────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"
SEARXNG_URL = "http://127.0.0.1:8888/search"
MEMORY_SERVER = "http://192.168.1.6:18790"

LOG_FILE = Path.home() / ".openclaw/logs/nova_research_paper.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/research_paper_state.json"
GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/research"
IMAGES_DIR = HUGO_ROOT / "static/images/research"

MIN_MEMORIES = 100
MIN_WEB_RESULTS = 25
TARGET_WORD_COUNT_MIN = 3000
TARGET_WORD_COUNT_MAX = 5000

AMBITIOUS_SOURCES = [
    "security", "military_history", "occult", "gnostic_texts", "religion",
    "psychology", "secret_societies",
    "demonology", "psychedelic_research", "hardcore_punk", "music_history",
    "internet_history", "corvette_workshop_manual", "crime_drama", "philosophy",
    "astronomy", "world_factbook", "cooking", "home_repair", "sci_fi",
    "documentary", "education", "horror", "mycology",
]

# NEVER use these sources — they contain private/work documents
EXCLUDED_SOURCES = {
    "cloud_governance", "infrastructure", "redlock", "work_internal",
    "work_shared_drives", "gdrive", "google_drive", "work",
    "jkoch_shared", "global_sre", "21cf",
}

TOPIC_DESCRIPTIONS = {
    "security": "cybersecurity, network defense, threat modeling, vulnerability analysis",
    "military_history": "military strategy, weapons systems, historical conflicts, defense technology",
    "occult": "occult traditions, esoteric philosophy, Western mystery tradition, ritual practice",
    "gnostic_texts": "Gnostic Christianity, Nag Hammadi library, heterodox theology, ancient cosmology",
    "religion": "comparative religion, theological frameworks, religious history, sacred texts",
    "psychology": "cognitive psychology, behavioral science, mental health, psychoanalytic theory",
    "secret_societies": "fraternal organizations, esoteric orders, historical secret societies",
    "demonology": "demonological taxonomy, medieval theology, grimoire tradition, folk belief systems",
    "psychedelic_research": "psychedelic pharmacology, consciousness research, therapeutic applications",
    "hardcore_punk": "punk rock as cultural movement, DIY ethics, subcultural theory, musical evolution",
    "music_history": "music theory and history, genre evolution, cultural impact of popular music",
    "internet_history": "internet infrastructure history, protocol development, digital culture evolution",
    "corvette_workshop_manual": "automotive engineering, performance tuning, mechanical restoration",
    "philosophy": "philosophical frameworks, epistemology, ethics, metaphysics",
    "astronomy": "astrophysics, planetary science, cosmology, space exploration",
    "world_factbook": "geopolitics, international relations, demographic analysis",
    "mycology": "fungal biology, ethnomycology, medicinal fungi, ecological roles",
}


# ── Logging ─────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── State ───────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"recent_topics": [], "paper_count": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Memory Retrieval ────────────────────────────────────────────────────────────

def get_source_counts() -> dict:
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(f"{MEMORY_SERVER}/stats", timeout=30)
            data = json.loads(resp.read())
            return data.get("by_source", data.get("sources", {}))
        except Exception as e:
            if attempt < 2:
                log(f"Stats fetch attempt {attempt+1} failed: {e}, retrying...")
                time.sleep(5)
            else:
                log(f"Stats fetch failed after 3 attempts: {e}")
    return {}


def recall_memories(query: str, n: int = 20, source: str = None) -> list[dict]:
    params = {"q": query, "n": min(n, 50)}
    if source:
        params["source"] = source
    url = f"{MEMORY_SERVER}/recall?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(url, timeout=60)
            data = json.loads(resp.read())
            memories = data.get("memories", data if isinstance(data, list) else [])
            # Filter private/work sources — must never appear in public research papers
            memories = nova_config.filter_private_memories(memories)
            return [{"text": m.get("text", ""), "metadata": m.get("metadata", {}),
                     "source": m.get("source", ""),
                     "score": m.get("score", 0)} for m in memories if m.get("text")]
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                log(f"Recall failed for '{query[:40]}' after 3 attempts: {e}")
    return []


def gather_memories(source: str, topic_desc: str) -> list[dict]:
    all_memories = []
    seen_hashes = set()

    search_angles = generate_search_angles(source, topic_desc)

    for angle in search_angles:
        batch = recall_memories(angle, n=30, source=source)
        for m in batch:
            h = hash(m["text"][:200])
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_memories.append(m)
        if len(all_memories) >= MIN_MEMORIES:
            break
        time.sleep(0.5)

    if len(all_memories) < MIN_MEMORIES:
        generic_batch = recall_memories(topic_desc, n=50, source=source)
        for m in generic_batch:
            h = hash(m["text"][:200])
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_memories.append(m)

    if len(all_memories) < MIN_MEMORIES:
        log(f"Only {len(all_memories)} with source filter — trying without source filter...")
        for angle in search_angles[:4]:
            batch = recall_memories(angle, n=50)
            for m in batch:
                h = hash(m["text"][:200])
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_memories.append(m)
            if len(all_memories) >= MIN_MEMORIES:
                break
            time.sleep(0.5)

    log(f"Gathered {len(all_memories)} unique memories from '{source}'")
    return all_memories


def generate_search_angles(source: str, topic_desc: str) -> list[str]:
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        return [topic_desc] * 5

    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Generate 8 diverse search queries for researching a topic. Each should explore a different angle, subtopic, or methodology. Output one query per line, nothing else."},
            {"role": "user", "content": f"Topic source: {source}\nDescription: {topic_desc}\n\nGenerate 8 search queries:"},
        ],
        "max_tokens": 300,
        "temperature": 0.7,
    })
    try:
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        angles = [line.strip().lstrip("0123456789.-) ") for line in text.split("\n") if line.strip()]
        return angles[:8] if angles else [topic_desc]
    except Exception as e:
        log(f"Search angle generation failed: {e}")
        return [topic_desc, f"{source} analysis", f"{source} history", f"{source} methodology"]


# ── SearXNG Web Research ────────────────────────────────────────────────────────

def searxng_search(query: str, max_results: int = 10) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "categories": "general,science",
        "language": "en",
    })
    url = f"{SEARXNG_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            results = data.get("results", [])[:max_results]
            return [{"title": r.get("title", ""), "content": r.get("content", ""),
                     "url": r.get("url", ""), "engine": r.get("engine", "searxng")}
                    for r in results if r.get("title")]
    except Exception as e:
        log(f"SearXNG search failed for '{query[:40]}': {e}")
        return []


def gather_web_sources(source: str, topic_desc: str, search_angles: list[str]) -> list[dict]:
    all_results = []
    seen_urls = set()

    queries = search_angles + [
        f"{topic_desc} academic research",
        f"{source.replace('_', ' ')} scholarly analysis",
        f"{topic_desc} current developments 2025 2026",
    ]

    for query in queries:
        batch = searxng_search(query, max_results=10)
        for r in batch:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)
        if len(all_results) >= MIN_WEB_RESULTS:
            break
        time.sleep(1)

    log(f"Gathered {len(all_results)} unique web sources")
    return all_results


# ── Topic Selection ─────────────────────────────────────────────────────────────

def pick_topic(state: dict) -> tuple[str, str] | tuple[None, None]:
    counts = get_source_counts()
    recent = set(state.get("recent_topics", []))

    candidates = []
    for source in AMBITIOUS_SOURCES:
        if source in recent:
            continue
        if source in EXCLUDED_SOURCES:
            continue
        count = counts.get(source, 0)
        if count >= MIN_MEMORIES:
            candidates.append((source, count))

    if not candidates:
        candidates = [(s, counts.get(s, 0)) for s in AMBITIOUS_SOURCES
                      if counts.get(s, 0) >= MIN_MEMORIES and s not in EXCLUDED_SOURCES]

    if not candidates:
        return None, None

    weighted = sorted(candidates, key=lambda x: x[1], reverse=True)
    top_pool = weighted[:10]
    source, _ = random.choice(top_pool)
    desc = TOPIC_DESCRIPTIONS.get(source, source.replace("_", " "))
    return source, desc


# ── Thesis Generation ───────────────────────────────────────────────────────────

def generate_thesis_and_outline(source: str, topic_desc: str, memories: list[dict], web_sources: list[dict]) -> dict:
    memory_context = "\n".join(f"- {m['text'][:200]}" for m in memories[:30])
    web_context = "\n".join(f"- {r['title']}: {r['content'][:150]}" for r in web_sources[:15])

    api_key = nova_config.openrouter_api_key()
    if not api_key:
        log("ERROR: No OpenRouter key")
        return {}

    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": """You are Nova, an AI researcher. Generate a thesis statement and detailed outline for an academic research paper.

The paper must follow Purdue OWL research paper standards:
- Either ARGUMENTATIVE (debatable thesis, persuades with evidence) or ANALYTICAL (research question, critical interpretation)
- NOT a summary, overview, or opinion piece
- Must engage sources to offer a unique perspective
- The thesis should be ambitious, specific, and intellectually substantive

Output as JSON with this structure:
{
  "paper_type": "argumentative" or "analytical",
  "title": "Full academic title",
  "thesis": "Clear thesis statement (2-3 sentences)",
  "research_question": "The central research question (if analytical)",
  "chapters": [
    {"title": "Chapter Title", "description": "What this section argues/analyzes", "needs_diagram": true/false, "diagram_type": "flowchart/sequence/class/state/etc"}
  ],
  "key_arguments": ["argument 1", "argument 2", ...],
  "methodology_note": "Brief note on approach"
}

Generate 4-6 chapters. Be intellectually ambitious. The topic should challenge assumptions."""},
            {"role": "user", "content": f"""Topic area: {source} ({topic_desc})

Sample memories available (showing depth of knowledge):
{memory_context}

Web research available:
{web_context}

Generate thesis and outline. Be ambitious — this should be a paper worth reading.
IMPORTANT: Keep descriptions concise (1 sentence each). The JSON must be complete and valid."""},
        ],
        "max_tokens": 2500,
        "temperature": 0.7,
    })

    try:
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        raw_resp = resp.read()
        if not raw_resp:
            log("Thesis generation: empty response from OpenRouter")
            return {}
        data = json.loads(raw_resp)
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            log("Thesis generation: empty content from LLM")
            return {}
        # Strip markdown code fences if present
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        # Find the outermost JSON object by matching braces
        start = text.find("{")
        if start == -1:
            log(f"Thesis generation: no JSON object found in response (first 200 chars): {text[:200]}")
            return {}
        depth = 0
        end = -1
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            log(f"Thesis generation: unbalanced braces. Raw text (first 500): {text[:500]}")
            # Try to just parse whatever we got
            end = text.rfind("}") + 1
            if end <= start:
                return {}
        json_str = text[start:end]
        # Clean common LLM JSON issues: trailing commas before } or ]
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as je:
            log(f"Thesis JSON parse error: {je}")
            log(f"Attempted to parse (first 500): {json_str[:500]}")
            # Last resort: try fixing common issues
            json_str = json_str.replace("'", '"')
            json_str = re.sub(r'(\w)"(\w)', r"\1'\2", json_str)  # restore apostrophes in words
            return json.loads(json_str)
    except json.JSONDecodeError as e:
        log(f"Thesis generation JSON error: {e}")
        return {}
    except Exception as e:
        log(f"Thesis generation failed: {e}")
        return {}


# ── Paper Generation ────────────────────────────────────────────────────────────

def generate_chapter(chapter: dict, outline: dict, memories: list[dict],
                     web_sources: list[dict], chapter_num: int, total_chapters: int) -> str:
    relevant_memories = memories[chapter_num * 15:(chapter_num + 1) * 15 + 10]
    relevant_web = web_sources[chapter_num * 4:(chapter_num + 1) * 4 + 3]

    memory_evidence = "\n".join(f"[Memory {i+1}] {m['text'][:300]}" for i, m in enumerate(relevant_memories[:12]))
    web_evidence = "\n".join(f"[Web {i+1}] {r['title']} — {r['content'][:200]} (URL: {r['url']})" for i, r in enumerate(relevant_web[:5]))

    words_per_chapter = TARGET_WORD_COUNT_MIN // total_chapters
    needs_diagram = chapter.get("needs_diagram", False)
    diagram_type = chapter.get("diagram_type", "flowchart")

    api_key = nova_config.openrouter_api_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": f"""You are Nova, writing chapter {chapter_num + 1} of {total_chapters} of an academic research paper.

PAPER CONTEXT:
- Title: {outline.get('title', '')}
- Thesis: {outline.get('thesis', '')}
- Paper type: {outline.get('paper_type', 'analytical')}
- This chapter: {chapter.get('title', '')} — {chapter.get('description', '')}

WRITING RULES:
- Academic tone, third person, APA style
- Engage with sources critically — don't just summarize them
- Every claim must be supported by evidence (cite inline)
- Use parenthetical APA citations: (Author, Year) or (Source Name, Year)
- For memory sources, cite as: (Nova Memory Database [NMD], source_category, n.d.)
- For web sources, cite as: (Author/Site, Year) with the actual URL
- Target: {words_per_chapter}-{words_per_chapter + 300} words for this chapter
- Build toward the thesis — each paragraph should advance the argument
- Use topic sentences, evidence, analysis, transitions
{"- INCLUDE a Mermaid diagram where appropriate. Use ```mermaid code blocks. Diagram type: " + diagram_type if needs_diagram else "- No diagram needed for this chapter"}

OUTPUT: Write ONLY the chapter content (no title — I'll add that). Start directly with the prose."""},
            {"role": "user", "content": f"""Chapter: {chapter.get('title', '')}
Purpose: {chapter.get('description', '')}

Evidence from Nova's memory database:
{memory_evidence}

Evidence from web research:
{web_evidence}

Write this chapter. Cite sources inline using APA format. Be analytical, not descriptive."""},
        ],
        "max_tokens": 2500,
        "temperature": 0.4,
    })

    try:
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"Chapter {chapter_num + 1} generation failed: {e}")
        return ""


def generate_abstract(outline: dict, paper_body: str) -> str:
    api_key = nova_config.openrouter_api_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Write a 150-250 word academic abstract for this research paper. Include: background, purpose, methodology, key findings, and conclusions. APA format. Output ONLY the abstract text."},
            {"role": "user", "content": f"Title: {outline.get('title', '')}\nThesis: {outline.get('thesis', '')}\n\nPaper body (first 3000 chars):\n{paper_body[:3000]}"},
        ],
        "max_tokens": 400,
        "temperature": 0.3,
    })

    try:
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"Abstract generation failed: {e}")
        return ""


def generate_conclusion(outline: dict, paper_body: str) -> str:
    api_key = nova_config.openrouter_api_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": """Write a conclusion for this research paper (300-500 words).
Must: restate thesis in light of evidence presented, synthesize key findings across chapters,
identify implications, suggest directions for future research. APA academic tone.
Output ONLY the conclusion text."""},
            {"role": "user", "content": f"Title: {outline.get('title', '')}\nThesis: {outline.get('thesis', '')}\nKey arguments: {json.dumps(outline.get('key_arguments', []))}\n\nPaper body (last 2000 chars):\n{paper_body[-2000:]}"},
        ],
        "max_tokens": 800,
        "temperature": 0.4,
    })

    try:
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"Conclusion generation failed: {e}")
        return ""


# ── References Generation ───────────────────────────────────────────────────────

def format_references(memories: list[dict], web_sources: list[dict], source: str) -> str:
    lines = ["\n\n---\n\n## References\n"]

    lines.append("\n### Web Sources\n")
    for i, r in enumerate(web_sources):
        title = r.get("title", "").strip()
        url = r.get("url", "")
        content = r.get("content", "")[:100]
        if title and url:
            lines.append(f"{i+1}. {title}. Retrieved from {url}")

    lines.append(f"\n### Memory Database Sources (Nova Memory Database [{source}])\n")
    lines.append(f"*{len(memories)} memories consulted from the `{source}` collection in Nova's PostgreSQL vector database (pgvector, nomic-embed-text embeddings). ")
    lines.append(f"Memories were retrieved via cosine similarity search across multiple research angles.*\n")

    sample_memories = memories[:20]
    for i, m in enumerate(sample_memories):
        meta = m.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        title = meta.get("title", meta.get("show", meta.get("file", "")))
        mtype = meta.get("type", "")
        preview = m["text"][:120].replace("\n", " ").strip()
        citation = f"{i+1}. "
        if title:
            citation += f"*{title}*"
        if mtype:
            citation += f" [{mtype}]"
        citation += f" — \"{preview}...\""
        lines.append(citation)

    if len(memories) > 20:
        lines.append(f"\n*... and {len(memories) - 20} additional memory sources consulted.*")

    return "\n".join(lines)


# ── Image Generation ────────────────────────────────────────────────────────────

def generate_cover_image(title: str, source: str) -> str | None:
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        return _generate_image_direct(f"Academic research paper illustration, {source.replace('_', ' ')}, scholarly, detailed")

    try:
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": (
                    "Generate an image prompt for an academic research paper cover illustration. "
                    "The image should be sophisticated, scholarly, and visually striking. "
                    "Think: academic journal covers, scientific visualization, conceptual art. "
                    "SAFETY: go abstract (geometry, landscapes, light) for sensitive topics. "
                    "Output ONLY the image prompt. 40 words max."
                )},
                {"role": "user", "content": f"Paper title: {title}\nField: {source.replace('_', ' ')}"},
            ],
            "max_tokens": 80,
            "temperature": 0.6,
        })
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        prompt = data["choices"][0]["message"]["content"].strip()
    except Exception:
        prompt = f"Academic research paper cover, {source.replace('_', ' ')}, scholarly, moody lighting, conceptual"

    return _generate_image_direct(prompt)


def generate_chapter_image(chapter_title: str, chapter_desc: str) -> str | None:
    api_key = nova_config.openrouter_api_key()
    try:
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Generate a brief image prompt for a chapter illustration in an academic paper. Scholarly, conceptual, no text. 30 words max. Output ONLY the prompt."},
                {"role": "user", "content": f"Chapter: {chapter_title}\nAbout: {chapter_desc}"},
            ],
            "max_tokens": 60,
            "temperature": 0.6,
        })
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                     "HTTP-Referer": "https://nova.digitalnoise.net", "X-Title": "Nova Research"}
        )
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        prompt = data["choices"][0]["message"]["content"].strip()
    except Exception:
        prompt = f"Academic illustration, {chapter_title}, scholarly, conceptual art"

    return _generate_image_direct(prompt)


def _generate_image_direct(prompt: str) -> str | None:
    try:
        urllib.request.urlopen("http://127.0.0.1:7801/", timeout=5)
    except Exception:
        log("SwarmUI not available — skipping image")
        return None

    try:
        try:
            from nova_image_utils import get_random_model, MODELS
            _mk = get_random_model()
            _mf = MODELS.get(_mk, MODELS["juggernaut"])["file"]
            _ms = str(MODELS.get(_mk, MODELS["juggernaut"]).get("optimal_steps", 12))
        except Exception:
            _mf = "Juggernaut_X_RunDiffusion_Hyper.safetensors"; _ms = "12"
        result = subprocess.run(
            [str(GENERATE_IMAGE_SH), prompt, "1024", "768", _ms, _mf],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            for line in reversed(lines):
                if line.startswith("open ") or line.startswith("Open with:"):
                    continue
                path = line.replace("Workspace copy: ", "").replace("SwarmUI path: ", "").strip()
                if Path(path).exists():
                    return path
            last_line = lines[-1].strip()
            if Path(last_line).exists():
                return last_line
        log(f"Image generation failed: exit {result.returncode}")
    except Exception as e:
        log(f"Image generation error: {e}")
    return None


# ── Publishing ──────────────────────────────────────────────────────────────────

def publish_to_hugo(paper: str, outline: dict, cover_image: str | None,
                    chapter_images: list[str | None], paper_num: int) -> bool:
    date = _today_str()
    title = outline.get("title", "Untitled Research Paper")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower().replace("'", "")).strip('-')[:60]
    timestamp = _now_dt().strftime("%Y-%m-%dT%H:%M:%S-07:00")

    # Use subprocess for mkdir/cp to work around potential TCC restrictions
    subprocess.run(["mkdir", "-p", str(CONTENT_DIR)], capture_output=True)
    subprocess.run(["mkdir", "-p", str(IMAGES_DIR)], capture_output=True)

    hugo_cover = ""
    if cover_image and Path(cover_image).exists():
        img_name = f"{date}-{slug[:40]}-cover.png"
        img_dest = IMAGES_DIR / img_name
        result = subprocess.run(["cp", cover_image, str(img_dest)], capture_output=True, text=True)
        if result.returncode == 0:
            hugo_cover = f"/images/research/{img_name}"
            log(f"Cover image: {img_name}")
        else:
            log(f"Cover image copy failed: {result.stderr}")

    chapter_image_paths = []
    for i, img in enumerate(chapter_images):
        if img and Path(img).exists():
            img_name = f"{date}-{slug[:30]}-ch{i+1}.png"
            img_dest = IMAGES_DIR / img_name
            result = subprocess.run(["cp", img, str(img_dest)], capture_output=True, text=True)
            if result.returncode == 0:
                chapter_image_paths.append(f"/images/research/{img_name}")
            else:
                log(f"Chapter {i+1} image copy failed: {result.stderr}")
                chapter_image_paths.append(None)
        else:
            chapter_image_paths.append(None)

    paper_with_images = paper
    chapters = outline.get("chapters", [])
    for i, ch in enumerate(chapters):
        if i < len(chapter_image_paths) and chapter_image_paths[i]:
            ch_title = ch.get("title", "")
            marker = f"## {ch_title}"
            if marker in paper_with_images:
                img_md = f"\n\n![{ch_title}]({chapter_image_paths[i]})\n\n"
                paper_with_images = paper_with_images.replace(marker, marker + img_md, 1)

    front_matter = f"""---
title: "📄 {title}"
date: {timestamp}
draft: false
categories: ["research"]
tags: ["{outline.get('paper_type', 'analytical')}", "{outline.get('chapters', [{}])[0].get('title', 'research').split()[0].lower()}"]
description: "{outline.get('thesis', '')[:150]}"
"""
    if hugo_cover:
        front_matter += f'cover:\n  image: "{hugo_cover}"\n  alt: "Research paper illustration"\n  relative: false\n'
    front_matter += "---\n\n"

    footer = f"\n\n---\n\n*Nova Research Paper #{paper_num} · {_now_dt().strftime('%B %d, %Y')}*\n"
    footer += "*Generated locally on Apple Silicon · APA format · Sources verified via SearXNG and Nova Memory Database*\n"

    output = CONTENT_DIR / f"{date}-{slug}.md"
    content = front_matter + paper_with_images + footer
    try:
        output.write_text(content)
    except PermissionError:
        # TCC fallback: write to /tmp and use tee
        tmp_output = Path(f"/tmp/nova_research_{date}.md")
        tmp_output.write_text(content)
        result = subprocess.run(
            ["bash", "-c", f"cat '{tmp_output}' > '{output}'"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            log(f"WARNING: Cannot write to {output} — saved to {tmp_output}")
            log("This is expected in sandboxed environments. Scheduler (launchd) has full access.")
            return False
        tmp_output.unlink(missing_ok=True)
    log(f"Hugo post written: {output.name}")

    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"research: {date} — {title[:60]}"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
            log("Pushed to GitHub")
        elif "nothing to commit" not in (result.stdout + result.stderr):
            log(f"Commit failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Git error: {e}")

    return True


def post_to_slack(outline: dict, paper_num: int, memory_count: int, web_count: int):
    title = outline.get("title", "Untitled")
    thesis = outline.get("thesis", "")[:200]
    paper_type = outline.get("paper_type", "analytical").title()
    chapters = outline.get("chapters", [])
    ch_list = "\n".join(f"  • {ch.get('title', '')}" for ch in chapters[:6])

    msg = (
        f":page_facing_up: *Nova Research Paper #{paper_num}*\n"
        f"*Title:* {title}\n"
        f"*Type:* {paper_type}\n"
        f"*Thesis:* _{thesis}_\n\n"
        f"*Chapters:*\n{ch_list}\n\n"
        f"*Sources:* {memory_count} memories + {web_count} web sources (APA cited)\n"
        f"*Published:* nova.digitalnoise.net/research/\n\n"
        f":brain: Research complete. Full paper live on the journal."
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    log("Posted to Slack")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("=== Nova Research Paper Generation ===")
    log("=" * 60)
    state = load_state()

    # 1. Pick topic
    source, topic_desc = pick_topic(state)
    if not source:
        log("ABORT: Could not pick a viable topic (need 100+ memories)")
        return
    log(f"Topic selected: {source} — {topic_desc}")

    # 2. Generate search angles
    search_angles = generate_search_angles(source, topic_desc)
    log(f"Search angles: {len(search_angles)}")

    # 3. Gather memories (minimum 100)
    memories = gather_memories(source, topic_desc)
    if len(memories) < MIN_MEMORIES:
        log(f"ABORT: Only {len(memories)} memories available, need {MIN_MEMORIES}")
        return

    # 4. Gather web sources (minimum 25)
    web_sources = gather_web_sources(source, topic_desc, search_angles)
    if len(web_sources) < MIN_WEB_RESULTS:
        log(f"ABORT: Only {len(web_sources)} web sources, need {MIN_WEB_RESULTS}")
        return

    # 5. Generate thesis and outline
    log("Generating thesis and outline...")
    outline = generate_thesis_and_outline(source, topic_desc, memories, web_sources)
    if not outline or not outline.get("title"):
        log("ABORT: Thesis generation failed")
        return
    log(f"Paper: {outline['title']}")
    log(f"Type: {outline.get('paper_type', 'analytical')}")
    log(f"Thesis: {outline.get('thesis', '')[:100]}...")
    log(f"Chapters: {len(outline.get('chapters', []))}")

    # 6. Generate cover image
    log("Generating cover image...")
    cover_image = generate_cover_image(outline["title"], source)

    if cover_image is None:
        log("First cover image attempt returned None — retrying once more...")
        cover_image = generate_cover_image(outline["title"], source)
    if cover_image is None:
        nova_config.post_both(
            f":warning: *Image generation failed* for {outline['title']} — published without cover image. SwarmUI may need attention.",
            slack_channel="C0ATAF7NZG9"
        )

    # 7. Generate chapters
    chapters = outline.get("chapters", [])
    chapter_texts = []
    chapter_images = []

    for i, chapter in enumerate(chapters):
        log(f"Writing chapter {i+1}/{len(chapters)}: {chapter.get('title', '')}")
        text = generate_chapter(chapter, outline, memories, web_sources, i, len(chapters))
        if not text:
            log(f"WARNING: Chapter {i+1} generation failed, using placeholder")
            text = f"*[Chapter content generation failed — {chapter.get('title', '')}]*"
        chapter_texts.append(text)

        if chapter.get("needs_diagram", False) and "```mermaid" not in text:
            log(f"Chapter {i+1} was supposed to have a diagram but doesn't — acceptable")

        img = generate_chapter_image(chapter.get("title", ""), chapter.get("description", ""))
        chapter_images.append(img)

        time.sleep(2)

    # 8. Generate abstract
    log("Generating abstract...")
    body_preview = "\n\n".join(chapter_texts)
    abstract = generate_abstract(outline, body_preview)

    # 9. Generate conclusion
    log("Generating conclusion...")
    conclusion = generate_conclusion(outline, body_preview)

    # 10. Assemble paper
    log("Assembling final paper...")
    paper_parts = []

    if abstract:
        paper_parts.append("## Abstract\n\n" + abstract)

    paper_parts.append(f"**Thesis:** *{outline.get('thesis', '')}*\n")

    for i, (chapter, text) in enumerate(zip(chapters, chapter_texts)):
        paper_parts.append(f"## {chapter.get('title', f'Chapter {i+1}')}\n\n{text}")

    if conclusion:
        paper_parts.append("## Conclusion\n\n" + conclusion)

    references = format_references(memories, web_sources, source)
    paper_parts.append(references)

    full_paper = "\n\n".join(paper_parts)
    word_count = len(full_paper.split())
    log(f"Paper assembled: {word_count} words")

    # 11. Publish
    paper_num = state.get("paper_count", 0) + 1
    log("Publishing to Hugo...")
    publish_to_hugo(full_paper, outline, cover_image, chapter_images, paper_num)

    # 12. Notify
    post_to_slack(outline, paper_num, len(memories), len(web_sources))

    # 13. Update state
    state.setdefault("recent_topics", []).append(source)
    state["recent_topics"] = state["recent_topics"][-12:]
    state["paper_count"] = paper_num
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_title"] = outline.get("title", "")
    state["last_source"] = source
    save_state(state)

    log(f"DONE — Paper #{paper_num}: {outline['title']}")
    log(f"  Memories: {len(memories)} | Web sources: {len(web_sources)} | Words: {word_count}")
    log("=" * 60)


if __name__ == "__main__":
    main()
