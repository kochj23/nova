#!/usr/bin/env python3
"""nova_topic_study.py — a focused study: everything Nova remembers about one subject, read for its lessons.

Jordan (2026-10-19) asked what Nova learned from Cheers. She had ~1,800 memories of it (comedy/television
transcripts + frame_vision) and had never studied it directly. This reads ONE topic on demand:

  1. gather — keyword (a TV show/series by metadata.show, else full-text tsv) + vector recall through the
     memory server; private sources, private-tagged rows and Nova's own outputs are dropped
     (nova_config.filter_private_memories + the work-lessons OWN_OUTPUTS list).
  2. sample — a series is read episode by episode: up to MAX_EPISODES spread over its seasons, two
     transcript chunks from the middle of each, plus a couple of frame_vision descriptions; any other
     topic takes its best passages. Every passage is numbered [n].
  3. extract — one model call returns lessons as JSON: lesson, episode, moment, a VERBATIM quote and the
     passage number. A lesson whose quote is not in its passage is dropped (grounding gate).
  4. write — a long-form "What Nova Learned From <topic>" piece in her voice
     (nova_voice.system_prompt(..., section=, topic=) — the sci-fi and faith seasoning comes with it),
     built only from the verified lessons and their passages; a deterministic "Episodes cited" list is
     appended. Published to Research (a study is research; --section opinions overrides) through
     nova_journal.publish_hugo with cited_memory_ids + sources, so the grounding line shows.
  5. remember — the distilled lessons become one memory (source 'work_lessons', metadata.type
     'work_lessons', privacy local-only, evidence ids), the same shape nova_work_lessons.py writes.

Requests: nova_topic_study.py "Frasier"            run now
          nova_topic_study.py --request "Frasier"  queue it (claude_queue 'topic study: Frasier')
          nova_topic_study.py --pending            run queued requests (scheduler topic_study_queue, every 30m)
Any Claude/Nova session can queue one with a claude_queue row whose description starts "topic study:".
Flags: --show "Cheers (1982)" (exact series), --section research|opinions, --dry-run (no publish/memory).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_EPISODES = 32
CHUNK_CHARS = 900
FRAME_PER_EP = 2
MAX_PASSAGES = 90
MIN_LESSONS = 4
MODEL = os.environ.get("NOVA_STUDY_MODEL", "anthropic/claude-sonnet-4.5")
MEMSRV = os.environ.get("NOVA_MEMORY_SERVER", "http://memory-server.digitalnoise.net:18790")
SESSION_ID = "nova-topic-study"
QUEUE_PREFIX = "topic study:"
SECTIONS = ("research", "opinions")
OWN_OUTPUTS = {"pursuit", "unclaimed", "private_notebook", "dream", "dreams", "film_lessons", "work_lessons",
               "nova_articles", "journal", "association", "reflection", "conversation", "memory_anchor",
               "weight_of_memory", "autobiography", "becoming"}
TRANSCRIPT_TYPES = {"tv_transcript", "tvshow_transcript", "comedy_transcript", "full_episode", "plex_auto_ingest"}
_EP_RX = re.compile(r"S(\d{1,2})E(\d{1,3})(?:\s*-\s*(.+))?", re.I)
_SAFE_TOPIC = re.compile(r"^[\w\s'&:,.!?()\-]{2,80}$", re.U)


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[topic-study] {m}", flush=True)


# ─────────────────────────── pure helpers ───────────────────────────
def clean_topic(topic: str) -> str | None:
    t = re.sub(r"\s+", " ", str(topic or "")).strip()
    return t if _SAFE_TOPIC.match(t) else None


def display_name(topic: str, show: str | None = None) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", show or topic).strip() or topic


def episode_key(title: str) -> tuple:
    """Sort key + label for an episode title like 'Cheers (1982) - S05E03 - Money Dearest'."""
    m = _EP_RX.search(title or "")
    if not m:
        return (99, 999, title or "")
    return (int(m.group(1)), int(m.group(2)), title)


def episode_label(title: str) -> str:
    m = _EP_RX.search(title or "")
    if not m:
        return re.sub(r"\.(ts|mkv|mp4)$", "", title or "").strip()
    name = (m.group(3) or "").strip()
    name = re.sub(r"\.(ts|mkv|mp4)$", "", name)
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" + (f" “{name}”" if name else "")


def spread(items: list, k: int) -> list:
    """k items evenly spread across a list (order kept)."""
    if len(items) <= k:
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", (s or "").lower().replace("’", "'"))).strip()


def quote_in(quote: str, passage: str) -> bool:
    q = norm(quote)
    return len(q.split()) >= 4 and q in norm(passage)


def parse_json(raw: str):
    if not raw:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    body = m.group(1) if m else raw
    start = min([i for i in (body.find("["), body.find("{")) if i >= 0], default=-1)
    if start < 0:
        return None
    for end in range(len(body), start, -1):
        try:
            return json.loads(body[start:end])
        except Exception:  # noqa: BLE001
            continue
    return None


def verify_lessons(raw_lessons, passages: list[dict]) -> list[dict]:
    """Keep lessons whose quote is verbatim in the passage they cite. Pure."""
    out, seen = [], set()
    for l in raw_lessons if isinstance(raw_lessons, list) else []:
        if not isinstance(l, dict):
            continue
        try:
            n = int(l.get("passage"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(passages):
            continue
        p = passages[n - 1]
        lesson, quote = str(l.get("lesson") or "").strip(), str(l.get("quote") or "").strip()
        if not lesson or not quote_in(quote, p["text"]) or norm(lesson) in seen:
            continue
        seen.add(norm(lesson))
        out.append({"lesson": lesson, "moment": str(l.get("moment") or "").strip(), "quote": quote,
                    "passage": n, "episode": p["label"], "memory_id": p["id"]})
    return out


def sources_block(passages: list[dict], topic: str) -> str:
    """The SOURCES block in sources_from_memories' "[n] (source) text" shape (publish_hugo grounding)."""
    return "\n\n".join([f"TOPIC: {topic}"] + [f"[{i}] ({p['source']}) {p['label']}: {p['text']}"
                                              for i, p in enumerate(passages, 1)])


