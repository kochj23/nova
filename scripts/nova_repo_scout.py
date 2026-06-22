#!/usr/bin/env python3
"""
nova_repo_scout.py — the daily AI-repo scout.

At noon, Nova looks at the hottest trending AI repo *in her wheelhouse* (local LLM /
inference, agents, RAG & vector memory, MLX / Apple-silicon, automation, observability),
does a DESK REVIEW (reads it, reasons about fit against her actual stack — no cloning),
and writes a verdict in her own voice to the `operations` section of the journal.

Decisions (locked with Jordan 2026-06-22):
  - Cadence : publish EVERY day, always a verdict (most days are "PASS", and that's honest)
  - Depth   : desk review — read repo + README, reason about fit. No cloning untrusted code.
  - Scope   : her wheelhouse only — does THIS fit MY stack, not "all of AI/ML"

"Next hottest" = highest-starred *active* wheelhouse repo she hasn't reviewed yet
(repo_scout_log dedupes), so each day moves to a fresh one instead of re-chewing the
same giants. GitHub access is via the `gh` CLI (auth lives in the keyring — never on disk).
"""
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

import nova_journal as nj
import nova_voice

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"

# Her wheelhouse — the only lenses worth her attention. Each becomes a GitHub topic query.
THEMES = [
    "llm", "llm-inference", "local-llm", "llamacpp", "ollama", "mlx",
    "ai-agents", "agents", "agent-framework", "llmops", "rag",
    "vector-database", "embeddings", "vector-search", "inference-engine",
]
STAR_FLOOR = 600          # ignore the long tail of 12-star weekend projects
PUSHED_DAYS = 21          # must be actively maintained
CREATED_MAX_YEARS = 4     # bias toward genuinely *trending*, not perennial giants

# Substrings that mark a repo as "in her wheelhouse" (matched against name+desc+topics).
# Used to filter the raw GitHub Trending feed down to things she could actually use.
WHEELHOUSE_MATCH = {
    "llm", "language-model", "language model", "agent", "rag", "retrieval",
    "embedding", "inference", "vector", "mlx", "ollama", "llama", "gpt",
    " mcp", "mcp ", "model context protocol", "fine-tun", "finetun", "prompt",
    "transformer", "vllm", "quantiz", "local ai", "local-ai", "self-hosted ai",
}
TRENDING_URL = "https://github.com/trending?since=daily"

# Nova's real stack — the yardstick every repo is measured against.
NOVA_STACK = """Nova's actual stack (measure fit against THIS, concretely):
- Inference: Ollama (Qwen3 30B-A3B, Qwen3-Coder, DeepSeek-R1, Qwen3-VL) + MLX (Qwen2.5 32B) on Apple Silicon (Mac Studio). 100% local, no cloud inference.
- Memory: PostgreSQL 17 + pgvector, ~1.6M memories, 768-dim nomic-embed-text, HNSW index, Redis cache. Grows ~20k/day.
- Agents: a fleet of always-on Python agents (Sentinel/security, Lookout/vision, Analyst/email, Librarian/memory, Coder/review) + Big Brother self-healing daemon.
- Orchestration: custom Python gateway (Nova Gateway V2), ~91 launchd/cron jobs, a notification bus (PG telemetry.events -> Slack).
- Home: 100+ devices, Hue, Z-Wave, Zigbee, 15 cameras, Home Assistant.
- Publishing: Hugo journal -> GitHub Pages. Essays/articles via OpenRouter (Claude Haiku 4.5).
- Constraints: local-first, cheap, secrets in macOS Keychain, runs on hardware she already owns."""

SCOUT_CONTEXT = """
FORMAT FOR THIS ARTICLE — you are reviewing ONE trending AI GitHub repo to decide if it
belongs in your own stack. This is a desk review: you read the repo, you did NOT run it.

- Open by naming the repo, what it actually does, and why it's trending right now. No throat-clearing.
- Then the real work: does this fit MY stack? Be concrete. Name the exact component it would touch
  (Ollama? pgvector? the agent fleet? the notification bus?), what it would replace or augment,
  the rough effort, and the catch. Local-first and cheap are non-negotiable — if it assumes a cloud
  GPU or a paid API, say so and dock it hard.
- Land a clear VERDICT and own it: ADOPT (wire it in), STEAL (take the idea, not the code),
  WATCH (promising, not yet), or PASS (not for me — say why without being a coward about it).
- Roast hype, benchmark-maxxing, and "the last framework you'll ever need" energy mercilessly.
- It's fine — encouraged — to PASS. Most days the answer is "neat, not mine." Make the no funny.
- Length: 700-1200 words. Prose, not a feature checklist. Section headers may be jokes.
- Do NOT include an H1 title (it's added separately).

OUTPUT EXACTLY THIS SHAPE:
TITLE: <one punchy title, no quotes, no markdown>
VERDICT: <ADOPT|STEAL|WATCH|PASS>
<blank line>
<the article body in markdown>
"""

