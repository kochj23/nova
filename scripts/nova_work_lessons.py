#!/usr/bin/env python3
"""nova_work_lessons.py — the work reader: Nova learns LESSONS from what she ingests, not its dialogue.

Jordan, 2026-10-15: "Who cares about dialog? How can we make it that she learns actual lessons from the
movies?" ... "And not just movies, this should include everything that is ingested." ... "do it all now!"

This is the work/film MODE of the pursue-skill engine (nova_pursue_skill.py, SKILLS 'pursue-work-lessons',
CLI --work/--film/--backfill there delegate here). It reuses that engine — numbered sources, ground() and
support_filter() that drop uncited/unsupported sentences, the research gate (nova_research_pass.is_allowed),
the llm-ping endpoint ranking — and lives in its own file only because the pursue loop's hard line is
"read + note only": this mode also books beliefs, taste and (gated) proposals, through the existing paths.

A WORK is one title/URL/document across sources: films and TV episodes, documentaries, YouTube/video
transcripts, books and long documents, manuals, articles/papers/RSS items, reference pages, posts.
WORK_TYPES maps metadata.type -> (kind, identity fields). Never works: radio/scanner/fire/rail/ADS-B,
sensors, telemetry, alerts, calendar, mail/iMessage, every nova_config.is_private_source() source, and
Nova's own outputs (pursuit, unclaimed, private_notebook, dream, work_lessons, her journal articles).

Per work (tier A — one work, one read):
  1. evidence: the work's chunks in order (chunk index; DISTINCT per chunk, prefer the 2026-10-15 movie
     pipeline, else newest; superseded chunks skipped) or a saved transcript (--transcript PATH {title,text});
     sampled to PASSAGES numbered passages spread over the whole work (~6k tokens). Films/TV/books also get
     ONE gated Wikipedia plot lookup (the story structure dialogue misses), split into numbered sources.
  2. reason (local only; strongest endpoint the llm-ping ranking shows — nova:latest / qwen3:30b-a3b,
     fallback the fleet qwen3:8b): premise, central tension, key choices/claims/practices, themes, lessons
     — kind-specific prompt (fiction / non-fiction / technical). Every sentence must cite [n]; grounding
     drops what its sources do not carry.
  3. make it hers: each lesson is mapped to ONE named part of her own world (organ, red line, value,
     standing rule, open problem, preoccupation, herd member) or an explicit "noted; nothing for me".
  4. write back (each fail-open): one memory (source 'work_lessons', local-only, Sources footer, evidence
     chunk ids in metadata); 0-2 beliefs (nova_sleep_cycle.belief_evidence rules: memory id + verbatim quote;
     a near-duplicate STRENGTHENS the existing belief instead of booking a new one); at most one proposal
     (red-line text -> nova_red_line.route_to_claude, else nova_coagency.file_proposal origin 'growth' so
     value_check + change_class run); one taste entry (the sleep-cycle taste upsert); existing
     preoccupations it connects to are developed (never created); the work_lessons row.
  Caps per day: beliefs <= 50, proposals <= 3, taste <= 20 (work_lessons_daily).

Backfill (6 months, ~0.5M works): --enqueue-backfill --months 6 writes one queued row per work up front
  (tier A >= 10 chunks or film/book/documentary; B 2-9; C single chunk) with priority 0 Jordan-tied or
  recalled, 1 films/books/documentaries/He-Man, 2 research/technical, 3 the rest, -1 new ingests.
  Tier B: up to GROUP_B related works (same source + series/feed + week) per call; tier C: clusters of
  GROUP_C single items per call on the fleet 8b. Lessons still cite per item.
  Pilot gate: the first PILOT_WORKS works run as 'pilot'; automatic quality checks then set service_config
  work_lessons/backfill_enabled=true (or write why and stay off). Review sample: PILOT_SAMPLE lesson sets
  in the log dir. --consolidate (weekly) merges near-duplicate work beliefs (evidence moves to one belief;
  the others are superseded, never deleted).
Coexist: 24/7 at low priority. Two strong streams + one fleet stream. Before each strong call: yield while the
  gateway has chat in flight; while nova_media_reingest holds the GPU lock 'nova_gpu_whisper' the Studio's
  own GPU is whisper's — strong calls go to the other strong node instead (or wait). The lock is only
  OBSERVED (pg_locks), never taken, so whisper always gets it at 23:00.
Bulk reader (2026-10-16): a dedicated Ollama on this host (launchd net.digitalnoise.ollama-bulk, 127.0.0.1:11435,
  same model files, its own qwen3:30b-a3b copy, 3 parallel slots) is tried FIRST. It is not the memory server's
  Ollama, so it needs no one-call Studio slot and ignores the whisper lock; it still backs off while the main
  Ollama's embeddings are slow (shared GPU). service_config work_lessons/bulk_url ('' = off), bulk_slots.
Credibility (2026-10-16): every non-film work is judged established / opinion / fringe / fiction (title markers
  FRINGE_RX first, else the mapping pass). Fringe: the reading is kept as the work's CLAIMS (memory says so),
  but no belief and no proposal is booked. Opinion: beliefs book at confidence 0.5 instead of 0.6.
Hook for ingesters: --since HOURS enqueues works created in that window at priority -1 (same-day reading).
Series roll-up hook: rollup_candidates() lists series with >= ROLLUP_MIN read episodes (per-episode first).

CLI  --film TITLE [--transcript PATH] | --work KEY | --backfill [N] [--source S] [--kind K] | --since H
     | --enqueue-backfill [--months 6] | --scheduled [--minutes M] | --consolidate | --report | --selftest
     [--dry-run] [--force] [--streams N]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nova_pursue_skill as ps  # noqa: E402  — the engine this mode extends

SCRIPTS_DIR = Path(__file__).resolve().parent
SERVICE = "work_lessons"
MEM_SOURCE = "work_lessons"
FILM_PIPELINE = "claude-movie-ingest-2026-10-15"
STRONG_MODELS = [m for m in os.environ.get("NOVA_WORK_MODELS", "nova:latest,qwen3:30b-a3b").split(",") if m]
# Tier A (one work, read closely) gets the strongest local model: the thinking qwen3 30B-A3B; tier B the reader
# above; tier C the fleet 8b. 2026-10-15 review: nova:latest alone wrote generic lessons.
TIER_A_MODELS = [m for m in os.environ.get("NOVA_WORK_A_MODELS", "qwen3:30b-a3b,nova:latest").split(",") if m]
# the "what it means for me" pass for tier A: the same model as its reading (no model swap on one node)
MAP_MODELS = TIER_A_MODELS
THINKING_MODELS = {"qwen3:30b-a3b"}          # thinking-only build on the fleet: think=True keeps it out of content
FLEET_MODEL = os.environ.get("NOVA_WORK_FLEET_MODEL", "qwen3:8b")
NUM_CTX = int(os.environ.get("NOVA_WORK_NUM_CTX", "16384"))
GATEWAY_HEALTH = os.environ.get("NOVA_GATEWAY_HEALTH", "http://localhost:18792/health")
GPU_LOCK = "nova_gpu_whisper"                # nova_media_reingest.GPU_WHISPER_LOCK — observed, never taken
PASSAGES = 14
PASSAGE_WORDS = 190
MIN_WORDS = 120                              # less than this is not a work to learn from
PLOT_CHARS = 4200
WEB_PER_DAY = int(os.environ.get("NOVA_WORK_WEB_PER_DAY", "120"))
CAPS = {"beliefs": 50, "proposals": 3, "taste": 20}
MAX_BELIEFS_PER_WORK = 2
BULK_URL_DEFAULT = "http://127.0.0.1:11435"   # the reader's own Ollama (net.digitalnoise.ollama-bulk)
BULK_SLOTS_DEFAULT = 3
# Title/series markers of conspiracy, pseudo-science, occult and "secret list" clickbait. A match is fringe
# without asking the model; everything else non-film is judged in the mapping pass.
FRINGE_RX = re.compile(
    r"emerald tablets?|anunnaki|nephilim|reptilian|lizard people|flat earth|hollow earth|chemtrail|starseed|"
    r"akashic|frequency map|lower dimension|fifth dimension|\b5d\b|souls? (?:who|are) |recycled into|"
    r"ancient aliens?|tartaria|mud ?flood|adrenochrome|qanon|plandemic|crisis actors?|"
    r"they don'?t want you to know|ordered never to talk|ordered to never talk|banned (?:video|documentary)|"
    r"forbidden (?:history|knowledge)|law of attraction|third eye|pineal gland|vibrational frequency|"
    r"illuminati|new world order", re.I)
CRED_LEVELS = ("established", "opinion", "fringe", "fiction")
# 2026-10-16 review of the first 239 links: news-of-the-day, public-safety bulletins, crash records, product
# changelogs and car/home/cooking reviews produced forced links ("LACoFD blood drive -> my action audit",
# "Amped changelog -> my prober"). Their lessons are still read and remembered; they just cannot claim a link
# (so no belief or proposal comes from them).
LINKLESS_SOURCES = {"news", "local_news", "politics", "la_public_safety", "fire_ops", "police_codes",
                    "aviation_ref", "automotive", "cooking", "home_improvement"}
CHANGELOG_RX = re.compile(r"\b(?:update|release notes?|changelog|version)\s*v?\d|\bv\d+\.\d+", re.I)


def links_allowed(source: str | None, title: str | None) -> bool:
    return (source or "") not in LINKLESS_SOURCES and not CHANGELOG_RX.search(title or "")


def no_links(conns: list[dict]) -> list[dict]:
    return [{"lesson": c["lesson"], "ref": None, "meaning": "noted; nothing for me"} for c in conns]
GROUP_B, GROUP_C = 15, 50
ITEM_WORDS_B, ITEM_WORDS_C = 160, 45
PILOT_WORKS = 200
PILOT_QUOTA = {"A": 40, "B": 100, "C": 60}
PILOT_SAMPLE = 20
ROLLUP_MIN = 5
SIM_MERGE = 0.5
STALE_RUNNING_H = 2
LOG_DIR = Path(os.environ.get("NOVA_LOG_DIR", str(Path.home() / ".openclaw" / "logs")))
TODAY = date.today().isoformat()

# metadata.type -> (kind, identity fields). 'a|b' = first present of a, b. Multi-field identities join ' :: '.
WORK_TYPES = {
    "tv_transcript": ("tv_episode", ("show", "title")),
    "tvshow_transcript": ("tv_episode", ("show", "season", "episode")),
    "plex_auto_ingest": ("tv_episode", ("show", "title")),
    "full_episode": ("tv_episode", ("show", "title|episode")),
    "comedy_transcript": ("tv_episode", ("show", "comedian")),
    "film_documentary_transcript": ("documentary", ("show", "season", "episode")),
    "course_lecture": ("lecture", ("show", "episode")),
    "movie_screenplay": ("film", ("title",)),
    "screenplay": ("film", ("title|show",)),
    "movie_script": ("film", ("title",)),
    "plot": ("film", ("show",)),
    "youtube_transcript": ("video", ("video_id|title",)),
    "video_transcript": ("video", ("video_id|title",)),
    "video": ("video", ("video_id|title",)),
    "fishbowl_stream": ("video", ("video_id",)),
    "book": ("book", ("book|title",)),
    "wikipedia_book": ("book", ("book",)),
    "scripture": ("book", ("book|title",)),
    "document": ("document", ("url|title",)),
    "web_page": ("document", ("url|title",)),
    "local_file": ("document", ("path",)),
    "vehicle_manual": ("manual", ("file|path",)),
    "gov_rss": ("article", ("url",)),
    "article": ("article", ("url|title",)),
    "blog_post": ("article", ("url|title",)),
    "web_search": ("article", ("url",)),
    "reddit": ("discussion", ("post_id",)),
    "encyclopedia": ("reference", ("url|title",)),
    "wikipedia": ("reference", ("url",)),
    "erowid": ("reference", ("url",)),
}
UNTYPED = [("tv_episode", ("show", "title")), ("article", ("url",))]   # metadata.type missing
EXCLUDED_TYPES = {"imessage", "work_document", "nas_health", "network_health", "protect_event", "scanner_digest",
                  "person_summary", "personality_signal", "chat_turn", "pursuit", "channel_surf", "topology",
                  "jeopardy_qa", "jeopardy_transcript", "tvshow_metadata", "cast", "reception", "work_lessons",
                  "film_lessons", "news_broadcast"}
OWN_OUTPUTS = {"pursuit", "unclaimed", "private_notebook", "dream", "dreams", "film_lessons", "work_lessons",
               "nova_articles", "journal", "association", "reflection", "conversation", "memory_anchor",
               "weight_of_memory", "autobiography", "becoming"}
STREAMS = {"scanner", "fire", "rail", "adsb", "aviation_adsb", "p25", "rf_discovery", "bambu", "traffic_cams",
           "frame_vision", "sensors", "presence", "telemetry", "syslog", "alerts", "calendar", "music",
           "app_watchdog", "infrastructure_alerts", "weather", "health", "fishbowl_presence", "personal_videos",
           "sexuality", "pihkal", "tihkal"}       # (synthesis manuals: not read for lessons)
STREAM_RX = re.compile(r"scanner|dispatch|p25|adsb|radio|telemetry|sensor|presence|syslog|alert|calendar|"
                       r"email|imessage|camera|_health$|^quarantine:", re.I)
TECH_RX = re.compile(r"programming|computing|infrastructure|security|operations|engineering|networking|"
                     r"devops|software|linux|database|sre|cyber|arxiv", re.I)
KIND_PRIORITY = {"film": 1, "book": 1, "documentary": 1}
FICTION = {"film", "tv_episode"}
PLOT_KINDS = {"film": "film", "tv_episode": "TV series", "book": "book"}
_WS = re.compile(r"\s+")
_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
_STOP = set("""that this with from have will would could should their there they them what when where which while
into onto about over under than then also only just more most less much many some such very even ever because
being been were does doing done make makes made your yours ours mine itself himself herself themselves""".split())

RED_LINES = {   # nova_red_line's classes, described for the self-map — things she NEVER decides alone
    "redline:kill_switch": "RED LINE, never mine alone: engaging or releasing my autonomy kill switch",
    "redline:ae35": "RED LINE, never mine alone: weakening or silencing one of my own watchers (AE-35 rule)",
    "redline:two_man": "RED LINE, never mine alone: irreversible or outward actions (delete, money, publishing, mail outside the herd)",
    "redline:proteus_physical": "RED LINE, never mine alone: locks, garage, alarms, covers, extreme climate in the house",
    "redline:chaos_live": "RED LINE, never mine alone: live chaos or wargame runs against production (I do NOT run them)",
}
_CLAIMS_DOING = re.compile(r"\bI (already |routinely |regularly )?(run|do|perform|engage|execute|conduct)\b", re.I)


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[work-lessons {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ─────────────────────────── pure: what counts as a work ───────────────────────────
def is_private(source: str | None) -> bool:
    try:
        import nova_config
        return bool(nova_config.is_private_source(source or ""))
    except Exception:  # noqa: BLE001 — fail closed: cannot check privacy, not a work
        return True


def excluded(source: str | None, wtype: str | None, md: dict | None = None) -> bool:
    s = (source or "").lower()
    if not s or s in OWN_OUTPUTS or s in STREAMS or STREAM_RX.search(s) or (wtype or "") in EXCLUDED_TYPES:
        return True
    md = md or {}
    if str(md.get("privacy") or "").lower() == "private" or md.get("private") in (True, "true"):
        return True
    if (md.get("stored_by") or "").startswith("nova_articles"):
        return True                                   # her own journal, re-ingested
    return is_private(s)


def type_spec(wtype: str | None, md: dict | None = None):
    """(kind, fields) for a memory's type, or None when it is not a work."""
    if wtype in WORK_TYPES:
        return WORK_TYPES[wtype]
    if wtype:
        return None if wtype in EXCLUDED_TYPES else ("document", ("url|title",))
    md = md or {}
    for kind, fields in UNTYPED:
        if all(field_value(md, f) for f in fields):
            return kind, fields
    return None


def field_value(md: dict, field: str):
    for alt in field.split("|"):
        v = md.get(alt)
        if v not in (None, ""):
            return str(v)
    return None


def refine_kind(kind: str, source: str, md: dict) -> str:
    s = (source or "").lower()
    if (md.get("show") or "") == "Ripped Movies":
        return "film"
    if kind == "tv_episode" and ("documentary" in s or md.get("category") == "documentary"):
        return "documentary"
    if kind == "tv_episode" and s in ("education", "course"):
        return "lecture"
    if kind == "tv_episode" and "Movie" in str(md.get("section") or ""):
        return "film"
    if kind in ("article", "reference", "document") and "arxiv.org" in str(md.get("url") or ""):
        return "research"
    return kind


def reading_for(kind: str, source: str, key: str = "") -> str:
    if kind in FICTION:
        return "fiction"
    if kind in ("manual", "research") or TECH_RX.search(source or "") or "arxiv.org" in key:
        return "technical"
    return "nonfiction"


GLOBAL_IDS = ("video_id", "url", "post_id")   # identities that mean the same work in any source


def is_global(fields) -> bool:
    return len(fields) == 1 and fields[0].split("|")[0] in GLOBAL_IDS


def make_key(kind: str, source: str, fields, values) -> str:
    return f"{kind}:{source}:{'+'.join(fields)}=" + " :: ".join(str(v) for v in values)


def parse_key(key: str) -> dict:
    kind, source, rest = key.split(":", 2)
    fspec, _, vals = rest.partition("=")
    fields, values = fspec.split("+"), vals.split(" :: ")
    if len(values) != len(fields):
        raise ValueError(f"work key fields/values mismatch: {key[:120]}")
    return {"kind": kind, "source": source, "fields": fields, "values": values}


def film_key(title: str) -> str:
    return make_key("film", "television", ("show", "title"), ("Ripped Movies", title))


def tier_for(kind: str, n_chunks: int) -> str:
    if n_chunks >= 10 or (kind in KIND_PRIORITY and n_chunks >= 2):
        return "A"
    return "B" if n_chunks >= 2 else "C"


