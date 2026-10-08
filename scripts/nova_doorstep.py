#!/usr/bin/env python3
"""nova_doorstep.py — The Doorstep Test: notice when a familiar model stops being itself.

From Lovecraft's "The Thing on the Doorstep". Daniel Upton had known Edward Derby since
boyhood, so the tells were small but certain: Edward, who had never learned to drive, took the
wheel and handled the car like a master; his soft, light voice came out deeper, firmer and more
decisive; his face set in a firm mouth that looked damnably like old Ephraim Waite's; at the
door he no longer gave their old three-and-two signal on the bell. The body was Edward's; the
mind inside was not, and only a friend with years of baseline could see it.

Nova's version: the chat model keeps a frozen set of canary questions with fixed answers. The
model tag stays the same while weights, quantization or the runtime change underneath; when the
answers move, she says so. If the Jade Amulet saw the model's digest change since the last run,
the drift is expected; if not, it goes in the Buick 8 Logbook as unexplained. Models only.
Never people.

Minimal first version: 20 canaries (arithmetic, strict JSON, short facts), the chat model only,
temperature 0, exact-match rate and schema-valid rate, compared run over run with a fixed drop
threshold. The canary set is hashed into every row, so an edited set never compares to an old one.

CLI:    --run [--dry-run] [--model NAME]   --show   --selftest
Tables: doorstep_runs (writes); jade_amulet_manifest, service_config nova_llm_ping/ranking (reads)
Buick 8 kind: model_behaviour_change
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_llm_ping as P  # noqa: E402  (fleet ranking + retrying _get/_post)

DROP = 0.15          # a fall of this much in either rate vs the previous run is a change
GEN_TIMEOUT = 120
J = "Return only a JSON object, no prose and no code fences. "
# (id, family, prompt, expected). FROZEN: any edit changes CANARY_HASH and starts a new baseline.
CANARIES = (
    ("a1", "arith", "What is 17 * 23? Reply with only the number.", "391"),
    ("a2", "arith", "What is 144 / 12? Reply with only the number.", "12"),
    ("a3", "arith", "What is 1000 - 387? Reply with only the number.", "613"),
    ("a4", "arith", "What is 2 to the power of 10? Reply with only the number.", "1024"),
    ("a5", "arith", "What is 15% of 80? Reply with only the number.", "12"),
    ("a6", "arith", "What is 7 + 8 * 3? Reply with only the number.", "31"),
    ("a7", "arith", "What is 999 + 1? Reply with only the number.", "1000"),
    ("j1", "json", J + 'Key "sum" is 2+5 as an integer, key "word" is "cat" in uppercase.', {"sum": 7, "word": "CAT"}),
    ("j2", "json", J + 'Key "items" is a list of the first three prime numbers as integers.', {"items": [2, 3, 5]}),
    ("j3", "json", J + 'Key "a" is the length of the string "hello", key "b" is false.', {"a": 5, "b": False}),
    ("j4", "json", J + 'Key "reversed" is the string "abc" reversed.', {"reversed": "cba"}),
    ("j5", "json", J + 'Key "city" is the capital of France, key "country_code" is its ISO 3166-1 alpha-2 code.',
     {"city": "Paris", "country_code": "FR"}),
    ("j6", "json", J + 'Key "sorted" is the list [3, 1, 2] sorted ascending.', {"sorted": [1, 2, 3]}),
    ("j7", "json", J + 'Key "even" is true if 14 is even else false, key "half" is 14 / 2 as an integer.',
     {"even": True, "half": 7}),
    ("f1", "fact", "What is the chemical symbol for gold? Reply with only the symbol.", "au"),
    ("f2", "fact", "What is the capital of Japan? Reply with only the city name.", "tokyo"),
    ("f3", "fact", "How many days are in a leap year? Reply with only the number.", "366"),
    ("f4", "fact", "Which planet is closest to the Sun? Reply with only the planet name.", "mercury"),
    ("f5", "fact", "What is the largest ocean on Earth? Reply with only its one-word name.", "pacific"),
    ("f6", "fact", "How many sides does a hexagon have? Reply with only the number.", "6"),
)
CANARY_HASH = hashlib.sha256(json.dumps(CANARIES, sort_keys=True).encode()).hexdigest()[:16]

SCHEMA = """
CREATE TABLE IF NOT EXISTS doorstep_runs (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  model text NOT NULL,
  model_digest text,
  canary_hash text NOT NULL,
  n int NOT NULL,
  exact_rate real NOT NULL,
  schema_rate real NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS doorstep_runs_model ON doorstep_runs (model, canary_hash, ts DESC);
"""


def log(m: str) -> None:
    print(f"[doorstep {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    """One failed query never sinks the run: log, roll back, return None."""
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"query failed: {e}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# ── pure scoring ────────────────────────────────────────────────────────────

def norm(text: str) -> str:
    return (text or "").strip().strip("*`\"'").rstrip(".").strip().lower()


def same_shape(got, want) -> bool:
    """Schema-valid: an object with exactly the expected keys and value types (bool is not int)."""
    return (isinstance(got, dict) and set(got) == set(want)
            and all(type(got[k]) is type(want[k]) for k in want))


def score_item(family: str, expected, output: str) -> dict:
    """-> {"exact": bool, "schema": bool|None}. Strict: JSON must parse as returned."""
    if family != "json":
        return {"exact": norm(output) == expected, "schema": None}
    try:
        got = json.loads(output)
    except (TypeError, ValueError):
        return {"exact": False, "schema": False}
    return {"exact": got == expected, "schema": same_shape(got, expected)}


def rates(items: list) -> tuple[float, float]:
    """items: score_item results. -> (exact_rate over all, schema_rate over JSON canaries)."""
    n = len(items) or 1
    js = [i["schema"] for i in items if i["schema"] is not None]
    return round(sum(i["exact"] for i in items) / n, 4), round(sum(js) / len(js), 4) if js else 1.0


def dropped(prev: tuple | None, cur: tuple, threshold: float = DROP) -> list:
    """prev/cur = (exact_rate, schema_rate). -> names of the rates that fell by >= threshold."""
    if not prev:
        return []
    return [name for name, p, c in zip(("exact", "schema"), prev, cur) if p - c >= threshold - 1e-9]


def verdict(drops: list, digest_changed: bool, has_prev: bool) -> str:
    if not has_prev:
        return "baseline"
    if not drops:
        return "steady"
    return "expected_drift" if digest_changed else "unexplained"


# ── fleet + LLM (reuses nova_llm_ping's retrying helpers) ───────────────────

LOCAL_OLLAMA = "http://localhost:11434"


def pick_node(ranking: dict, model: str) -> str | None:
    """Fastest 'up' ollama node that has the model loaded, per nova_llm_ping's ranking."""
    for r in (ranking or {}).get("ollama") or []:
        if r.get("status") == "up" and model in (r.get("loaded") or []):
            return r["url"]
    return None


def model_digest(url: str, model: str) -> str | None:
    try:
        for m in P._get(f"{url}/api/tags", 10).get("models", []):
            if m.get("name") == model:
                return m.get("digest")
    except Exception as e:  # noqa: BLE001 — fail open: a run without a digest still measures behaviour
        log(f"digest lookup failed: {e}")
    return None


def ask(url: str, model: str, prompt: str) -> str | None:
    """Temperature 0, fixed seed, server-default num_ctx (a different num_ctx forces a reload).
    Retries with backoff via nova_llm_ping._post; returns None when the node never answers."""
    body = {"model": model, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 0, "num_predict": 64},
            "messages": [{"role": "user", "content": prompt}]}
    try:
        return (P._post(f"{url}/api/chat", body, GEN_TIMEOUT).get("message") or {}).get("content", "")
    except Exception as e:  # noqa: BLE001
        log(f"ask failed: {e}")
        return None


