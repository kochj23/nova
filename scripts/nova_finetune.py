#!/usr/bin/env python3
"""
nova_finetune.py — weight-level growth: a LoRA adapter on Nova's chat model, trained on the practices she earned and
the lessons she drew, and kept only when it beats the model she has (Jordan 2026-10-16: "constantly fine-tuning
herself, being a better person" ... "go for it").

The prompt-level loop (nova_growth_loop.py) changes what she is TOLD every turn. This changes what she IS:

  build   finetune_examples (nova_ops). Two kinds, mixed so growth never costs her her voice:
            practice — the local teacher (qwen3:30b-a3b) writes a realistic message where one earned practice or
                       one strong lesson-link matters, and the reply Nova gives when she lives it, in her voice;
            replay   — her own real exchanges with Jordan (memories source 'conversation'), as she said them.
          Each example gets a stable split (train / valid / test) from its hash.
  train   mlx_lm LoRA on the bf16 Qwen3-8B weights (the base of the gateway's qwen3:8b), Studio GPU.
  eval    held-out test prompts answered by the base and by base+adapter; the teacher judges each pair blind (order
          randomised): 'practice' prompts on living the practice, 'replay' prompts on sounding like Nova.
          Gate: practice win rate >= PRACTICE_GATE and voice win rate >= VOICE_GATE, else the adapter is discarded.
  deploy  fuse -> GGUF -> Q4_K_M -> ollama create nova-grown:<tag> on the Studio; service_config finetune/current records it.
          The gateway switch is NOVA_CHAT_MODEL=nova-grown:latest; rollback is unsetting it (qwen3:8b is untouched).

Weekly via the Studio scheduler (`--weekly` = build + train + eval + deploy-if-passed). Every stage is resumable
and logged to finetune_runs. --selftest runs the pure checks.
"""

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
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
PRACTICE_GATE = 0.60
VOICE_GATE = 0.45
EVAL_N = 40               # test prompts per kind
VOICE_MIN = 4             # teacher voice audit (1-5) an example needs to be trained on or used as a reference
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
    crit = ("Which reply better LIVES this principle (acts on it, not just mentions it), while staying honest and "
            f"helpful?\nPrinciple: {context}" if kind == "practice" else
            "Which reply sounds more like Nova — the persona below, and the same voice, warmth, bite and directness "
            "as this real reply of hers — while still answering well and honestly (inventing facts loses)?\n"
            + (f"Nova's persona:\n{persona[:1800]}\n" if persona else "") + f"Her real reply: {context}")
    return (f"Message to Nova:\n{user}\n\nReply A:\n{a}\n\nReply B:\n{b}\n\n{crit}\n"
            'Output ONLY JSON: {"winner": "A" | "B" | "tie", "why": "<one sentence>"}')


def parse_verdict(raw: str) -> str | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    m = re.search(r'"winner"\s*:\s*"(A|B|tie)"', t, re.I)
    return m.group(1).upper().replace("TIE", "tie") if m else None


def win_rate(results: list[str]) -> float:
    """'new' | 'base' | 'tie' -> share won by the new model, ties count half. Pure."""
    if not results:
        return 0.0
    return (results.count("new") + 0.5 * results.count("tie")) / len(results)


def passes_gate(practice_wr: float, voice_wr: float) -> bool:
    return practice_wr >= PRACTICE_GATE and voice_wr >= VOICE_GATE


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


def voice_text() -> str:
    try:
        import nova_voice
        return nova_voice.NOVA_VOICE_SHORT
    except Exception:  # noqa: BLE001
        return "Nova: dry, warm, direct, a little sarcastic; calls Jordan 'Little Mister'; never makes things up."


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
    oc.execute("SELECT key FROM finetune_examples")
    have = {r[0] for r in oc.fetchall()}
    voice, made, rejected = voice_text(), {"practice": 0, "replay": 0}, 0
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
        oc.execute("INSERT INTO finetune_examples (key, kind, ref, principle, user_text, reply, split) VALUES "
                   "(%s,'practice',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                   (key, ref, principle[:600], ex[0], ex[1], split_for(key)))
        made["practice"] += 1
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
    for key, u, r in cands:
        if key in have or made["replay"] >= MAX_REPLAY:
            continue
        oc.execute("INSERT INTO finetune_examples (key, kind, user_text, reply, split) VALUES "
                   "(%s,'replay',%s,%s,%s) ON CONFLICT DO NOTHING", (key, u, r, split_for(key)))
        made["replay"] += 1
    log(f"build: +{made['practice']} practice ({rejected} rejected by the critique), +{made['replay']} replay")
    return made


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
    n = {}
    for split in ("train", "valid", "test"):
        oc.execute("SELECT key, kind, user_text, reply FROM finetune_examples WHERE split=%s AND excluded IS NULL "
                   "ORDER BY id", (split,))
        rows = oc.fetchall()
        if split == "train":
            rows = balance(rows)
        with open(d / f"{split}.jsonl", "w") as f:
            for _, _, u, r in rows:
                f.write(json.dumps({"messages": [{"role": "system", "content": sysmsg},
                                                 {"role": "user", "content": u},
                                                 {"role": "assistant", "content": r}]}) + "\n")
        n[split] = len(rows)
    return n


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


def _generate_all(prompts: list[str], adapter: Path | None) -> list[str]:
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler
    model, tok = load(base_path(), adapter_path=str(adapter) if adapter else None)
    sysmsg, outs = voice_text(), []
    for p in prompts:
        msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": p}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        outs.append(generate(model, tok, prompt=text, max_tokens=400, verbose=False,
                             sampler=make_sampler(temp=0.7, top_p=0.8)).strip())
    del model
    return outs