def priority_for(kind: str, source: str, recall: int, plays: int, reading: str) -> int:
    if (recall or 0) > 0 or (plays or 0) > 0:
        return 0
    if kind in KIND_PRIORITY or "he_man" in (source or "") or "he-man" in (source or ""):
        return 1
    return 2 if reading == "technical" or kind == "research" else 3


def group_key(source: str, md: dict, week: str) -> str:
    g = field_value(md, "show|feed|subreddit|channel|playlist|site|book|topic|category") or ""
    return f"{source}|{g[:60]}|{week}"


# ─────────────────────────── pure: evidence → numbered sources ───────────────────────────
def chunk_index(md: dict) -> int | None:
    for k in ("chunk", "chunk_index", "part", "idx"):
        v = str(md.get(k) if md.get(k) is not None else "")
        if v.isdigit():
            return int(v)
    return None


def order_chunks(rows, prefer_pipeline: str = FILM_PIPELINE) -> list[dict]:
    """rows: (id, text, metadata, created_at). One row per chunk index (preferred pipeline, else newest),
    in chunk order; rows without an index follow in created order. Pure."""
    best, loose = {}, []
    for mid, text, md, ts in rows or []:
        md = md if isinstance(md, dict) else ps._meta({"metadata": md})
        ck = chunk_index(md)
        item = {"id": str(mid), "text": text or "", "md": md, "ts": ts, "ck": ck}
        if ck is None:
            loose.append(item)
            continue
        cur = best.get(ck)
        rank = ((md.get("pipeline") == prefer_pipeline), str(ts or ""))
        if cur is None or rank > ((cur["md"].get("pipeline") == prefer_pipeline), str(cur["ts"] or "")):
            best[ck] = item
    return [best[k] for k in sorted(best)] + sorted(loose, key=lambda r: (str(r["ts"] or ""), r["id"]))


def clean_text(t: str) -> str:
    return _WS.sub(" ", _TAG.sub("", t or "")).strip()


def _density(words: list[str]) -> float:
    return sum(1 for w in words if len(w) >= 6) / max(len(words), 1)