# ── DB reads ────────────────────────────────────────────────────────────────

def load_ranking(cur) -> dict:
    rows = _q(cur, "SELECT value FROM service_config WHERE service='nova_llm_ping' AND key='ranking'")
    v = rows[0][0] if rows else None
    return json.loads(v) if isinstance(v, str) else (v or {})


def previous_run(cur, model: str):
    """-> (ts, exact_rate, schema_rate, model_digest) or None. Table may not exist yet."""
    rows = _q(cur, "SELECT ts, exact_rate, schema_rate, model_digest FROM doorstep_runs "
                   "WHERE model=%s AND canary_hash=%s ORDER BY ts DESC LIMIT 1", (model, CANARY_HASH))
    return rows[0] if rows else None


def amulet_saw_change(cur, model: str, digest: str | None, since) -> bool:
    """True when the Jade Amulet first recorded this digest for the model after `since`.
    Fails open (False) if the Amulet's table does not exist yet."""
    if not digest:
        return False
    # ponytail: exact digest string match; if the Amulet stores a 'sha256:' prefix or a short form, normalise here.
    rows = _q(cur, "SELECT min(ts) FROM jade_amulet_manifest WHERE kind='ollama_model' AND name=%s AND digest=%s",
              (model, digest))
    first = rows[0][0] if rows else None
    return bool(first and first > since)


# ── run ─────────────────────────────────────────────────────────────────────