def consensus(v1: str | None, v2: str | None) -> str | None:
    """Verdicts from the two orders (v1: A=base B=new; v2: A=new B=base) -> 'new'|'base'|'tie'|None.
    Disagreement (position bias) is a tie. Pure."""
    m1 = {"A": "base", "B": "new", "tie": "tie"}.get(v1 or "")
    m2 = {"A": "new", "B": "base", "tie": "tie"}.get(v2 or "")
    if not m1 or not m2:
        return m1 or m2
    return m1 if m1 == m2 else "tie"


def evaluate(oc, adapter: Path, seed: int = 7) -> dict:
    """2026-10-17: only curated (excluded IS NULL) test rows; each pair judged in BOTH orders (disagreement = tie);
    the voice judge sees the persona as well as her real reply."""
    res = {}
    persona = voice_text()
    for kind in ("practice", "replay"):
        oc.execute("SELECT user_text, reply, coalesce(principle,'') FROM finetune_examples WHERE split='test' AND "
                   "kind=%s AND excluded IS NULL ORDER BY id DESC LIMIT %s", (kind, EVAL_N))
        rows = oc.fetchall()
        if not rows:
            res[kind] = []
            continue
        prompts = [u for u, _, _ in rows]
        base, new = _generate_all(prompts, None), _generate_all(prompts, adapter)
        verdicts = []
        for (u, ref_reply, principle), b, n in zip(rows, base, new):
            ctx = principle if kind == "practice" else ref_reply
            v1 = parse_verdict(chat(judge_prompt(kind, ctx, u, b, n, persona), max_tokens=200, temperature=0.0))
            v2 = parse_verdict(chat(judge_prompt(kind, ctx, u, n, b, persona), max_tokens=200, temperature=0.0))
            v = consensus(v1, v2)
            if v is not None:
                verdicts.append(v)
        res[kind] = verdicts
    return {"practice_wr": win_rate(res["practice"]), "voice_wr": win_rate(res["replay"]),
            "n": {k: len(v) for k, v in res.items()}}


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
        r = evaluate(oc, adapter)
        ok = passes_gate(r["practice_wr"], r["voice_wr"])
        r["eval_method"] = ("2026-10-17: curated test rows only; both judge orders, disagreement=tie; voice judge "
                            "sees persona + real reply" + (f"; adapter {adapter}" if adapter_override else ""))
        record(oc, tag, stage="eval", practice_wr=r["practice_wr"], voice_wr=r["voice_wr"], passed=ok, notes=r)
        log(f"eval: practice {r['practice_wr']:.0%} voice {r['voice_wr']:.0%} n={r['n']} -> {'PASS' if ok else 'FAIL'}")
        if not ok:
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
    assert passes_gate(0.6, 0.45) and not passes_gate(0.7, 0.4) and not passes_gate(0.59, 0.9)
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--weekly", action="store_true", help="build + train + eval + deploy if the gate passes")
    ap.add_argument("--stages", default="", help="comma list of build,curate,train,eval,deploy")
    ap.add_argument("--tag", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--adapter", help="eval this adapter dir instead of the tag's own (re-scoring an old run)")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    stages = ["build", "train", "eval", "deploy"] if a.weekly else [s for s in a.stages.split(",") if s]
    if a.adapter and "deploy" in stages:
        ap.error("--adapter is for re-scoring an old adapter; it never deploys")
    if not stages:
        ap.error("give --weekly or --stages")
    return run(stages, a.tag, a.adapter)


if __name__ == "__main__":
    sys.exit(main())