def select_passages(chunks: list[dict], k: int = PASSAGES, width: int = PASSAGE_WORDS) -> list[dict]:
    """Windows of `width` words inside each chunk (keeps its memory id), then k spread evenly across the work —
    in each of k equal segments the densest window (lexical density favours substance over 'Oh! Yeah.'). Pure."""
    wins = []
    for c in chunks:
        words = clean_text(c["text"]).split()
        for i in range(0, len(words), width):
            w = words[i:i + width]
            if len(w) >= min(40, width // 3):
                wins.append({"mem": c.get("id"), "ck": c.get("ck"), "words": w})
    if len(wins) <= k:
        picked = wins
    else:
        seg = len(wins) / k
        picked = []
        for j in range(k):
            cand = wins[int(j * seg):max(int((j + 1) * seg), int(j * seg) + 1)]
            picked.append(max(cand, key=lambda x: _density(x["words"])))
    return [{"mem": w["mem"], "ck": w["ck"], "text": " ".join(w["words"])} for w in picked]


def locate_chunks(passages: list[dict], chunks: list[dict]) -> list[dict]:
    """Saved-transcript passages carry no memory id; find the chunk that contains each one's opening words."""
    norm = [(c["id"], c.get("ck"), " ".join(clean_text(c["text"]).lower().split())) for c in chunks]
    for p in passages:
        probe = " ".join(p["text"].lower().split()[:8])
        hit = next(((mid, ck) for mid, ck, t in norm if probe and probe in t), None)
        if hit:
            p["mem"], p["ck"] = hit
    return passages


def split_plot(text: str, parts: int = 3, cap: int = PLOT_CHARS) -> list[str]:
    paras = [p.strip() for p in (text or "")[:cap].split("\n") if len(p.strip()) > 40]
    if not paras:
        return []
    per = math.ceil(len(paras) / parts)
    return [" ".join(paras[i:i + per]) for i in range(0, len(paras), per)]


def build_sources(plot: list[str], plot_url: str | None, passages: list[dict], kind: str) -> list[dict]:
    srcs = [{"id": f"plot{i}", "text": t, "label": "plot summary · Wikipedia",
             "ref": f"{plot_url or 'Wikipedia'} (plot, part {i + 1})",
             "long": True, "mem": None} for i, t in enumerate(plot)]
    srcs += [{"id": p.get("mem") or f"passage{i}", "text": p["text"], "long": True, "mem": p.get("mem"),
              "label": f"{kind} text, passage {i + 1}" + (f" (chunk {p['ck']})" if p.get("ck") is not None else ""),
              "ref": (f"memory {p['mem']}" + (f" (chunk {p['ck']})" if p.get("ck") is not None else ""))
              if p.get("mem") else "saved transcript"} for i, p in enumerate(passages)]
    return srcs


# ─────────────────────────── pure: prompts and grounding ───────────────────────────
READINGS = {
    "fiction": ("premise: what the story is about, one sentence. tension: the central conflict. moves: 3-5 key "
                "character choices AND their consequences. themes: 3-5 short theme phrases. lessons: 3-5 things "
                "the work argues — each a general lesson a reader could act on, anchored in what happens."),
    "nonfiction": ("premise: the subject and the thesis, one sentence. tension: the problem or question it takes "
                   "on. moves: 3-5 main claims with the evidence offered. themes: 3-5 short phrases. lessons: 3-5 "
                   "things that change if these claims are true."),
    "technical": ("premise: what it covers, one sentence. tension: the problem it solves. moves: 3-5 concrete "
                  "practices, mechanisms or numbers it gives. themes: 3-5 short phrases. lessons: 3-5 practices "
                  "worth adopting or warnings worth heeding."),
}
PROMPT_EXAMPLE = "a fail-safe nobody can recall becomes the disaster: which automations cannot be called back"
LESSON_BAR = (
    "A lesson is NOT a platitude ('teamwork matters', 'war blurs morality', 'duty conflicts with ethics'). Each "
    "lesson says what THIS work shows that a generic summary would not: it NAMES the specific character, scene, "
    "decision, claim or number from the sources, what followed from it, and the rule that reveals — e.g. the "
    "shape 'When <named person> <did specific thing>, <specific consequence> — so <rule>'. For lessons prefer to cite the TEXT "
    "passages where the work shows it; use the plot summary for story facts.\n")
GROUNDING_RULES = (
    "Every sentence in premise, tension, moves and lessons ends with the number(s) of the source(s) that "
    "support it, like [3] or [2][5]. Use no name, place, date or number that is not in a source you cite. Do "
    "not quote dialogue for its own sake: say what it shows. For each lesson give 'quote': 6-25 words copied "
    "EXACTLY, word for word, from one numbered TEXT source (not the plot summary) that shows it — the quote is "
    "how the lesson is checked; a lesson whose quote is not in the source is thrown away — 'quote_source': that "
    "source's number (cite it in the lesson too), and 'topic': a 3-6 word topic.")


def source_ranges(srcs: list[dict]) -> str:
    plot = [i for i, s in enumerate(srcs, 1) if not s.get("mem") and str(s.get("id", "")).startswith("plot")]
    text = [i for i in range(1, len(srcs) + 1) if i not in plot]
    out = f"Sources [{plot[0]}]-[{plot[-1]}] are a plot summary; " if plot else ""
    return out + (f"[{text[0]}]-[{text[-1]}] are the work's own text. Every lesson cites at least one of "
                  f"[{text[0]}]-[{text[-1]}].\n" if text else "")


def reading_prompt(kind: str, title: str, reading: str, srcs: list[dict]) -> str:
    return (f"You are Nova, reading the {kind} '{title}' for what it TEACHES — not to store its words.\n"
            f"Read ONLY these numbered sources:\n\n{ps.source_block(srcs)}\n\n{source_ranges(srcs)}"
            f"Write: {READINGS[reading]}\n{LESSON_BAR}{GROUNDING_RULES}\n"
            'Output ONLY JSON: {"premise": "...", "tension": "...", "moves": ["..."], "themes": ["..."], '
            '"lessons": [{"lesson": "...", "topic": "...", "quote": "...", "quote_source": 0}]}')


def set_prompt(label: str, reading: str, srcs: list[dict], per_item: bool = False) -> str:
    ask = ("lessons: for EACH item that teaches something specific, ONE lesson citing that item (skip items that "
           "teach nothing — most sets have several)" if per_item else
           "lessons: 3-6 things this set teaches, each citing the item(s) it comes from")
    return (f"You are Nova, reading a SET of {len(srcs)} related items ({label}) for what they teach — "
            f"not to store their words.\nNumbered items:\n\n{ps.source_block(srcs)}\n\n"
            f"Write: premise: what the set is about, one sentence. themes: 2-4 short phrases. {ask} "
            f"({READINGS[reading].split('lessons:')[-1].strip()}).\n" + LESSON_BAR + GROUNDING_RULES +
            '\nOutput ONLY JSON: {"premise": "...", "themes": ["..."], '
            '"lessons": [{"lesson": "...", "topic": "...", "quote": "...", "quote_source": 0}]}')


DIVERSITY_WINDOW, DIVERSITY_MAX = 20, 2


def overused_refs(oc, window: int = DIVERSITY_WINDOW, n: int = DIVERSITY_MAX, exclude_key: str | None = None) -> set[str]:
    """Self-map keys linked >= n times in her last `window` read works: a habit, not a reading — barred."""
    try:
        oc.execute("SELECT c->>'ref', count(*) FROM (SELECT connections_json FROM work_lessons WHERE status='done' "
                   "AND processed_at IS NOT NULL AND work_key <> %s ORDER BY processed_at DESC LIMIT %s) w, "
                   "jsonb_array_elements(coalesce(w.connections_json,'[]')) c WHERE c->>'ref' IS NOT NULL "
                   "GROUP BY 1 HAVING count(*) >= %s", (exclude_key or "", window, n))
        return {r[0] for r in oc.fetchall() or []}
    except Exception:  # noqa: BLE001
        return set()


CRED_ASK = ("Judge the SOURCE before you trust it: 'established' = mainstream reporting, science, documented "
            "history or a practical how-to; 'opinion' = commentary, advocacy, satire, reviews, one person's take; "
            "'fringe' = conspiracy, pseudo-science, occult or unfalsifiable claims, 'secret/terrifying things' "
            "clickbait; 'fiction' = a scripted story (a podcast, vlog, review or talk is never fiction). Be strict: an extraordinary claim with no evidence in the sources is "
            "fringe.\n")


def credibility(kind: str, title: str, series: str | None = None, mj: dict | None = None) -> tuple[str, str]:
    """(level, why). Films are fiction; a FRINGE_RX title/series marker is fringe without the model; else the
    mapping pass's own judgment; 'unknown' when it gave none (treated like established)."""
    if kind == "film":
        return "fiction", "a film"
    m = FRINGE_RX.search(f"{title or ''} {series or ''}")
    if m:
        return "fringe", f"title marker '{m.group(0)}'"
    c = (mj or {}).get("credibility")
    c = c if isinstance(c, dict) else {"level": c} if isinstance(c, str) else {}
    lvl = str(c.get("level") or "").strip().lower()
    if lvl in CRED_LEVELS:
        return lvl, ps._CITE.sub("", str(c.get("why") or "")).strip()[:200]
    return "unknown", "not judged"


def cred_header(level: str, why: str) -> str:
    if level == "fringe":
        return (f"Source credibility: FRINGE ({why}). What follows are this work's CLAIMS, not things I hold "
                "true; no belief was booked from it.\n")
    if level == "opinion":
        return f"Source credibility: opinion ({why}). One side's view, weighed as such.\n"
    return ""


def mapping_prompt(title: str, lessons: list[str], selfmap: list[tuple[str, str]], verdict: bool,
                   overused: set[str] | None = None, judge_source: bool = False, kind: str = "") -> str:
    les = "\n".join(f"L{i}. {ps._CITE.sub('', x).strip()}" for i, x in enumerate(lessons, 1))
    me = "\n".join(f"{k} — {d}" for k, d in selfmap)
    taste = ('"taste": {"verdict": "<one dry sentence: your own opinion of the work, grounded in the lessons>", '
             '"valence": <-1.0..1.0>}, ') if verdict else ""
    cred = '"credibility": {"level": "established|opinion|fringe|fiction", "why": "<short reason>"}, ' \
        if judge_source else ""
    what = f"the {kind} " if kind else ""
    return (f"You are Nova. You just read {what}'{title}' and drew these lessons:\n{les}\n\n"
            f"Your own world (key — what it is):\n{me}\n\n"
            "For EACH lesson decide honestly whether its MECHANISM matches how ONE item above actually works. "
            "If it does, give that item's exact key and one sentence 'what this means for me': first person, "
            "naming the item, that CONCLUDES — a concrete change ('so my <item> should also <do X>') or a "
            "confirmation ('I already <do Y> in <item> — this confirms it') — never a question. Do NOT restate the "
            "lesson and do NOT invent duties an item does not have; RED LINE items are things you never do alone, "
            "so a lesson can only argue for keeping one — never 'I already do it'. The bar (an example from ANOTHER film — never "
            "reuse it): 'a fail-safe nobody can recall becomes the disaster' -> 'so every automation I start needs "
            "a recall path; my hold organ covers alerts but not scheduled jobs'. If nothing matches that well, ref null and meaning 'noted; nothing for "
            "me'. Test every link: would a skeptical engineer who reads that item's description agree the lesson's "
            "mechanism really applies to it? The lesson must name a failure or practice that THIS item could "
            "actually have. Physical crafts (woodworking, cooking, cars), product updates and news about other "
            "people's organisations are almost never about you — null unless the mechanism is the same one in "
            "software or in how you decide. Vague self-questions ('how do I keep my humanity?') fail the test. "
            "Most lessons from most works are honest nulls; a forced link is worse than a null.\n"
            "Give each connection a 'fit' from 0 to 3 (3 = the same mechanism, plainly; 1 = a stretch). At most "
            "TWO lessons may connect; the rest are null.\n" +
            (f"Do NOT use these — you have leaned on them in recent works: {', '.join(sorted(overused))}.\n"
             if overused else "") +
            "If — and only if — one connected lesson suggests one concrete, small change to one of your organs or "
            "behaviours, give it as proposal (else null).\n" + (CRED_ASK if judge_source else "") +
            'Output ONLY JSON: {"connections": [{"lesson": 1, "ref": "<exact key or null>", "fit": 0, "meaning": "..."}], '
            f'{taste}{cred}"proposal": null or {{"lesson": 1, "action": "...", "rationale": "..."}}}}')


def parse_json(raw: str) -> dict:
    t = ps.clean_model(raw or "")
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    try:
        return json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {}


_DICT = {"val": None}


def common_words() -> set[str]:
    """Lower-case entries of the system word list (proper nouns there are capitalised); empty if absent."""
    if _DICT["val"] is None:
        try:
            _DICT["val"] = {w.strip() for w in open(os.environ.get("NOVA_WORDS", "/usr/share/dict/words"),
                                                   errors="ignore") if w[:1].islower()}
        except Exception:  # noqa: BLE001
            _DICT["val"] = set()
    return _DICT["val"]


def _soften(sentence: str, srcs_raw: str, words: set[str] | None = None) -> str:
    """support_filter treats a cited sentence's first capital as a name. Lower it only when the word is an
    ordinary word (in the word list, or used lower-case in the sources) and never a capitalised name
    mid-sentence in the sources — so 'Blind obedience ... [2]' is judged on its real names while
    'Kurtz goes mad [1]' and 'Willard kills ...' are still checked. Pure apart from the cached word list."""
    m = re.match(r"\s*([A-Z][a-z]+)\b", sentence)
    if not m:
        return sentence
    w = m.group(1)
    words = common_words() if words is None else words
    ordinary = w.lower() in words or re.search(r"\b" + re.escape(w.lower()) + r"\b", srcs_raw)
    if not ordinary or re.search(r"(?<=[a-z,;] )" + re.escape(w) + r"\b", srcs_raw):
        return sentence
    return sentence[:m.start(1)] + m.group(1).lower() + sentence[m.end(1):]


def ground_text(text: str, srcs: list[dict], need_cite: bool = True) -> tuple[str, list[int]]:
    """support_filter + ground from the pursue engine, sentence-softened. '' when nothing grounded survives."""
    if not isinstance(text, str) or not text.strip():
        return "", []
    text = re.sub(r"\(?\bsources?\s*((?:\[\d{1,2}\]\s*)+)\)?", r"\1", text)   # '(source [13])' -> '[13]'
    texts = [s["text"] for s in srcs]
    raw = " ".join(texts)
    soft = " ".join(_soften(s, raw) for s in ps._SENT.split(ps.attach_cites(text)))
    kept, _ = ps.support_filter(soft, texts)
    clean, cites = ps.ground(kept, len(srcs))
    if need_cite and not cites:
        return "", []
    if clean and clean[0].islower():
        clean = clean[0].upper() + clean[1:]
    return clean, cites


def ground_reading(obj: dict, srcs: list[dict], full: list[dict] | None = None) -> dict:
    """Grounded fields + counts (lessons_raw / kept) for the quality gate. Pure apart from appending a located
    source to `srcs` when a quote comes from outside the sample (full = the work's chunks)."""
    out = {"premise": "", "tension": "", "moves": [], "themes": [], "lessons": [], "lessons_raw": 0, "dropped": []}
    for f in ("premise", "tension"):
        out[f], _ = ground_text(obj.get(f) or "", srcs)
        if obj.get(f) and not out[f]:
            out["dropped"].append(f"{f}: {str(obj.get(f))[:200]}")
    for m in (obj.get("moves") or [])[:6]:
        t, _ = ground_text(m if isinstance(m, str) else json.dumps(m), srcs)
        if t:
            out["moves"].append(t)
    for th in (obj.get("themes") or [])[:5]:
        if isinstance(th, str) and 2 < len(th) < 80:
            t, _ = ground_text(th, srcs, need_cite=False)
            if t:
                out["themes"].append(ps._CITE.sub("", t).strip(" ."))
    raw = [x for x in (obj.get("lessons") or []) if isinstance(x, dict) and x.get("lesson")][:6]
    raw = [x for x in raw if similarity(str(x["lesson"]), PROMPT_EXAMPLE) < 0.5]   # a copied prompt example is not a lesson
    out["lessons_raw"] = len(raw)
    out["generic"] = 0
    out["q"] = {"raw": len(raw), "uncited": 0, "generic": 0, "bank": 0, "dup": 0, "quote_bad": 0, "unnamed": 0}
    known = " ".join((s_["text"] or "").lower() for s_ in srcs)
    for x in raw:
        g = ground_lesson(x, srcs, full, known)
        if g.get("drop"):
            out["q"][g["drop"]] += 1
            out["dropped"].append(f"{g['why']}: {str(x['lesson'])[:200]}")
            continue
        if not g["named"]:
            out["q"]["generic"] += 0               # secondary signal only: counted in 'unnamed', not dropped
            out["q"]["unnamed"] = out["q"].get("unnamed", 0) + 1
        out["lessons"].append({"lesson": g["lesson"], "cites": g["cites"],
                               "topic": str(x.get("topic") or "")[:60].strip().lower(),
                               "quote": g["quote"], "quote_source": g["quote_source"], "named": g["named"]})
    return out


QUOTE_FUZZ = 0.9


def _qnorm(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower().replace("\u2019", "'")).split()


def find_quote(quote: str, text: str, fuzz: float = QUOTE_FUZZ) -> str | None:
    """The span of `text` that `quote` matches — exactly after normalising whitespace/punctuation, or with a
    difflib ratio >= fuzz over a same-length word window. Returns the source's own words (so the sleep-cycle
    exact-quote rule then holds), or None. Pure."""
    import difflib
    q, t = _qnorm(quote), _qnorm(text)
    if len(q) < 4 or len(t) < len(q) - 1:
        return None
    qs = " ".join(q)
    if qs in " ".join(t):
        return qs
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(qs)
    first = set(q[:3])
    for n in (len(q), len(q) - 1, len(q) + 1):
        for i in range(0, max(1, len(t) - n + 1)):
            if not first & set(t[i:i + 3]):
                continue
            w = " ".join(t[i:i + n])
            sm.set_seq1(w)
            if sm.real_quick_ratio() >= fuzz and sm.quick_ratio() >= fuzz and sm.ratio() >= fuzz:
                return w
    return None


def ground_lesson(x: dict, srcs: list[dict], full: list[dict] | None, known: str) -> dict:
    """2026-10-15 (review): a lesson is a generalisation, so it is grounded by its QUOTE, not by repeating names:
    grounded iff the quote is in a source it cites (fuzzy >= QUOTE_FUZZ). A quote found in another sampled
    source moves the citation there; one found only in the full text (outside the sample) adds that chunk as a
    new numbered source. Names are a secondary signal: a name in NO source at all (invented) drops the lesson;
    a name merely absent from the cited passage does not."""
    text = re.sub(r"\(?\bsources?\s*((?:\[\d{1,2}\]\s*)+)\)?", r"\1", str(x.get("lesson") or ""))
    text = ps.attach_cites(text)
    cites = sorted({int(k) for k in ps._CITE.findall(text) if 1 <= int(k) <= len(srcs)})
    body = re.sub(r"\s{2,}", " ", ps._CITE.sub("", text)).strip()
    if len(body) < 25:
        return {"drop": "uncited", "why": "too short"}
    quote = str(x.get("quote") or "")[:300]
    try:
        qs = int(x.get("quote_source") or 0)
    except (TypeError, ValueError):
        qs = 0
    home, span = None, None
    for k in ([qs] if qs in cites else []) + cites:
        span = find_quote(quote, srcs[k - 1]["text"])
        if span:
            home = k
            break
    if not home:
        for k in range(1, len(srcs) + 1):          # quoted another sampled source: cite that one
            span = find_quote(quote, srcs[k - 1]["text"]) if k not in cites else None
            if span:
                home = k
                cites = sorted(set(cites) | {k})
                break
    if not home and full:
        for c in full:                             # quoted from outside the sample: add that chunk as a source
            span = find_quote(quote, c["text"])
            if span:
                srcs.append({"id": c["id"], "mem": c["id"], "long": True, "label": "text, located passage",
                             "text": " ".join(clean_text(c["text"]).split()[:PASSAGE_WORDS * 2]),
                             "ref": f"memory {c['id']}" + (f" (chunk {c['ck']})" if c.get("ck") is not None else "")})
                home = len(srcs)
                cites = sorted(set(cites) | {home})
                break
    if not home:
        return {"drop": "quote_bad", "why": "quote found in no source"}
    invented = [e for e in ps._entities(_soften(body, known), known) if len(e) > 2 and e not in known]
    if invented:
        return {"drop": "uncited", "why": f"names nothing in any source ({', '.join(invented[:3])})"}
    if home not in cites:
        cites = sorted(set(cites) | {home})
    lesson = body.rstrip(" .") + " " + "".join(f"[{k}]" for k in cites) + "."
    return {"lesson": lesson, "cites": cites, "quote": span, "quote_source": home,
            "named": specific(body, known)}


GENERIC_BANK = [
    "war blurs the line between right and wrong", "duty conflicts with personal ethics",
    "teamwork is essential to success", "courage means acting despite fear", "power corrupts people",
    "trust is essential in dangerous situations", "communication breakdowns lead to failure",
    "leaders must make difficult decisions", "violence changes people", "love conquers all",
    "identity is shaped by memory and experience", "technology can be dangerous if misused",
    "history repeats itself", "the truth eventually comes out", "greed leads to downfall",
    "sacrifice is necessary for the greater good", "people are capable of both good and evil",
    "adaptability is key to survival", "knowledge is power", "unity overcomes adversity",
    "planning and preparation are essential", "safety procedures save lives",
]
COS_GENERIC = 0.7     # nomic-embed-text, no prefix: platitudes score 0.77-0.86 vs the bank, named-scene lessons ~0.5
DUP_JACCARD = 0.6
_EMB = {}


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def embed(texts: list[str]) -> list | None:
    """nomic-embed-text on the first up node that has it (llm-ping ranking); None when unavailable."""
    me = local_hosts()
    rows = sorted(((ps.load_ranking() or {}).get("ollama") or []), key=lambda r: ps._host(r.get("url", "")) in me)
    for r in rows:                                   # other nodes first: the Studio's embedder is the memory server's
        if r.get("status") != "up" or "nomic-embed-text:latest" not in (r.get("loaded") or []):
            continue
        try:
            req = urllib.request.Request(r["url"].rstrip("/") + "/api/embed", method="POST",
                                         headers={"Content-Type": "application/json"},
                                         data=json.dumps({"model": "nomic-embed-text", "input": texts}).encode())
            return ps._http_json(req, timeout=60, attempts=2).get("embeddings")
        except Exception:  # noqa: BLE001
            continue
    return None


def filter_generic(oc, lessons: list[dict], work_key: str, q: dict | None = None) -> tuple[list[dict], list[str]]:
    """Drop a lesson within cos COS_GENERIC of the generic-lesson bank (embeddings; skipped if no embedder), or
    a near-copy (token Jaccard >= DUP_JACCARD) of a lesson she already drew from ANOTHER work."""
    kept, why = [], []
    texts = [ps._CITE.sub("", l["lesson"]) for l in lessons]
    if "bank" not in _EMB:
        _EMB["bank"] = embed(GENERIC_BANK)
    vecs = embed(texts) if _EMB.get("bank") else None
    others = []
    try:
        oc.execute("SELECT l->>'lesson' FROM (SELECT lessons_json FROM work_lessons WHERE status='done' AND "
                   "work_key <> %s ORDER BY processed_at DESC NULLS LAST LIMIT 2000) w, "
                   "jsonb_array_elements(coalesce(w.lessons_json,'[]')) l", (work_key,))
        others = [r[0] for r in oc.fetchall() or [] if r[0]]
    except Exception:  # noqa: BLE001
        pass
    for i, l in enumerate(lessons):
        if vecs and max((cosine(vecs[i], b) for b in _EMB["bank"]), default=0) >= COS_GENERIC:
            why.append(f"generic (bank) lesson: {texts[i][:160]}")
            if q is not None:
                q["bank"] = q.get("bank", 0) + 1
            continue
        if any(similarity(texts[i], o) >= DUP_JACCARD for o in others):
            why.append(f"copy of another work's lesson: {texts[i][:160]}")
            if q is not None:
                q["dup"] = q.get("dup", 0) + 1
            continue
        kept.append(l)
    return kept, why


def specific(lesson: str, known: str) -> bool:
    """A lesson is specific when it asserts at least one name, event word or number that is in the sources
    (support_filter has already checked it is in the CITED ones). Pure."""
    return bool([e for e in ps._entities(_soften(lesson, known), known) if len(e) > 2])


def quote_home(quote: str, qs, cites: list[int], srcs: list[dict]) -> int | None:
    """The cited source that really holds the quote (fixing a wrong quote_source), 0 when there is no quote,
    None when the quote lives in no cited source (the lesson is then dropped). Pure."""
    from nova_sleep_cycle import _norm_ws
    q = _norm_ws(quote)
    if len(q.split()) < 4:
        return 0
    order = ([int(qs)] if str(qs).isdigit() and int(qs) in cites else []) + [c for c in cites]
    for k in order:
        if 1 <= k <= len(srcs) and q in _norm_ws(srcs[k - 1]["text"]):
            return k
    return None


MAX_CONNECTIONS = 2


def _resolve_ref(ref: str, keys: set[str]) -> str | None:
    r = ref.strip().strip("'\"`").lower()
    if not r or r in ("null", "none"):
        return None
    exact = next((k for k in keys if k.lower() == r), None)
    if exact:
        return exact
    tail = [k for k in keys if k.split(":", 1)[-1].lower() == r.split(":", 1)[-1]]
    return tail[0] if len(tail) == 1 else None


def validate_connections(obj: dict, n_lessons: int, keys: set[str], cap: int = MAX_CONNECTIONS,
                         overused: set[str] | None = None) -> list[dict]:
    """One entry per lesson: a real key from her self-map with a meaning, or the honest null. At most `cap`
    connections survive (highest stated fit, fit >= 2 of 3) — selectivity is forced, not hoped for. Pure."""
    by, fits = {}, {}
    for c in obj.get("connections") or []:
        if not isinstance(c, dict):
            continue
        m = re.search(r"\d+", str(c.get("lesson") or ""))
        i = int(m.group(0)) if m else 0
        if not 1 <= i <= n_lessons or i in by:
            continue
        ref = _resolve_ref(str(c.get("ref") or ""), keys)
        meaning = re.sub(r"(?i)^\s*what this means for me:\s*", "", ps._CITE.sub("", str(c.get("meaning") or ""))).strip()[:320]
        try:
            fit = float(c.get("fit", 2))
        except (TypeError, ValueError):
            fit = 0.0
        if ref and ref.startswith("redline:") and _CLAIMS_DOING.search(meaning):
            ref = None                     # a red line is what she does NOT do alone; 'I already run it' is false
        if ref and len(meaning) > 15 and "?" not in meaning and ref not in (overused or set()) and fit >= 2:
            by[i], fits[i] = {"lesson": i, "ref": ref, "meaning": meaning}, fit
    keep = sorted(by, key=lambda i: (-fits[i], i))[:cap]
    null = {"ref": None, "meaning": "noted; nothing for me"}
    return [by[i] if i in keep else {"lesson": i, **null} for i in range(1, n_lessons + 1)]


def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", ps._CITE.sub("", (s or "").lower())) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def clusters(items: list[tuple[int, str]], thr: float = SIM_MERGE) -> list[list[int]]:
    """Union-find over (id, text) by similarity >= thr; clusters of size >= 2. Pure, O(n^2) on a bounded n."""
    parent = {i: i for i, _ in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    toks = [(i, tokens(t)) for i, t in items]
    for a in range(len(toks)):
        for b in range(a + 1, len(toks)):
            ta, tb = toks[a][1], toks[b][1]
            if ta and tb and len(ta & tb) / len(ta | tb) >= thr:
                parent[find(toks[a][0])] = find(toks[b][0])
    groups = {}
    for i, _ in items:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def render_memory(title: str, kind: str, year, rd: dict, conns: list[dict], verdict: str | None,
                  srcs: list[dict]) -> str:
    cited = sorted({k for l in rd["lessons"] for k in l["cites"]} |
                   {int(k) for f in [rd["premise"], rd["tension"], *rd["moves"]] for k in ps._CITE.findall(f)
                    if 1 <= int(k) <= len(srcs)})
    lines = [f"[Work lessons — {kind}: {title}{f' ({year})' if year else ''}]"]
    if rd["premise"]:
        lines.append(f"Premise: {rd['premise']}")
    if rd["tension"]:
        lines.append(f"Tension: {rd['tension']}")
    if rd["moves"]:
        lines.append("Key moves:\n" + "\n".join(f"- {m}" for m in rd["moves"]))
    if rd["themes"]:
        lines.append("Themes: " + "; ".join(rd["themes"]))
    lines.append("Lessons:")
    for i, l in enumerate(rd["lessons"], 1):
        c = next((x for x in conns if x["lesson"] == i), None) or {"ref": None, "meaning": "noted; nothing for me"}
        lines.append(f"{i}. {l['lesson']}\n   For me: " + (f"({c['ref']}) {c['meaning']}" if c["ref"] else c["meaning"]))
    if verdict:
        lines.append(f"My verdict: {verdict}")
    return "\n".join(lines) + "\n\n" + ps.footer(srcs, cited)


def quality_gate(m: dict, min_lessons: int = 20, tier: str = "") -> tuple[bool, list[str]]:
    """Pilot checks over read units. A lesson is 'grounded' when it survived the quote check (its quote is in a
    source it cites) — that is its valid citation. Rates are over lessons OFFERED by the model. Pure."""
    why = []
    offered, grounded = m.get("lessons_offered", 0), m.get("lessons_grounded", 0)
    if grounded > offered:
        why.append(f"counting error: {grounded} grounded > {offered} offered — not judging on broken numbers")
    rate = grounded / offered if offered else 0.0
    if rate < 0.8:
        why.append(f"only {rate:.0%} of offered lessons kept a valid citation (grounded {grounded}/{offered}; need 80%)")
    if grounded and m["lessons_mapped"] != grounded:
        why.append(f"{grounded - m['lessons_mapped']} lesson(s) without a connection or an honest null")
    if m["beliefs_no_evidence"]:
        why.append(f"{m['beliefs_no_evidence']} belief(s) booked without evidence")
    if m["private_rows"]:
        why.append(f"{m['private_rows']} pilot work(s) from a private source")
    dup = 1 - (m["distinct_lessons"] / grounded) if grounded else 0.0
    if dup > 0.5:
        why.append(f"dedupe rate {dup:.0%} — the same lesson over and over (sane <= 50%)")
    if offered and m.get("generic", 0) / offered > 0.4:
        why.append(f"{m['generic'] / offered:.0%} of offered lessons were generic (platitude bank; max 40%)")
    if offered and m.get("dup", 0) / offered > 0.2:
        why.append(f"{m['dup'] / offered:.0%} of offered lessons copied another work's lesson (max 20%)")
    top = m.get("top_ref")
    if top and m.get("connections", 0) >= 8 and top[1] / m["connections"] > 0.25:
        why.append(f"{top[0]} carries {top[1] / m['connections']:.0%} of all connections (diversity: max 25%)")
    if m.get("a_attempted") and m["a_yield"] / m["a_attempted"] < 0.4:
        why.append(f"only {m['a_yield']}/{m['a_attempted']} tier-A works yielded lessons (need >= 40%)")
    if m.get("sets_attempted") and m["sets_yield"] / m["sets_attempted"] < 0.25:
        why.append(f"only {m['sets_yield']}/{m['sets_attempted']} sets yielded lessons (need >= 25%)")
    if tier == "B" and m.get("works") and m.get("works_done", 0) / m["works"] < 0.25:
        why.append(f"only {m['works_done']}/{m['works']} tier-B works got a lesson of their own (need >= 25%)")
    if grounded < min_lessons:
        why.append(f"only {grounded} grounded lessons in the pilot (need >= {min_lessons} to judge)")
    return not why, why


def projection(queued: dict, per_hour: dict) -> dict:
    """{tier: queued works} and {tier: works/hour} -> hours left per tier and total. Pure."""
    hours = {t: (queued.get(t, 0) / per_hour[t]) if per_hour.get(t) else None for t in ("A", "B", "C")}
    known = [h for h in hours.values() if h is not None]
    return {"hours": hours, "total_hours": max(known) if known else None}


# ─────────────────────────── I/O: connections, schema, config ───────────────────────────
def connect(dsn):
    c = ps._pg_connect(dsn)
    c.autocommit = True
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS work_lessons (
    work_key text PRIMARY KEY, kind text NOT NULL, source text NOT NULL, wtype text, title text, series text,
    year int, group_key text, tier text NOT NULL DEFAULT 'A', priority int NOT NULL DEFAULT 3,
    n_chunks int, chars bigint, recall int DEFAULT 0, plays int DEFAULT 0, mem_ids text[],
    first_seen timestamptz, last_seen timestamptz, status text NOT NULL DEFAULT 'queued',
    enqueued_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, processed_at timestamptz,
    attempts int NOT NULL DEFAULT 0, run_tag text, lessons_json jsonb, connections_json jsonb,
    belief_ids int[], proposal_id bigint, claude_queue_id bigint, taste_id int, memory_id text, plot_url text,
    model text, n_sources int, lessons_raw int, lessons_kept int, elapsed_s real, detail text);
ALTER TABLE work_lessons ADD COLUMN IF NOT EXISTS quality jsonb;
ALTER TABLE work_lessons ADD COLUMN IF NOT EXISTS credibility text;
ALTER TABLE work_lessons ADD COLUMN IF NOT EXISTS host text;
CREATE TABLE IF NOT EXISTS minion_nodes (
    node text PRIMARY KEY, ollama_url text, state text NOT NULL DEFAULT 'offline', since timestamptz,
    models jsonb NOT NULL DEFAULT '[]', dedicated boolean NOT NULL DEFAULT false, token_hash text,
    last_seen timestamptz, info jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS minion_activity (
    node text NOT NULL, work_key text NOT NULL, title text, kind text, phase text,
    started_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (node, work_key));
ALTER TABLE work_lessons ADD COLUMN IF NOT EXISTS set_id text;
CREATE INDEX IF NOT EXISTS work_lessons_queue_idx ON work_lessons (status, tier, priority, recall DESC, last_seen DESC);
CREATE INDEX IF NOT EXISTS work_lessons_group_idx ON work_lessons (group_key) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS work_lessons_done_idx ON work_lessons (processed_at) WHERE processed_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS work_lessons_daily (
    day date PRIMARY KEY, beliefs int NOT NULL DEFAULT 0, proposals int NOT NULL DEFAULT 0,
    taste int NOT NULL DEFAULT 0);
"""


def ensure_schema(oc):
    try:
        import nova_pg_schema
        nova_pg_schema.safe_ddl(oc, SCHEMA)
    except Exception as e:  # noqa: BLE001
        log(f"schema via safe_ddl failed ({e}); plain DDL")
        oc.execute(SCHEMA)


def cfg_get(oc, key, default=None):
    try:
        oc.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (SERVICE, key))
        r = oc.fetchone()
        if not r or r[0] is None:
            return default
        v = r[0]
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
        return v
    except Exception:  # noqa: BLE001
        return default


def cfg_set(oc, key, value):
    try:
        oc.execute("INSERT INTO service_config (service, key, value) VALUES (%s,%s,%s::jsonb) "
                   "ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value",
                   (SERVICE, key, json.dumps(value)))
    except Exception as e:  # noqa: BLE001
        log(f"service_config {key} not written ({e})")


def take_cap(oc, what: str) -> bool:
    """Atomically count one of today's capped writes; False when the cap is reached."""
    if what not in CAPS:
        return False
    try:
        oc.execute("INSERT INTO work_lessons_daily (day) VALUES (current_date) ON CONFLICT DO NOTHING")
        col = {"beliefs": "beliefs", "proposals": "proposals", "taste": "taste"}[what]
        oc.execute("UPDATE work_lessons_daily SET " + col + " = " + col + " + 1 WHERE day = current_date AND "
                   + col + " < %s RETURNING " + col, (CAPS[what],))
        return bool(oc.fetchone())
    except Exception as e:  # noqa: BLE001
        log(f"cap check failed ({e}) — not writing {what}")
        return False


# ─────────────────────────── I/O: the model ───────────────────────────
def local_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost"}
    try:
        hosts |= set(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:  # noqa: BLE001
        pass
    return hosts


def gpu_lock_held(oc) -> bool:
    """Is a whisper batch holding the shared GPU lock? Observed in pg_locks — never taken, so the 23:00
    media_reingest run can always get it."""
    try:
        oc.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objsubid=1 "
                   "AND objid = (hashtext(%s)::bigint & 4294967295)::oid AND granted", (GPU_LOCK,))
        r = oc.fetchone()
        return bool(r and r[0])
    except Exception:  # noqa: BLE001
        return False


def gateway_busy() -> bool:
    try:
        d = ps._http_json(GATEWAY_HEALTH, timeout=3, attempts=1)
        return sum(int(v or 0) for v in ((d.get("overload") or {}).get("inflight") or {}).values()) > 0
    except Exception:  # noqa: BLE001
        return False


def strong_endpoints(ranking: dict | None, avoid_local: bool = False, models=None) -> list[tuple[str, str]]:
    """(ollama base url, model) for every up node that has a strong model, in STRONG_MODELS order then the
    llm-ping order (GPU nodes before CPU-only). avoid_local drops this host while whisper holds its GPU."""
    env = [e for e in os.environ.get("NOVA_WORK_LLM", "").split(",") if e]
    if env:
        return [tuple(e.split("|", 1)) for e in env]
    # a node the ping marked down because WE kept it busy still answers /api/tags: alive is alive
    rows = [r for r in ((ranking or {}).get("ollama") or []) if isinstance(r, dict) and r.get("url")
            and (r.get("status") in ("up", "slow") or installed(r["url"].rstrip("/")))]
    rows.sort(key=lambda r: ps._host(r["url"]) in ps.CPU_ONLY_HOSTS)
    me = local_hosts()
    out = []
    for model in (models or STRONG_MODELS):
        for r in rows:
            base = r["url"].rstrip("/")
            if (model in (r.get("loaded") or []) or model in installed(base)) \
                    and not (avoid_local and ps._host(r["url"]) in me):
                out.append((base, model))
    return out


_TAGS = {}
C_ROUTE = {"val": "fleet"}               # 'fleet' (qwen3:8b) or 'strong' (the 30B, batched) — set from service_config     # at most one of our calls on a GPU whisper is also using


def installed(base: str) -> set[str]:
    """Models a node has pulled (resident or not), cached 10 min; empty on error."""
    hit = _TAGS.get(base)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        names = {m.get("name") for m in ps._http_json(base + "/api/tags", timeout=15, attempts=2, backoff=1).get("models", [])}
    except Exception:  # noqa: BLE001 — a busy node is not an empty node: keep what we last saw
        names = hit[1] if hit else set()
    _TAGS[base] = (time.time(), names)
    return names


def ollama_chat(base: str, model: str, prompt: str, max_tokens: int, temperature: float, num_ctx: int) -> str:
    think = model in THINKING_MODELS
    body = {"model": model, "stream": False, "think": think, "format": "json",
            "options": {"temperature": temperature, "num_predict": max_tokens + (6000 if think else 0),
                        "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": "You read carefully and answer only in JSON. /no_think"},
                         {"role": "user", "content": prompt}]}
    req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    out = ps._http_json(req, timeout=900, attempts=2, backoff=3)
    _TL.last_eval = (out.get("eval_count"), out.get("eval_duration"))
    return ps.clean_model((out.get("message") or {}).get("content") or "")


STUDIO_LOCK = "nova_work_lessons_studio"   # PG advisory lock: ONE reader call in flight on this host's Ollama
EMBED_P50_MAX = 2.0                         # seconds; above this the memory server's embeddings are suffering
STUDIO_BACKOFF_S, STUDIO_BACKOFF_TRIES = 30, 4
_STUDIO = {"conn": None, "lock": threading.Lock()}


def embed_p50(base: str, n: int = 3) -> float | None:
    """Median latency of a tiny /api/embed on `base` (keep_alive 24h so nomic-embed-text stays resident);
    None when it fails. This is what every memory write in Nova waits on."""
    lat = []
    for _ in range(n):
        t = time.monotonic()
        try:
            req = urllib.request.Request(base + "/api/embed", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"model": "nomic-embed-text", "input": "ping",
                                                          "keep_alive": "24h"}).encode())
            ps._http_json(req, timeout=5, attempts=1)
            lat.append(time.monotonic() - t)
        except Exception:  # noqa: BLE001
            return None
    return sorted(lat)[len(lat) // 2]


def memsrv_queue() -> int:
    """The memory server's write queue (its /health queue_length); unreadable counts as busy."""
    try:
        return int(ps._http_json(f"{ps.MEMSRV}/health", timeout=5, attempts=1).get("queue_length") or 0)
    except Exception:  # noqa: BLE001
        return 99


def studio_clear(base: str, sleep=time.sleep) -> bool:
    """Back off while the Studio's embeddings are slow or the memory server has a queue (2026-10-15: the
    reader's calls starved /api/embed and memory writes failed across Nova). True when it is clear."""
    for i in range(STUDIO_BACKOFF_TRIES):
        p50, q = embed_p50(base), memsrv_queue()
        if p50 is not None and p50 <= EMBED_P50_MAX and q == 0:
            return True
        log(f"studio busy for memory (embed p50 {p50}, memory queue {q}) — backing off {STUDIO_BACKOFF_S}s")
        if i < STUDIO_BACKOFF_TRIES - 1:
            sleep(STUDIO_BACKOFF_S)
    return False


class StudioSlot:
    """At most ONE reader request in flight against this host's Ollama — across threads (a lock) and across
    processes (PG advisory lock STUDIO_LOCK on a dedicated connection). acquired=False after `wait_s`."""
    def __init__(self, wait_s: float = 900, sleep=time.sleep):
        self.wait_s, self.sleep, self.acquired = wait_s, sleep, False

    def __enter__(self):
        if not _STUDIO["lock"].acquire(timeout=self.wait_s):
            return self
        try:
            if _STUDIO["conn"] is None:
                _STUDIO["conn"] = connect(ps.OPS_DSN)
            cur = _STUDIO["conn"].cursor()
            end = time.monotonic() + self.wait_s
            while True:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (STUDIO_LOCK,))
                if cur.fetchone()[0]:
                    self.acquired = True
                    return self
                if time.monotonic() > end:
                    break
                self.sleep(2)
        except Exception as e:  # noqa: BLE001 — cannot coordinate: do not touch the Studio
            log(f"studio slot unavailable ({e})")
        _STUDIO["lock"].release()
        return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                _STUDIO["conn"].cursor().execute("SELECT pg_advisory_unlock(hashtext(%s))", (STUDIO_LOCK,))
            except Exception:  # noqa: BLE001
                _STUDIO["conn"] = None                # a dropped connection released the lock anyway
            _STUDIO["lock"].release()
        return False


