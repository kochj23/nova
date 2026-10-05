#!/usr/bin/env python3
"""nova_account.py — the ACCOUNT organ: Nova answers questions about herself from her own ledgers, not from guesswork.

Jordan (2026-10-05): every question he asked that day was ABOUT Nova — what did you learn, what did you do in your
free time, where is the 10am article, how are the renders going — and each took Claude 3–8 hand-written queries over
tables Nova already owns. This gives her (and Claude, and her reaches) one typed answer per question, as facts:

  nova_account.py learned   [--date YYYY-MM-DD]   # new memories by vector + every ingest job and its outcome
  nova_account.py free      [--date YYYY-MM-DD]   # pursuits, tinkering, projects, tangents, self-questions, growth
  nova_account.py pipelines                       # renders/uploads, running ingests, scheduler failures, open incidents
  nova_account.py article   <slug|words|HH:MM>    # one article: written -> committed -> pushed -> deployed -> live, with why-late

Output is JSON (the chat agent narrates it in her voice; Claude reads it as-is). Read-only. Any ledger that is
unreachable is reported as such — never guessed around.
ponytail: deploy state uses `gh` when present (Studio) and says "unknown" elsewhere (.2 has no gh).
"""
import argparse, json, os, re, shutil, subprocess, sys, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
import psycopg2, psycopg2.extras

OPS = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
MEM = os.environ.get("NOVA_MEM_DSN", "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
JOURNAL = Path(os.environ.get("NOVA_JOURNAL_DIR", str(Path.home() / "nova-journal")))
SITE = "https://nova.digitalnoise.net"
SELF_VECTORS = ("unclaimed", "gravel", "imagination", "learning", "projects", "self_answer", "research", "association",
                "growth", "self_eval", "attention_focus", "weight_of_memory", "episodic", "empathy_core", "hold",
                "self_model", "principal_model", "becoming")


def _q(dsn, sql, args=()):
    try:
        c = psycopg2.connect(dsn, connect_timeout=5); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, args); rows = [dict(r) for r in cur.fetchall()]; c.close(); return rows
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _d(s): return date.fromisoformat(s) if s else date.today()


# ── learned ──────────────────────────────────────────────────────────────────────────────
def learned(day: date) -> dict:
    by_vector = _q(MEM, "SELECT source AS vector, count(*) AS n FROM memories WHERE created_at::date=%s GROUP BY 1 ORDER BY 2 DESC", (day,))
    total = sum(r["n"] for r in by_vector) if isinstance(by_vector, list) else None
    ingests = _q(OPS, """SELECT id, status, description, left(coalesce(outcome,''),160) AS outcome, to_char(created_at,'HH24:MI') AS at
                         FROM claude_queue WHERE created_at::date=%s AND description ILIKE 'ingest%%' ORDER BY id""", (day,))
    jobs = _q(OPS, """SELECT mode, left(query,80) AS query, vector, status, memories_stored AS stored, to_char(coalesce(started_at,created_at),'HH24:MI') AS at, left(coalesce(error,''),100) AS error
                      FROM ingest_jobs WHERE coalesce(started_at,created_at)::date=%s ORDER BY coalesce(started_at,created_at)""", (day,))
    zero = [j for j in jobs if (j.get("stored") or 0) == 0 and j.get("status") not in ("running", "queued", None)] if isinstance(jobs, list) else []
    samples = _q(MEM, """WITH s AS (SELECT source, text, row_number() OVER (PARTITION BY source ORDER BY created_at DESC) rn
                         FROM memories WHERE created_at::date=%s) SELECT source AS vector, left(regexp_replace(text,'\\s+',' ','g'),140) AS sample
                         FROM s WHERE rn=1 ORDER BY source""", (day,))
    return {"date": str(day), "total_new_memories": total, "by_vector": by_vector, "ingest_requests": ingests,
            "ingest_jobs": jobs, "ingests_that_stored_nothing": zero, "sample_per_vector": samples}


