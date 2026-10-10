#!/opt/homebrew/bin/python3
"""nova_directive_review.py — asks a local model, pair by pair, whether two standing feedback rules conflict.

Every pair of feedback rules in claude_memories is checked. Each check sends the two rules to a local Ollama
model and asks for a verdict in JSON. Verdicts are stored in nova_ops.directive_conflict_reviews. A conflict
is only a candidate: the model can be wrong, so a person resolves it. Nothing here edits a rule.

Usage: nova_directive_review.py [--model qwen3:8b] [--limit N] [--dry-run] [--notify]
Written by Jordan Koch (via Claude).
"""
import argparse
import itertools
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()
OLLAMA = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3:8b"
MAX_CHARS = 700

PROMPT = """You are checking two standing instructions for an AI assistant for conflict.

A conflict means the two cannot both be followed in the same situation: one requires an action the other
forbids, or they give different answers about whether to ask, publish, alert or act. Different topics, or rules
that merely mention the same subject without contradicting each other, are NOT a conflict.

RULE A ({a_name}):
{a_text}

RULE B ({b_name}):
{b_text}

Reply with only a JSON object: {{"conflict": true or false, "reason": "one short sentence"}}"""


def clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:MAX_CHARS]


def build_prompt(a: tuple, b: tuple) -> str:
    return PROMPT.format(a_name=a[0], a_text=clip(a[1]), b_name=b[0], b_text=clip(b[1]))


def parse_verdict(raw: str) -> dict:
    """Model reply -> {"conflict": bool, "reason": str}. Anything unparseable is 'not a conflict' with an error
    note, so a broken reply never raises a false alarm on its own. Pure."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {"conflict": False, "reason": "unparseable reply", "error": True}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"conflict": False, "reason": "unparseable reply", "error": True}
    return {"conflict": bool(obj.get("conflict")), "reason": str(obj.get("reason", ""))[:300], "error": False}


def pairs(rules: list) -> list:
    return list(itertools.combinations(rules, 2))


def ask_model(prompt: str, model: str = DEFAULT_MODEL, attempts: int = 3, _sleep=None) -> str:
    """One chat call to the local model, retried with backoff on transient failures."""
    import time
    body = json.dumps({"model": model, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for i in range(attempts):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["message"]["content"]
        except (OSError, ValueError, KeyError):
            if i == attempts - 1:
                raise
            (_sleep or time.sleep)(3 * (i + 1))


def review(pair: tuple, model: str = DEFAULT_MODEL, _ask=ask_model) -> dict:
    a, b = pair
    try:
        verdict = parse_verdict(_ask(build_prompt(a, b), model))
    except Exception as e:  # noqa: BLE001 — one failed call must not stop the review
        verdict = {"conflict": False, "reason": f"model call failed: {e}", "error": True}
    return {"a": a[0], "b": b[0], **verdict}


def connect(attempts: int = 3):
    """Connect to nova_ops with retry, so a dropped connection cannot lose a finished review."""
    return _nova_dsn.pg_connect(attempts=attempts)


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS directive_conflict_reviews (
        id bigserial PRIMARY KEY, reviewed_at timestamptz NOT NULL DEFAULT now(), model text NOT NULL,
        rule_a text NOT NULL, rule_b text NOT NULL, conflict boolean NOT NULL, reason text,
        error boolean NOT NULL DEFAULT false)""")
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="review only the first N pairs (0 = all)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="review, print, do not store or notify")
    ap.add_argument("--notify", action="store_true")
    a = ap.parse_args(argv)
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT name, content FROM claude_memories WHERE type = 'feedback' ORDER BY name")
    rules = cur.fetchall()
    todo = pairs(rules)
    if a.limit:
        todo = todo[:a.limit]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(lambda p: review(p, a.model), todo))
    conflicts = [r for r in results if r["conflict"]]
    errors = sum(1 for r in results if r["error"])
    print(f"reviewed {len(results)} pairs with {a.model}: {len(conflicts)} candidate conflict(s), {errors} error(s)")
    for r in conflicts:
        print(f"  {r['a']}  <->  {r['b']}: {r['reason']}")
    if not a.dry_run:
        conn = connect()
        ensure_table(conn)
        cur = conn.cursor()
        for r in results:
            cur.execute("""INSERT INTO directive_conflict_reviews (model, rule_a, rule_b, conflict, reason, error)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (a.model, r["a"], r["b"], r["conflict"], r["reason"], r["error"]))
        conn.commit()
    conn.close()
    if conflicts and a.notify and not a.dry_run:
        import nova_notify
        nova_notify.notify(
            f"{len(conflicts)} candidate rule conflict(s) for review",
            "\n".join(f"{r['a']} <-> {r['b']}: {r['reason']}" for r in conflicts),
            level="info", category="governance", source="nova_directive_review",
            dedup_key="directive-review-" + a.model + "-" + str(len(conflicts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