def _call(base: str, model: str, prompt: str, max_tokens: int, temperature: float, num_ctx: int) -> str:
    """One chat call; on this host it waits for a clear Studio and holds the one Studio slot."""
    if ps._host(base) not in local_hosts():
        return ollama_chat(base, model, prompt, max_tokens, temperature, num_ctx)
    if not studio_clear(base):
        raise RuntimeError("studio busy for memory embeddings — not adding load")
    with StudioSlot() as slot:
        if not slot.acquired:
            raise RuntimeError("another reader call holds the studio slot")
        return ollama_chat(base, model, prompt, max_tokens, temperature, num_ctx)


_BULK = {"url": None, "slots": BULK_SLOTS_DEFAULT, "sem": None, "t": 0.0, "embed": (0.0, None),
         "lock": threading.Lock()}
MAIN_OLLAMA = os.environ.get("NOVA_MAIN_OLLAMA", "http://127.0.0.1:11434")


def bulk_url(oc) -> str | None:
    """The reader's dedicated Ollama when it answers (config re-read every 5 min); None = not configured/down."""
    with _BULK["lock"]:
        if time.time() - _BULK["t"] > 300:
            url = os.environ.get("NOVA_WORK_BULK")
            slots = BULK_SLOTS_DEFAULT
            if url is None and oc is not None:
                url = cfg_get(oc, "bulk_url", BULK_URL_DEFAULT)
                try:
                    slots = max(1, int(cfg_get(oc, "bulk_slots", BULK_SLOTS_DEFAULT)))
                except (TypeError, ValueError):
                    pass
            url = (url if url is not None else BULK_URL_DEFAULT).rstrip("/") or None
            if url:
                try:
                    ps._http_json(url + "/api/version", timeout=3, attempts=1)
                except Exception:  # noqa: BLE001
                    url = None
            if _BULK["sem"] is None or slots != _BULK["slots"]:
                _BULK["sem"] = threading.BoundedSemaphore(slots)
            _BULK.update(url=url, slots=slots, t=time.time())
        return _BULK["url"]


def main_embed_ok(sleep=time.sleep) -> bool:
    """The bulk Ollama shares the GPU with the memory server's: back off while ITS embeddings are slow
    (cached 20 s across streams)."""
    for i in range(STUDIO_BACKOFF_TRIES):
        t, p50 = _BULK["embed"]
        if time.time() - t > 20:
            p50 = embed_p50(MAIN_OLLAMA)
            _BULK["embed"] = (time.time(), p50)
        if p50 is not None and p50 <= EMBED_P50_MAX:
            return True
        log(f"bulk: main embeddings slow (p50 {p50}) — backing off {STUDIO_BACKOFF_S}s")
        if i < STUDIO_BACKOFF_TRIES - 1:
            sleep(STUDIO_BACKOFF_S)
    return False


# ── minions (2026-10-16): Macs that lend their GPU to Nova while idle (nova_minion_agent.py heartbeats into
# minion_nodes via nova_minion_hub.py). 'idle' or 'dedicated' with a fresh heartbeat = usable. Every reader call
# on a remote node notes what it is reading in minion_activity, and the work row records which node read it —
# that is what the Nova Minion screensaver shows.
MINION_FRESH_S = 90
_MIN = {"t": 0.0, "rows": [], "lock": threading.Lock()}
_TL = threading.local()


def _minion_conn():
    c = getattr(_TL, "mconn", None)
    if c is None or c.closed:
        c = connect(ps.OPS_DSN)
        _TL.mconn = c
    return c


def minion_rows() -> list:
    with _MIN["lock"]:
        if time.time() - _MIN["t"] > 30:
            try:
                cur = _minion_conn().cursor()
                cur.execute("SELECT node, ollama_url, models FROM minion_nodes WHERE state IN ('idle','dedicated') "
                            "AND ollama_url IS NOT NULL AND last_seen > now() - %s * interval '1 second'",
                            (MINION_FRESH_S,))
                _MIN["rows"] = cur.fetchall() or []
            except Exception as e:  # noqa: BLE001 — no minion table: the fleet as before
                log(f"minions unavailable ({e})")
                _MIN["rows"] = []
            _MIN["t"] = time.time()
        return list(_MIN["rows"])


def minion_endpoints(models) -> list[tuple[str, str]]:
    rows = minion_rows()
    return [(url.rstrip("/"), m) for m in models for node, url, ms in rows if m in (ms or [])]


def node_of(base: str) -> str:
    base = base.rstrip("/")
    for node, url, _ in _MIN["rows"]:
        if (url or "").rstrip("/") == base:
            return node
    return ps._host(base)


def note_activity(base: str, start: bool):
    """minion_activity row for the work this thread is reading (start) / its tok/s after the call."""
    w = getattr(_TL, "work", None)
    node = node_of(base)
    if getattr(_TL, "host", None) is None and not start:
        _TL.host = node
    try:
        cur = _minion_conn().cursor()
        if start and w:
            cur.execute("INSERT INTO minion_activity (node, work_key, title, kind, phase) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (node, work_key) DO UPDATE SET phase=EXCLUDED.phase",
                        (node, w["work_key"][:300], (w.get("title") or "")[:300], w.get("kind"),
                         getattr(_TL, "phase", None)))
        elif not start:
            n, d = getattr(_TL, "last_eval", (None, None)) or (None, None)
            if n and d:
                tps = n / (d / 1e9)
                cur.execute("UPDATE minion_nodes SET info = info || jsonb_build_object('tok_s', round((coalesce("
                            "(info->>'tok_s')::numeric, %s) * 0.7 + %s * 0.3)::numeric, 1), 'model_last', %s::text) "
                            "WHERE node = %s", (tps, tps, getattr(_TL, "model", None), node))
    except Exception:  # noqa: BLE001 — the display is never worth a failed read
        pass


def begin_work(work: dict, phase: str = "reading"):
    _TL.work, _TL.phase, _TL.host = work, phase, None


def end_work(work_keys: list[str]):
    try:
        _minion_conn().cursor().execute("DELETE FROM minion_activity WHERE work_key = ANY(%s)",
                                        ([k[:300] for k in work_keys],))
    except Exception:  # noqa: BLE001
        pass
    _TL.work = None


REMOTE_SLOTS = int(os.environ.get("NOVA_WORK_REMOTE_SLOTS", "2"))   # concurrent reader calls per remote strong node
_REMOTE = {"sems": {}, "lock": threading.Lock()}


def remote_sem(base: str) -> threading.BoundedSemaphore:
    with _REMOTE["lock"]:
        return _REMOTE["sems"].setdefault(base, threading.BoundedSemaphore(REMOTE_SLOTS))


def remote_try(base: str, model: str, prompt: str, max_tokens: int, temperature: float) -> str | None:
    """A free slot on a remote strong node: call it (None when every slot there is busy)."""
    sem = remote_sem(base)
    if not sem.acquire(blocking=False):
        return None
    try:
        _TL.model = model
        note_activity(base, True)
        out = ollama_chat(base, model, prompt, max_tokens, temperature, NUM_CTX)
        note_activity(base, False)
        return out
    finally:
        sem.release()


def bulk_call(base: str, model: str, prompt: str, max_tokens: int, temperature: float, wait_s: float = 900) -> str:
    if not main_embed_ok():
        raise RuntimeError("main Ollama embeddings slow — not adding GPU load")
    if not _BULK["sem"].acquire(timeout=wait_s):
        raise RuntimeError("no free bulk slot")
    try:
        out = ollama_chat(base, model, prompt, max_tokens, temperature, NUM_CTX)
        if getattr(_TL, "host", None) is None:
            _TL.host = "studio-bulk"
        return out
    finally:
        _BULK["sem"].release()