# ── free time ────────────────────────────────────────────────────────────────────────────
def free(day: date) -> dict:
    nxt = day + timedelta(days=1)
    return {
        "date": str(day),
        "projects_worked": _q(OPS, """SELECT p.title, to_char(l.ts,'HH24:MI') AS at, left(l.work_note,240) AS note, l.next_step
                                     FROM project_log l JOIN projects p ON p.id=l.project_id WHERE l.ts::date=%s ORDER BY l.ts""", (day,)),
        "pursuit_threads": _q(OPS, """SELECT topic, kind, wakes, to_char(updated_at,'HH24:MI') AS at, left(regexp_replace(last_note,'\\s+',' ','g'),200) AS note, next_step
                                     FROM pursuit_threads WHERE updated_at::date=%s ORDER BY updated_at DESC""", (day,)),
        "tinkering": _q(OPS, """SELECT to_char(ts,'HH24:MI') AS at, kind, left(topic,120) AS topic, reflected, wanted_fix, proposal_status
                               FROM tinker_log WHERE ts::date=%s ORDER BY ts""", (day,)),
        "proposals_filed": _q(OPS, """SELECT id, origin, left(proposed_action,120) AS action, status FROM coagency_proposals
                                     WHERE created_at::date=%s ORDER BY id""", (day,)),
        "reaches_to_jordan": _q(OPS, "SELECT to_char(ts,'HH24:MI') AS at, status, left(message,140) AS message FROM reach_log WHERE ts::date=%s ORDER BY ts", (day,)),
        "self_directed_memories": _q(MEM, """SELECT source AS vector, to_char(created_at,'HH24:MI') AS at, left(regexp_replace(text,'\\s+',' ','g'),260) AS text
                                            FROM memories WHERE created_at >= %s AND created_at < %s AND source = ANY(%s) ORDER BY created_at""",
                                     (day, nxt, list(SELF_VECTORS))),
        "growth": _q(OPS, "SELECT id, status, left(weakness,160) AS weakness, left(coalesce(outcome,''),120) AS outcome FROM growth_commitments WHERE created_at::date=%s OR resolved_at::date=%s ORDER BY id DESC", (day, day)),
        "journal_pieces": _q(OPS, "SELECT section, title FROM (SELECT DISTINCT ON (slug) section, title, slug FROM nova_speaks_renders WHERE queued_at::date=%s) s ORDER BY section", (day,)),
    }


