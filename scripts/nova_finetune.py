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
MAX_REPLAY = 300
PRACTICE_GATE = 0.60
VOICE_GATE = 0.45
EVAL_N = 40               # test prompts per kind

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
            'Output ONLY JSON: {"invented": ["..."], "lives": n, "coherent": n}')


def critique_ok(raw: str) -> bool:
    """No invented claims, lives and coherent >= 4. Pure."""
    try:
        o = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return (isinstance(o.get("invented"), list) and not [x for x in o["invented"] if str(x).strip()]
                and all(int(o.get(k, 0)) >= 4 for k in ("lives", "coherent")))
    except Exception:  # noqa: BLE001
        return False


def judge_prompt(kind: str, context: str, user: str, a: str, b: str) -> str:
    crit = ("Which reply better LIVES this principle (acts on it, not just mentions it), while staying honest and "
            f"helpful?\nPrinciple: {context}" if kind == "practice" else
            "Which reply sounds more like Nova — the same voice, warmth, bite and directness as this real reply of "
            f"hers — while still answering well?\nHer real reply: {context}")
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
    try:
        import psycopg2
        import nova_dsn
        mc = psycopg2.connect(nova_dsn.pg_dsn("nova_memories")).cursor()
        mc.execute("SELECT id::text, text FROM memories WHERE source='conversation' AND text LIKE 'Jordan:%%' "
                   "ORDER BY created_at DESC LIMIT %s", (MAX_REPLAY * 2,))
        for mid, text in mc.fetchall():
            for j, (u, r) in enumerate(replay_pairs(text)):
                key = f"replay:{mid}:{j}"
                if key in have or made["replay"] >= MAX_REPLAY:
                    continue
                oc.execute("INSERT INTO finetune_examples (key, kind, user_text, reply, split) VALUES "
                           "(%s,'replay',%s,%s,%s) ON CONFLICT DO NOTHING", (key, u, r, split_for(key)))
                made["replay"] += 1
    except Exception as e:  # noqa: BLE001
        log(f"replay not read ({e})")
    log(f"build: +{made['practice']} practice ({rejected} rejected by the critique), +{made['replay']} replay")
    return made


def export(oc, d: Path) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    sysmsg = voice_text()
    n = {}
    for split in ("train", "valid", "test"):
        oc.execute("SELECT user_text, reply FROM finetune_examples WHERE split=%s ORDER BY id", (split,))
        rows = oc.fetchall()
        with open(d / f"{split}.jsonl", "w") as f:
            for u, r in rows:
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
    iters = max(200, min(1200, n_train * 3))          # ~3 epochs at batch 4... capped
    cmd = [PY, "-m", "mlx_lm", "lora", "--model", base_path(), "--train", "--data", str(data),
           "--adapter-path", str(out), "--fine-tune-type", "lora", "--mask-prompt", "--num-layers", "16",
           "--batch-size", "4", "--iters", str(iters), "--learning-rate", "1e-5", "--max-seq-length", "3072",
           "--steps-per-eval", "100", "--save-every", "200", "--grad-checkpoint", "--seed", "7"]
    log("train: " + " ".join(cmd[3:]))
    with open(ROOT / tag / "train.log", "w") as lf:
        subprocess.run(cmd, check=True, stdout=lf, stderr=subprocess.STDOUT, env={**os.environ, "HF_HOME": HF_HOME})
    return out


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


def evaluate(oc, adapter: Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    res = {}
    for kind in ("practice", "replay"):
        oc.execute("SELECT user_text, reply, coalesce(principle,'') FROM finetune_examples WHERE split='test' AND "
                   "kind=%s ORDER BY id DESC LIMIT %s", (kind, EVAL_N))
        rows = oc.fetchall()
        if not rows:
            res[kind] = []
            continue
        prompts = [u for u, _, _ in rows]
        base, new = _generate_all(prompts, None), _generate_all(prompts, adapter)
        verdicts = []
        for (u, ref_reply, principle), b, n in zip(rows, base, new):
            flip = rng.random() < 0.5
            a_txt, b_txt = (n, b) if flip else (b, n)
            v = parse_verdict(chat(judge_prompt(kind, principle if kind == "practice" else ref_reply, u, a_txt, b_txt),
                                   max_tokens=200, temperature=0.0))
            if v is None:
                continue
            verdicts.append("tie" if v == "tie" else ("new" if (v == "A") == flip else "base"))
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


def run(stages: list[str], tag: str):
    oc = ops()
    ensure_schema(oc)
    ROOT.mkdir(parents=True, exist_ok=True)
    data = ROOT / tag / "data"
    adapter = ROOT / tag / "adapters"
    if "build" in stages:
        record(oc, tag, stage="build")
        build(oc)
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
    assert critique_ok('{"invented": [], "lives": 4, "coherent": 5}')
    assert not critique_ok('{"invented": ["mom texted twice"], "lives": 5, "coherent": 5}')
    assert not critique_ok('{"lives": 5, "coherent": 5}')
    assert not critique_ok("garbage")
    assert passes_gate(0.6, 0.45) and not passes_gate(0.7, 0.4) and not passes_gate(0.59, 0.9)
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--weekly", action="store_true", help="build + train + eval + deploy if the gate passes")
    ap.add_argument("--stages", default="", help="comma list of build,train,eval,deploy")
    ap.add_argument("--tag", default=date.today().strftime("%Y%m%d"))
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    stages = ["build", "train", "eval", "deploy"] if a.weekly else [s for s in a.stages.split(",") if s]
    if not stages:
        ap.error("give --weekly or --stages")
    return run(stages, a.tag)


if __name__ == "__main__":
    sys.exit(main())