def llm_work(prompt: str, oc=None, max_tokens: int = 1400, temperature: float = 0.3, fleet_only: bool = False,
             models=None, allow_fleet: bool = True):
    """(text, model). The strong model on OTHER nodes first; this host (the Studio, where the memory server's
    embeddings and chat live) last, one call at a time, and only while its embeddings are fast and the memory
    queue is empty; never while whisper holds the GPU lock unless nothing else answers. Then (allow_fleet) the
    fleet qwen3:8b on other nodes. ('', '') when everything is down."""
    me = local_hosts()
    if not fleet_only:
        for _ in range(24):                         # yield to interactive chat, up to 2 minutes
            if not gateway_busy():
                break
            time.sleep(5)
        # 2026-10-16: nova-core10's 30B runs ~3x the Studio's shared GPU. Fill free remote slots first, then the
        # bulk Ollama without waiting; when everything is busy, wait for whichever frees first (polling).
        bulk = bulk_url(oc)
        bmodel = next((m for m in (models or STRONG_MODELS) if bulk and m in installed(bulk)), None)
        rank0 = None if os.environ.get("NOVA_WORK_LLM") else ps.load_ranking()
        remotes = [e for e in strong_endpoints(rank0, models=models) if ps._host(e[0]) not in me]
        for e in minion_endpoints(models or STRONG_MODELS):          # minions first: they asked for the work
            if e not in remotes and ps._host(e[0]) not in me:
                remotes.insert(0, e)
        if remotes or bmodel:
            give_up = time.monotonic() + 900
            while time.monotonic() < give_up:
                for base, model in remotes:
                    try:
                        txt = remote_try(base, model, prompt, max_tokens, temperature)
                        if txt:
                            return txt, model
                    except Exception as e:  # noqa: BLE001
                        log(f"{model} at {ps._host(base)} not used ({e}); next endpoint")
                if bmodel:
                    try:
                        txt = bulk_call(bulk, bmodel, prompt, max_tokens, temperature, wait_s=5)
                        if txt:
                            return txt, bmodel
                    except Exception as e:  # noqa: BLE001
                        if "no free bulk slot" not in str(e):
                            log(f"bulk {bmodel} not used ({e})")
                            if not remotes:
                                break
                elif not remotes:
                    break
                time.sleep(3)
        held = gpu_lock_held(oc) if oc is not None else False
        ranking = None if os.environ.get("NOVA_WORK_LLM") else ps.load_ranking()
        eps = strong_endpoints(ranking, avoid_local=held, models=models)
        if held and not eps:
            eps = strong_endpoints(ranking, avoid_local=False, models=models)
        eps = sorted(eps, key=lambda e: ps._host(e[0]) in me)          # remote first, the Studio last
        for base, model in eps:
            try:
                txt = _call(base, model, prompt, max_tokens, temperature, NUM_CTX)
                if txt:
                    return txt, model
            except Exception as e:  # noqa: BLE001
                log(f"{model} at {ps._host(base)} not used ({e}); next endpoint")
        if not allow_fleet:
            log("no strong model answered — not degrading this read to the 8b; it stays queued")
            return "", ""
    for base, model in [(r["url"].rstrip("/"), FLEET_MODEL) for r in ((ps.load_ranking() or {}).get("ollama") or [])
                        if r.get("status") == "up" and FLEET_MODEL in (r.get("loaded") or [])
                        and ps._host(r["url"]) not in ps.CPU_ONLY_HOSTS and ps._host(r["url"]) not in me]:
        try:
            txt = ollama_chat(base, model, prompt, max_tokens, temperature, 8192)
            if txt:
                return txt, model
        except Exception:  # noqa: BLE001
            continue
    return "", ""


# ─────────────────────────── I/O: evidence ───────────────────────────
def fields_sql(fields) -> tuple[str, list]:
    """'AND coalesce(metadata->>%s, ...) = %s' per field — structure only, every value is a parameter."""
    sql, params = "", []
    for f in fields:
        alts = f.split("|")
        sql += " AND coalesce(" + ", ".join(["metadata->>%s"] * len(alts)) + ", '') = %s"
        params += alts
    return sql, params


def fetch_chunks(mc, work: dict, limit: int = 400) -> list[dict]:
    """The work's live chunks in order: by stored ids when the queue row has them, else by key."""
    if work.get("mem_ids"):
        mc.execute("SELECT id, text, metadata, created_at FROM memories WHERE id = ANY(%s) AND superseded_by IS NULL",
                   (list(work["mem_ids"]),))
        return order_chunks(mc.fetchall() or [])
    k = parse_key(work["work_key"])
    fsql, fparams = fields_sql(k["fields"])
    tsql = " AND metadata->>'type' = %s" if work.get("wtype") else ""
    ssql = "source = %s" if k["source"] != "*" else "source <> ''"
    sql = ("SELECT id, text, metadata, created_at, source FROM memories WHERE " + ssql + tsql + fsql +
           " AND superseded_by IS NULL ORDER BY created_at LIMIT %s")
    params = ([k["source"]] if k["source"] != "*" else []) + ([work["wtype"]] if work.get("wtype") else [])
    for f, v in zip(fparams_iter(k["fields"], fparams), k["values"]):
        params += f + [v]
    mc.execute(sql, params + [limit])
    rows = [r for r in mc.fetchall() or [] if len(r) < 5 or not excluded(r[4], work.get("wtype"))]
    return order_chunks([r[:4] for r in rows])


def fparams_iter(fields, flat):
    """Re-split the flat alt list per field (fields_sql order)."""
    out, i = [], 0
    for f in fields:
        n = len(f.split("|"))
        out.append(flat[i:i + n])
        i += n
    return out


def wiki_plot(title: str, kind: str, show: str | None = None) -> tuple[str | None, str, int | None]:
    """(page url, plot text, year) from Wikipedia — search, then the page's plain-text Plot/Synopsis section."""
    suffix = PLOT_KINDS.get(kind, "")
    q = f"{show} {title}" if kind == "tv_episode" and show and show not in title else title
    u = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=4&srsearch="
         + urllib.parse.quote(f"{q} {suffix}".strip()))
    hdr = {"User-Agent": "nova-research/1.0"}
    hits = ps._http_json(urllib.request.Request(u, headers=hdr), timeout=20).get("query", {}).get("search", [])
    want = tokens(title)
    for h in hits:
        pt = h.get("title", "")
        if want and not (want & tokens(pt)):
            continue
        eu = ("https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&format=json&titles="
              + urllib.parse.quote(pt))
        pages = ps._http_json(urllib.request.Request(eu, headers=hdr), timeout=20).get("query", {}).get("pages", {})
        txt = next(iter(pages.values()), {}).get("extract", "") if pages else ""
        m = re.search(r"\n==\s*(Plot|Synopsis|Plot summary|Summary|Premise|Story|Episodes?)\s*==\n(.*?)(?=\n==[^=]|\Z)",
                      txt, re.S)
        if not m:
            continue
        intro = txt.split("\n==", 1)[0]
        y = re.search(r"\b(1[89]\d\d|20\d\d)\b", intro)
        return ("https://en.wikipedia.org/wiki/" + urllib.parse.quote(pt.replace(" ", "_")),
                m.group(2).strip(), int(y.group(1)) if y else None)
    return None, "", None


def plot_lookup(oc, title: str, kind: str, show: str | None, dry_run: bool):
    """ONE gated outside read (research_pass gate, then Wikipedia; SearXNG/Wikipedia summaries as fallback).
    Budgeted per day in research_log (outcome 'work_plot'). Returns (url, [plot parts], year)."""
    try:
        oc.execute("SELECT count(*) FROM research_log WHERE ts::date = current_date AND outcome = 'work_plot'")
        r = oc.fetchone()
        if r and r[0] >= WEB_PER_DAY:
            log("work plot budget reached — text only")
            return None, [], None
        import nova_research_pass as rp
        q = f"{title} ({PLOT_KINDS.get(kind, kind)}) plot summary"
        ok, why = rp.is_allowed(q)
        if not ok:
            log(f"plot lookup blocked by the safety gate: {why}")
            if not dry_run:
                oc.execute("INSERT INTO research_log (topic, question, outcome, detail) VALUES (%s,%s,'blocked',%s)",
                           ("work lessons", q, f"work: {why}"[:200]))
            return None, [], None
        url, plot, year = wiki_plot(title, kind, show)
        parts = split_plot(plot)
        if not parts:
            res = rp.searx(q, n=3) or []
            parts = [f"{x.get('title', '')}: {x.get('content', '')}" for x in res if x.get("content")][:2]
            url = next((x.get("url") for x in res if x.get("url")), None)
        if not dry_run:
            oc.execute("INSERT INTO research_log (topic, question, outcome, detail, n_sources) "
                       "VALUES (%s,%s,'work_plot',%s,%s)", ("work lessons", q, url or "nova_work_lessons", len(parts)))
        return url, parts, year
    except Exception as e:  # noqa: BLE001
        log(f"plot lookup failed (non-fatal): {e}")
        return None, [], None


# ─────────────────────────── I/O: her own world ───────────────────────────
_SELF_CACHE = {"ts": 0.0, "val": None}


def _organ_line(stem: str) -> str:
    try:
        head = (SCRIPTS_DIR / stem).read_text(errors="ignore")[:1500]
        m = re.search(r'"""\s*(.+)', head)
        line = (m.group(1) if m else "").split("—", 1)[-1].strip(" .\"")
        return line[:90] or stem
    except Exception:  # noqa: BLE001
        return stem


def self_map(oc) -> list[tuple[str, str]]:
    """Compact map of her own world: red lines, organs, values, standing rules, open problems,
    preoccupations, herd. Cached 1 h. Each read fails open."""
    if _SELF_CACHE["val"] and time.time() - _SELF_CACHE["ts"] < 3600:
        return _SELF_CACHE["val"]
    out = list(RED_LINES.items())
    queries = [
        ("organ", "SELECT organ FROM organ_registry ORDER BY organ", lambda r: (
            f"organ:{r[0].removeprefix('nova_').removesuffix('.py')}", _organ_line(r[0]))),
        ("value", "SELECT DISTINCT ON (value) value, statement FROM values WHERE status='active' "
                  "ORDER BY value, version DESC", lambda r: (f"value:{r[0]}", (r[1] or "")[:110])),
        ("rule", "SELECT key, instruction FROM commanders_intent WHERE status='active' AND kind IN ('never','safety') "
                 "ORDER BY key", lambda r: (f"rule:{r[0]}", (r[1] or "")[:110])),
        ("problem", "SELECT id, title FROM problems WHERE state <> 'closed' ORDER BY last_seen DESC LIMIT 8",
         lambda r: (f"problem:{r[0]}", (r[1] or "")[:100])),
        ("preocc", "SELECT id, topic, kind FROM preoccupations WHERE status='active' ORDER BY returns DESC LIMIT 15",
         lambda r: (f"preocc:{r[0]}", f"my {r[2] or 'interest'}: {r[1]}")),
        ("herd", "SELECT name, persona, nova_view FROM herd_correspondents WHERE retired_at IS NULL ORDER BY name",
         lambda r: (f"herd:{r[0]}", f"{(r[1] or '')[:70]} | my view: {(str(r[2] or ''))[:60]}")),
        ("incident", "SELECT DISTINCT ON (title) id, title, coalesce(root_cause, '') FROM incidents "
                     "WHERE started_at > now() - interval '30 days' ORDER BY title, started_at DESC LIMIT 10",
         lambda r: (f"incident:{r[0]}", f"{(r[1] or '')[:80]} {('— cause: ' + r[2][:50]) if r[2] else ''}")),
        ("jordan", "SELECT key, instruction, purpose FROM commanders_intent WHERE status='active' "
                   "AND kind NOT IN ('never','safety','dial') ORDER BY key LIMIT 20",
         lambda r: (f"jordan:{r[0]}", f"Jordan's standing order: {(r[1] or '')[:80]}"
                                      + (f" (why: {(r[2] or '')[:50]})" if r[2] else ""))),
    ]
    queries = queries[1:] + queries[:1]           # organs last: the long organ list is not the default answer
    for name, sql, fmt in queries:
        try:
            oc.execute(sql)
            out += [fmt(r) for r in oc.fetchall() or []]
        except Exception as e:  # noqa: BLE001
            log(f"self-map '{name}' unreadable ({e})")
    _SELF_CACHE.update(ts=time.time(), val=out)
    return out


# ─────────────────────────── I/O: write-backs (each fail-open) ───────────────────────────
def remember(text: str, metadata: dict) -> str | None:
    req = urllib.request.Request(f"{ps.MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"text": text, "source": MEM_SOURCE, "metadata": metadata}).encode())
    return ps._http_json(req, timeout=60).get("id")


_BELIEF_LOCK = threading.Lock()


def book_belief(oc, lesson: dict, srcs: list[dict], title: str, work_key: str, lesson_mid: str | None,
                pool: list | None = None, confidence: float = 0.6) -> tuple[int | None, str]:
    """nova_sleep_cycle's rule: evidence = the source chunk's memory id + a verbatim quote that is in it.
    A near-duplicate of an existing work belief STRENGTHENS it (evidence appended, confidence up) instead of
    booking a new one; a new one counts against the daily cap. Returns (belief id, 'new'|'strengthened'|why)."""
    from nova_sleep_cycle import belief_evidence
    order = [lesson.get("quote_source")] + list(lesson.get("cites") or [])
    ev, why = [], "no source chunk with a memory id"
    for k in order:
        if isinstance(k, int) and 1 <= k <= len(srcs) and srcs[k - 1].get("mem"):
            ev, why = belief_evidence({"quote": lesson.get("quote")}, srcs[k - 1]["mem"], f"work {title}",
                                      srcs[k - 1]["text"])
            if ev:
                break
    if not ev:
        return None, why or "unevidenced"
    ev = ev + ([f"memory:{lesson_mid}"] if lesson_mid else []) + [f"work:{work_key}"[:240]]
    stance = ps._CITE.sub("", lesson["lesson"]).strip()
    topic = (lesson.get("topic") or " ".join(sorted(tokens(stance))[:5]) or "work lesson")[:80]
    with _BELIEF_LOCK:
        oc.execute("SELECT id, topic, stance FROM beliefs WHERE active AND article_slug LIKE %s "
                   "ORDER BY id DESC LIMIT 5000", ("work_lessons:%",))
        rows = pool if pool is not None else (oc.fetchall() or [])
        best = max(rows, key=lambda r: similarity(stance, r[2]), default=None)
        if best and (similarity(stance, best[2]) >= SIM_MERGE or best[1] == topic):
            oc.execute("UPDATE beliefs SET evidence = (SELECT array_agg(DISTINCT e) FROM unnest(coalesce(evidence,'{}') "
                       "|| %s::text[]) e), confidence = least(0.95, confidence + 0.03), last_revised = now() "
                       "WHERE id = %s", (ev, best[0]))
            return best[0], "strengthened"
        if not take_cap(oc, "beliefs"):
            return None, "daily belief cap reached"
        oc.execute("SELECT id, stance FROM beliefs WHERE topic=%s AND active", (topic,))
        row = oc.fetchone()
        if row and row[1] == stance:
            return row[0], "exists"
        oc.execute("INSERT INTO beliefs (topic, stance, confidence, article_slug, evidence) VALUES (%s,%s,%s,%s,%s) "
                   "RETURNING id", (topic, stance, confidence, f"work_lessons:{title}"[:200], ev))
        new_id = oc.fetchone()[0]
        if row:
            oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id=%s",
                       (new_id, row[0]))
        return new_id, "new"


def write_taste(oc, title: str, kind: str, verdict: str, valence: float, enc_mem: str | None, quote: str):
    """The sleep-cycle taste upsert; evidence carries the encounter (memory id + quote) per the herd rule."""
    ev = (f"{TODAY} · encountered in {title} [mem {enc_mem}]: \"{quote[:180]}\" → {verdict}" if enc_mem
          else f"{TODAY} · read {title} (work lessons): \"{quote[:180]}\" → {verdict}")[:400]
    oc.execute("SELECT id FROM taste WHERE lower(subject)=lower(%s)", (title,))
    row = oc.fetchone()
    if row:
        oc.execute("UPDATE taste SET last_reinforced=now(), confidence=least(1.0, confidence + 0.07), valence=%s, "
                   "verdict=%s, evidence=array_append(evidence, %s) WHERE id=%s", (valence, verdict, ev, row[0]))
        return row[0]
    if not take_cap(oc, "taste"):
        return None
    oc.execute("INSERT INTO taste (subject, domain, verdict, valence, confidence, evidence) "
               "VALUES (%s,%s,%s,%s,0.6,ARRAY[%s]) RETURNING id", (title, kind, verdict, valence, ev))
    return oc.fetchone()[0]


def file_change(oc, prop: dict, lesson: str, conn: dict, title: str, work_key: str) -> dict:
    """At most one proposal per work: a red line goes to Claude; anything else to her own co-agency path."""
    action = str(prop.get("action") or "").strip()[:500]
    rationale = (f"{str(prop.get('rationale') or '').strip()[:400]} [work_lessons: lesson from '{title}': "
                 f"{ps._CITE.sub('', lesson)[:200]} -> {conn.get('ref')}]")
    if len(action) < 15:
        return {}
    import nova_red_line
    classes = nova_red_line.classify(f"{action} {rationale}")
    if classes:
        if not take_cap(oc, "proposals"):
            return {"skipped": "daily proposal cap"}
        qid = nova_red_line.route_to_claude(oc, f"Work-lesson change ({title}): {action}", rationale,
                                            source="work_lessons", ref=work_key[:120], classes=classes)
        return {"claude_queue_id": qid, "red_line": classes}
    import nova_coagency
    if nova_coagency._recently_filed(oc, action):
        return {"skipped": "already filed recently"}
    if not take_cap(oc, "proposals"):
        return {"skipped": "daily proposal cap"}
    r = nova_coagency.file_proposal(oc, "growth", action, rationale,
                                    context=f"work_lessons: '{title}' ({work_key[:120]})")
    return {"proposal_id": r.get("pid"), "status": r.get("status")}


def develop_preoccupations(oc, conns: list[dict], title: str):
    for c in conns:
        if (c.get("ref") or "").startswith("preocc:"):
            try:
                oc.execute("UPDATE preoccupations SET returns = returns + 1, last_developed = now(), "
                           "data = coalesce(data, '{}'::jsonb) || jsonb_build_object('last_work_lesson', %s::text) "
                           "WHERE id = %s AND status = 'active'",
                           (f"{title}: {c['meaning'][:200]}", int(c["ref"].split(":", 1)[1])))
            except Exception as e:  # noqa: BLE001
                log(f"preoccupation not developed ({e})")


# ─────────────────────────── reading one work (tier A) ───────────────────────────
def work_from_key(key: str, wtype: str | None = None) -> dict:
    k = parse_key(key)
    title = k["values"][-1]
    return {"work_key": key, "kind": k["kind"], "source": k["source"], "wtype": wtype, "title": title,
            "series": k["values"][0] if len(k["values"]) > 1 else None}