# ── pipelines ────────────────────────────────────────────────────────────────────────────
def pipelines() -> dict:
    return {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "nova_speaks": {
            "in_flight": _q(OPS, """SELECT slug, status, host, to_char(coalesce(started_at,queued_at),'HH24:MI') AS since
                                   FROM nova_speaks_renders WHERE status IN ('queued','rendering') ORDER BY queued_at"""),
            "done_today": _q(OPS, """SELECT count(*) AS rendered, count(*) FILTER (WHERE youtube_id ~ '^[A-Za-z0-9_-]{11}$') AS on_youtube,
                                    count(*) FILTER (WHERE youtube_id IS NULL) AS not_uploaded FROM nova_speaks_renders WHERE status='done' AND finished_at::date=current_date"""),
            "failed_today": _q(OPS, "SELECT slug, left(note,160) AS note FROM nova_speaks_renders WHERE status='failed' AND finished_at::date=current_date"),
        },
        "ingests_running": _q(OPS, """SELECT id, left(description,120) AS description, to_char(created_at,'HH24:MI') AS since
                                     FROM claude_queue WHERE status='in_progress' AND description ILIKE 'ingest%%' ORDER BY id"""),
        "claude_queue_open": _q(OPS, "SELECT status, count(*) AS n FROM claude_queue WHERE status IN ('pending','in_progress','queued') GROUP BY 1"),
        "scheduler_failures_today": _q(OPS, """SELECT task_id, count(*) AS n, max(to_char(to_timestamp(started_at),'HH24:MI')) AS last, max(left(error_tail,100)) AS error
                                              FROM scheduler_runs WHERE to_timestamp(started_at)::date=current_date AND (status IN ('failed','error','timeout') OR coalesce(exit_code,0)<>0)
                                              GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),
        "open_incidents": _q(OPS, "SELECT id, severity, left(title,120) AS title, to_char(started_at,'MM-DD HH24:MI') AS at FROM incidents WHERE status NOT IN ('resolved','closed') ORDER BY started_at DESC LIMIT 10"),
    }


# ── intent: which report answers a question ABOUT Nova (used by the gateway before the model sees it) ──
_INTENTS = [
    ("article",   ("article", "post", "piece", "dispatch", "essay", "journal entry", "blog"),
                  ("where is", "where's", "why was", "why is", "late", "go out", "went out", "publish", "on the site", "live yet", "missing", "didn't", "did the", "status of the", "happened to", "what about", "update on", "when will", "is it up", "is it live")),
    ("pipelines", ("status", "eta", "running", "pipeline", "render", "upload", "ingests", "ingest done", "batch", "queue", "failing", "anything broken", "what's going on", "whats going on", "how are the"), ()),
    ("learned",   ("learn", "school", "new memories", "ingested", "ingest today", "what did you read", "what came in", "vectors"), ()),
    ("free",      ("free time", "your own time", "unclaimed", "what did you do", "what have you been doing", "what have you been up to", "been up to", "working on", "pursu", "tinker", "your day", "how was your day"), ()),
]


def classify_question(q: str):
    """Return 'learned' | 'free' | 'pipelines' | 'article' | None for a question about Nova herself."""
    ql = " " + re.sub(r"\s+", " ", q.lower().strip()) + " "
    for what, needs, also in _INTENTS:
        if any(k in ql for k in needs) and (not also or any(k in ql for k in also)):
            return what
    return None


_STOP = set("the a an of to is in on at for and or what where why when how did was were it its that this today yesterday happened happen "
            "article post piece dispatch essay blog entry journal nova you your late out go went about tell me please status".split())


def question_to_article_query(q: str) -> str:
    """'where is the 10am burbank article' -> '10:00' if a clock time is present, else the content words."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", q.lower())
    words = [w for w in re.findall(r"[a-z'-]+", q.lower()) if w not in _STOP and len(w) > 2]
    if m and (m.group(2) or m.group(3)):
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        return f"{h:02d}:{m.group(2) or '00'} " + " ".join(words[:5])        # time first, then words to break ties
    return " ".join(words[:6])


# ── article ──────────────────────────────────────────────────────────────────────────────
def _git(args, cwd=JOURNAL, timeout=30):
    try: return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e: return f"error: {e}"


def _http(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "nova-account"})
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e: return e.code
    except Exception: return None