def render_memory(name: str, lessons: list[dict], url: str | None) -> str:
    lines = [f"[Work lessons — tv_series/topic study: {name}]", "Lessons:"]
    for i, l in enumerate(lessons, 1):
        lines.append(f"{i}. {l['lesson']}\n   Moment: {l['episode']} — {l['moment']} (\"{l['quote']}\")")
    if url:
        lines.append(f"Written up: {url}")
    return "\n".join(lines)


def episodes_cited(lessons: list[dict]) -> str:
    eps = sorted({l["episode"] for l in lessons}, key=lambda e: episode_key(e.replace("“", "- ").replace("”", "")))
    return "\n\n## Episodes cited\n\n" + "\n".join(f"- {e}" for e in eps) + "\n" if eps else ""


# ─────────────────────────── gather ───────────────────────────
def _mem_pg():
    import psycopg2
    import nova_dsn
    return psycopg2.connect(nova_dsn.pg_dsn("nova_memories", "connect_timeout=10"))


def _keep(rows: list[dict]) -> list[dict]:
    import nova_config
    rows = [r for r in rows if (r.get("source") or "").split(":")[0] not in OWN_OUTPUTS
            and not (r.get("source") or "").startswith("quarantine:")]
    return nova_config.filter_private_memories(rows)


def find_show(cur, topic: str) -> str | None:
    cur.execute("SELECT metadata->>'show', count(*) FROM memories WHERE metadata->>'show' ILIKE %s "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 1", (topic + "%",))
    r = cur.fetchone()
    return r[0] if r and r[1] >= 20 else None


def gather_series(cur, show: str) -> list[dict]:
    cur.execute("SELECT id, source, text, metadata FROM memories WHERE metadata->>'show' = %s "
                "AND tier <> 'scratchpad'", (show,))
    return _keep([{"id": r[0], "source": r[1], "text": r[2], "metadata": r[3] or {}} for r in cur.fetchall()])


def gather_keyword(cur, topic: str, limit: int = 400) -> list[dict]:
    cur.execute("SELECT id, source, text, metadata FROM memories WHERE tsv @@ plainto_tsquery('english', %s) "
                "AND tier <> 'scratchpad' LIMIT %s", (topic, limit))
    return _keep([{"id": r[0], "source": r[1], "text": r[2], "metadata": r[3] or {}} for r in cur.fetchall()])


def gather_vector(topic: str, n: int = 60) -> list[dict]:
    try:
        import nova_journal as nj
        rows = nj.recall_memories(topic, n=n)
    except Exception as e:  # noqa: BLE001
        log(f"vector recall failed (keyword only): {e}")
        return []
    word = re.compile(r"\b" + re.escape(topic.split("(")[0].strip()) + r"\b", re.I)
    return _keep([r for r in rows if word.search(str(r.get("text", "")))])


def sample_series(rows: list[dict], max_eps: int = MAX_EPISODES) -> list[dict]:
    """Numbered passages: two middle transcript chunks + FRAME_PER_EP frame descriptions per episode."""
    eps: dict[str, list[dict]] = {}
    frames: dict[str, list[dict]] = {}
    for r in rows:
        md = r.get("metadata") or {}
        if md.get("type") in TRANSCRIPT_TYPES and md.get("title"):
            eps.setdefault(str(md["title"]), []).append(r)
        elif md.get("kind") == "frame" and md.get("video"):
            frames.setdefault(re.sub(r"\.(ts|mkv|mp4)$", "", str(md["video"])), []).append(r)
    order = sorted(eps, key=episode_key)
    passages = []
    for title in spread(order, max_eps):
        chunks = sorted({str((c.get("metadata") or {}).get("chunk")): c for c in eps[title]}.values(),
                        key=lambda c: int((c.get("metadata") or {}).get("chunk") or 0))
        picks = [chunks[len(chunks) // 3], chunks[(2 * len(chunks)) // 3]] if len(chunks) >= 3 else chunks[:2]
        label = episode_label(title)
        for c in {id(p): p for p in picks}.values():
            passages.append({"id": c["id"], "source": c["source"], "label": label,
                             "text": re.sub(r"\s+", " ", c["text"])[:CHUNK_CHARS]})
        fr = frames.get(title) or frames.get(re.sub(r"\.(ts|mkv|mp4)$", "", title)) or []
        seen = set()
        for f in sorted(fr, key=lambda f: float((f.get("metadata") or {}).get("t_seconds") or 0)):
            desc = re.sub(r"^\[[^\]]*\]\s*", "", f["text"]).strip()
            if desc and desc not in seen and len(seen) < FRAME_PER_EP:
                seen.add(desc)
                passages.append({"id": f["id"], "source": f["source"], "label": label + " (on screen)",
                                 "text": desc[:300]})
    return passages[:MAX_PASSAGES]


def sample_generic(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        t = re.sub(r"\s+", " ", str(r.get("text", ""))).strip()
        if len(t) < 80 or t[:120] in seen:
            continue
        seen.add(t[:120])
        md = r.get("metadata") or {}
        label = str(md.get("title") or md.get("show") or md.get("book") or r.get("source"))[:100]
        out.append({"id": r["id"], "source": r["source"], "label": label, "text": t[:CHUNK_CHARS]})
    return out[:MAX_PASSAGES]


def gather(topic: str, show: str | None = None) -> tuple[str | None, list[dict], int]:
    """(series or None, numbered passages, total memories found)."""
    c = _mem_pg()
    try:
        with c.cursor() as cur:
            show = show or find_show(cur, topic)
            if show:
                rows = gather_series(cur, show)
                log(f"series {show!r}: {len(rows)} memories")
                return show, sample_series(rows), len(rows)
            rows = gather_keyword(cur, topic)
    finally:
        c.close()
    rows = {r["id"]: r for r in rows + gather_vector(topic)}.values()
    rows = list(rows)
    log(f"topic {topic!r}: {len(rows)} memories (keyword + vector)")
    return None, sample_generic(rows), len(rows)


# ─────────────────────────── model passes ───────────────────────────
def passages_text(passages: list[dict]) -> str:
    return "\n\n".join(f"[{i}] {p['label']} — {p['text']}" for i, p in enumerate(passages, 1))


def extract_lessons(name: str, passages: list[dict], llm) -> list[dict]:
    system = ("You are a careful reader extracting LESSONS (not plot summary, not dialogue trivia) from "
              "numbered passages of a work. Output JSON only.")
    user = (f"WORK: {name}\n\nNUMBERED PASSAGES (transcript excerpts and on-screen descriptions):\n\n"
            f"{passages_text(passages)}\n\n"
            "Find 10-14 lessons an attentive viewer learns from these specific moments — about people, work, "
            "friendship, pride, failure, loneliness, belonging, honesty, running a place people depend on. "
            "Each lesson must rest on ONE passage. Return a JSON array of objects with keys:\n"
            '  "lesson": one general sentence (the takeaway),\n'
            '  "moment": 1-2 sentences describing what happens in that passage — ONLY what the passage shows,\n'
            '  "quote": a VERBATIM span of 6-25 words copied exactly from that passage,\n'
            '  "passage": the passage number.\n'
            "Do not invent names, events or lines that are not in the passage. JSON only.")
    return verify_lessons(parse_json(llm(system, user)), passages)


def write_article(name: str, topic: str, lessons: list[dict], passages: list[dict], section: str,
                  n_memories: int, llm) -> tuple[str, str] | None:
    import nova_voice
    ctx = (f"Write a long-form study for Nova's journal ({section.title()} section): WHAT NOVA LEARNED FROM "
           f"{name.upper()}. Nova has {n_memories} memories of it and has never studied it directly until now. "
           "This is about LESSONS — what the work taught her about people, and about her own job as the "
           "steward of a household and its machines — each anchored to a specific episode and moment from "
           "the material below. Quote the given lines exactly when you quote. Name episodes the way they "
           "are labelled. Never invent a scene, a line, a character action or a number not in the material. "
           "First person, her voice, warm and dry; let a sci-fi or faith reference land where it fits. "
           "Structure: an opening on why this, then one ### section per lesson (episode + moment + what she "
           "takes from it), then a closing on what she'll carry into her own work. 1600-2400 words, markdown, "
           "no H1.\n\nOUTPUT EXACTLY THIS SHAPE:\nSUBTITLE: <a subtitle of at most 8 words, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx, section=section, topic=f"{name} lessons friendship belonging")
    used = sorted({l["passage"] for l in lessons})
    material = "\n\n".join(
        f"LESSON {i}: {l['lesson']}\n  episode: {l['episode']}\n  moment: {l['moment']}\n  verbatim line: \"{l['quote']}\""
        for i, l in enumerate(lessons, 1))
    support = "\n\n".join(f"[{n}] {passages[n - 1]['label']} — {passages[n - 1]['text']}" for n in used)
    raw = llm(system, f"VERIFIED LESSONS:\n\n{material}\n\nTHE PASSAGES THEY COME FROM:\n\n{support}\n\nWrite the study.")
    if not raw or len(raw) < 800:
        return None
    sub, body = "", []
    for ln in raw.splitlines():
        if not sub and ln.upper().startswith("SUBTITLE:"):
            sub = ln.split(":", 1)[1].strip().strip('"')
            if len(sub) > 70:
                sub = sub[:70].rsplit(" ", 1)[0].rstrip(",;:—- ")
        else:
            body.append(ln)
    title = f"What Nova Learned From {name}" + (f": {sub}" if sub else "")
    return title, "\n".join(body).strip()


def _llm(system: str, user: str) -> str | None:
    import nova_journal as nj
    return nj.call_openrouter(system, user, model=MODEL, max_tokens=8000, timeout=900)


def remember(text: str, metadata: dict) -> str | None:
    req = urllib.request.Request(f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"text": text, "source": "work_lessons", "metadata": metadata}).encode())
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}").get("id")
        except Exception as e:  # noqa: BLE001
            log(f"remember attempt {attempt + 1}/3 failed: {e}")
    return None


def article_url(section: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return f"https://nova.digitalnoise.net/{section}/{slug}/"


# ─────────────────────────── run ───────────────────────────
def study(topic: str, show: str | None = None, section: str = "research", dry_run: bool = False,
          llm=None) -> dict:
    llm = llm or _llm
    res = {"topic": topic, "status": "failed"}
    series, passages, total = gather(topic, show)
    name = display_name(topic, series)
    res.update(series=series, passages=len(passages), memories=total, name=name)
    if len(passages) < 6:
        res["status"] = "skipped"
        res["reason"] = f"only {len(passages)} usable passages for {topic!r}"
        log(res["reason"])
        return res
    lessons = extract_lessons(name, passages, llm)
    res["lessons"] = lessons
    log(f"{len(lessons)} verified lesson(s) from {len(passages)} passages")
    if len(lessons) < MIN_LESSONS:
        res["reason"] = f"only {len(lessons)} lessons survived the quote check (need {MIN_LESSONS})"
        log(res["reason"])
        return res
    art = write_article(name, topic, lessons, passages, section, total, llm)
    if not art:
        res["reason"] = "article pass produced nothing"
        return res
    title, body = art
    body += episodes_cited(lessons)
    res.update(title=title, words=len(body.split()))
    url = article_url(section, title)
    if dry_run:
        Path(os.environ.get("NOVA_STUDY_PREVIEW", "/tmp/topic_study_preview.md")).write_text(f"# {title}\n\n{body}")
        res.update(status="dry-run", url=url)
        return res
    import nova_journal as nj
    cited = list(dict.fromkeys(l["memory_id"] for l in lessons))
    desc = f"Nova studies {name} for the first time on purpose: the lessons, episode by episode."
    tags = ["topic-study", "lessons", re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "television"]
    ok = nj.publish_hugo(title, body, section, tags, desc, emoji="🎓", cited_memory_ids=cited,
                         sources=sources_block(passages, name), profile="topic-study")
    if not ok:
        res["reason"] = "publish guard rejected"
        return res
    push = nj.git_push(section, title)
    res.update(status="published", url=url, push=push)
    mid = remember(render_memory(name, lessons, url),
                   {"type": "work_lessons", "kind": "tv_series" if series else "topic", "title": series or name,
                    "work_key": f"topic_study::{series or name}"[:300], "privacy": "local-only",
                    "evidence_ids": cited[:60], "date": date.today().isoformat(), "n_lessons": len(lessons),
                    "model": MODEL, "study": True, "article_url": url})
    res["memory_id"] = mid
    try:
        nj.notify_slack(section, f"🎓 {title}", f"Topic study: {len(lessons)} lessons from {total} memories.")
    except Exception:  # noqa: BLE001
        pass
    log(f"published {url} ({len(lessons)} lessons, memory {mid})")
    return res


# ─────────────────────────── queue ───────────────────────────
def _ops_pg():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"))
    c.autocommit = True
    return c


def request(topic: str) -> int | None:
    t = clean_topic(topic)
    if not t:
        log(f"refusing topic {topic!r}")
        return None
    c = _ops_pg()
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO claude_sessions (session_id, project, status, host) VALUES (%s,'nova_topic_study',"
                        "'active',%s) ON CONFLICT DO NOTHING", (SESSION_ID, socket.gethostname()))
            cur.execute("INSERT INTO claude_queue (session_id, priority, description, context) VALUES (%s, 5, %s, %s) "
                        "RETURNING id", (SESSION_ID, f"{QUEUE_PREFIX} {t}",
                                         "Run: nova_topic_study.py --pending (scheduler topic_study_queue picks it up)"))
            return cur.fetchone()[0]
    finally:
        c.close()


def pending(limit: int = 1) -> int:
    """Claim and run queued 'topic study: X' requests. Returns how many ran."""
    me = f"nova_topic_study@{socket.gethostname()}"
    c = _ops_pg()
    ran = 0
    try:
        with c.cursor() as cur:
            for _ in range(limit):
                cur.execute("UPDATE claude_queue SET status='claimed', claimed_by=%s, lease_until=now()+interval '1 hour', "
                            "heartbeat_at=now(), updated_at=now() WHERE id = (SELECT id FROM claude_queue WHERE "
                            "status='queued' AND description ILIKE %s ORDER BY priority, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                            "RETURNING id, description", (me, QUEUE_PREFIX + "%"))
                row = cur.fetchone()
                if not row:
                    break
                qid, desc = row
                topic = clean_topic(desc[len(QUEUE_PREFIX):])
                if not topic:
                    cur.execute("UPDATE claude_queue SET status='failed', outcome=%s, updated_at=now() WHERE id=%s",
                                ("unusable topic text", qid))
                    continue
                try:
                    res = study(topic)
                except Exception as e:  # noqa: BLE001
                    res = {"status": "failed", "reason": str(e)[:300]}
                ok = res.get("status") in ("published", "skipped")
                outcome = res.get("url") if res.get("status") == "published" else res.get("reason", res.get("status"))
                cur.execute("UPDATE claude_queue SET status=%s, outcome=%s, completed_at=now(), updated_at=now() "
                            "WHERE id=%s", ("done" if ok else "failed", str(outcome)[:1000], qid))
                ran += 1
    finally:
        c.close()
    return ran


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("topic", nargs="?")
    ap.add_argument("--show")
    ap.add_argument("--section", choices=SECTIONS, default="research")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--request", metavar="TOPIC")
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args(argv)
    if a.request:
        qid = request(a.request)
        print(f"queued topic study #{qid}" if qid else "not queued")
        return 0 if qid else 2
    if a.pending:
        n = pending()
        log(f"{n} queued study(ies) run")
        return 0
    if not a.topic:
        ap.print_help()
        return 0
    topic = clean_topic(a.topic)
    if not topic:
        log(f"refusing topic {a.topic!r}")
        return 2
    res = study(topic, a.show, a.section, a.dry_run)
    print(json.dumps({k: v for k, v in res.items() if k != "lessons"}, default=str))
    return 0 if res["status"] in ("published", "dry-run", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
