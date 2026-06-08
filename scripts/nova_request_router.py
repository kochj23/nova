#!/usr/bin/env python3
"""
nova_request_router.py — Unified Interface (#32)

Single entrypoint for all requests. Routes to Claude or Nova based on intent:
  - Code changes, debugging, architecture → Claude
  - Runtime actions, knowledge queries, monitoring → Nova
  - Ambiguous → both see it, first responder wins

Integrates with Nova gateway (ws://127.0.0.1:18789) and Claude queue.

Port: 37473 (HTTP API for routing decisions)
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
PORT = 37473

# ── Routing Rules ────────────────────────────────────────────────────────────

CLAUDE_SIGNALS = [
    r"\b(fix|debug|refactor|implement|build|write|code|script|edit|deploy|commit|push|PR|pull request)\b",
    r"\b(python|swift|ruby|javascript|typescript|sql|bash|zsh)\b",
    r"\b(function|class|method|variable|import|compile|lint|test)\b",
    r"\b(error|traceback|exception|stack trace|segfault|crash)\b",
    r"\b(git|github|branch|merge|rebase|checkout)\b",
    r"\b(cookbook|recipe|cinc|chef|ansible|terraform)\b",
    r"\b(API|endpoint|route|schema|migration|database)\b",
]

NOVA_SIGNALS = [
    r"\b(what|who|when|where|why|how|tell me|explain|recall|remember)\b",
    r"\b(play|record|watch|tune|channel|TV|music)\b",
    r"\b(weather|news|time|schedule|calendar|remind)\b",
    r"\b(light|thermostat|camera|door|lock|garage|homekit|home)\b",
    r"\b(slack|discord|signal|email|message|chat)\b",
    r"\b(status|health|uptime|monitor|check)\b",
    r"\b(journal|diary|blog|post|thought|muse)\b",
    r"\b(plex|movie|show|episode|season)\b",
]

BOTH_SIGNALS = [
    r"\b(investigate|look into|figure out|troubleshoot)\b",
    r"\b(why is .+ (down|broken|slow|failing))\b",
]


def classify_request(text):
    """
    Classify a request as claude, nova, or both.
    Returns: {"target": "claude"|"nova"|"both", "confidence": float, "reason": str}
    """
    text_lower = text.lower()
    claude_score = 0
    nova_score = 0
    both_score = 0

    for pattern in CLAUDE_SIGNALS:
        matches = re.findall(pattern, text_lower)
        claude_score += len(matches) * 2

    for pattern in NOVA_SIGNALS:
        matches = re.findall(pattern, text_lower)
        nova_score += len(matches) * 2

    for pattern in BOTH_SIGNALS:
        if re.search(pattern, text_lower):
            both_score += 5

    # Boost: if message starts with a command prefix
    if text.startswith("/") or text.startswith("!"):
        nova_score += 3
    if text.startswith("```") or "def " in text or "function " in text:
        claude_score += 5

    total = claude_score + nova_score + both_score
    if total == 0:
        return {"target": "nova", "confidence": 0.5, "reason": "default_to_nova"}

    if both_score > claude_score and both_score > nova_score:
        return {"target": "both", "confidence": both_score / total, "reason": "investigation_needed"}

    if claude_score > nova_score:
        conf = claude_score / total
        return {"target": "claude", "confidence": conf, "reason": "code_or_engineering_task"}
    else:
        conf = nova_score / total
        return {"target": "nova", "confidence": conf, "reason": "knowledge_or_runtime_task"}


def route_to_claude(text, context=None):
    """Queue a request for Claude."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO claude_queue (session_id, description, context, priority)
        VALUES ('router', %s, %s, 3)
        RETURNING id
    """, (text, json.dumps(context or {})))
    queue_id = cur.fetchone()[0]
    cur.close()
    conn.close()
    return queue_id


def route_to_nova(text, context=None):
    """Send request to Nova via gateway."""
    import urllib.request
    try:
        payload = json.dumps({
            "type": "request",
            "text": text,
            "source": "router",
            "context": context or {},
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:18789/api/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def route_request(text, source="unknown", context=None):
    """Main routing function — classify and dispatch."""
    classification = classify_request(text)
    target = classification["target"]

    # Log the routing decision
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO shared_observations (observer, category, subject, observation, metadata)
        VALUES ('router', 'routing', %s, %s, %s)
    """, (target, f"Routed from {source}: {text[:100]}",
          json.dumps({"classification": classification, "source": source})))
    cur.close()
    conn.close()

    result = {"classification": classification}

    if target == "claude":
        queue_id = route_to_claude(text, context)
        result["action"] = f"Queued for Claude (#{queue_id})"
    elif target == "nova":
        nova_resp = route_to_nova(text, context)
        result["action"] = "Sent to Nova"
        result["nova_response"] = nova_resp
    else:  # both
        queue_id = route_to_claude(text, context)
        nova_resp = route_to_nova(text, context)
        result["action"] = f"Sent to both (Claude #{queue_id})"
        result["nova_response"] = nova_resp

    return result


# ── HTTP API ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "service": "request_router"})
            elif parsed.path == "/classify":
                params = urllib.parse.parse_qs(parsed.query)
                text = params.get("q", [""])[0]
                if text:
                    self._json(200, classify_request(text))
                else:
                    self._json(400, {"error": "missing ?q= parameter"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if self.path == "/route":
                text = body.get("text", "")
                source = body.get("source", "api")
                context = body.get("context")
                if not text:
                    self._json(400, {"error": "missing text"})
                    return
                result = route_request(text, source, context)
                self._json(200, result)
            elif self.path == "/classify":
                text = body.get("text", "")
                self._json(200, classify_request(text))
            else:
                self._json(404, {"error": "not found"})

        def _json(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())

        def log_message(self, *args):
            pass

    print(f"Request Router listening on port {PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