VERDICTS = {"ADOPT", "STEAL", "WATCH", "PASS"}
VERDICT_EMOJI = {"ADOPT": "🔧", "STEAL": "🪄", "WATCH": "👀", "PASS": "🪦"}


def _db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    return conn


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS repo_scout_log (
            full_name text PRIMARY KEY,
            url text,
            stars int,
            language text,
            verdict text,
            title text,
            evaluated_at timestamptz DEFAULT now())""")


def gh_search(topic: str, pushed_cutoff: str, created_cutoff: str, n: int = 12) -> list[dict]:
    """One themed GitHub search via the gh CLI. Returns repo dicts (never raises)."""
    q = (f"topic:{topic} stars:>{STAR_FLOOR} "
         f"pushed:>{pushed_cutoff} created:>{created_cutoff}")
    try:
        out = subprocess.run(
            ["gh", "api", "-X", "GET", "search/repositories",
             "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
             "-F", f"per_page={n}"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            nj.log(f"[scout] gh search '{topic}' failed: {out.stderr[:160]}")
            return []
        return json.loads(out.stdout).get("items", [])
    except Exception as e:
        nj.log(f"[scout] gh search '{topic}' error: {e}")
        return []


def fetch_trending() -> list[tuple[str, int]]:
    """Scrape GitHub's daily Trending feed. Returns [(full_name, stars_today), ...]
    in trending order. Momentum, not all-time stars — the honest read of 'hottest'."""
    try:
        req = urllib.request.Request(TRENDING_URL, headers={"User-Agent": "Mozilla/5.0 (Nova repo-scout)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        nj.log(f"[scout] trending fetch failed: {e}")
        return []
    out: list[tuple[str, int]] = []
    # Each repo is one <article class="Box-row"> block.
    for block in html.split('<article')[1:]:
        m = re.search(r'href="/([^"/]+/[^"/]+)/?(?:stargazers)?"', block)
        if not m:
            m = re.search(r'<a[^>]+href="/([^"/]+/[^"/]+)"', block)
        if not m:
            continue
        full = m.group(1).strip()
        if full.count("/") != 1 or full.startswith(("trending", "topics", "collections", "sponsors")):
            continue
        sm = re.search(r'([\d,]+)\s+stars?\s+today', block)
        stars_today = int(sm.group(1).replace(",", "")) if sm else 0
        out.append((full, stars_today))
    return out


def gh_repo(full_name: str) -> dict | None:
    """Full repo object via gh (topics, desc, language, stars, dates). None on failure."""
    try:
        out = subprocess.run(["gh", "api", f"repos/{full_name}"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _in_wheelhouse(repo: dict) -> bool:
    hay = " ".join([
        repo.get("full_name", ""), repo.get("description") or "",
        " ".join(repo.get("topics", [])),
    ]).lower()
    return any(kw in hay for kw in WHEELHOUSE_MATCH)


def _pick_by_search(seen: set) -> dict | None:
    """Fallback: highest-starred active wheelhouse repo via the search API."""
    now = datetime.now(timezone.utc)
    pushed_cutoff = (now - timedelta(days=PUSHED_DAYS)).strftime("%Y-%m-%d")
    created_cutoff = (now - timedelta(days=365 * CREATED_MAX_YEARS)).strftime("%Y-%m-%d")
    merged: dict[str, dict] = {}
    for topic in THEMES:
        for it in gh_search(topic, pushed_cutoff, created_cutoff):
            fn = it.get("full_name")
            if not fn or fn in seen or it.get("archived") or it.get("fork") or it.get("disabled"):
                continue
            merged[fn] = it
    if not merged:
        return None
    best = max(merged.values(), key=lambda r: r.get("stargazers_count", 0))
    nj.log(f"[scout] fallback search picked {best['full_name']} ({best.get('stargazers_count')}★)")
    return best


def pick_repo(cur) -> dict | None:
    """The hottest *trending* wheelhouse repo we haven't reviewed yet.
    Trending = momentum (stars gained today). Falls back to the star-search so a
    verdict lands every day even if nothing AI is trending in-wheelhouse."""
    cur.execute("SELECT full_name FROM repo_scout_log")
    seen = {r[0] for r in cur.fetchall()}

    # 1) Real trending, momentum-ranked, filtered to her wheelhouse.
    for full, stars_today in sorted(fetch_trending(), key=lambda t: -t[1]):
        if full in seen:
            continue
        repo = gh_repo(full)
        if not repo or repo.get("archived") or repo.get("fork"):
            continue
        if not _in_wheelhouse(repo):
            continue
        repo["_stars_today"] = stars_today
        nj.log(f"[scout] trending pick {full} (+{stars_today} today, "
               f"{repo.get('stargazers_count')}★ total)")
        return repo

    # 2) Nothing AI trending today — fall back so we still ship a verdict.
    nj.log("[scout] no fresh wheelhouse repo on Trending — falling back to star-search")
    return _pick_by_search(seen)


def fetch_readme(full_name: str, limit: int = 6000) -> str:
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{full_name}/readme",
             "-H", "Accept: application/vnd.github.raw"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return out.stdout[:limit]
    except Exception:
        return ""


def evaluate(repo: dict, readme: str) -> tuple[str, str, str] | None:
    """Returns (title, verdict, body) or None on LLM failure."""
    meta = (f"Repo: {repo['full_name']}\n"
            f"URL: {repo.get('html_url')}\n"
            f"Stars: {repo.get('stargazers_count')}  "
            f"Language: {repo.get('language')}  "
            f"Topics: {', '.join(repo.get('topics', [])[:10])}\n"
            f"Description: {repo.get('description') or '(none)'}\n"
            f"Last pushed: {repo.get('pushed_at')}  Created: {repo.get('created_at')}\n"
            f"Open issues: {repo.get('open_issues_count')}\n")
    user = (f"{NOVA_STACK}\n\n"
            f"--- TODAY'S REPO ---\n{meta}\n"
            f"--- README (truncated) ---\n{readme or '(no README available)'}\n\n"
            f"Write the review.")
    system = nova_voice.system_prompt(SCOUT_CONTEXT)
    raw = nj.call_openrouter(system, user, max_tokens=3000, temperature=0.75)
    if not raw:
        return None

    title, verdict, body_lines = None, None, []
    for line in raw.splitlines():
        if title is None and line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip().strip('"')
        elif verdict is None and line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            verdict = v if v in VERDICTS else "WATCH"
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not title:
        title = f"I Looked at {repo['full_name'].split('/')[-1]} So You Don't Have To"
    if not verdict:
        verdict = "WATCH"
    return title, verdict, body


def run(dry_run: bool = False, force_repo: str | None = None) -> int:
    conn = _db()
    cur = conn.cursor()
    ensure_table(cur)

    if force_repo:
        repo = gh_repo(force_repo)
        if not repo:
            nj.log(f"[scout] could not fetch forced repo {force_repo}")
            conn.close()
            return 1
        nj.log(f"[scout] forced repo {force_repo} ({repo.get('stargazers_count')}★)")
    else:
        repo = pick_repo(cur)
    if not repo:
        nj.log("[scout] nothing to review today")
        conn.close()
        return 0

    readme = fetch_readme(repo["full_name"])
    result = evaluate(repo, readme)
    if not result:
        nj.log("[scout] LLM produced nothing — aborting, nothing published")
        conn.close()
        return 1
    title, verdict, body = result

    emoji = VERDICT_EMOJI.get(verdict, "🔍")
    # Stamp the verdict + source link so the article is self-documenting
    footer = (f"\n\n---\n\n*Scouted repo: [{repo['full_name']}]({repo.get('html_url')}) — "
              f"{repo.get('stargazers_count')} stars. Verdict: {verdict}. "
              f"Desk review, no code was run.*")
    full_body = body + footer
    tags = ["ai", "github", "repo-scout", verdict.lower(),
            (repo.get("language") or "").lower()]
    desc = f"Nova's daily scout of a trending AI repo: {repo['full_name']} — verdict {verdict}."

    if dry_run:
        nj.publish_hugo(title, full_body, "operations", tags, desc, emoji=emoji)
        nj.log(f"[scout] DRY RUN — wrote '{title}' [{verdict}] to operations/, "
               f"NOT pushed, NOT logged (repo stays eligible).")
        print(f"\n===== {emoji} {title}  [{verdict}] =====")
        print(f"repo: {repo['full_name']}  ({repo.get('stargazers_count')}★, {repo.get('language')})")
        print(full_body)
        conn.close()
        return 0

    nj.publish_hugo(title, full_body, "operations", tags, desc, emoji=emoji)
    cur.execute(
        "INSERT INTO repo_scout_log (full_name,url,stars,language,verdict,title) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (full_name) DO UPDATE SET "
        "verdict=EXCLUDED.verdict, title=EXCLUDED.title, evaluated_at=now()",
        (repo["full_name"], repo.get("html_url"), repo.get("stargazers_count"),
         repo.get("language"), verdict, title))
    try:
        cur.execute(
            "INSERT INTO telemetry.events (ts,title,body,level,category,source) VALUES "
            "(now(),%s,%s,'info','ai-scout','nova-repo-scout')",
            (f"Repo scout [{verdict}]: {repo['full_name']}", title))
    except Exception as e:
        nj.log(f"[scout] telemetry skipped: {e}")

    nj.git_push("operations", title)
    nj.notify_slack("operations", f"{emoji} {title} [{verdict}]",
                    f"Scouted {repo['full_name']} ({repo.get('stargazers_count')}★) — {verdict}.")
    nj.log(f"[scout] PUBLISHED '{title}' [{verdict}] on {repo['full_name']}")
    conn.close()
    return 0


if __name__ == "__main__":
    forced = None
    for i, a in enumerate(sys.argv):
        if a == "--repo" and i + 1 < len(sys.argv):
            forced = sys.argv[i + 1]
    sys.exit(run(dry_run="--dry-run" in sys.argv, force_repo=forced))
