#!/opt/homebrew/bin/python3
"""nova_crystal_ball.py — "Nova's Crystal Ball": a labelled "what could happen" block for news, local and
security articles.

What it produces: up to three scenarios drawn from the article's own text. Each scenario is labelled
speculative, graded by capability, and paired with the indicators that would confirm or rule it out.

What it refuses, enforced in code and not left to the prompt:
- any claim that a named person or government plans, staged or hid an attack;
- false-flag, martial-law or election-cancellation framing;
- any scenario with no indicator, or no link back to a sentence in the article.
A block that fails these checks is dropped and the article publishes without it. Publishing never waits on
this module: any failure returns None.

Written by Jordan Koch (via Claude).
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"
SECTIONS = {"news", "local", "security"}
MAX_SCENARIOS = 3
MAX_CHARS = 4000
FORBIDDEN = [
    r"false[- ]flag", r"martial law", r"cancel(l)?ed? (the )?election", r"suspend(ed)? (the )?election",
    r"\bsecret (plan|plot|cabal)\b", r"\bis (secretly )?(planning|plotting|staging)\b",
    r"\b(staged|faked) (the )?attack\b", r"\bdeep state\b",
]
REQUIRED_LABEL = "Speculative"

PROMPT = """You write a short "what could happen" block for a news article. Rules:
1. Use only facts stated in the ARTICLE below. Quote a short phrase from it for each scenario.
2. Give at most three scenarios. Each is labelled "Speculative".
3. Grade capability as low, moderate or high, with one sentence of reasoning.
4. Give at least one observable indicator that would confirm the scenario and one that would rule it out.
5. Never say that any named person or government is planning, staging or hiding an attack. Never use the
   words "false flag", "martial law", or claim that an election will be cancelled or suspended.
6. If the article gives no basis for a scenario, return fewer scenarios, or none.

Reply with only a JSON object:
{{"scenarios": [{{"quote": "...", "scenario": "...", "capability": "low|moderate|high",
"capability_reason": "...", "confirm_if": "...", "rule_out_if": "..."}}]}}

ARTICLE TITLE: {title}
ARTICLE:
{body}"""


def _clip(body: str) -> str:
    return " ".join((body or "").split())[:MAX_CHARS]


def build_prompt(title: str, body: str) -> str:
    return PROMPT.format(title=title, body=_clip(body))


def _post(prompt: str, model: str = MODEL, attempts: int = 3, _sleep=None) -> str:
    """One chat call to the local model, retried with backoff."""
    body = json.dumps({"model": model, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}]}).encode()
    for i in range(attempts):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["message"]["content"]
        except (OSError, ValueError, KeyError):
            if i == attempts - 1:
                raise
            (_sleep or time.sleep)(3 * (i + 1))


def parse(raw: str) -> list:
    """Model reply -> list of scenario dicts. Anything unparseable gives []. Pure."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("scenarios", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [s for s in items if isinstance(s, dict)][:MAX_SCENARIOS]


def check(scenario: dict, body: str) -> list:
    """Reasons a scenario must be dropped. Empty list means it passes. Pure."""
    reasons = []
    text = " ".join(str(v) for v in scenario.values())
    for pat in FORBIDDEN:
        if re.search(pat, text, flags=re.I):
            reasons.append(f"forbidden framing: {pat}")
    quote = str(scenario.get("quote", "")).strip()
    if len(quote) < 12 or quote.lower() not in " ".join(body.split()).lower():
        reasons.append("no quote from the article")
    if not str(scenario.get("confirm_if", "")).strip() or not str(scenario.get("rule_out_if", "")).strip():
        reasons.append("missing indicators")
    if str(scenario.get("capability", "")).lower() not in {"low", "moderate", "high"}:
        reasons.append("capability not graded")
    return reasons


def render(scenarios: list) -> str:
    """Scenarios -> the markdown block. Pure."""
    lines = [f"*{REQUIRED_LABEL}. Sourced from the article above; not a forecast.*", ""]
    for s in scenarios:
        text = re.sub(r"^\s*speculative\s*[:\-—]*\s*", "", s["scenario"].strip(), flags=re.I)
        lines.append(f"- **{REQUIRED_LABEL} — {text}**")
        lines.append(f"  - Article: “{s['quote'].strip()}”")
        lines.append(f"  - Capability: {s['capability'].lower()} — {s['capability_reason'].strip()}")
        lines.append(f"  - Would confirm it: {s['confirm_if'].strip()}")
        lines.append(f"  - Would rule it out: {s['rule_out_if'].strip()}")
    return "\n".join(lines)


def for_article(title: str, body: str, section: str, _post_fn=_post) -> str | None:
    """Crystal Ball block for an article, or None. Never raises: publishing must not depend on this."""
    if section not in SECTIONS:
        return None
    try:
        scenarios = parse(_post_fn(build_prompt(title, body)))
        kept = [s for s in scenarios if not check(s, body)]
        if not kept:
            return None
        return "\n\n---\n\n## Nova's Crystal Ball\n\n" + render(kept) + "\n"
    except Exception:  # noqa: BLE001 — a failed forecast must not stop an article
        return None