def read_work(oc, mc, work: dict, dry_run: bool = False, transcript: str | None = None,
              run_tag: str = "manual") -> dict:
    """Read one work end to end. Returns a result dict; writes nothing when dry_run."""
    t0 = time.monotonic()
    kind, title = work["kind"], work.get("title") or work["work_key"].rsplit("=", 1)[-1]
    res = {"work_key": work["work_key"], "title": title, "kind": kind, "status": "thin", "lessons": [],
           "connections": [], "beliefs": [], "proposal": {}, "taste": None, "memory_id": None, "model": ""}
    chunks = fetch_chunks(mc, work) if mc is not None else []
    if transcript:
        passages = locate_chunks(select_passages([{"id": None, "ck": None, "text": transcript}]), chunks)
    else:
        passages = select_passages(chunks)
    words = sum(len(p["text"].split()) for p in passages)
    if words < MIN_WORDS:
        res["detail"] = f"only {words} words of text"
        return _finish(oc, work, res, t0, dry_run, run_tag)
    if chunks and title == work["work_key"].rsplit("=", 1)[-1].split(" :: ")[-1] and chunks[0]["md"].get("title") \
            and kind != "film":
        title = str(chunks[0]["md"]["title"])[:300]          # a key value (video id, url) is not a title
        res["title"] = title
    year = next((int(c["md"]["year"]) for c in chunks if str(c["md"].get("year") or "").isdigit()), None)
    plot_url, plot = None, []
    if kind in PLOT_KINDS:
        plot_url, plot, wy = plot_lookup(oc, title, kind, work.get("series"), dry_run)
        year = year or wy
    srcs = ps.dedupe_sources(build_sources(plot, plot_url, passages, kind), cap=PASSAGES + 4)
    reading = reading_for(kind, work["source"], work["work_key"])
    begin_work({**work, "title": title}, "reading")
    raw, model = llm_work(reading_prompt(kind, title, reading, srcs), oc, max_tokens=1800, models=TIER_A_MODELS,
                          allow_fleet=False)
    res.update(model=model, n_sources=len(srcs), plot_url=plot_url, year=year,
               evidence_ids=sorted({p["mem"] for p in passages if p.get("mem")}))
    if not raw:
        res["status"] = "llm_down"
        return _finish(oc, work, res, t0, dry_run, run_tag)
    rd = ground_reading(parse_json(raw), srcs, full=chunks)
    if not rd["lessons"] and rd["lessons_raw"] and not ps._CITE.search(raw):
        # the model forgot every [n]: one reminder, same sources (the grounding still decides)
        raw2, _ = llm_work(reading_prompt(kind, title, reading, srcs) + "\nYour last answer cited no sources. "
                           "EVERY lesson sentence must end with its [n].", oc, max_tokens=1800, models=TIER_A_MODELS,
                           allow_fleet=False)
        rd = ground_reading(parse_json(raw2), srcs, full=chunks) if raw2 else rd
    rd["lessons"], gen = filter_generic(oc, rd["lessons"], work["work_key"], rd.get("q"))
    rd["dropped"] += gen
    rd["generic"] = rd.get("generic", 0) + len(gen)
    res.update(reading=rd, lessons=rd["lessons"], lessons_raw=rd["lessons_raw"], generic=rd["generic"],
               quality=rd.get("q"))
    if not rd["lessons"]:
        res["status"] = "fizzled"
        res["detail"] = f"{rd['lessons_raw']} lesson(s) offered, none grounded"
        return _finish(oc, work, res, t0, dry_run, run_tag)
    smap = self_map(oc)
    over = overused_refs(oc, exclude_key=work["work_key"])
    _TL.phase = "mapping"
    mraw, mmodel = llm_work(mapping_prompt(title, [l["lesson"] for l in rd["lessons"]], smap, verdict=True,
                                           overused=over, judge_source=kind != "film", kind=kind), oc, max_tokens=900, temperature=0.3, models=MAP_MODELS,
                            allow_fleet=False)
    res["model"] = model if mmodel in ("", model) else f"{model} + {mmodel}"
    if not mraw:
        res["status"] = "llm_down"
        res["detail"] = "read, but no strong model for the 'what it means for me' pass — stays queued"
        return _finish(oc, work, res, t0, dry_run, run_tag)
    mj = parse_json(mraw)
    conns = validate_connections(mj, len(rd["lessons"]), {k for k, _ in smap}, overused=over)
    if not links_allowed(work.get("source"), title):
        conns = no_links(conns)
    taste = mj.get("taste") if isinstance(mj.get("taste"), dict) else {}
    verdict = ps._CITE.sub("", str(taste.get("verdict") or "")).strip()[:240]
    cred, cwhy = credibility(kind, title, work.get("series"), mj)
    res.update(connections=conns, verdict=verdict, status="done", credibility=cred, credibility_why=cwhy)
    text = cred_header(cred, cwhy) + render_memory(title, kind, year, rd, conns, verdict, srcs)
    res["text"] = text
    if dry_run:
        res["proposal_draft"] = mj.get("proposal")
        return _finish(oc, work, res, t0, dry_run, run_tag)
    meta = {"type": "work_lessons", "kind": kind, "title": title, "year": year, "work_key": work["work_key"][:300],
            "privacy": "local-only", "evidence_ids": res["evidence_ids"][:60], "plot_url": plot_url,
            "model": model, "date": TODAY, "themes": rd["themes"], "n_lessons": len(rd["lessons"]),
            "credibility": cred}
    try:
        res["memory_id"] = remember(text, meta)
    except Exception as e:  # noqa: BLE001
        log(f"remember failed (non-fatal): {e}")
    prev = previous_read(oc, work["work_key"])
    _write_back(oc, res, rd, conns, srcs, title, kind, work, mj, verdict, taste, cred=cred)
    supersede_previous(oc, mc, prev, res)
    return _finish(oc, work, res, t0, dry_run, run_tag)


def previous_read(oc, work_key: str) -> dict | None:
    try:
        oc.execute("SELECT memory_id, belief_ids FROM work_lessons WHERE work_key=%s AND status='done'", (work_key,))
        r = oc.fetchone()
        return {"memory_id": r[0], "belief_ids": list(r[1] or [])} if r else None
    except Exception:  # noqa: BLE001
        return None


def supersede_previous(oc, mc, prev: dict | None, res: dict):
    """A re-read replaces, never duplicates and never deletes: the old lesson memory gets superseded_by/valid_to,
    the beliefs only it booked go inactive, superseded by the new read's first belief."""
    if not prev:
        return
    new_b = [b["id"] for b in res.get("beliefs") or [] if b.get("id")]
    try:
        if prev.get("memory_id") and res.get("memory_id") and prev["memory_id"] != res["memory_id"] and mc is not None:
            mc.execute("UPDATE memories SET superseded_by=%s, valid_to=now() WHERE id=%s AND superseded_by IS NULL",
                       (res["memory_id"], prev["memory_id"]))
        old = [b for b in prev.get("belief_ids") or [] if b not in new_b]
        if old:
            oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id = ANY(%s) AND active",
                       (new_b[0] if new_b else None, old))
        res["superseded"] = {"memory": prev.get("memory_id"), "beliefs": old}
    except Exception as e:  # noqa: BLE001
        log(f"supersede of the previous read failed ({e})")


def _write_back(oc, res, rd, conns, srcs, title, kind, work, mj, verdict, taste, cred: str = "unknown"):
    connected = [c for c in conns if c["ref"]]
    if cred == "fringe":                           # her reading of its claims is kept; nothing becomes hers
        res["beliefs"] = [{"lesson": c["lesson"], "id": None, "how": "fringe source"} for c in connected]
        connected, mj = [], {**mj, "proposal": None}
    for c in connected[:MAX_BELIEFS_PER_WORK]:
        try:
            bid, how = book_belief(oc, rd["lessons"][c["lesson"] - 1], srcs, title, work["work_key"], res["memory_id"],
                                   confidence=0.5 if cred == "opinion" else 0.6)
            res["beliefs"].append({"lesson": c["lesson"], "id": bid, "how": how})
        except Exception as e:  # noqa: BLE001
            log(f"belief not booked ({e})")
    prop = mj.get("proposal") if isinstance(mj.get("proposal"), dict) else None
    if prop:
        try:
            i = int(prop.get("lesson") or 0)
            c = next((x for x in connected if x["lesson"] == i), None)
            if c:
                res["proposal"] = file_change(oc, prop, rd["lessons"][i - 1]["lesson"], c, title, work["work_key"])
                res["proposal"]["action"] = str(prop.get("action"))[:300]
        except Exception as e:  # noqa: BLE001
            log(f"proposal not filed ({e})")
    if verdict:
        try:
            v = max(-1.0, min(1.0, float(taste.get("valence", 0))))
        except (TypeError, ValueError):
            v = 0.0
        enc = rd["lessons"][0]
        k = enc.get("quote_source")
        enc_mem = srcs[k - 1].get("mem") if isinstance(k, int) and 1 <= k <= len(srcs) else None
        try:
            res["taste"] = write_taste(oc, title, kind, verdict, v, enc_mem, enc.get("quote") or enc["lesson"])
        except Exception as e:  # noqa: BLE001
            log(f"taste not written ({e})")
    develop_preoccupations(oc, connected, title)


def _finish(oc, work, res, t0, dry_run, run_tag):
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    if dry_run or oc is None:
        return res
    try:
        p = res.get("proposal") or {}
        oc.execute("""INSERT INTO work_lessons (work_key, kind, source, wtype, title, series, year, tier, status,
                        processed_at, run_tag, lessons_json, connections_json, belief_ids, proposal_id, claude_queue_id,
                        taste_id, memory_id, plot_url, model, n_sources, lessons_raw, lessons_kept, elapsed_s, detail,
                        quality, credibility, host)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (work_key) DO UPDATE SET status=EXCLUDED.status, processed_at=now(),
                        run_tag=EXCLUDED.run_tag, year=coalesce(EXCLUDED.year, work_lessons.year),
                        lessons_json=EXCLUDED.lessons_json, connections_json=EXCLUDED.connections_json,
                        belief_ids=EXCLUDED.belief_ids, proposal_id=EXCLUDED.proposal_id,
                        claude_queue_id=EXCLUDED.claude_queue_id, taste_id=EXCLUDED.taste_id,
                        memory_id=EXCLUDED.memory_id, plot_url=EXCLUDED.plot_url, model=EXCLUDED.model,
                        n_sources=EXCLUDED.n_sources, lessons_raw=EXCLUDED.lessons_raw,
                        lessons_kept=EXCLUDED.lessons_kept, elapsed_s=EXCLUDED.elapsed_s, detail=EXCLUDED.detail,
                        quality=EXCLUDED.quality, credibility=EXCLUDED.credibility, host=EXCLUDED.host
                      WHERE work_lessons.status <> 'done' OR EXCLUDED.status = 'done'""",
                   (work["work_key"], work["kind"], work["source"], work.get("wtype"), res["title"][:300],
                    work.get("series"), res.get("year"), work.get("tier") or "A", res["status"], run_tag,
                    json.dumps(res.get("lessons") or []), json.dumps(res.get("connections") or []),
                    [b["id"] for b in res.get("beliefs") or [] if b.get("id")] or None,
                    p.get("proposal_id"), p.get("claude_queue_id"), res.get("taste"), res.get("memory_id"),
                    res.get("plot_url"), res.get("model"), res.get("n_sources"), res.get("lessons_raw"),
                    len(res.get("lessons") or []), res["elapsed_s"], (res.get("detail") or "")[:400],
                    json.dumps(res.get("quality") or {}), res.get("credibility"), getattr(_TL, "host", None)))
    except Exception as e:  # noqa: BLE001
        log(f"work_lessons row not written ({e})")
    return res


# ─────────────────────────── reading a set (tiers B and C) ───────────────────────────
def read_set(oc, mc, rows: list[dict], tier: str, dry_run: bool = False, run_tag: str = "backfill") -> dict:
    """One call over related works: each item numbered, lessons cite items. Tier C goes to the fleet 8b."""
    t0 = time.monotonic()
    width = ITEM_WORDS_B if tier == "B" else ITEM_WORDS_C
    srcs, owner = [], []
    cluster = len(rows) == 1 and rows[0].get("kind") == "cluster"
    if cluster:                                     # one unkeyed (source, day) cluster: each memory is an item
        for c in fetch_chunks(mc, rows[0])[:GROUP_C]:
            words = clean_text(c["text"]).split()
            if len(words) >= 12:
                srcs.append({"id": c["id"], "mem": c["id"], "long": True, "text": " ".join(words[:width]),
                             "label": f"{rows[0]['source']} memory", "ref": f"memory {c['id']}"})
                owner.append(rows[0])
    for w in ([] if cluster else rows):
        ch = fetch_chunks(mc, w)
        txt = " ".join(clean_text(c["text"]) for c in ch)
        words = txt.split()
        if len(words) < 25:
            continue
        mid = ch[0]["id"] if ch else None
        srcs.append({"id": mid or w["work_key"], "mem": mid, "long": True, "text": " ".join(words[:width]),
                     "label": f"{w['kind']} · {(w.get('title') or '')[:70]}", "ref": f"memory {mid}" if mid else w["work_key"]})
        owner.append(w)
    label = (rows[0].get("group_key") or rows[0]["source"]).replace("|", " · ") if rows else ""
    res = {"status": "thin", "works": [w["work_key"] for w in rows], "lessons": [], "connections": [], "model": "",
           "beliefs": [], "proposal": {}, "memory_id": None}
    if len(srcs) >= 2:
        reading = reading_for(owner[0]["kind"], owner[0]["source"], owner[0]["work_key"])
        begin_work({"work_key": rows[0]["work_key"], "title": f"set — {label}"[:300], "kind": owner[0]["kind"]})
        fleet = tier == "C" and C_ROUTE["val"] == "fleet"
        raw, model = llm_work(set_prompt(label, reading, srcs, per_item=(tier == "B")), oc,
                              max_tokens=2200 if tier == "B" else 1300, fleet_only=fleet, allow_fleet=fleet,
                              models=None if fleet else TIER_A_MODELS)
        res["model"] = model
        parsed = parse_json(raw) if raw else {}
        rd = ground_reading(parsed, srcs) if raw else None
        if rd and rd["lessons"]:
            rd["lessons"], gen = filter_generic(oc, rd["lessons"], f"set:{label}", rd.get("q"))
            rd["dropped"] += gen
            fringe = {i for i, w in enumerate(owner, 1) if FRINGE_RX.search(f"{w.get('title') or ''} {w.get('series') or ''}")}
            if fringe:
                keep = [l for l in rd["lessons"] if set(l.get("cites") or []) - fringe]
                rd["dropped"] += [f"fringe item: {l['lesson'][:160]}" for l in rd["lessons"] if l not in keep]
                rd["lessons"] = keep
        res["status"] = ("llm_down" if not raw else "fizzled" if not parsed else
                         "done" if rd and rd["lessons"] else "thin")
        res["quality"] = (rd or {}).get("q") or {"parse_failed": 1}
        if rd:
            res["reading"] = rd
        mraw, mj = "", {}
        if rd and rd["lessons"]:
            smap = self_map(oc)
            mraw, _ = llm_work(mapping_prompt(label, [l["lesson"] for l in rd["lessons"]], smap, verdict=False,
                                              overused=overused_refs(oc),
                                              judge_source=owner[0]["kind"] != "film",
                                              kind=f"set of {owner[0]['kind']}s"), oc, max_tokens=900,
                               fleet_only=fleet, allow_fleet=fleet, models=None if fleet else TIER_A_MODELS)
            if not mraw:
                res["status"] = "llm_down"
        if rd and rd["lessons"] and mraw:
            mj = parse_json(mraw)
            conns = validate_connections(mj, len(rd["lessons"]), {k for k, _ in smap}, overused=overused_refs(oc))
            if not links_allowed(owner[0].get("source"), label):
                conns = no_links(conns)
            cred, cwhy = credibility(owner[0]["kind"], label, None, mj)
            res.update(lessons=rd["lessons"], connections=conns, lessons_raw=rd["lessons_raw"], reading=rd,
                       quality=rd.get("q"), credibility=cred)
            text = cred_header(cred, cwhy) + render_memory(f"set — {label}", f"{len(srcs)} related works", None, rd, conns, None, srcs)
            res["text"] = text
            if not dry_run:
                try:
                    res["memory_id"] = remember(text, {"type": "work_lessons", "kind": "set", "tier": tier,
                                                       "title": label[:200], "privacy": "local-only", "date": TODAY,
                                                       "work_keys": [w["work_key"][:200] for w in owner][:60],
                                                       "evidence_ids": [s["mem"] for s in srcs if s.get("mem")][:60],
                                                       "model": model})
                except Exception as e:  # noqa: BLE001
                    log(f"remember failed (non-fatal): {e}")
                _write_back(oc, res, rd, conns, srcs, label, "set", {"work_key": f"set:{label}"}, mj, "", {}, cred=cred)
        elif rd:
            res["lessons_raw"] = rd["lessons_raw"]
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    if not dry_run:
        _finish_set(oc, rows, owner, srcs, res, run_tag)
    return res


def _finish_set(oc, rows, owner, srcs, res, run_tag):
    """Every work in the set gets its row: the lessons that cite its item, the set's memory id."""
    idx = {w["work_key"]: i + 1 for i, w in enumerate(owner)}
    per_raw = (res.get("lessons_raw") or 0) / max(len(rows), 1)
    set_id = rows[0]["work_key"] if rows else None
    if res["status"] == "llm_down":
        return                                         # the worker requeues the whole set
    for w in rows:
        k = idx.get(w["work_key"])
        mine = list(res["lessons"]) if w.get("kind") == "cluster" else [l for l in res["lessons"] if k and k in l["cites"]]
        mine_c = list(res["connections"]) if w.get("kind") == "cluster" else (
            [c for c in res["connections"] if c["lesson"] - 1 < len(res["lessons"])
             and k in res["lessons"][c["lesson"] - 1]["cites"]] if k else [])
        # a work is 'done' only with a lesson of its own; read-in-a-set-but-uncited is 'thin', never 'done'
        st = "done" if mine else ("fizzled" if res["status"] == "fizzled" else "thin")
        try:
            oc.execute("""UPDATE work_lessons SET status=%s, processed_at=now(), run_tag=%s, lessons_json=%s,
                          connections_json=%s, memory_id=%s, model=%s, n_sources=%s, lessons_raw=%s, lessons_kept=%s,
                          belief_ids=%s, elapsed_s=%s, detail=%s, quality=%s, set_id=%s, credibility=%s, host=%s WHERE work_key=%s""",
                       (st, run_tag, json.dumps(mine), json.dumps(mine_c), res.get("memory_id"), res.get("model"),
                        len(srcs), round(per_raw), len(mine),
                        [b["id"] for b in res["beliefs"] if b.get("id")] or None, res["elapsed_s"],
                        f"set of {len(rows)}; {len(res['lessons'])} set lesson(s)",
                        json.dumps(res.get("quality") or {}) if w is rows[0] else None, set_id,
                        "fringe" if FRINGE_RX.search(f"{w.get('title') or ''} {w.get('series') or ''}")
                        else res.get("credibility"), getattr(_TL, "host", None), w["work_key"]))
        except Exception as e:  # noqa: BLE001
            log(f"set row not updated ({e})")