def article(query: str, day: date | None = None) -> dict:
    """Find one article by slug fragment, title words, or a scheduled time like 10:00 (on `day`), then trace it."""
    day = day or date.today()
    _git(["fetch", "-q", "origin", "main"], timeout=60)
    local = {str(f.relative_to(JOURNAL)): f for f in (JOURNAL / "content").glob("*/*.md")}
    remote = [l for l in _git(["ls-tree", "-r", "--name-only", "origin/main", "content"]).splitlines() if l.endswith(".md")]
    files = []                                                  # (relpath, reader) over local ∪ origin/main
    for rel in sorted(set(local) | set(remote)):
        if rel in local: files.append((rel, (lambda f: lambda: f.read_text(errors="replace"))(local[rel])))
        else: files.append((rel, (lambda r: lambda: _git(["show", f"origin/main:{r}"]))(rel)))
    q = query.strip().lower()
    tm = re.match(r"(\d{1,2}:\d{2})\b\s*(.*)$", q)                 # "10:00 local burbank" -> time + tie-break words
    want_time, q = (tm.group(1).zfill(5), tm.group(2).strip()) if tm else (None, q)
    hits = []
    for rel, read in files:
        f = Path(rel)
        if f.name == "_index.md": continue
        head = read()[:1500]
        slug, section = f.stem, f.parent.name
        m = re.search(r'^date:\s*"?(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})', head, re.M)
        fdate, ftime = (m.group(1), m.group(2)) if m else ("", "")
        title = (re.search(r'^title:\s*"?(.+?)"?\s*$', head, re.M) or [None, slug])[1]
        if want_time:
            if fdate == str(day) and ftime == want_time:
                hay = (title + " " + slug.replace("-", " ") + " " + section).lower()
                hits.append((f, slug, section, title, fdate, ftime, 100 + sum(1 for w in q.split() if w in hay)))
        elif q in slug.lower() or all(w in title.lower() for w in q.split()):
            hits.append((f, slug, section, title, fdate, ftime, 100))
        else:                                                       # free text: score by word overlap with title+slug, recent first
            words = [w for w in q.split() if w not in _STOP]
            hay = (title + " " + slug.replace("-", " ")).lower()
            score = sum(1 for w in words if w in hay)
            if words and score >= max(1, min(2, len(words))): hits.append((f, slug, section, title, fdate, ftime, score))
    if not hits: return {"query": query, "found": False}
    hits = [h if len(h) == 7 else h + (100,) for h in hits]
    hits.sort(key=lambda h: (h[6], h[4]), reverse=True)
    f, slug, section, title, fdate, ftime, _score = hits[0]
    url = f"{SITE}/{section}/{slug}/"
    rel = str(f)
    committed = _git(["log", "-1", "--format=%h %ci", "--", rel])
    author_ts = _git(["log", "-1", "--format=%ai", "--", rel])
    on_origin = bool(_git(["log", "-1", "--format=%h", "origin/main", "--", rel]))
    in_this_clone = rel in local
    live = _http(url)
    deploy = "unknown (gh not installed here)"
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "run", "list", "--workflow", "deploy.yml", "--limit", "3", "--json", "status,conclusion,createdAt,headSha"],
                                 cwd=JOURNAL, capture_output=True, text=True, timeout=30).stdout
            deploy = [{"at": r["createdAt"][11:16] + "Z", "status": r["status"], "conclusion": r["conclusion"], "sha": r["headSha"][:8]} for r in json.loads(out or "[]")]
        except Exception as e: deploy = f"error: {e}"
    push_log = []
    for lf in (Path.home() / ".openclaw/logs/nova_journal.log",):
        if lf.exists():
            try:
                tail = subprocess.run(["tail", "-c", "400000", str(lf)], capture_output=True, text=True, timeout=10).stdout
                push_log = [l[:160] for l in tail.splitlines() if l.startswith(f"[{fdate}") and re.search(r"Push|push|rebase|Git error|timed out|Pushed", l)][-8:]
            except Exception: pass
    why = []
    if committed and author_ts and committed.split(" ", 1)[1][:16] != author_ts[:16]:
        why.append(f"committed at {author_ts[:16]} but only pushed/rebased at {committed.split(' ',1)[1][:16]} — the push failed in between (see push_log)")
    if not on_origin: why.append(f"not on origin/main yet — committed in the {os.uname().nodename} clone but the push has not happened")
    if on_origin and not in_this_clone: why.append(f"(note: this clone on {os.uname().nodename} is behind origin; trace used origin/main)")
    if on_origin and live != 200: why.append("pushed but not live — the GitHub Pages deploy has not completed (see deploy)")
    return {"query": query, "found": True, "title": title, "section": section, "slug": slug, "scheduled": f"{fdate} {ftime}",
            "file": rel, "committed": committed or "not committed", "on_origin_main": on_origin, "in_this_clone": in_this_clone, "live_http": live, "url": url,
            "deploy_runs": deploy, "push_log": push_log, "why_late": why or ["on time as far as the ledgers show"]}


