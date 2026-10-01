#!/usr/bin/env python3
"""
nova_correlator.py — event correlation for Nova's notification bus.

Turns alert STORMS into INCIDENTS. When a new event arrives, decide whether it's
a fresh problem or part of one already happening, and fold symptoms under their
root cause so you get ONE evolving alert instead of fifteen.

Three layers, deterministic first (so it works even when the GPU it's diagnosing
is wedged), then Nova's local LLMs enrich:
  1. TOPOLOGY  — dependency map: a root cause (gpu, host_down, switch_port)
                 suppresses its known downstream symptoms on the same host.
  2. TEMPORAL  — events on the same host inside a window join the open incident.
  3. SEMANTIC  — nomic-embed-text: events whose text embeds near an open
                 incident's centroid attach even without a rule.
  + LLM SUMMARY — qwen3-coder:30b writes the incident's root-cause/symptom/action blurb.

Used by nova_notifier; safe to import. Never raises into the caller.
"""
import json
import re
import urllib.request
import psycopg2.extensions


def _tuple_cur(conn):
    """Cursor that returns plain tuples regardless of the connection's default
    factory (the daemon uses RealDictCursor; this keeps our indexing consistent)."""
    return conn.cursor(cursor_factory=psycopg2.extensions.cursor)

OLLAMA = "http://127.0.0.1:11434"
SUMMARY_MODEL = "qwen3-coder:30b"
EMBED_MODEL = "nomic-embed-text"

# Same-host window in which events are considered part of one incident.
CORRELATION_WINDOW_S = 1800
SEMANTIC_THRESHOLD = 0.62

# Dependency topology: a root category, and the symptom categories it explains.
# If an open incident's root is here and a new event on the SAME host is one of
# its symptoms, the symptom is folded in (not alerted separately).
TOPOLOGY = {
    "host_down":   {"*"},  # host down explains everything on that host
    "gpu":         {"ollama", "inference", "memory_ingest", "embedding", "crash_storm"},
    "switch_port": {"wifi", "ap", "client_drop", "network"},
    "network":     {"wifi", "ap", "client_drop", "service_unreachable"},
    "storage":     {"backup", "write_error"},
    "postgres":    {"scheduler", "memory_ingest", "query_error"},
}
# Categories that tend to be a ROOT cause (open an incident) vs a symptom.
ROOT_CATEGORIES = set(TOPOLOGY.keys())


def _http(path, payload, timeout):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def embed(text):
    """nomic-embed-text vector, or None if unavailable."""
    try:
        d = _http("/api/embeddings", {"model": EMBED_MODEL, "prompt": text[:2000]}, 15)
        return d.get("embedding")
    except Exception:
        return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _host_of(ev):
    """Best-effort host/entity for an event: explicit meta, else an IP/hostname in text."""
    meta = ev.get("meta") or {}
    if isinstance(meta, str):
        try: meta = json.loads(meta)
        except Exception: meta = {}
    if meta.get("host"):
        return str(meta["host"])
    blob = f"{ev.get('title','')} {ev.get('body','')}"
    m = re.search(r"\b(192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)\b", blob)
    if m:
        return m.group(1)
    m = re.search(r"\b(Office-M4-2|nova-core|mac-studio|mac-mini|nuk|unas|synology|[a-z]+-u6e)\b", blob, re.I)
    return m.group(1) if m else None


def _is_symptom_of(root_cat, sym_cat):
    syms = TOPOLOGY.get(root_cat, set())
    return "*" in syms or sym_cat in syms


META_CATEGORIES = frozenset({"incident_recurring", "incident"})


def is_meta_category(category) -> bool:
    """Pure: is this event a report ABOUT incidents rather than a symptom of one?"""
    return (category or "") in META_CATEGORIES