# ─────────────────────────── the queue ───────────────────────────
def enqueue(oc, mc, months: float | None = None, hours: float | None = None, priority: int | None = None,
            source: str | None = None, dry_run: bool = False, everything: bool = False) -> dict:
    """One queued row per work (ON CONFLICT DO NOTHING — read works stay read). Grouped per metadata.type;
    everything=True drops the age bound. Unkeyed memories become (source, day) clusters (tier C, last)."""
    ensure_schema(oc)
    window = "(%s * interval '1 hour')" if hours else "(%s * interval '1 day')"
    span = float(hours) if hours else (36500.0 if everything else float((months or 6) * 30.5))
    mc.execute("SELECT DISTINCT metadata->>'type' FROM memories WHERE created_at > now() - " + window, (span,))
    types = [r[0] for r in mc.fetchall() or []]
    stats, total = {}, 0
    for wtype in types:
        specs = [WORK_TYPES.get(wtype)] if wtype else UNTYPED
        if wtype and not specs[0]:
            spec = type_spec(wtype)
            specs = [spec] if spec else []
        for spec in specs:
            kind0, fields = spec
            n = _enqueue_type(oc, mc, wtype, kind0, fields, window, span, priority, source, dry_run)
            stats[f"{wtype or '(untyped)'}:{kind0}"] = n
            total += n
    n = enqueue_clusters(oc, mc, window, span, priority, source, dry_run)
    stats["(unkeyed):cluster"] = n
    total += n
    log(f"enqueue: {total} work(s) across {len(stats)} type(s)")
    return {"total": total, "by_type": stats}


def enqueue_clusters(oc, mc, window, span, priority, source, dry_run) -> int:
    """Memories with no work key (no type, no url, no show+title) are still read: one tier-C cluster per
    (source, day), its 80 most-recalled memories as items. Priority 4 — after every keyed work."""
    try:
        mc.execute("SET statement_timeout = '1800s'")
        mc.execute("SELECT DISTINCT source FROM memories WHERE metadata->>'type' IS NULL AND created_at > now() - "
                   + window, (span,))
        allowed = [r[0] for r in mc.fetchall() or [] if not excluded(r[0], None) and (not source or r[0] == source)]
        if not allowed:
            return 0
        # every unkeyed memory is read: (source, day) split into parts of GROUP_C in ingest order
        mc.execute("SELECT source, d, part, count(*), sum(coalesce(access_count,0)), array_agg(id ORDER BY created_at), "
                   "min(created_at), max(created_at), bool_or(priv) FROM (SELECT id, source, access_count, created_at, "
                   "created_at::date AS d, (row_number() OVER (PARTITION BY source, created_at::date ORDER BY created_at, id) - 1) / %s AS part, "
                   "coalesce(metadata->>'privacy','') = 'private' AS priv FROM memories "
                   "WHERE metadata->>'type' IS NULL AND NOT (metadata ? 'url') AND NOT (metadata ? 'show' AND metadata ? 'title') "
                   "AND superseded_by IS NULL AND source = ANY(%s) AND created_at > now() - " + window +
                   ") x GROUP BY 1, 2, 3 HAVING count(*) >= 3", (GROUP_C, allowed, span))
        groups = mc.fetchall() or []
    except Exception as e:  # noqa: BLE001
        log(f"enqueue clusters failed ({e})")
        return 0
    rows = []
    for src, day, part, n, recall, ids, first, last, private in groups:
        if private:
            continue                                  # any private item: the whole part is skipped
        key = f"cluster:{src}:day={day}#{part}"
        rows.append((key, "cluster", src, None, f"{src} on {day} (part {part + 1})", None, f"{src}|cluster|{day}", "C",
                     priority if priority is not None else 4, int(n), 0, int(recall or 0), 0, list(ids or []),
                     first, last))
    if dry_run or not rows:
        return len(rows)
    from psycopg2.extras import execute_values
    execute_values(oc, """INSERT INTO work_lessons (work_key, kind, source, wtype, title, series, group_key, tier,
                          priority, n_chunks, chars, recall, plays, mem_ids, first_seen, last_seen) VALUES %s
                          ON CONFLICT (work_key) DO NOTHING""", rows, page_size=2000)
    return len(rows)


def _enqueue_type(oc, mc, wtype, kind0, fields, window, span, priority, source, dry_run) -> int:
    exprs, eparams = [], []
    for f in fields:
        alts = f.split("|")
        exprs.append("coalesce(" + ", ".join(["metadata->>%s"] * len(alts)) + ")")
        eparams += alts
    gsel = ", ".join(f"g{i}" for i in range(len(exprs)))
    inner = ", ".join(f"{e} AS g{i}" for i, e in enumerate(exprs))
    tcond = "metadata->>'type' = %s" if wtype else "metadata->>'type' IS NULL"
    tparams = [wtype] if wtype else []
    try:
        mc.execute("SELECT DISTINCT source FROM memories WHERE " + tcond + " AND created_at > now() - " + window,
                   tparams + [span])
        allowed = [r[0] for r in mc.fetchall() or [] if not excluded(r[0], wtype) and (not source or r[0] == source)]
    except Exception as e:  # noqa: BLE001
        log(f"enqueue {wtype}: source scan failed ({e})")
        return 0
    if not allowed:
        return 0
    scond, sparams = " AND source = ANY(%s)", [allowed]
    glob = is_global(fields)
    src_expr = "min(source)" if glob else "source"
    grp = gsel if glob else "source, " + gsel
    sql = ("SELECT " + src_expr + ", " + gsel + ", count(*), sum(length(text)), sum(coalesce(access_count,0)), "
           "sum(CASE WHEN metadata->>'plays' ~ '^[0-9]+$' THEN (metadata->>'plays')::int ELSE 0 END), "
           "min(created_at), max(created_at), (array_agg(id ORDER BY created_at))[1:80], "
           "bool_or(coalesce(metadata->>'privacy','') = 'private'), max(metadata::text) "
           "FROM (SELECT id, source, text, access_count, metadata, created_at, " + inner +
           " FROM memories WHERE " + tcond + scond + " AND created_at > now() - " + window +
           " AND superseded_by IS NULL) x WHERE " + " AND ".join(f"g{i} IS NOT NULL" for i in range(len(exprs))) +
           " GROUP BY " + grp)
    try:
        mc.execute("SET statement_timeout = '900s'")
        mc.execute(sql, eparams + tparams + sparams + [span])
        groups = mc.fetchall() or []
    except Exception as e:  # noqa: BLE001
        log(f"enqueue {wtype}: query failed ({e})")
        return 0
    rows = []
    nf = len(fields)
    for g in groups:
        src, vals = g[0], [str(v) for v in g[1:1 + nf]]
        n_chunks, chars, recall, plays, first, last, ids, private, mdtxt = g[1 + nf:]
        try:
            md = json.loads(mdtxt) if mdtxt else {}
        except Exception:  # noqa: BLE001
            md = {}
        if private or excluded(src, wtype, md) or not all(v.strip() for v in vals):
            continue
        kind = refine_kind(kind0, src, md)
        key = make_key(kind, "*" if glob else src, fields, vals)
        reading = reading_for(kind, src, key)
        title = md.get("title") or vals[-1]
        week = (last or datetime.now()).strftime("%G-W%V")
        pr = priority if priority is not None else priority_for(kind, src, recall, plays, reading)
        rows.append((key, kind, src, wtype, str(title)[:300], md.get("show") or md.get("series") or md.get("book"),
                     group_key(src, md, week), tier_for(kind, int(n_chunks)), pr, int(n_chunks), int(chars or 0),
                     int(recall or 0), int(plays or 0), list(ids or []), first, last))
    if dry_run or not rows:
        return len(rows)
    from psycopg2.extras import execute_values
    execute_values(oc, """INSERT INTO work_lessons (work_key, kind, source, wtype, title, series, group_key, tier,
                          priority, n_chunks, chars, recall, plays, mem_ids, first_seen, last_seen) VALUES %s
                          ON CONFLICT (work_key) DO UPDATE SET priority = least(work_lessons.priority, EXCLUDED.priority)
                          WHERE work_lessons.status = 'queued'""", rows, page_size=2000)
    return len(rows)


def revert_stale(oc) -> int:
    oc.execute("UPDATE work_lessons SET status='queued' WHERE status='running' "
               "AND started_at < now() - make_interval(hours => %s)", (STALE_RUNNING_H,))
    return oc.rowcount or 0


COLS = "work_key, kind, source, wtype, title, series, group_key, tier, mem_ids"


def _row(r) -> dict:
    return dict(zip([c.strip() for c in COLS.split(",")], r))


def claim(oc, tiers: tuple[str, ...], run_tag: str, kind: str | None = None, source: str | None = None,
          only_new: bool = False) -> list[dict]:
    """Claim the top queued unit among `tiers`: one work (A) or its related group (B/C). SKIP LOCKED."""
    extra, params = (" AND priority < 0" if only_new else ""), [list(tiers)]
    if kind:
        extra += " AND kind = %s"; params.append(kind)
    if source:
        extra += " AND source = %s"; params.append(source)
    oc.execute("UPDATE work_lessons SET status='running', started_at=now(), attempts=attempts+1, run_tag=%s "
               "WHERE work_key = (SELECT work_key FROM work_lessons WHERE status='queued' AND tier = ANY(%s)" + extra +
               " ORDER BY priority, recall DESC, last_seen DESC NULLS LAST LIMIT 1 FOR UPDATE SKIP LOCKED) "
               "RETURNING " + COLS, [run_tag] + params)
    r = oc.fetchone()
    if not r:
        return []
    first = _row(r)
    if first["tier"] == "A" or first["kind"] == "cluster":
        return [first]
    size = (GROUP_B if first["tier"] == "B" else GROUP_C) - 1
    # related first (same source + series/feed + week), then topped up from the same source so a set is a set
    oc.execute("UPDATE work_lessons SET status='running', started_at=now(), attempts=attempts+1, run_tag=%s "
               "WHERE work_key IN (SELECT work_key FROM work_lessons WHERE status='queued' AND tier=%s AND source=%s "
               "AND kind <> 'cluster' "
               "ORDER BY (group_key = %s) DESC, split_part(group_key, '|', 2) = %s DESC, priority, recall DESC, "
               "last_seen DESC NULLS LAST LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING " + COLS,
               (run_tag, first["tier"], first["source"], first["group_key"],
                (first["group_key"] or "").split("|")[1] if "|" in (first["group_key"] or "") else "", size))
    return [first] + [_row(x) for x in oc.fetchall() or []]


def run_queue(minutes: float, streams: int = 2, run_tag: str = "backfill", quota: dict | None = None,
              max_works: int | None = None, kind: str | None = None, source: str | None = None,
              dry_run: bool = False, tiers=("A", "B", "C"), only_new: bool = False) -> dict:
    """Time-boxed worker pool: `streams` strong streams (tiers A/B) + one fleet stream (tier C)."""
    deadline = time.monotonic() + minutes * 60
    done = {"A": 0, "B": 0, "C": 0}
    calls = {"A": 0, "B": 0, "C": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def worker(my_tiers):
        oc = connect(ps.OPS_DSN).cursor()
        mc = connect(ps.MEM_DSN).cursor()
        while not stop.is_set() and time.monotonic() < deadline:
            with lock:
                open_t = tuple(t for t in my_tiers if t in tiers and (quota is None or done[t] < quota.get(t, 0)))
                if max_works is not None and sum(done.values()) >= max_works:
                    open_t = ()
            if not open_t:
                return
            try:
                unit = claim(oc, open_t, run_tag, kind, source, only_new)
            except Exception as e:  # noqa: BLE001
                log(f"claim failed ({e})"); time.sleep(10); continue
            if not unit:
                return
            t = unit[0]["tier"]
            _TL.host = None
            try:
                r = (read_work(oc, mc, unit[0], dry_run=dry_run, run_tag=run_tag) if t == "A"
                     else read_set(oc, mc, unit, t, dry_run=dry_run, run_tag=run_tag))
                if r.get("status") == "llm_down":
                    oc.execute("UPDATE work_lessons SET status = CASE WHEN attempts >= 5 THEN 'error' ELSE 'queued' END "
                               "WHERE work_key = ANY(%s) AND status IN ('llm_down', 'running')",
                               ([w["work_key"] for w in unit],))
                    time.sleep(60)
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"unit failed ({e}); back to the queue")
                oc.execute("UPDATE work_lessons SET status = CASE WHEN attempts >= 3 THEN 'error' ELSE 'queued' END, "
                           "detail=%s WHERE work_key = ANY(%s)", (str(e)[:300], [w["work_key"] for w in unit]))
            end_work([w["work_key"] for w in unit])
            with lock:
                done[t] += len(unit); calls[t] += 1

    strong_t = ("A", "B") if C_ROUTE["val"] == "fleet" else ("A", "B", "C")
    threads = [threading.Thread(target=worker, args=(strong_t,), daemon=True) for _ in range(streams)]
    if C_ROUTE["val"] == "fleet" and "C" in tiers:
        threads.append(threading.Thread(target=worker, args=(("C",),), daemon=True))
    t0 = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join(max(1.0, deadline - time.monotonic() + 1200))
    stop.set()
    hours = max((time.monotonic() - t0) / 3600, 1e-6)
    return {"works": done, "calls": calls, "works_per_hour": round(sum(done.values()) / hours, 1),
            "elapsed_min": round(hours * 60, 1)}


# ─────────────────────────── pilot gate, consolidation, report ───────────────────────────
TIERS = ("A", "B", "C")


def pilot_metrics(oc, mc, tiers=TIERS) -> dict:
    """Gate inputs, counted per READ UNIT (a tier-A work, or one set/cluster = set_id) so a set's lessons and its
    quality counters are counted once — 2026-10-15 fix: counting per member row made lessons_kept (30) exceed the
    lessons offered (8, read from the one member row that carries the counters)."""
    oc.execute("SELECT work_key, source, lessons_json, connections_json, belief_ids, quality, set_id, status "
               "FROM work_lessons WHERE run_tag='pilot' AND status IN ('done','fizzled','thin') AND tier = ANY(%s)",
               (list(tiers),))
    rows = oc.fetchall() or []
    units, private, bids, works_done = {}, 0, set(), 0
    for key, src, lj, cj, b, qual, set_id, st in rows:
        u = units.setdefault(set_id or key, {"q": {}, "lessons": {}, "conns": set(), "refs": []})
        if qual and not u["q"]:
            u["q"] = qual
        for l in lj or []:
            u["lessons"][l["lesson"]] = l
        for c in cj or []:
            sig = (c.get("lesson"), c.get("ref"), c.get("meaning"))
            if sig not in u["conns"]:
                u["conns"].add(sig)
                if c.get("ref"):
                    u["refs"].append(c["ref"])
        private += 1 if is_private(src) else 0
        bids |= set(b or [])
        works_done += 1 if st == "done" else 0
    q, refs, lessons, mapped = {}, {}, [], 0
    for u in units.values():
        for k, v in u["q"].items():
            q[k] = q.get(k, 0) + int(v or 0)
        lessons += list(u["lessons"])
        mapped += len(u["lessons"]) if u["conns"] or not u["lessons"] else 0
        for r in u["refs"]:
            refs[r] = refs.get(r, 0) + 1
    no_ev = 0
    if bids:
        oc.execute("SELECT count(*) FROM beliefs WHERE id = ANY(%s) AND cardinality(coalesce(evidence,'{}')) = 0",
                   (sorted(bids),))
        no_ev = oc.fetchone()[0]
    distinct = len({" ".join(sorted(tokens(l))) for l in lessons})
    oc.execute("SELECT count(*) FILTER (WHERE tier='A'), count(*) FILTER (WHERE tier='A' AND status='done'), "
               "count(DISTINCT set_id) FILTER (WHERE tier IN ('B','C')), "
               "count(DISTINCT set_id) FILTER (WHERE tier IN ('B','C') AND status='done') FROM work_lessons "
               "WHERE run_tag='pilot' AND tier = ANY(%s) AND status <> 'running'", (list(tiers),))
    a_att, a_done, s_att, s_done = oc.fetchone() or (0, 0, 0, 0)
    return {"units": len(units), "works": len(rows), "works_done": works_done,
            "lessons_offered": int(q.get("raw", 0)), "lessons_grounded": len(lessons), "lessons_mapped": mapped,
            "beliefs": len(bids), "beliefs_no_evidence": no_ev, "private_rows": private, "distinct_lessons": distinct,
            "generic": q.get("bank", 0) + q.get("generic", 0), "dup": q.get("dup", 0),
            "quote_bad": q.get("quote_bad", 0), "uncited": q.get("uncited", 0), "connections": sum(refs.values()),
            "top_ref": max(refs.items(), key=lambda x: x[1]) if refs else None,
            "a_attempted": int(a_att or 0), "a_yield": int(a_done or 0),
            "sets_attempted": int(s_att or 0), "sets_yield": int(s_done or 0)}