def brief(what: str, out: dict) -> dict:
    """Squeeze a report under the gateway's 3000-char tool cap: top-N rows, short strings, no samples."""
    return _shrink(_brief(what, out))


def _brief(what: str, out: dict) -> dict:
    def cut(rows, n, keys=None, w=90):
        if not isinstance(rows, list): return rows
        return [{k: (v[:w] if isinstance(v, str) else v) for k, v in r.items() if not keys or k in keys} for r in rows[:n]]
    if what == "learned":
        return {"date": out["date"], "total_new_memories": out["total_new_memories"],
                "top_vectors": cut(out["by_vector"], 12, ("vector", "n")), "ingests": cut(out["ingest_requests"], 8, ("status", "description", "outcome"), 70),
                "stored_nothing": cut(out["ingests_that_stored_nothing"], 5, ("query", "vector"), 60)}
    if what == "free":
        return {"date": out["date"], "projects": cut(out["projects_worked"], 2, ("title", "note", "next_step"), 120),
                "pursuits": cut(out["pursuit_threads"], 5, ("topic", "wakes", "note"), 110), "tinkering": cut(out["tinkering"], 3, ("topic", "wanted_fix"), 80),
                "reaches": cut(out["reaches_to_jordan"], 3, ("status", "message"), 80),
                "self_directed_count": len(out["self_directed_memories"]) if isinstance(out["self_directed_memories"], list) else "?",
                "self_directed": cut(out["self_directed_memories"], 6, ("vector", "text"), 110), "growth": cut(out["growth"], 2, ("status", "weakness"), 100)}
    if what == "pipelines":
        ns = out["nova_speaks"]
        return {"as_of": out["as_of"], "renders_in_flight": cut(ns["in_flight"], 6, ("slug", "status", "host", "since"), 60), "renders_today": ns["done_today"], "render_failures": cut(ns["failed_today"], 3, w=80),
                "ingests_running": len(out["ingests_running"]) if isinstance(out["ingests_running"], list) else "?",
                "ingests_running_sample": cut(out["ingests_running"], 4, ("description",), 70), "scheduler_failures": cut(out["scheduler_failures_today"], 6, ("task_id", "n", "error"), 60),
                "open_incidents": cut(out["open_incidents"], 4, ("severity", "title"), 80)}
    if what == "article":
        o = dict(out); o["push_log"] = (out.get("push_log") or [])[-3:]; o["deploy_runs"] = (out.get("deploy_runs") or [])[:2] if isinstance(out.get("deploy_runs"), list) else out.get("deploy_runs")
        return o
    return out


def _shrink(d: dict, cap: int = 2900) -> dict:
    """Hard guarantee for the tool cap: while the JSON is too long, halve every list in the report."""
    while len(json.dumps(d, default=str, ensure_ascii=False)) > cap:
        lists = [k for k, v in d.items() if isinstance(v, list) and len(v) > 1]
        if not lists: break
        for k in lists: d[k] = d[k][: max(1, len(d[k]) // 2)]
    return d


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("what", choices=["learned", "free", "pipelines", "article"])
    ap.add_argument("query", nargs="?", default=""); ap.add_argument("--date", default=None)
    ap.add_argument("--brief", action="store_true", help="compact for the chat tool (gateway caps tool output at 3000 chars)")
    a = ap.parse_args(); day = _d(a.date)
    out = {"learned": lambda: learned(day), "free": lambda: free(day), "pipelines": pipelines,
           "article": lambda: article(a.query, day)}[a.what]()
    if a.brief: out = brief(a.what, out)
    print(json.dumps(out, indent=None if a.brief else 1, default=str, ensure_ascii=False)); return 0


if __name__ == "__main__":
    sys.exit(main())
