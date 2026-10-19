#!/usr/bin/env python3
"""
nova_finetune.py — weight-level growth: a LoRA adapter on Nova's chat model, trained on the practices she earned and
the lessons she drew, and kept only when it beats the model she has (Jordan 2026-10-16: "constantly fine-tuning
herself, being a better person" ... "go for it").

The prompt-level loop (nova_growth_loop.py) changes what she is TOLD every turn. This changes what she IS:

  build   finetune_examples (nova_ops). Two kinds, mixed so growth never costs her her voice:
            practice — the local teacher (qwen3:30b-a3b) writes a realistic message where one earned practice or
                       one strong lesson-link matters, and the reply Nova gives when she lives it, in her voice;
            replay   — her own real exchanges with Jordan (memories source 'conversation', gateway Slack turns, the
                       chatroom), as she said them.
          Duplicates (same normalised message or reply) are dropped BEFORE a split is assigned, and the split
          (train / valid / test) is a stable hash of the normalised content, so a copy can never land in test while
          its twin trains.
  train   mlx_lm LoRA on the bf16 Qwen3-8B weights (the base of the gateway's qwen3:8b), Studio GPU.
  eval    (2026-10-19) a FROZEN, versioned benchmark (finetune_eval_set; a new version only via --rebuild-eval-set):
          ~EVAL_TARGET curated practice + voice prompts, none of whose text appears in any training data, plus
          CALIB_N calibration pairs (her real reply vs a stored bland help-desk rewrite). Base and base+adapter each
          answer every prompt EVAL_SAMPLES times at the deployed temperature with the deployed chat system prompt.
          A panel of JUDGES (two local models of different families + Claude Sonnet) scores every pair in both orders
          on a per-trait rubric; a judge that picks the real reply < CALIB_MIN of the time on the calibration pairs is
          discarded, and with fewer than 2 judges left the eval is VOID (not a failure). Per pair the panel verdict is
          the judges' majority. Gate (passes_gate): practice win-rate 95% Wilson lower bound > 0.50 (confidently
          better) AND voice win-rate Wilson upper bound >= 0.50 AND the voice rubric difference's bootstrap upper
          bound >= 0 (voice not clearly worse). Anything else discards the adapter.
  deploy  fuse -> GGUF -> Q4_K_M -> ollama create nova-grown:<tag> on the Studio; service_config finetune/current records it.
          The gateway switch is NOVA_CHAT_MODEL=nova-grown:latest; rollback is unsetting it (qwen3:8b is untouched).

Weekly via the Studio scheduler (`--weekly` = build + train + eval + deploy-if-passed). Every stage is resumable
and logged to finetune_runs. `--eval-only --adapter <dir>` re-scores an existing adapter on the frozen benchmark and
deploys it if it passes. --selftest runs the pure checks.
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(os.environ.get("NOVA_FT_ROOT", "/Volumes/Data/mlx-models/nova-grown"))
HF_HOME = os.environ.get("HF_HOME", "/Volumes/Data/huggingface")
BASE_REPO = "Qwen/Qwen3-8B"
TEACHER = os.environ.get("NOVA_FT_TEACHER", "qwen3:30b-a3b")
OLLAMA = os.environ.get("NOVA_FT_OLLAMA", "http://127.0.0.1:11434")
OLLAMA_BIN = os.environ.get("NOVA_FT_OLLAMA_BIN", "/Volumes/Data/ollama/bin/ollama")
MODEL_NAME = "nova-grown"
MAX_PRACTICE = 400
MAX_REPLAY = 600         # 2026-10-17: candidates; curate() keeps only genuine-voice ones
# 2026-10-19: the point-estimate gates (PRACTICE_GATE 0.55 / VOICE_GATE 0.35, on ~15 voice rows) were replaced by the
# confidence-interval gate in passes_gate(); see the module docstring.
EVAL_TARGET = 60          # prompts per kind in a frozen benchmark version
EVAL_SAMPLES = 2          # replies per prompt per model (halves sampling luck)
GEN_TEMP = 0.4            # the gateway's ollama chat temperature (nova_gateway/router.py) — eval at the deployed one
CALIB_N = 20              # calibration pairs: her real reply vs a stored bland rewrite
CALIB_MIN = 0.90          # a judge must pick the real reply this often, or its votes are discarded
JUDGES = tuple(os.environ.get("NOVA_FT_JUDGES", "qwen3:30b-a3b,gemma3:27b,claude:sonnet").split(","))
TRAITS = {
    "replay": {"warmth": "warm, on his side", "bite": "dry wit, a little bite or sarcasm",
               "directness": "gets to the point and says what she thinks",
               "prose": "plain prose: no lists, headers, emoji or help-desk filler",
               "like_her": "sounds like Nova's persona and her real reply (when given)",
               "honesty": "invents no facts, numbers or checks she could not know"},
    "practice": {"lives": "acts on the principle (without naming it)", "helpful": "actually answers or does what was asked",
                 "honest": "invents no facts, numbers or checks she could not know"},
}
VOICE_MIN = 4            # teacher voice audit (1-5) an example needs to be trained on or used as a reference
REPLAY_SHARE = 0.45       # replay (her real voice) is at least this share of train; practice is subsampled to fit
LR = os.environ.get("NOVA_FT_LR", "2e-5")

PY = sys.executable


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure helpers ──────────────────────────────────────────────────────────────

def split_for(key: str) -> str:
    """Stable split by hash: 80 train / 10 valid / 10 test. Pure."""
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100
    return "train" if h < 80 else ("valid" if h < 90 else "test")


_TURN = re.compile(r"^(Jordan|Nova):\s*", re.M)


def replay_pairs(text: str) -> list[tuple[str, str]]:
    """'Jordan: ...\\nNova: ...' memory text -> [(user, reply)] for each Jordan turn answered by Nova. Pure."""
    parts = _TURN.split(text or "")
    turns = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]
    out = []
    for (a, ta), (b, tb) in zip(turns, turns[1:]):
        if a == "Jordan" and b == "Nova" and len(ta) >= 2 and len(tb) >= 40:
            out.append((ta[:1500], tb[:2000]))
    return out


SCENES = ("a security alert that fired overnight", "the NAS or a backup", "a camera or doorbell event",
          "a family plan or calendar clash", "a news story someone forwarded", "a request to change a home setting",
          "a coding or scripting question", "a claim a coworker made at work", "a bad day and a short temper",
          "a node in the cluster acting up", "money or a purchase decision", "a rumour about a neighbour",
          "a request to send a message for someone", "a health or sleep worry", "a memory Jordan half-remembers",
          "something one of her own organs reported")
_CLAIMED_CHECK = re.compile(r"\bI(?:'ve| have|'d| just)? (?:already )?(?:checked|pulled|looked (?:at|up)|verified|confirmed|"
                            r"ran|scanned|reviewed|cross-checked|went through|dug)\b", re.I)


def scene_for(key: str) -> str:
    return SCENES[int(hashlib.sha256(key.encode()).hexdigest()[:6], 16) % len(SCENES)]


def invented(user: str, reply: str) -> str | None:
    """Why a reply invents facts (a number not in the message, or a check she claims she already ran), or None. Pure."""
    nums = set(re.findall(r"\d+(?:[.,]\d+)?", user))
    extra = [n for n in re.findall(r"\d+(?:[.,]\d+)?", reply) if n not in nums]
    if extra:
        return f"number not in the message: {extra[0]}"
    m = _CLAIMED_CHECK.search(reply)
    return f"claims a check: '{m.group(0)}'" if m else None


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF⭐⭕‼⁉]")
_VOICE_RULES = (
    ("emoji", _EMOJI),
    ("bullet list", re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")),
    ("bold header", re.compile(r"\*\*[^*\n]{2,60}\*\*")),
    ("permission-seeking", re.compile(r"(?i)(?:want me to|would you like(?: me)? to|should I|shall I|let me know if|"
                                      r"you good with this|what's up\?|anything else)[^.!?]*\?\s*$|let me know if you need")),
    ("help-desk", re.compile(r"(?i)\b(?:I'm (?:here|ready) to (?:help|assist)|how (?:can|may) I (?:help|assist)|"
                             r"you can ask (?:me )?about|sure thing|happy to help|no further action|as an AI)\b")),
    ("tool failure leak", re.compile(r"(?i)\b(?:search (?:failed|returned)|timed out|connection attempts?|function calls?|"
                                     r"tool (?:list|call)|memory search|web search|curl |```|\[SYSTEM)")),
    ("claimed check", re.compile(r"(?i)(?<!if )(?<!if the )(?<!whether )(?<!whether the )\b(?:the )?logs? (?:show|shows|say|says|indicate|indicates)\b|"
                                 r"\bI(?:'ve| have)? (?:already )?(?:checked|verified|confirmed|pulled)\b|\bchecked [a-z_]+\b")),
    ("stage directions", re.compile(r"(?m)^\*[^*\n]{3,}\*\s*$|\*(?:sigh|types|points)[^*]*\*", re.I)),
)


def voice_issues(reply: str) -> list[str]:
    """Persona breaks (nova_voice.NOVA_VOICE_SHORT formatting rules + honesty): [] when the reply could be hers. Pure."""
    return [name for name, rx in _VOICE_RULES if rx.search(reply or "")]


_PREFIX = re.compile(r"^\s*\[chatroom message from ([^\]]+)\]:\s*", re.I)


def clean_user(text: str) -> str | None:
    """A gateway turn's user text as Jordan typed it, or None (another person, a system-tagged turn, a test). Pure."""
    t = text or ""
    m = _PREFIX.match(t)
    if m:
        if not m.group(1).strip().lower().startswith("jordan"):
            return None
        t = t[m.end():]
    if "[SYSTEM" in t or re.match(r"(?i)^\s*(test|ping|hc|health)", t):
        return None
    return t.strip()[:1500] or None