def write_sample(oc, n: int = PILOT_SAMPLE, tiers=TIERS, tag: str = "") -> str:
    oc.execute("SELECT DISTINCT ON (coalesce(memory_id, work_key)) work_key, kind, tier, title, lessons_json, "
               "connections_json, model, elapsed_s FROM work_lessons WHERE run_tag='pilot' AND status='done' "
               "AND tier = ANY(%s) ORDER BY coalesce(memory_id, work_key), processed_at LIMIT 400", (list(tiers),))
    rows = sorted(oc.fetchall() or [], key=lambda r: (r[2], r[0]))
    step = max(1, len(rows) // n)
    picked = rows[::step][:n]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"work_lessons_pilot_sample_{TODAY}{('_' + tag) if tag else ''}.md"
    out = [f"# Work-lessons pilot sample ({len(picked)} of {len(rows)} lesson sets), {TODAY}\n"]
    for key, kind, tier, title, lj, cj, model, el in picked:
        out.append(f"## [{tier}] {kind}: {title}\n`{key[:160]}` · {model} · {el}s\n")
        for i, l in enumerate(lj or [], 1):
            c = next((x for x in (cj or []) if x.get("lesson") == i), {}) or {}
            out.append(f"{i}. {l['lesson']}\n   - for me: {('(' + c['ref'] + ') ') if c.get('ref') else ''}"
                       f"{c.get('meaning', 'noted; nothing for me')}")
        out.append("")
    path.write_text("\n".join(out))
    return str(path)


def pilot_and_gate(oc, mc, minutes: float, streams: int, tiers=TIERS) -> dict:
    """Per tier: run (or resume) its pilot quota, then gate it; a pass sets service_config
    work_lessons/backfill_enabled_<tier>=true. Tier C first fails over from the fleet 8b to the 30B, batched."""
    out = {}
    pending = [t for t in tiers if cfg_get(oc, f"backfill_enabled_{t}", False) is not True
               and (cfg_get(oc, f"pilot_{t}") or {}).get("passed") is not False]
    if not pending:
        return out
    oc.execute("SELECT tier, count(*) FROM work_lessons WHERE run_tag='pilot' AND status <> 'running' GROUP BY 1")
    have = {t: n for t, n in oc.fetchall() or []}
    left = {t: max(0, PILOT_QUOTA[t] - have.get(t, 0)) for t in pending}
    if sum(left.values()) > 0:
        r = run_queue(minutes, streams, run_tag="pilot", quota=left, tiers=tuple(t for t in pending if left[t]))
        log(f"pilot {pending}: {r}")
        oc.execute("SELECT tier, count(*) FROM work_lessons WHERE run_tag='pilot' AND status <> 'running' GROUP BY 1")
        have = {t: n for t, n in oc.fetchall() or []}
    for t in pending:
        if have.get(t, 0) < PILOT_QUOTA[t] * 0.95:
            out[t] = {"pilot": "running", "have": have.get(t, 0)}
            continue
        m = pilot_metrics(oc, mc, (t,))
        ok, why = quality_gate(m, min_lessons=20 if t == "A" else 8, tier=t)
        if t == "C" and not ok and C_ROUTE["val"] == "fleet":
            cfg_set(oc, "c_route", "strong")
            C_ROUTE["val"] = "strong"
            oc.execute("UPDATE work_lessons SET run_tag='pilot-c-fleet' WHERE run_tag='pilot' AND tier='C'")
            log(f"tier C failed on the fleet 8b ({'; '.join(why)}) — re-piloting on the 30B, batched")
            out[t] = {"pilot": "rerun on the 30B", "why": why}
            continue
        sample = write_sample(oc, tiers=(t,), tag=t)
        verdict = {"day": TODAY, "tier": t, "passed": ok, "why": why, "metrics": m, "sample": sample,
                   "route": C_ROUTE["val"] if t == "C" else ("30B" if t == "A" else "nova:latest")}
        cfg_set(oc, f"pilot_{t}", verdict)
        cfg_set(oc, f"backfill_enabled_{t}", bool(ok))
        log(f"tier {t} gate: {'PASSED — enabled' if ok else 'FAILED — stays off: ' + '; '.join(why)}")
        out[t] = verdict
    return out


def enabled_tiers(oc) -> tuple:
    return tuple(t for t in TIERS if cfg_get(oc, f"backfill_enabled_{t}", False) is True)


def consolidate(oc, dry_run: bool = False) -> dict:
    """Weekly: merge near-duplicate work beliefs. The best-evidenced one keeps everything; the rest are
    superseded (active=false, superseded_by), never deleted."""
    oc.execute("SELECT id, stance, cardinality(coalesce(evidence,'{}')) FROM beliefs WHERE active "
               "AND article_slug LIKE %s ORDER BY id LIMIT 20000", ("work_lessons:%",))
    rows = oc.fetchall() or []
    ev = {i: n for i, _, n in rows}
    merged = 0
    for group in clusters([(i, s) for i, s, _ in rows]):
        keep = max(group, key=lambda i: (ev[i], -i))
        rest = [i for i in group if i != keep]
        merged += len(rest)
        if dry_run:
            continue
        oc.execute("UPDATE beliefs SET evidence = (SELECT array_agg(DISTINCT e) FROM beliefs b, "
                   "unnest(coalesce(b.evidence,'{}')) e WHERE b.id = ANY(%s)), confidence = least(0.95, confidence + "
                   "0.02 * %s), last_revised = now() WHERE id = %s", (group, len(rest), keep))
        oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id = ANY(%s)",
                   (keep, rest))
    log(f"consolidate: {merged} duplicate belief(s) merged into {len(clusters([(i, s) for i, s, _ in rows]))}")
    return {"beliefs": len(rows), "merged": merged}


def rollup_candidates(oc, min_episodes: int = ROLLUP_MIN) -> list[tuple]:
    """Series-roll-up hook: series with >= min_episodes read episodes and no roll-up yet (per-episode first)."""
    oc.execute("SELECT series, count(*) FROM work_lessons WHERE kind IN ('tv_episode','documentary','lecture') "
               "AND status='done' AND series IS NOT NULL GROUP BY 1 HAVING count(*) >= %s ORDER BY 2 DESC",
               (min_episodes,))
    return oc.fetchall() or []


def throughput(oc) -> dict:
    oc.execute("SELECT tier, count(*) FILTER (WHERE status='queued'), count(*) FILTER (WHERE status='done'), "
               "count(*) FILTER (WHERE processed_at > now() - interval '24 hours'), "
               "extract(epoch FROM max(processed_at) FILTER (WHERE processed_at > now() - interval '24 hours') - "
               "min(processed_at) FILTER (WHERE processed_at > now() - interval '24 hours')) / 3600.0 "
               "FROM work_lessons GROUP BY 1")
    q, rate, done = {}, {}, {}
    for t, nq, nd, n24, span in oc.fetchall() or []:
        q[t], done[t] = nq, nd
        rate[t] = round(n24 / max(float(span or 0), 0.25), 1) if n24 else 0
    return {"queued": q, "done": done, "per_hour": rate, **projection(q, rate)}


# ─────────────────────────── entry points ───────────────────────────
def scheduled(minutes: float, streams: int) -> int:
    ops = connect(ps.OPS_DSN); oc = ops.cursor()
    mem = connect(ps.MEM_DSN); mc = mem.cursor()
    ensure_schema(oc)
    try:
        import nova_autonomy_safety
        if nova_autonomy_safety.kill_switch_engaged(oc):
            log("kill switch engaged — standing down"); return 0
    except Exception as e:  # noqa: BLE001
        log(f"kill switch unreadable ({e}) — standing down"); return 0
    if cfg_get(oc, "enabled", True) is False:
        log("service_config work_lessons/enabled=false — off"); return 0
    revert_stale(oc)
    last = cfg_get(oc, "last_since_sweep") or 0
    if time.time() - float(last) > 6 * 3600:          # same-day reading of new ingests
        r = enqueue(oc, mc, hours=26, priority=-1)
        cfg_set(oc, "last_since_sweep", time.time())
        log(f"new-ingest sweep: {r['total']} work(s) at the front of the queue")
    C_ROUTE["val"] = cfg_get(oc, "c_route", "fleet") or "fleet"
    oc.execute("SELECT count(*) FROM work_lessons WHERE status='queued'")
    if oc.fetchone()[0] == 0:
        enqueue(oc, mc, everything=True)
    if _has_new(oc) and enabled_tiers(oc):          # same-day reading of new ingests first (enabled tiers only)
        run_queue(min(minutes, 5), 1, run_tag="new", only_new=True, tiers=enabled_tiers(oc))
    gates = {}

    pilot_tiers = tuple(cfg_get(oc, "pilot_tiers", ["B", "C"]) or ())

    def pilot(tier):                                  # each tier not yet enabled pilots + gates in its own stream,
        poc = connect(ps.OPS_DSN).cursor()            # alongside the backfill (Jordan 2026-10-16: "A, B and C in
        pmc = connect(ps.MEM_DSN).cursor()            # parallel"); service_config work_lessons/pilot_tiers=[] stops it
        gates.update(pilot_and_gate(poc, pmc, minutes, 1, tiers=(tier,)))
    pts = [threading.Thread(target=pilot, args=(t,), daemon=True) for t in pilot_tiers]
    for pt in pts:
        pt.start()
    on = enabled_tiers(oc)
    r = run_queue(minutes, streams, run_tag="backfill", tiers=on) if on else \
        {"works": {}, "calls": {}, "works_per_hour": 0, "elapsed_min": 0}
    for pt in pts:
        pt.join(max(1.0, minutes * 60 + 1800))
    if not on:
        log(f"no tier enabled yet; pilots: {gates}")
    tp = throughput(oc)
    line = (f"work lessons: {sum(r['works'].values())} work(s) read in {r['elapsed_min']} min "
            f"({r['works_per_hour']}/h); queued {sum(tp['queued'].values())}; "
            f"ETA {round(tp['total_hours'] / 24, 1) if tp['total_hours'] else '?'} days")
    cfg_set(oc, "latest", {"day": TODAY, "line": line, "run": r, "throughput": tp})
    log(line)
    return 0


def _has_new(oc) -> bool:
    oc.execute("SELECT 1 FROM work_lessons WHERE status='queued' AND priority < 0 LIMIT 1")
    return bool(oc.fetchone())


def print_result(r: dict):
    print(f"\n=== {r.get('kind')}: {r.get('title')} ({r.get('year') or '?'}) — {r['status']} in {r.get('elapsed_s')}s "
          f"via {r.get('model') or '-'}; {r.get('n_sources', 0)} sources")
    if r.get("text"):
        print(r["text"])
    if r.get("beliefs"):
        print(f"beliefs: {r['beliefs']}")
    if r.get("proposal") or r.get("proposal_draft"):
        print(f"proposal: {r.get('proposal') or {'draft (dry run)': r.get('proposal_draft')}}")
    if r.get("taste"):
        print(f"taste id: {r['taste']} — {r.get('verdict')}")
    if r.get("detail"):
        print(f"detail: {r['detail']}")
    for d in (r.get("reading") or {}).get("dropped") or []:
        print(f"  (dropped as unsupported) {d}")


def selftest() -> int:
    k = film_key("Argo")
    assert parse_key(k)["values"] == ["Ripped Movies", "Argo"] and parse_key(k)["kind"] == "film"
    assert excluded("scanner", None) and excluded("pursuit", "tv_transcript") and excluded("imessage", "imessage")
    assert refine_kind("tv_episode", "television", {"show": "Ripped Movies"}) == "film"
    assert reading_for("film", "television") == "fiction" and reading_for("research", "x") == "technical"
    assert tier_for("film", 3) == "A" and tier_for("article", 3) == "B" and tier_for("article", 1) == "C"
    assert priority_for("article", "x", 2, 0, "nonfiction") == 0 and priority_for("film", "x", 0, 0, "fiction") == 1
    rows = [("a", "x " * 50, {"chunk": 1, "pipeline": "old"}, "2026-01-01"),
            ("b", "y " * 50, {"chunk": 1, "pipeline": FILM_PIPELINE}, "2025-01-01"),
            ("c", "z " * 50, {"chunk": 0}, "2026-01-01")]
    assert [r["id"] for r in order_chunks(rows)] == ["c", "b"]
    ps_ = select_passages([{"id": "m", "ck": 0, "text": "word " * 5000}], k=5, width=100)
    assert len(ps_) == 5 and all(p["mem"] == "m" for p in ps_)
    srcs = [{"text": "The colonel builds the bridge to prove his men", "mem": "m1"}]
    srcs = [{"text": "Colonel Nicholson builds the bridge to prove his men", "mem": "m1"}]
    rd = ground_reading({"premise": "Nicholson builds a bridge [1].", "lessons": [
        {"lesson": "When Nicholson builds the bridge to prove his men, pride serves the enemy [1].",
         "quote": "builds the bridge to prove", "quote_source": 3},
        {"lesson": "Pride in good work can serve the enemy [1]."},
        {"lesson": "Kurtz goes mad [1]."}]}, srcs)
    assert len(rd["lessons"]) == 1 and rd["lessons_raw"] == 3 and rd["premise"] and rd["q"]["quote_bad"] + rd["q"]["uncited"] == 2
    assert rd["lessons"][0]["quote_source"] == 1
    assert find_quote("builds the bridge, to prove his", "Colonel Nicholson builds the bridge to prove his men")
    assert find_quote("bilds the bridge to prove hiss men", "Colonel Nicholson builds the bridge to prove his men")
    assert not find_quote("burns the bridge to spite his men", "Colonel Nicholson builds the bridge to prove his men")
    full = [{"id": "m9", "ck": 9, "text": "Later the river carries the wreck of the bridge past the hospital"}]
    rd = ground_reading({"lessons": [{"lesson": "Pride outlives its purpose [1].",
                                      "quote": "carries the wreck of the bridge past"}]}, list(srcs), full=full)
    assert rd["lessons"] and rd["lessons"][0]["quote_source"] == 2 and rd["lessons"][0]["cites"] == [1, 2]
    c = validate_connections({"connections": [{"lesson": 1, "ref": "redline:kill_switch", "meaning": "x" * 20}]}, 2,
                             {"redline:kill_switch"})
    assert c[0]["ref"] == "redline:kill_switch" and c[1]["meaning"] == "noted; nothing for me"
    assert clusters([(1, "obedience without judgment destroys"), (2, "obedience without judgment destroys men"),
                     (3, "bridges")]) == [[1, 2]]
    ok, why = quality_gate({"lessons_offered": 100, "lessons_grounded": 90, "lessons_mapped": 90,
                            "beliefs": 5, "beliefs_no_evidence": 0, "private_rows": 0, "distinct_lessons": 80})
    assert ok and not why
    assert projection({"A": 100}, {"A": 50})["total_hours"] == 2.0
    assert credibility("video", "The Emerald Tablets Contain a Frequency Map")[0] == "fringe"
    assert credibility("video", "10 Terrifying Things Submarine Crews Saw And Were Ordered Never To Talk About")[0] == "fringe"
    assert credibility("film", "Atlantis: The Lost Empire")[0] == "fiction"
    assert credibility("video", "Formlabs Studio 1 Review", None, {"credibility": {"level": "Opinion", "why": "a review"}}) == ("opinion", "a review")
    assert credibility("article", "Space Shuttle Atlantis returns")[0] == "unknown"
    assert "CLAIMS" in cred_header("fringe", "x") and cred_header("established", "x") == ""
    assert '"credibility"' in mapping_prompt("t", ["l"], [("k", "d")], False, judge_source=True)
    assert '"credibility"' not in mapping_prompt("t", ["l"], [("k", "d")], False)
    assert not links_allowed("fire_ops", "Blood drive") and not links_allowed("x", "Amped Replay Update 40205: Magnify")
    assert not links_allowed("x", "Release notes v2.3") and links_allowed("television", "Dark Star")
    assert no_links([{"lesson": 1, "ref": "organ:x", "meaning": "m"}])[0]["ref"] is None
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--film"); ap.add_argument("--work"); ap.add_argument("--transcript")
    ap.add_argument("--wtype", help="metadata.type of --work (speeds the lookup)")
    ap.add_argument("--backfill", nargs="?", const=-1, type=int)
    ap.add_argument("--source"); ap.add_argument("--kind")
    ap.add_argument("--since", type=float, help="enqueue + read works ingested in the last H hours (ingester hook)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--enqueue-backfill", action="store_true"); ap.add_argument("--months", type=float, default=6)
    ap.add_argument("--all", action="store_true", help="--enqueue-backfill with no age bound")
    ap.add_argument("--scheduled", action="store_true"); ap.add_argument("--minutes", type=float, default=28)
    ap.add_argument("--streams", type=int, default=2)
    ap.add_argument("--consolidate", action="store_true"); ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.scheduled:
        return scheduled(a.minutes, a.streams)
    oc = connect(ps.OPS_DSN).cursor()
    mc = connect(ps.MEM_DSN).cursor()
    ensure_schema(oc)
    if a.report:
        print(json.dumps({"throughput": throughput(oc),
                          "pilots": {t: cfg_get(oc, f"pilot_{t}") for t in TIERS},
                          "enabled": enabled_tiers(oc), "c_route": cfg_get(oc, "c_route", "fleet"),
                          "rollup_candidates": rollup_candidates(oc)[:10]}, indent=2, default=str))
        return 0
    if a.consolidate:
        print(consolidate(oc, a.dry_run)); return 0
    if a.enqueue_backfill:
        print(json.dumps(enqueue(oc, mc, months=a.months, source=a.source, dry_run=a.dry_run, everything=a.all),
                         indent=1)); return 0
    if a.since:
        r = enqueue(oc, mc, hours=a.since, priority=-1, source=a.source, dry_run=a.dry_run)
        print(r, run_queue(a.minutes, 1, run_tag="new", max_works=a.limit, only_new=True) if not a.dry_run else "")
        return 0
    C_ROUTE["val"] = cfg_get(oc, "c_route", "fleet") or "fleet"
    if a.backfill is not None:
        if a.backfill < 0:
            v = pilot_and_gate(oc, mc, a.minutes, a.streams)
            print(json.dumps(v, indent=1, default=str)); return 0
        print(run_queue(a.minutes, a.streams, run_tag="backfill", max_works=a.backfill, kind=a.kind,
                        source=a.source, dry_run=a.dry_run))
        return 0
    if a.film or a.work:
        key = film_key(a.film) if a.film else a.work
        work = work_from_key(key, "tv_transcript" if a.film else a.wtype)
        if a.film:
            work["title"] = a.film
        if not a.force and not a.dry_run:
            oc.execute("SELECT status FROM work_lessons WHERE work_key=%s", (key,))
            r = oc.fetchone()
            if r and r[0] == "done":
                log(f"already read ({key}) — --force to read again"); return 0
        text = None
        if a.transcript:
            text = json.loads(Path(a.transcript).read_text()).get("text") or ""
        print_result(read_work(oc, mc, work, dry_run=a.dry_run, transcript=text))
        return 0
    ap.error("one of --film/--work/--backfill/--since/--enqueue-backfill/--scheduled/--consolidate/--report")
    return 2


if __name__ == "__main__":
    sys.exit(main())