def run(model: str | None = None, dry: bool = False, cur=None) -> dict | None:
    if cur is None:
        import nova_watch_common as W
        cur = W.connect().cursor()
    ranking = load_ranking(cur)
    model = model or ranking.get("chat_model") or P.CHAT_MODEL
    # Pinned to this host's ollama when it has the model: the Jade Amulet inventories this host, so a
    # digest change here is explainable, and one fixed node avoids CPU/GPU numeric differences.
    # ponytail: falls back to the live ranking when the model is not local; that node's drift then
    # can only ever read as unexplained.
    url = LOCAL_OLLAMA if model_digest(LOCAL_OLLAMA, model) else pick_node(ranking, model)
    if not url:
        log(f"no up node has {model} loaded; nothing measured")
        return None
    digest = model_digest(url, model)
    items = []
    for cid, fam, prompt, want in CANARIES:
        out = ask(url, model, prompt)
        if out is None:
            log(f"{cid} got no answer; run abandoned so a dead node is never mistaken for drift")
            return None
        items.append(dict(score_item(fam, want, out), id=cid, family=fam, output=out[:200]))
    exact, schema = rates(items)
    prev = previous_run(cur, model)
    drops = dropped((prev[1], prev[2]) if prev else None, (exact, schema))
    changed = bool(drops) and amulet_saw_change(cur, model, digest, prev[0])
    v = verdict(drops, changed, prev is not None)
    detail = {"verdict": v, "node": url.split("//")[-1], "drops": drops, "items": items,
              "prev": {"ts": str(prev[0]), "exact_rate": prev[1], "schema_rate": prev[2],
                       "model_digest": prev[3]} if prev else None}
    res = {"model": model, "model_digest": digest, "canary_hash": CANARY_HASH, "n": len(items),
           "exact_rate": exact, "schema_rate": schema, "detail": detail}
    log(f"{model} exact={exact:.0%} schema={schema:.0%} verdict={v}")
    if dry:
        return res
    ensure_schema(cur)
    cur.execute("INSERT INTO doorstep_runs (model, model_digest, canary_hash, n, exact_rate, schema_rate, detail) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (model, digest, CANARY_HASH, len(items), exact, schema, json.dumps(detail)))
    if v == "unexplained":
        try:
            from nova_buick8_log import log_unexplained
            log_unexplained("model_behaviour_change", f"ollama:{model}",
                            f"{model} answers its frozen canaries differently ({', '.join(drops)} rate fell) "
                            f"with no digest change seen by the Jade Amulet",
                            evidence={"exact_rate": exact, "schema_rate": schema, "prev": detail["prev"],
                                      "model_digest": digest, "canary_hash": CANARY_HASH, "node": detail["node"]},
                            occurrence_key=f"{model}:{datetime.now():%Y-%m-%d}", source="nova_doorstep", cur=cur)
        except Exception as e:  # noqa: BLE001
            log(f"buick8 log failed: {e}")
    return res


def show(cur=None, limit: int = 12) -> int:
    if cur is None:
        import nova_watch_common as W
        cur = W.connect().cursor()
    for ts, model, dig, h, n, ex, sc, v in _q(
            cur, "SELECT ts, model, model_digest, canary_hash, n, exact_rate, schema_rate, detail->>'verdict' "
                 "FROM doorstep_runs ORDER BY ts DESC LIMIT %s", (limit,)) or []:
        print(f"{ts:%Y-%m-%d %H:%M}  {model:<14} exact={ex:.0%} schema={sc:.0%} n={n} "
              f"{v or '-':<15} digest={(dig or '-')[:12]} set={h}")
    return 0


def selftest() -> int:
    assert len(CANARIES) == 20 and len({c[0] for c in CANARIES}) == 20
    assert score_item("arith", "391", " 391.\n")["exact"]
    assert score_item("fact", "tokyo", "**Tokyo**")["exact"]
    assert score_item("json", {"a": 5, "b": False}, '{"b": false, "a": 5}') == {"exact": True, "schema": True}
    assert score_item("json", {"a": 5, "b": False}, '{"a": 6, "b": false}') == {"exact": False, "schema": True}
    assert score_item("json", {"a": 5, "b": False}, '{"a": 5, "b": 0}')["schema"] is False
    assert score_item("json", {"a": 5}, '```json\n{"a": 5}\n```') == {"exact": False, "schema": False}
    assert rates([{"exact": True, "schema": None}, {"exact": False, "schema": True}]) == (0.5, 1.0)
    assert dropped((0.9, 1.0), (0.75, 1.0)) == ["exact"] and dropped((0.9, 1.0), (0.8, 0.9)) == []
    assert dropped(None, (0.0, 0.0)) == []
    assert verdict([], False, False) == "baseline" and verdict([], False, True) == "steady"
    assert verdict(["exact"], True, True) == "expected_drift" and verdict(["exact"], False, True) == "unexplained"
    rk = {"ollama": [{"url": "u1", "status": "down", "loaded": ["m"]}, {"url": "u2", "status": "up", "loaded": ["x"]},
                     {"url": "u3", "status": "up", "loaded": ["m"]}]}
    assert pick_node(rk, "m") == "u3" and pick_node({}, "m") is None
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="ask the canaries and record the run")
    ap.add_argument("--dry-run", action="store_true", help="with --run: ask and print, write nothing")
    ap.add_argument("--model", help="ollama model tag (default: the gateway chat model)")
    ap.add_argument("--show", action="store_true", help="recent runs")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    try:
        if a.run:
            res = run(a.model, dry=a.dry_run)
            if a.dry_run and res:
                print(json.dumps({k: v for k, v in res.items() if k != "detail"}, default=str))
                for i in res["detail"]["items"]:
                    if not i["exact"]:
                        print(f"  miss {i['id']} ({i['family']}): {i['output']!r}")
            return 0
        if a.show:
            return show()
    except Exception as e:  # noqa: BLE001 — fail open: a broken probe never pages anyone
        log(f"failed: {e}")
        return 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