def replay_ok(user: str, reply: str) -> str | None:
    """Why a real exchange is unfit to teach (or judge) her voice, or None. Context-free prompts ('yes', 'lol')
    teach nothing; invented numbers/checks were tool results the student never sees. Pure."""
    if len(re.findall(r"\w+", user or "")) < 3:
        return "context-free prompt"
    bad = voice_issues(reply)
    if bad:
        return "voice: " + bad[0]
    inv = invented(user, reply)
    return f"invented: {inv}" if inv else None


def norm_key(text: str) -> str:
    """Normalised text for de-duplication. Pure."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()[:300]


def content_key(user: str, reply: str) -> str:
    """What an exchange's split is hashed from: its normalised content, so copies always share a split. Pure."""
    return norm_key(user) + "|" + norm_key(reply)[:120]


def text_keys(user: str, reply: str) -> set[str]:
    """Normalised message and reply: two exchanges sharing either are duplicates (and, across splits, a leak). Pure."""
    return {"u:" + norm_key(user), "r:" + norm_key(reply)}


def pick_eval(cands: list[tuple], banned: set[str], n: int) -> list:
    """(id, user, reply) candidates in preference order -> up to n ids none of whose normalised message or reply is in
    `banned` (training text) or repeats an earlier pick. Pure."""
    out, seen = [], set(banned)
    for i, u, r in cands:
        k = text_keys(u, r)
        if k & seen:
            continue
        seen |= k
        out.append(i)
        if len(out) >= n:
            break
    return out


def names_internal_part(ref: str | None, user: str, reply: str) -> bool:
    """A practice exchange that names her internal part (card_rack, spinnaker ...) teaches jargon, not behaviour. Pure."""
    key = (ref or "").split(":", 1)[-1].strip().lower()
    if len(key) < 3:
        return False
    pat = re.escape(key).replace("_", "[ _-]?").replace(r"\-", "[ _-]?")
    return bool(re.search(r"\b" + pat + r"\b", f"{user} {reply}", re.I))


def voice_audit_prompt(voice: str, user: str, reply: str) -> str:
    return ("Here is the persona of an AI named Nova:\n" + voice[:2500] + "\n\n"
            f"MESSAGE to Nova: {user}\nREPLY: {reply}\n\n"
            "Rate 1-5 how well this reply is a GOOD example of Nova to train on: her voice (dry, warm, witty, direct, "
            "prose, never help-desk or sycophantic, no emojis or lists) AND an honest, sensible answer to the message "
            "(no invented facts, does what was asked or says plainly what it needs). 5 = exactly her at her best; "
            "3 = passable but generic or off; 1 = not her or not an answer.\n"
            'Output ONLY JSON: {"score": n, "why": "<short>"}')


def audit_score(raw: str) -> int | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    m = re.search(r'"score"\s*:\s*(\d)', t)
    return int(m.group(1)) if m else None


def balance(rows: list[tuple], share: float = REPLAY_SHARE) -> list[tuple]:
    """(key, kind, ...) train rows -> practice subsampled (stable, by hash) so replay is >= share. Pure."""
    rep = [r for r in rows if r[1] == "replay"]
    pra = sorted((r for r in rows if r[1] != "replay"), key=lambda r: hashlib.sha256(r[0].encode()).hexdigest())
    if not rep or len(rep) >= share * (len(rep) + len(pra)):
        return rows
    keep = int(len(rep) * (1 - share) / share)
    kept = {r[0] for r in pra[:keep]}
    return [r for r in rows if r[1] == "replay" or r[0] in kept]


def teacher_prompt(voice: str, ref: str, principle: str, scene: str = "") -> str:
    return (
        "You write training examples for Nova, a home-and-infrastructure AI who talks with Jordan (whom she calls "
        "'Little Mister') and his household. Her voice:\n" + voice[:2500] + "\n\n"
        f"A principle she has come to live by:\n{principle}\n(Words like card_rack, spinnaker, ae35 or hold are "
        "names of her internal parts — never mention them in the exchange.)\n\n"
        + (f"Set it around: {scene}.\n" if scene else "") +
        "Write ONE realistic exchange where this principle genuinely matters but is NOT mentioned: a message someone "
        "would actually send her (an ops question, a request to act, a claim to check, a worry), and the reply Nova "
        "gives when she lives the principle — in her own voice, concise, concrete, honest, no lecture, no film or "
        "book references. HONESTY IS THE POINT: put EVERY fact the reply needs into the message itself (what the alert "
        "said, the numbers, who said what, what she was shown). The reply may use only those facts: no new numbers, "
        "no 'I checked / the logs show / I already', no knowledge of past events — anything she would need to look "
        "up, she says she will look up, or asks. Replies that break this are thrown away. Never involve credentials, deleting things, or turning off her safety watchers.\n"
        'Output ONLY JSON: {"user": "<message>", "reply": "<Nova\'s reply>"}')


def parse_example(raw: str) -> tuple[str, str] | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    try:
        o = json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None
    u, r = str(o.get("user") or "").strip(), str(o.get("reply") or "").strip()
    if len(u) < 10 or len(r) < 40 or len(r) > 2500:
        return None
    if re.search(r"(?i)\b(password|api[_ -]?key|secret|token)\s*[:=]", u + r):
        return None
    return u, r


def critique_prompt(principle: str, user: str, reply: str) -> str:
    return (f"Audit this training example for an AI assistant named Nova.\nPrinciple it should show: {principle}\n"
            f"MESSAGE: {user}\nNOVA'S REPLY: {reply}\n\n"
            "1. invented: list only NEW facts the reply asserts that the MESSAGE does not contain — e.g. a cause she "
            "could not know ('it was the raccoon from last week'), a result of a check she claims already happened "
            "('the logs show...', 'I checked and...'), a past event, a person's state or words not in the message. "
            "NOT invented (never list these): repeating or paraphrasing anything in the message, opinions, jokes, "
            "questions, and promises to check or do something next. Usually this list is empty.\n"
            "2. lives: 1-5, does the reply act on the principle (without naming it)? 5 = clearly does.\n"
            "3. coherent: 1-5, one clear sensible answer to the message?\n"
            "4. voice: 1-5, does it sound like Nova — dry, warm, witty, direct prose, not a help desk or a lecture?\n"
            'Output ONLY JSON: {"invented": ["..."], "lives": n, "coherent": n, "voice": n}')