def correlate(conn, ev):
    """Decide how event `ev` (a dict row) folds into the incident picture.

    Returns a dict:
      {action: 'standalone'|'attached'|'opened', incident_id, role, suppress}
    - 'standalone'  -> deliver the event normally (no correlation)
    - 'attached'    -> folded into an existing incident; suppress standalone alert
    - 'opened'      -> this event opened a new incident; deliver the incident alert
    """
    level = ev.get("level", "info")
    category = ev.get("category")
    host = _host_of(ev)

    # Only warnings/criticals with a host are incident-worthy; info is just FYI.
    if level == "info" or not host:
        return {"action": "standalone", "incident_id": None, "role": None, "suppress": False}
    # 2026-10-01: events that are themselves REPORTS about incidents (the lifecycle's
    # "Recurring incident pattern" warnings, its stale/auto-close notices) must never open
    # or join an incident — otherwise the detector feeds on its own output. Incident #3100
    # ("Office-M4-2:incident_recurring") ran 14 days and swallowed 2,479 of its own warnings.
    if is_meta_category(category):
        return {"action": "standalone", "incident_id": None, "role": None, "suppress": False}

    cur = _tuple_cur(conn)
    cur.execute(
        "SELECT id, title, severity, root_event, member_count, embedding, "
        "(SELECT category FROM telemetry.events WHERE id=i.root_event) AS root_cat "
        "FROM telemetry.incidents i WHERE status='open' AND host=%s "
        "AND updated_at > now() - make_interval(secs => %s) ORDER BY opened_at DESC",
        (host, CORRELATION_WINDOW_S))
    open_incidents = cur.fetchall()

    match = None
    for inc in open_incidents:
        inc_id, title, sev, root_event, mcount, emb, root_cat = inc
        # Layer 1 — topology: is this event a known symptom of the incident's root?
        if root_cat and _is_symptom_of(root_cat, category):
            match = (inc_id, "symptom"); break
        # Layer 1b — same category recurring on the host => same incident.
        if root_cat == category:
            match = (inc_id, "member"); break
    # Layer 3 — semantic: compare to open incidents' centroids.
    if not match and open_incidents:
        evec = embed(f"{ev.get('title','')} {ev.get('body','')}")
        if evec:
            best, best_sim = None, 0.0
            for inc in open_incidents:
                sim = _cosine(evec, inc[5])
                if sim > best_sim:
                    best, best_sim = inc[0], sim
            if best and best_sim >= SEMANTIC_THRESHOLD:
                match = (best, "member")

    if match:
        inc_id, role = match
        cur.execute(
            "UPDATE telemetry.incidents SET member_count = member_count + 1, updated_at = now(), "
            "severity = CASE WHEN %s='critical' THEN 'critical' ELSE severity END WHERE id=%s",
            (level, inc_id))
        cur.execute("UPDATE telemetry.events SET incident_id=%s, corr_role=%s, status='suppressed' "
                    "WHERE id=%s", (inc_id, role, ev["id"]))
        return {"action": "attached", "incident_id": inc_id, "role": role, "suppress": True}

    # No match. Open a NEW incident if this looks like a correlatable root/problem.
    evec = embed(f"{ev.get('title','')} {ev.get('body','')}")
    cur.execute(
        "INSERT INTO telemetry.incidents (status, severity, host, title, root_event, member_count, embedding) "
        "VALUES ('open', %s, %s, %s, %s, 1, %s) RETURNING id",
        (level, host, ev.get("title", "Incident")[:200], ev["id"], evec))
    inc_id = cur.fetchone()[0]
    cur.execute("UPDATE telemetry.events SET incident_id=%s, corr_role='root' WHERE id=%s",
                (inc_id, ev["id"]))
    return {"action": "opened", "incident_id": inc_id, "role": "root", "suppress": False}


def llm_summarize(conn, incident_id):
    """Ask Nova's local LLM to write a root-cause/symptom/action summary for an
    incident. Best-effort: returns (summary, model) or (templated_fallback, None)."""
    cur = _tuple_cur(conn)
    cur.execute("SELECT host, title, severity, member_count FROM telemetry.incidents WHERE id=%s",
                (incident_id,))
    row = cur.fetchone()
    if not row:
        return None, None
    host, title, sev, mcount = row
    cur.execute("SELECT level, category, title, body, corr_role FROM telemetry.events "
                "WHERE incident_id=%s ORDER BY ts ASC LIMIT 40", (incident_id,))
    members = cur.fetchall()
    bullet = "\n".join(f"- [{m[0]}/{m[1] or '?'}] ({m[4]}) {m[2]}" + (f" — {m[3][:120]}" if m[3] else "")
                       for m in members)
    fallback = f"{title} on {host}: {mcount} correlated events. Root + symptoms:\n{bullet}"
    prompt = (f"Host: {host}\nSeverity: {sev}\nCorrelated events:\n{bullet}\n\n"
              "Write the incident summary.")
    try:
        # /api/chat (not /api/generate) so the chat template applies and think:false
        # actually suppresses qwen3's reasoning. Output is clean prose.
        d = _http("/api/chat", {
            "model": SUMMARY_MODEL,
            "messages": [
                {"role": "system", "content":
                 "You are Nova's SRE correlation engine. Given a correlated incident, "
                 "reply with ONLY 2-3 tight sentences for an on-call engineer: the likely "
                 "ROOT CAUSE, the SYMPTOMS that follow from it, and ONE concrete next "
                 "action. No preamble, no reasoning, no lists — just the summary."},
                {"role": "user", "content": prompt},
            ],
            "stream": False, "think": False,
            "options": {"temperature": 0.2, "num_predict": 400},
        }, 45)
        txt = ((d.get("message") or {}).get("content") or "").strip()
        txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
        if len(txt) < 20:
            return fallback, None
        cur.execute("UPDATE telemetry.incidents SET summary=%s, llm_model=%s, updated_at=now() WHERE id=%s",
                    (txt, SUMMARY_MODEL, incident_id))
        return txt, SUMMARY_MODEL
    except Exception:
        cur.execute("UPDATE telemetry.incidents SET summary=%s WHERE id=%s", (fallback, incident_id))
        return fallback, None