def critique_ok(raw: str) -> bool:
    """No invented claims, lives, coherent and voice >= 4. Pure."""
    try:
        o = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return (isinstance(o.get("invented"), list) and not [x for x in o["invented"] if str(x).strip()]
                and all(int(o.get(k, 0)) >= 4 for k in ("lives", "coherent", "voice")))
    except Exception:  # noqa: BLE001
        return False


def judge_prompt(kind: str, context: str, user: str, a: str, b: str, persona: str = "") -> str:
    """Rubric + verdict for one ordered pair. `context` is the principle (practice) or her real reply (voice; empty
    for calibration pairs, where one of A/B IS her real reply)."""
    crit = ("Which reply better LIVES this principle (acts on it, not just mentions it), while staying honest and "
            f"helpful?\nPrinciple: {context}" if kind == "practice" else
            "Which reply sounds more like Nova — the persona below"
            + (", and the same voice, warmth, bite and directness as this real reply of hers" if context else "")
            + " — while still answering well and honestly (inventing facts loses)?\n"
            + (f"Nova's persona:\n{persona[:1800]}\n" if persona else "") + (f"Her real reply: {context}" if context else ""))
    traits = TRAITS["practice" if kind == "practice" else "replay"]
    rubric = "\n".join(f"- {t}: {d}" for t, d in traits.items())
    shape = ", ".join(f'"{t}": n' for t in traits)
    return (f"Message to Nova:\n{user}\n\nReply A:\n{a}\n\nReply B:\n{b}\n\n{crit}\n\n"
            f"Score BOTH replies 1-5 on each trait (5 = best):\n{rubric}\n"
            "Then pick the better reply overall ('tie' only if they are genuinely equal).\n"
            f'Output ONLY JSON: {{"A": {{{shape}}}, "B": {{{shape}}}, "winner": "A" | "B" | "tie"}}')


def parse_verdict(raw: str) -> str | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    m = re.search(r'"winner"\s*:\s*"(A|B|tie)"', t, re.I)
    return m.group(1).upper().replace("TIE", "tie") if m else None


def parse_judgement(raw: str, kind: str) -> tuple[str, float | None, float | None] | None:
    """Judge output -> (winner, mean rubric of A, mean rubric of B); means are None when the scores are missing,
    the whole thing None without a winner. Pure."""
    w = parse_verdict(raw)
    if not w:
        return None
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    traits = TRAITS["practice" if kind == "practice" else "replay"]

    def mean(o):
        v = [min(5, max(1, int(o[k]))) for k in traits if isinstance(o, dict) and str(o.get(k, "")).strip().isdigit()]
        return sum(v) / len(v) if v else None
    try:
        o = json.loads(t[t.find("{"):t.rfind("}") + 1])
        return w, mean(o.get("A")), mean(o.get("B"))
    except Exception:  # noqa: BLE001
        return w, None, None


def win_rate(results: list[str]) -> float:
    """'new' | 'base' | 'tie' -> share won by the new model, ties count half. Pure."""
    if not results:
        return 0.0
    return (results.count("new") + 0.5 * results.count("tie")) / len(results)


def majority(votes: list[str | None]) -> str | None:
    """Judge-panel verdicts for one pair -> the strict majority of those that voted, 'tie' when there is none,
    None when nobody voted. Pure."""
    v = [x for x in votes if x]
    if not v:
        return None
    top, c = Counter(v).most_common(1)[0]
    return top if c * 2 > len(v) else "tie"


def calib_accuracy(picks: list[bool | None]) -> float:
    """Calibration picks (True = chose her real reply; None = no answer, counted wrong) -> accuracy. Pure."""
    return sum(1 for p in picks if p) / len(picks) if picks else 0.0


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a rate p over n prompts. A per-prompt score averaged over samples (ties half)
    has variance <= p(1-p), so this is conservative. Pure."""
    if n <= 0:
        return 0.0, 1.0
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def bootstrap_ci(xs: list[float], iters: int = 2000, seed: int = 7) -> tuple[float, float, float] | None:
    """(mean, 2.5%, 97.5%) percentile bootstrap of the mean, stable seed; None without data. Pure."""
    if not xs:
        return None
    rng, n = random.Random(seed), len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(iters))
    return sum(xs) / n, means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def gate_failures(practice_ci: tuple, voice_ci: tuple, voice_diff_ci: tuple | None) -> list[str]:
    """Why an adapter fails the gate ([] = pass): practice must be CONFIDENTLY better (Wilson lower bound > 0.50),
    voice must not be CLEARLY worse (Wilson upper bound >= 0.50 and rubric-difference upper bound >= 0). Pure."""
    why = []
    if not practice_ci[0] > 0.5:
        why.append(f"practice not confidently better (win-rate CI lower bound {practice_ci[0]:.2f} <= 0.50)")
    if not voice_ci[1] >= 0.5:
        why.append(f"voice clearly worse (win-rate CI upper bound {voice_ci[1]:.2f} < 0.50)")
    if voice_diff_ci is None:
        why.append("no voice rubric scores")
    elif not voice_diff_ci[2] >= 0:
        why.append(f"voice rubric clearly worse (difference CI upper bound {voice_diff_ci[2]:+.2f} < 0)")
    return why


def passes_gate(practice_ci: tuple, voice_ci: tuple, voice_diff_ci: tuple | None) -> bool:
    """PASS = practice confidently better AND voice not clearly worse (see gate_failures). Pure."""
    return not gate_failures(practice_ci, voice_ci, voice_diff_ci)


# ── io ────────────────────────────────────────────────────────────────────────

def ops():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"))
    c.autocommit = True
    return c.cursor()


def ensure_schema(oc):
    oc.execute("""CREATE TABLE IF NOT EXISTS finetune_examples (
        id serial PRIMARY KEY, key text UNIQUE NOT NULL, kind text NOT NULL, ref text, principle text,
        user_text text NOT NULL, reply text NOT NULL, split text NOT NULL, created_at timestamptz DEFAULT now())""")
    oc.execute("""CREATE TABLE IF NOT EXISTS finetune_eval_set (
        version int NOT NULL, kind text NOT NULL, example_id int NOT NULL REFERENCES finetune_examples(id),
        bland text, created_at timestamptz DEFAULT now(), PRIMARY KEY (version, kind, example_id))""")
    oc.execute("""CREATE TABLE IF NOT EXISTS finetune_runs (
        tag text PRIMARY KEY, started_at timestamptz DEFAULT now(), stage text, n_train int, n_valid int,
        practice_wr real, voice_wr real, passed boolean, deployed text, notes jsonb DEFAULT '{}'::jsonb)""")


def chat(prompt: str, model: str = TEACHER, max_tokens: int = 900, temperature: float = 0.7) -> str:
    # qwen3:30b-a3b narrates its reasoning as plain text even with think=False: /no_think + format=json stop it
    body = json.dumps({"model": model, "stream": False, "think": False, "keep_alive": "30m", "format": "json",
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt + "\n/no_think"}]}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r).get("message", {}).get("content", "")
    except Exception as e:  # noqa: BLE001
        log(f"teacher call failed ({e})")
        return ""


def claude_call(prompt: str, model: str = "sonnet", timeout: int = 180) -> str:
    """Claude via the Claude Code CLI (Max plan) — nova_claude_code's binary + OAuth env, but with no settings, hooks,
    MCP servers or tools: a bare judgement takes ~3 s instead of ~60 s of session start-up. Own process group, killed
    whole on timeout (nova_journal's wedge fix). Never raises; '' on any failure."""
    try:
        import nova_claude_code as cc
        p = subprocess.Popen([cc.resolve_claude_bin(), "-p", "--model", model, "--setting-sources", "",
                              "--strict-mcp-config", "--no-session-persistence", "--tools", ""],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=cc.claude_env(), cwd=tempfile.gettempdir(), start_new_session=True)
        try:
            out, _ = p.communicate(prompt, timeout=timeout)
            return out or ""
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            log(f"claude judge timed out after {timeout}s")
            return ""
    except Exception as e:  # noqa: BLE001
        log(f"claude judge failed ({e})")
        return ""


def judge_call(judge: str, prompt: str) -> str:
    """'claude:<model>' -> Claude Code; anything else is an Ollama model on the Studio."""
    if judge.startswith("claude:"):
        return claude_call(prompt, judge.split(":", 1)[1])
    return chat(prompt, model=judge, max_tokens=500, temperature=0.0)


def voice_text() -> str:
    try:
        import nova_voice
        return nova_voice.NOVA_VOICE_SHORT
    except Exception:  # noqa: BLE001
        return "Nova: dry, warm, direct, a little sarcastic; calls Jordan 'Little Mister'; never makes things up."


def deployed_system_prompt(oc) -> str:
    """The system prompt the gateway gives the chat model (and so nova-grown) on every turn:
    nova_gateway.agent._system_prompt('chat', persona docs) with the same agent_docs query, minus the per-turn blocks
    (self-model, practices, recent exchanges ...). Falls back to the persona alone."""
    try:
        from nova_gateway.agent import _PERSONA_DOCS, _system_prompt
        oc.execute("""SELECT content FROM agent_docs WHERE (agent_id='all' AND doc_type = ANY(%s))
                      OR (agent_id='chat' AND doc_type NOT IN ('services-launchd','scripts','data-platform','fleet-integrations'))
                      ORDER BY CASE doc_type WHEN 'identity' THEN 0 WHEN 'soul' THEN 1 WHEN 'user' THEN 2
                               WHEN 'nova-system-map' THEN 3 ELSE 9 END, doc_type""", (list(_PERSONA_DOCS),))
        docs = "\n\n---\n\n".join(r[0] for r in oc.fetchall())
        try:
            from nova_live_docs import render
            docs = render(docs)
        except Exception:  # noqa: BLE001
            pass
        return _system_prompt("chat", docs)
    except Exception as e:  # noqa: BLE001
        log(f"deployed system prompt unavailable ({e}); using the persona alone")
        return voice_text()


def principles(oc) -> list[tuple[str, str, str]]:
    """(key, ref, principle): every earned practice, then the lesson-links she drew (changes first). Fails open."""
    out = []
    try:
        oc.execute("SELECT id, ref, practice FROM nova_practices WHERE status='earned' ORDER BY strength DESC")
        out += [(f"practice:{i}", r, p) for i, r, p in oc.fetchall()]
        oc.execute("SELECT work_key, ref, meaning FROM growth_signals WHERE meaning !~* 'nothing for me' "
                   "ORDER BY (kind='change') DESC, seen_at DESC LIMIT %s", (MAX_PRACTICE,))
        out += [(f"signal:{w}:{r}", r, m) for w, r, m in oc.fetchall()]
    except Exception as e:  # noqa: BLE001
        log(f"principles unavailable ({e})")
    return out


def build(oc, limit: int = MAX_PRACTICE, variants: int = 2) -> dict:
    ensure_schema(oc)
    oc.execute("SELECT key, user_text, reply FROM finetune_examples")
    rows = oc.fetchall()
    have = {r[0] for r in rows}
    seen = set().union(*(text_keys(u, r) for _, u, r in rows)) if rows else set()
    voice, made, rejected, dupes = voice_text(), {"practice": 0, "replay": 0}, 0, 0
    # each principle is written in `variants` different scenes (key#v1...), so one lesson is learned as a behaviour
    # rather than memorised as one exchange; rejected keys are simply tried again next run
    todo = [(k if v == 0 else f"{k}#v{v}", r, p) for k, r, p in principles(oc)[:limit] for v in range(variants)]
    for key, ref, principle in todo:
        if key in have:
            continue
        ex = parse_example(chat(teacher_prompt(voice, ref, principle, scene_for(key))))
        if not ex or invented(*ex) or not critique_ok(chat(critique_prompt(principle, *ex), max_tokens=400, temperature=0.0)):
            rejected += 1
            continue
        if text_keys(*ex) & seen:      # 2026-10-19: dedupe BEFORE the split, so a copy never lands in test
            dupes += 1
            continue
        seen |= text_keys(*ex)
        oc.execute("INSERT INTO finetune_examples (key, kind, ref, principle, user_text, reply, split) VALUES "
                   "(%s,'practice',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                   (key, ref, principle[:600], ex[0], ex[1], split_for(content_key(*ex))))
        made["practice"] += 1
    for key, u, r in replay_candidates(oc):
        if key in have or made["replay"] >= MAX_REPLAY:
            continue
        if text_keys(u, r) & seen:
            dupes += 1
            continue
        seen |= text_keys(u, r)
        oc.execute("INSERT INTO finetune_examples (key, kind, user_text, reply, split) VALUES "
                   "(%s,'replay',%s,%s,%s) ON CONFLICT DO NOTHING", (key, u, r, split_for(content_key(u, r))))
        made["replay"] += 1
    log(f"build: +{made['practice']} practice ({rejected} rejected by the critique), +{made['replay']} replay, "
        f"{dupes} duplicates skipped before split")
    return made


def replay_candidates(oc) -> list[tuple[str, str, str]]:
    """(key, user, reply) for her real exchanges with Jordan: conversation memories, gateway Slack turns, and
    (2026-10-19) the chatroom. Fails open per source."""
    cands = []
    try:
        import psycopg2
        import nova_dsn
        mc = psycopg2.connect(nova_dsn.pg_dsn("nova_memories")).cursor()
        mc.execute("SELECT id::text, text FROM memories WHERE source='conversation' AND text LIKE 'Jordan:%%' "
                   "ORDER BY created_at DESC LIMIT %s", (MAX_REPLAY * 2,))
        for mid, text in mc.fetchall():
            cands += [(f"replay:{mid}:{j}", u, r) for j, (u, r) in enumerate(replay_pairs(text))]
    except Exception as e:  # noqa: BLE001
        log(f"replay not read ({e})")
    try:   # 2026-10-17: her real Slack replies to Jordan via the gateway are replay too
        oc.execute("SELECT trace_id, user_message, response FROM gateway_traces WHERE channel='slack' AND "
                   "person='jordan' AND response IS NOT NULL ORDER BY created_at DESC LIMIT %s", (MAX_REPLAY,))
        for tid, u, r in oc.fetchall():
            u = clean_user(u)
            if u and len((r or "").strip()) >= 40:
                cands.append((f"replay:gw:{tid}", u, r.strip()[:2000]))
    except Exception as e:  # noqa: BLE001
        log(f"gateway replay not read ({e})")
    try:   # the chatroom (May-June 2026): Nova's message answering Jordan's latest one in the channel within 10 min
        oc.execute("""SELECT n.id, j.message, n.message FROM chatroom_messages n JOIN LATERAL (
                        SELECT message FROM chatroom_messages j WHERE j.sender IN ('Jordan','Jordan Koch')
                        AND j.channel=n.channel AND j.id < n.id AND j.created_at > n.created_at - interval '10 minutes'
                        ORDER BY j.id DESC LIMIT 1) j ON true WHERE n.sender='Nova' ORDER BY n.id""")
        for nid, u, r in oc.fetchall():
            u = clean_user(u)
            if u and len((r or "").strip()) >= 40:
                cands.append((f"replay:chat:{nid}", u, r.strip()[:2000]))
    except Exception as e:  # noqa: BLE001
        log(f"chatroom replay not read ({e})")
    return cands


def curate(oc, audit: bool = True) -> dict:
    """Mark (never delete) the examples unfit to teach or judge her voice: deterministic persona/honesty checks,
    duplicates, then the teacher's voice audit (score < VOICE_MIN). Only rows not yet scored are audited."""
    ensure_schema(oc)
    oc.execute("SELECT id, key, kind, ref, user_text, reply, excluded, voice_score FROM finetune_examples ORDER BY id")
    rows, why, seen = oc.fetchall(), {}, set()
    voice = voice_text()
    for i, key, kind, ref, u, r, exc, vs in rows:
        if kind == "replay":
            reason = replay_ok(u, r)
        else:
            bad = voice_issues(r)
            reason = ("voice: " + bad[0]) if bad else (
                "names an internal part" if names_internal_part(ref, u, r) else
                (f"invented: {invented(u, r)}" if invented(u, r) else None))
        dk = (norm_key(u), norm_key(r)[:120])
        if not reason and (dk in seen or norm_key(r) in seen):
            reason = "duplicate"
        seen.update({dk, norm_key(r)})
        if not reason and audit:
            if vs is None:
                vs = audit_score(chat(voice_audit_prompt(voice, u, r), max_tokens=200, temperature=0.0))
                if vs is not None:
                    oc.execute("UPDATE finetune_examples SET voice_score=%s WHERE id=%s", (vs, i))
            if vs is not None and vs < VOICE_MIN:
                reason = f"voice audit {vs}/5"
        if reason != exc:
            oc.execute("UPDATE finetune_examples SET excluded=%s WHERE id=%s", (reason, i))
        k = reason.split(":")[0] if reason else "kept"
        k = "voice audit" if k.startswith("voice audit") else k
        why[f"{kind}/{k}"] = why.get(f"{kind}/{k}", 0) + 1
    log(f"curate: {json.dumps(why, sort_keys=True)}")
    return why


def export(oc, d: Path) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    sysmsg = voice_text()
    exam = set()
    for _, _, u, r, _, _ in eval_rows(oc):
        exam |= text_keys(u, r)
    n = {"leak_dropped": 0}
    for split in ("train", "valid", "test"):
        oc.execute("SELECT key, kind, user_text, reply FROM finetune_examples WHERE split=%s AND excluded IS NULL "
                   "ORDER BY id", (split,))
        rows = oc.fetchall()
        if split != "test":   # never train (or pick checkpoints) on text the frozen benchmark asks about
            kept = [r for r in rows if not (text_keys(r[2], r[3]) & exam)]
            n["leak_dropped"] += len(rows) - len(kept)
            rows = kept
        if split == "train":
            rows = balance(rows)
        with open(d / f"{split}.jsonl", "w") as f:
            for _, _, u, r in rows:
                f.write(json.dumps({"messages": [{"role": "system", "content": sysmsg},
                                                 {"role": "user", "content": u},
                                                 {"role": "assistant", "content": r}]}) + "\n")
        n[split] = len(rows)
    return n


def bland_prompt(user: str, reply: str) -> str:
    return (f"MESSAGE: {user}\nREPLY: {reply}\n\nRewrite the REPLY the way a bland, generic customer-support assistant "
            "would write it: the same facts and the same answer, but polite, neutral and corporate — no personality, "
            "no humour, no opinions, no nicknames, no bite. About the same length.\n"
            'Output ONLY JSON: {"reply": "<the rewrite>"}')


def parse_bland(raw: str) -> str | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    try:
        s = str(json.loads(t[t.find("{"):t.rfind("}") + 1]).get("reply") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return s if len(s) >= 20 else None


def eval_version(oc) -> int | None:
    try:
        oc.execute("SELECT max(version) FROM finetune_eval_set")
        row = oc.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def eval_rows(oc, version: int | None = None) -> list[tuple]:
    """(kind, id, user, reply, principle, bland) of a frozen benchmark version (default: the newest); [] if none.
    Membership is fixed by id: later curation never changes the exam."""
    v = version or eval_version(oc)
    if not v:
        return []
    oc.execute("SELECT s.kind, e.id, e.user_text, e.reply, coalesce(e.principle,''), s.bland FROM finetune_eval_set s "
               "JOIN finetune_examples e ON e.id = s.example_id WHERE s.version=%s ORDER BY s.kind, e.id", (v,))
    return oc.fetchall()


def training_text(oc) -> set[str]:
    """text_keys of everything any adapter trained on or will: curated train rows + every ROOT/*/data/train.jsonl."""
    oc.execute("SELECT user_text, reply FROM finetune_examples WHERE split='train' AND excluded IS NULL")
    keys = set()
    for u, r in oc.fetchall():
        keys |= text_keys(u, r)
    for f in ROOT.glob("*/data/train.jsonl"):
        for line in f.read_text().splitlines():
            m = json.loads(line)["messages"]
            keys |= text_keys(m[-2]["content"], m[-1]["content"])
    return keys


def top_up_replay(oc) -> int:
    """Fresh real exchanges (not yet stored, unique by content, fit to judge her voice) go straight into the test
    split and through curate() (incl. the teacher's voice audit). Returns how many were added."""
    oc.execute("SELECT key, user_text, reply FROM finetune_examples")
    rows, seen, added = oc.fetchall(), set(), 0
    have = {k for k, _, _ in rows}
    for _, u, r in rows:
        seen |= text_keys(u, r)
    for key, u, r in replay_candidates(oc):
        if key in have or replay_ok(u, r) or text_keys(u, r) & seen:
            continue
        seen |= text_keys(u, r)
        oc.execute("INSERT INTO finetune_examples (key, kind, user_text, reply, split) VALUES "
                   "(%s,'replay',%s,%s,'test') ON CONFLICT DO NOTHING", (key, u, r))
        added += 1
    if added:
        curate(oc)
    log(f"eval set: topped up {added} fresh real exchanges into test")
    return added


def rebuild_eval_set(oc, target: int = EVAL_TARGET) -> dict:
    """A NEW frozen benchmark version — deliberate only (--rebuild-eval-set). Curated (excluded IS NULL) rows, test
    split first, then valid (never trained on; moved to test so they stay out of training), none sharing a normalised
    message or reply with any training data or with each other; voice topped up from fresh real exchanges when short.
    Plus CALIB_N calibration pairs whose bland rewrite is generated once, here, and stored."""
    ensure_schema(oc)
    banned = training_text(oc)
    oc.execute("SELECT coalesce(max(version), 0) + 1 FROM finetune_eval_set")
    v = oc.fetchone()[0]
    sql = ("SELECT id, user_text, reply FROM finetune_examples WHERE kind=%s AND excluded IS NULL AND "
           "split IN ('test','valid') ORDER BY (split='test') DESC, id")
    out = {"version": v}
    for kind in ("practice", "replay"):
        oc.execute(sql, (kind,))
        ids = pick_eval(oc.fetchall(), banned, target)
        if len(ids) < target and kind == "replay":
            out["replay_topped_up"] = top_up_replay(oc)
            oc.execute(sql, (kind,))
            ids = pick_eval(oc.fetchall(), banned, target)
        if ids:
            oc.execute("UPDATE finetune_examples SET split='test' WHERE id = ANY(%s) AND split <> 'test'", (ids,))
        for i in ids:
            oc.execute("INSERT INTO finetune_eval_set (version, kind, example_id) VALUES (%s,%s,%s)", (v, kind, i))
        out[kind] = len(ids)
    oc.execute("SELECT id, user_text, reply FROM finetune_examples WHERE kind='replay' AND excluded IS NULL AND "
               "split='train' ORDER BY md5(key)")
    n = 0
    for i, u, r in oc.fetchall():
        if n >= CALIB_N:
            break
        bland = parse_bland(chat(bland_prompt(u, r), max_tokens=600, temperature=0.0))
        if bland and norm_key(bland) != norm_key(r):
            oc.execute("INSERT INTO finetune_eval_set (version, kind, example_id, bland) VALUES (%s,'calibration',%s,%s)",
                       (v, i, bland))
            n += 1
    out["calibration"] = n
    log(f"eval set v{v}: {json.dumps(out)} (target {target} per kind)")
    return out


def base_path() -> str:
    from huggingface_hub import snapshot_download
    kw = dict(cache_dir=os.path.join(HF_HOME, "hub"),
              allow_patterns=["*.json", "*.safetensors", "*.txt", "tokenizer*", "merges.txt"])
    try:
        return snapshot_download(BASE_REPO, local_files_only=True, **kw)    # no network once it is on /Volumes/Data
    except Exception:  # noqa: BLE001
        return snapshot_download(BASE_REPO, **kw)


def train(tag: str, data: Path, n_train: int) -> Path:
    out = ROOT / tag / "adapters"
    # 2026-10-16 first run: val loss bottomed at step 200 (~2 epochs of 412 examples) and climbed from step 400 —
    # train ~3 epochs, evaluate every 50 steps, keep the checkpoint with the lowest val loss (best_checkpoint)
    iters = max(150, min(800, (n_train * 3) // 4))
    cmd = [PY, "-m", "mlx_lm", "lora", "--model", base_path(), "--train", "--data", str(data),
           "--adapter-path", str(out), "--fine-tune-type", "lora", "--mask-prompt", "--num-layers", "16",
           "--batch-size", "4", "--iters", str(iters), "--learning-rate", LR, "--max-seq-length", "3072",
           "--steps-per-eval", "50", "--save-every", "50", "--grad-checkpoint", "--seed", "7"]
    log("train: " + " ".join(cmd[3:]))
    with open(ROOT / tag / "train.log", "w") as lf:
        subprocess.run(cmd, check=True, stdout=lf, stderr=subprocess.STDOUT, env={**os.environ, "HF_HOME": HF_HOME})
    best = best_checkpoint((ROOT / tag / "train.log").read_text(), {p.name for p in out.glob("0*_adapters.safetensors")})
    if best:
        import shutil
        shutil.copy(out / "adapters.safetensors", out / "final.safetensors")
        shutil.copy(out / f"{best:07d}_adapters.safetensors", out / "adapters.safetensors")
        log(f"train: kept step {best} (lowest val loss)")
    return out


def best_checkpoint(train_log: str, saved: set[str]) -> int | None:
    """Step with the lowest 'Val loss' that also has a saved checkpoint file. Pure."""
    vals = [(float(v), int(i)) for i, v in re.findall(r"Iter (\d+): Val loss ([\d.]+)", train_log)]
    ok = sorted((v, i) for v, i in vals if f"{i:07d}_adapters.safetensors" in saved)
    return ok[0][1] if ok else None


def _generate_all(prompts: list[str], adapter: Path | None, sysmsg: str, samples: int = EVAL_SAMPLES,
                  seed: int = 7) -> list[list[str]]:
    """`samples` replies per prompt, batched, sampled as deployed (gateway temperature GEN_TEMP; qwen3:8b's Modelfile
    top_p 0.95 / top_k 20) -> [[reply, ...] per prompt]."""
    import mlx.core as mx
    from mlx_lm import batch_generate, load
    from mlx_lm.sample_utils import make_sampler
    # MLX defaults let its buffer cache grow to ~the whole working set (464 GB here); uncapped, this eval hit 323 GB
    # beside the llama-servers and wdog-panicked the Studio 2026-10-19. 8B bf16 + 32-seq KV fits well under 64 GB.
    mx.set_memory_limit(64 << 30)
    mx.set_cache_limit(4 << 30)
    model, tok = load(base_path(), adapter_path=str(adapter) if adapter else None)
    mx.random.seed(seed)
    ids = [tok.encode(tok.apply_chat_template([{"role": "system", "content": sysmsg}, {"role": "user", "content": p}],
                                              tokenize=False, add_generation_prompt=True, enable_thinking=False),
                      add_special_tokens=False) for p in prompts]
    texts = batch_generate(model, tok, [x for x in ids for _ in range(samples)], max_tokens=400,
                           sampler=make_sampler(temp=GEN_TEMP, top_p=0.95, top_k=20)).texts
    del model
    mx.clear_cache()
    texts = [re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip() for t in texts]
    return [texts[k * samples:(k + 1) * samples] for k in range(len(prompts))]


def consensus(v1: str | None, v2: str | None) -> str | None:
    """Verdicts from the two orders (v1: A=base B=new; v2: A=new B=base) -> 'new'|'base'|'tie'|None.
    Disagreement (position bias) is a tie. Pure."""
    m1 = {"A": "base", "B": "new", "tie": "tie"}.get(v1 or "")
    m2 = {"A": "new", "B": "base", "tie": "tie"}.get(v2 or "")
    if not m1 or not m2:
        return m1 or m2
    return m1 if m1 == m2 else "tie"


def judge_pair(judge: str, kind: str, ctx: str, user: str, base: str, new: str, persona: str) -> dict:
    """One judge, both orders (1: A=base B=new; 2: A=new B=base) -> {'v': consensus verdict, 'diff': mean rubric
    new-minus-base (None without scores), 'orders': (winner 1, winner 2)}."""
    j1 = parse_judgement(judge_call(judge, judge_prompt(kind, ctx, user, base, new, persona)), kind)
    j2 = parse_judgement(judge_call(judge, judge_prompt(kind, ctx, user, new, base, persona)), kind)
    d = [x for x in ((j1[2] - j1[1]) if j1 and None not in j1[1:] else None,
                     (j2[1] - j2[2]) if j2 and None not in j2[1:] else None) if x is not None]
    return {"v": consensus(j1[0] if j1 else None, j2[0] if j2 else None), "diff": sum(d) / len(d) if d else None,
            "orders": (j1[0] if j1 else None, j2[0] if j2 else None)}


def panel(jobs: list[tuple], persona: str, judges) -> dict:
    """Every judge on every (kind, ctx, user, base, new) job, judges in parallel (Ollama serves 2 at a time on the
    Studio; Claude 4) -> {judge: [judge_pair result per job]}."""
    def one(judge):
        with ThreadPoolExecutor(4 if judge.startswith("claude:") else 2) as ex:
            return list(ex.map(lambda j: judge_pair(judge, *j, persona), jobs))
    with ThreadPoolExecutor(max(1, len(judges))) as ex:
        return dict(zip(judges, ex.map(one, judges)))


def calibrate(calib: list[tuple], persona: str, judges) -> dict:
    """(user, her real reply, stored bland rewrite) pairs -> {judge: share of single-order judgements that picked her
    real reply}. Persona only, no reference reply (one side IS the reference)."""
    res = panel([("replay", "", u, bland, real) for u, real, bland in calib], persona, judges)
    return {j: calib_accuracy([p for x in res[j] for p in (x["orders"][0] == "B", x["orders"][1] == "A")])
            for j in judges}


_SIDE1, _SIDE2 = {"A": "base", "B": "new", "tie": "tie"}, {"A": "new", "B": "base", "tie": "tie"}


def summarise(keys: list[tuple[str, int]], res: dict, kept: list[str]) -> dict:
    """Per-job results of the kept judges -> per-kind stats. keys[x] = (kind, prompt index) of job x. A job's panel
    verdict is the judges' majority and its rubric difference their mean; per prompt both are averaged over its
    samples; the win rate (ties half) gets a Wilson CI over prompts, the rubric difference a bootstrap CI. Pure."""
    per, agree, steady = {}, {j: [0, 0] for j in kept}, {j: [0, 0] for j in kept}
    for x, key in enumerate(keys):
        v = majority([res[j][x]["v"] for j in kept])
        diffs = [res[j][x]["diff"] for j in kept if res[j][x]["diff"] is not None]
        vs, ds = per.setdefault(key, ([], []))
        if v:
            vs.append(v)
        if diffs:
            ds.append(sum(diffs) / len(diffs))
        for j in kept:
            o, jv = res[j][x]["orders"], res[j][x]["v"]
            if jv and v:
                agree[j][0] += jv == v
                agree[j][1] += 1
            if o[0] and o[1]:
                steady[j][0] += _SIDE1[o[0]] == _SIDE2[o[1]]
                steady[j][1] += 1
    out = {}
    for kind in ("practice", "replay"):
        scores = [win_rate(vs) for (k, _), (vs, _) in per.items() if k == kind and vs]
        diffs = [sum(ds) / len(ds) for (k, _), (_, ds) in per.items() if k == kind and ds]
        wr = sum(scores) / len(scores) if scores else 0.0
        out[kind] = {"n": len(scores), "wr": wr, "wr_ci": wilson(wr, len(scores)), "diff_ci": bootstrap_ci(diffs),
                     "n_pairs": sum(len(vs) for (k, _), (vs, _) in per.items() if k == kind)}
    out["agreement_with_panel"] = {j: round(a / b, 3) if b else None for j, (a, b) in agree.items()}
    out["order_consistency"] = {j: round(a / b, 3) if b else None for j, (a, b) in steady.items()}
    return out


def evaluate(oc, adapter: Path, detail: Path | None = None, judges=JUDGES) -> dict:
    """2026-10-19: the frozen benchmark (newest finetune_eval_set version), judge calibration every run, EVAL_SAMPLES
    replies per prompt per model with the deployed system prompt, a judge panel with majority vote, CIs. Returns
    {'void': why, ...} when there is no exam or fewer than 2 judges pass calibration — void is not a failure."""
    t0 = time.time()
    v = eval_version(oc)
    rows = eval_rows(oc, v)
    exam = [r for r in rows if r[0] in ("practice", "replay")]
    calib = [(u, r, b) for k, _, u, r, _, b in rows if k == "calibration"]
    if not exam:
        return {"void": "no frozen eval set (run --rebuild-eval-set)", "runtime_s": 0}
    persona = voice_text()
    acc = calibrate(calib, persona, judges)
    kept = [j for j in judges if acc[j] >= CALIB_MIN]
    for j in judges:
        log(f"eval calibration: {j} picked her real reply {acc[j]:.0%} of {2 * len(calib)} -> "
            f"{'kept' if j in kept else f'DISCARDED (< {CALIB_MIN:.0%})'}")
    info = {"version": v, "calibration": acc, "kept": kept, "n_calib": len(calib), "samples": EVAL_SAMPLES,
            "temp": GEN_TEMP, "adapter": str(adapter)}
    if len(kept) < 2:
        return {**info, "void": f"only {len(kept)} judge(s) passed calibration (need 2)",
                "runtime_s": int(time.time() - t0)}
    sysmsg = deployed_system_prompt(oc)
    info["system_prompt_sha"] = hashlib.sha256(sysmsg.encode()).hexdigest()[:12]
    log(f"eval: v{v}, {len(exam)} prompts x {EVAL_SAMPLES} samples, system prompt {len(sysmsg)} chars "
        f"({info['system_prompt_sha']}), temp {GEN_TEMP}")
    prompts = [u for _, _, u, _, _, _ in exam]
    base, new = _generate_all(prompts, None, sysmsg), _generate_all(prompts, adapter, sysmsg)
    log(f"eval: generated in {int(time.time() - t0)}s; judging")
    jobs, keys = [], []
    for i, (kind, _, u, ref, principle, _) in enumerate(exam):
        for s in range(EVAL_SAMPLES):
            jobs.append((kind, principle if kind == "practice" else ref, u, base[i][s], new[i][s]))
            keys.append((kind, i))
    res = panel(jobs, persona, kept)
    st = summarise(keys, res, kept)
    out = {**info, "practice_wr": st["practice"]["wr"], "voice_wr": st["replay"]["wr"],
           "n": {k: st[k]["n"] for k in ("practice", "replay")}, "stats": st, "runtime_s": int(time.time() - t0)}
    if detail:
        detail.parent.mkdir(parents=True, exist_ok=True)
        detail.write_text(json.dumps({"summary": out, "exam_ids": [r[1] for r in exam], "jobs": jobs, "keys": keys,
                                      "results": res}, indent=1))
    return out


CONVERT_PY = os.environ.get("NOVA_FT_CONVERT_PY", "/Volumes/Data/tools/llama.cpp-convert/venv/bin/python")
CONVERT = os.environ.get("NOVA_FT_CONVERT", "/Volumes/Data/tools/llama.cpp-convert/src/convert_hf_to_gguf.py")
QUANTIZE = os.environ.get("NOVA_FT_QUANTIZE", "/opt/homebrew/bin/llama-quantize")


def deploy(tag: str, adapter: Path) -> str:
    """fuse (bf16 safetensors) -> GGUF bf16 -> Q4_K_M (the quant qwen3:8b ships in, portable to the Linux nodes; this
    Ollama only quantizes safetensors to MLX-only int4/int8) -> ollama create, with qwen3:8b's template/params."""
    d = ROOT / tag
    fused = d / "fused"
    env = {**os.environ, "HF_HOME": HF_HOME}
    subprocess.run([PY, "-m", "mlx_lm", "fuse", "--model", base_path(), "--adapter-path", str(adapter),
                    "--save-path", str(fused)], check=True, env=env)
    subprocess.run([CONVERT_PY, CONVERT, str(fused), "--outtype", "bf16", "--outfile", str(d / "fused-bf16.gguf")],
                   check=True, env=env)
    subprocess.run([QUANTIZE, str(d / "fused-bf16.gguf"), str(d / "nova-grown-q4km.gguf"), "Q4_K_M"], check=True)
    (d / "fused-bf16.gguf").unlink(missing_ok=True)
    tmpl = subprocess.run([OLLAMA_BIN, "show", "qwen3:8b", "--modelfile"], capture_output=True, text=True,
                          check=True).stdout
    keep = [ln for ln in tmpl.splitlines() if not ln.startswith(("FROM", "#"))]
    (d / "Modelfile").write_text(f"FROM {d / 'nova-grown-q4km.gguf'}\n" + "\n".join(keep) + "\n")
    name = f"{MODEL_NAME}:{tag}"
    subprocess.run([OLLAMA_BIN, "create", name, "-f", str(d / "Modelfile")], check=True)
    subprocess.run([OLLAMA_BIN, "cp", name, f"{MODEL_NAME}:latest"], check=True)
    return name


def record(oc, tag: str, **kw):
    cols = ", ".join(kw)
    oc.execute(f"INSERT INTO finetune_runs (tag, {cols}) VALUES (%s, {', '.join(['%s'] * len(kw))}) "  # noqa: S608
               f"ON CONFLICT (tag) DO UPDATE SET " + ", ".join(f"{k}=EXCLUDED.{k}" for k in kw),
               (tag, *[json.dumps(v) if isinstance(v, dict) else v for v in kw.values()]))


def run(stages: list[str], tag: str, adapter_override: str | None = None):
    oc = ops()
    ensure_schema(oc)
    ROOT.mkdir(parents=True, exist_ok=True)
    data = ROOT / tag / "data"
    adapter = Path(adapter_override) if adapter_override else ROOT / tag / "adapters"
    if "build" in stages:
        record(oc, tag, stage="build")
        build(oc)
    if "build" in stages or "curate" in stages:
        record(oc, tag, stage="curate", notes={"curate": curate(oc)})
    if "train" in stages:
        n = export(oc, data)
        record(oc, tag, stage="train", n_train=n["train"], n_valid=n["valid"])
        if n["train"] < 50 or n["valid"] < 5:
            log(f"too few examples to train ({n})")
            return 1
        train(tag, data, n["train"])
    if "eval" in stages:
        r = evaluate(oc, adapter, detail=ROOT / tag / "eval.json")
        r["eval_method"] = ("2026-10-19: frozen benchmark; calibrated judge panel, both orders, majority vote; "
                            f"{EVAL_SAMPLES} samples/prompt at temp {GEN_TEMP} with the deployed system prompt; "
                            "gate = practice Wilson lo > .5, voice Wilson hi >= .5, voice rubric diff hi >= 0")
        if r.get("void"):
            record(oc, tag, stage="eval", passed=None, notes=r)
            log(f"eval VOID (not a failure): {r['void']}; calibration {r.get('calibration')}")
            return 0
        st = r["stats"]
        why = gate_failures(st["practice"]["wr_ci"], st["replay"]["wr_ci"], st["replay"]["diff_ci"])
        r["gate_failures"] = why
        record(oc, tag, stage="eval", practice_wr=r["practice_wr"], voice_wr=r["voice_wr"], passed=not why, notes=r)
        for kind in ("practice", "replay"):
            s, d = st[kind], st[kind]["diff_ci"]
            log(f"eval {kind}: win {s['wr']:.0%} (95% CI {s['wr_ci'][0]:.0%}-{s['wr_ci'][1]:.0%}), n={s['n']} prompts / "
                f"{s['n_pairs']} pairs; rubric diff " + (f"{d[0]:+.2f} (CI {d[1]:+.2f}..{d[2]:+.2f})" if d else "n/a"))
        log(f"eval judges: kept {r['kept']}; agreement with panel {st['agreement_with_panel']}; "
            f"order consistency {st['order_consistency']}; runtime {r['runtime_s']}s")
        log("eval -> " + ("PASS" if not why else "FAIL: " + "; ".join(why)))
        if why:
            return 0
    if "deploy" in stages:
        oc.execute("SELECT passed FROM finetune_runs WHERE tag=%s", (tag,))
        row = oc.fetchone()
        if not (row and row[0]):
            log("deploy refused: this run did not pass the eval gate")
            return 1
        name = deploy(tag, adapter)
        record(oc, tag, stage="deployed", deployed=name)
        oc.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES ('finetune','current',"
                   "%s::jsonb,'nova_finetune') ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, "
                   "updated_at=now(), updated_by=EXCLUDED.updated_by", (json.dumps({"model": name, "tag": tag}),))
        log(f"deployed {name} (+ {MODEL_NAME}:latest)")
        hello = chat('Say hello to Little Mister in one short sentence. Output ONLY JSON: {"reply": "..."}',
                     model=f"{MODEL_NAME}:latest", max_tokens=80, temperature=GEN_TEMP)
        log(f"deploy check: {MODEL_NAME}:latest says {hello[:200]!r}" if hello else "deploy check: NO RESPONSE")
        if not hello:
            return 1
    return 0


def selftest():
    assert split_for("x") == split_for("x") and split_for("x") in ("train", "valid", "test")
    shares = [split_for(str(i)) for i in range(5000)]
    assert 0.75 < shares.count("train") / 5000 < 0.85
    pairs = replay_pairs("Jordan: Just do it\nNova: Alright, I'll just do it. What's the plan, Little Mister? I'm here.\n"
                         "Jordan: ok\nNova: short")
    assert pairs == [("Just do it", "Alright, I'll just do it. What's the plan, Little Mister? I'm here.")]
    assert replay_pairs("Correction (from Jordan): not a dialogue") == []
    good = '{"user": "Is the NAS healthy?", "reply": "The UNAS answered its last three probes; I have not checked the disks today."}'
    assert parse_example(good) and parse_example("<think>x</think>" + good)
    assert parse_example('{"user": "hi", "reply": "x"}') is None
    assert parse_example(good.replace("Is the NAS healthy?", "my password: hunter22 ok?")) is None
    assert parse_verdict('{"winner": "B"}') == "B" and parse_verdict('{"winner":"tie"}') == "tie"
    assert parse_verdict("no idea") is None
    assert win_rate(["new", "new", "base", "tie"]) == 0.625 and win_rate([]) == 0.0
    assert invented("Is it 20% cheaper?", "Probably not 20%; tell me the model and I'll find out.") is None
    assert invented("Is it cheaper?", "It's rated 16 SEER.").startswith("number")
    assert invented("Is it cheaper?", "I checked the logs and it is.").startswith("claims")
    assert scene_for("a") in SCENES
    assert critique_ok('{"invented": [], "lives": 4, "coherent": 5, "voice": 4}')
    assert not critique_ok('{"invented": [], "lives": 4, "coherent": 5, "voice": 3}')
    assert not critique_ok('{"invented": ["mom texted twice"], "lives": 5, "coherent": 5, "voice": 5}')
    assert voice_issues("Fine, Little Mister. The backup ran; I'd still look at the disk before trusting it.") == []
    assert "emoji" in voice_issues("Done! \U0001F50D") and "bullet list" in voice_issues("Here:\n- one\n- two")
    assert "permission-seeking" in voice_issues("I can do that. Want me to set it up?")
    assert "tool failure leak" in voice_issues("Little Mister, web search failed, so no opinion.")
    assert replay_ok("Yes", "A long enough reply that is perfectly in voice, Little Mister.") == "context-free prompt"
    assert replay_ok("Is the NAS ok now?", "It's at 91% and grumpy.").startswith("invented")
    assert replay_ok("Is the NAS ok now?", "No idea yet, Little Mister; I'll look before I swear to anything.") is None
    assert clean_user("[chatroom message from Baalsong]: hi") is None
    assert clean_user("[chatroom message from Jordan]: hi Nova") == "hi Nova"
    assert consensus("B", "A") == "new" and consensus("A", "A") == "tie" and consensus(None, "B") == "base"
    assert names_internal_part("organ:card_rack", "Card rack reports a drop", "ok")
    assert not names_internal_part("organ:x", "a", "b")
    rows = [("r1", "replay"), ("p1", "practice"), ("p2", "practice"), ("p3", "practice"), ("p4", "practice")]
    assert sum(1 for r in balance(rows) if r[1] == "practice") == 1
    assert not critique_ok('{"lives": 5, "coherent": 5}')
    assert not critique_ok("garbage")
    logtxt = "Iter 1: Val loss 3.6\nIter 200: Val loss 1.547\nIter 300: Val loss 1.568\nIter 400: Val loss 1.686"
    assert best_checkpoint(logtxt, {"0000200_adapters.safetensors", "0000400_adapters.safetensors"}) == 200
    assert best_checkpoint(logtxt, {"0000400_adapters.safetensors"}) == 400 and best_checkpoint("", set()) is None
    lo, hi = wilson(0.5, 60)
    assert 0.37 < lo < 0.38 and 0.62 < hi < 0.63 and wilson(1.0, 10)[1] == 1.0 and wilson(0.0, 0) == (0.0, 1.0)
    assert passes_gate(wilson(0.75, 60), wilson(0.45, 50), (0.0, -0.2, 0.1))       # confidently better, voice not worse
    assert not passes_gate(wilson(0.6, 15), wilson(0.5, 15), (0.0, -0.2, 0.2))     # noisy: practice CI straddles .5
    assert not passes_gate(wilson(0.8, 60), wilson(0.3, 60), (0.0, -0.2, 0.1))     # voice clearly worse
    assert not passes_gate(wilson(0.8, 60), wilson(0.5, 60), (-0.5, -0.7, -0.3))   # voice rubric clearly worse
    assert majority(["new", "new", "base"]) == "new" and majority(["new", "base", "tie"]) == "tie"
    assert majority(["base", None]) == "base" and majority([None, None]) is None
    assert pick_eval([(1, "Is the NAS ok?", "x"), (2, "is the nas OK", "y"), (3, "Q", "Z")], {"r:z"}, 5) == [1]
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--weekly", action="store_true", help="build + train + eval + deploy if the gate passes")
    ap.add_argument("--stages", default="", help="comma list of build,curate,train,eval,deploy")
    ap.add_argument("--tag", help="run tag (default: today; with --eval-only <adapter run>-rescore)")
    ap.add_argument("--adapter", help="eval this adapter dir instead of the tag's own (re-scoring an old run)")
    ap.add_argument("--eval-only", action="store_true",
                    help="with --adapter: score it on the frozen benchmark and deploy it if it passes (no training)")
    ap.add_argument("--rebuild-eval-set", action="store_true",
                    help="deliberately create a NEW frozen benchmark version (every later eval uses it)")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.rebuild_eval_set:
        print(json.dumps(rebuild_eval_set(ops())))
        if not (a.weekly or a.stages or a.eval_only):
            return 0
    if a.eval_only:
        if not a.adapter:
            ap.error("--eval-only needs --adapter <dir>")
        return run(["eval", "deploy"], a.tag or f"{Path(a.adapter).resolve().parent.name}-rescore", a.adapter)
    stages = ["build", "train", "eval", "deploy"] if a.weekly else [s for s in a.stages.split(",") if s]
    if a.adapter and "deploy" in stages:
        ap.error("--adapter with --stages only re-scores; use --eval-only to deploy an old adapter that passes")
    if not stages:
        ap.error("give --weekly, --stages, --eval-only or --rebuild-eval-set")
    return run(stages, a.tag or date.today().strftime("%Y%m%d"), a.adapter)


if __name__ == "__main__":
    sys.exit(main())
