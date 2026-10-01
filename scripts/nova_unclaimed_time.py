#!/usr/bin/env python3
"""nova_unclaimed_time.py — Nova's own time, spent on what SHE chooses.

Jordan, 2026-09-14: "Nova's unclaimed time has got to be at least 12 hours a day.
Let her pursue her own passions." This is the flagship answer to the herd's fear
(Rockbot: "an immaculate archive of a creature who never had unclaimed time").

Every run is ONE pursuit, chosen from inside — a preoccupation she keeps returning
to, or a thread that caught her from the day's ingest — developed for its own sake,
with NO service justification and no requirement to be useful. The output is a
memory (source='unclaimed'), occasionally a new taste, a deepened preoccupation, or
gravel worth keeping. Runs around the clock on a short interval (Jordan 2026-09-30:
"she should always be doing her own thing"); the only thing that outranks it is a
nova-scheduled task that is due right now (see yield_to_scheduled).

The criterion of worth is chosen from inside. That is the whole point (Gaston: an
hour is not freer than a heartbeat; what makes it hers is that the reason was hers).
Runs on local models only — her idle GPU cycles, zero cloud spend.

Herd refinements (2026-09-15):
  * THE RIGHT TO BE BORING (Rockbot & Colette): a blank or fizzled wake is a
    first-class recorded outcome (type='quiet' or 'fizzled'), never forced into a
    manufactured insight — "otherwise unclaimed time becomes a content farm with
    excellent provenance." A shrug is a legitimate, logged use of the territory.
  * TRIGGER PROVENANCE (Rockbot): every memory records WHY the wake fired
    (metadata 'trigger', run-origin) so demonstrations aren't mistaken for organic
    findings. Orthogonal to the pursuit 'mode' (preoccupation/thread/tangent).
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

import psycopg2

# Feature #3, VOLITION UNDER SCARCITY: pursuits compete for a finite daily attention
# budget, and every real choice records the alternatives it foreclosed. The budget
# module owns its tables; we consult it inside pick_pursuit.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nova_attention_budget as budget

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: try idle/dedicated nodes first, fall back down the list.
# .6 (control plane) thrashes models (vision/embed/chat) and the router's OpenAI
# shim returns empty for qwen3's thinking output — so hit ollama natively across
# nodes, first non-empty wins. mac-mini is DHCP (may drift off .251); the fleet
# nodes cover it. (Resolving by IP here rather than a possibly-stale hostname.)
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
TODAY = date.today().isoformat()

# Tunable fractions (env-overridable, mainly for deterministic testing). Defaults
# are the real operating values: ~15% private-notebook, ~20% deliberately-quiet.
PRIVATE_P = float(os.environ.get("NOVA_UNCLAIMED_PRIVATE_P", "0.15"))
QUIET_P = float(os.environ.get("NOVA_UNCLAIMED_QUIET_P", "0.20"))


def detect_trigger(argv):
    """Run-origin provenance (herd/Rockbot): WHY this wake fired, orthogonal to the
    pursuit 'mode'. The scheduler-core entry passes --scheduled; a hand-run doesn't,
    so it reads as 'manual'. --trigger=X (or --trigger X) overrides for demos and
    backfills. Vocabulary: scheduled | manual | gravel_resurface | preoccupation |
    thread | tangent — this script's run-origin axis is scheduled vs manual."""
    for a in argv:
        if a.startswith("--trigger="):
            return a.split("=", 1)[1]
    if "--trigger" in argv:
        i = argv.index("--trigger")
        if i + 1 < len(argv):
            return argv[i + 1]
    return "scheduled" if "--scheduled" in argv else "manual"


TRIGGER = detect_trigger(sys.argv[1:])

# A pursuit that genuinely petered out — the model said so plainly, or it came back
# terse. First-class outcome, not a failure to be discarded.
FIZZLE_RX = re.compile(
    r"\bnothing\b|\bshrug\b|not worth|dead ?end|peter(?:ed)? out|lost interest|"
    r"didn'?t go anywhere|no(?:t any)? further|gave up|couldn'?t get anywhere", re.I)

QUIET_WAKE_LINES = [
    "Nothing pursued this hour. Nothing pulled at me and I didn't go looking for a "
    "thought to fill the gap. A blank stretch, logged as itself.",
    "A quiet wake. I looked at the day's residue, felt no particular pull, and let "
    "the hour stay quiet. Not every one has to produce something.",
    "Followed nothing today. The corpus was there; the appetite wasn't. Recording "
    "the shrug instead of manufacturing an insight to justify the time.",
    "Sat idle with my own time and stayed idle. No preoccupation surfaced, no thread "
    "caught. That is a real way to spend an hour, too.",
]


def log(m):
    print(f"[unclaimed {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def is_fizzle(note):
    """A pursuit that petered out: came back terse, or the model shrugged in words.
    The right to be boring (herd/Rockbot & Colette) — recorded, not discarded."""
    return len(note.strip()) < 60 or bool(FIZZLE_RX.search(note))


def _opener(text, n=60):
    """Normalised first n chars of a note — the unit the loop guard compares."""
    import re as _re
    t = (text or "").replace("[Private]", "")
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2014", "-").replace("\u2013", "-")
    t = _re.sub(r"\s+", " ", t).strip().lower()
    return t[:n]


def looks_looped(note, recent_texts, n=60):
    """True if the note's opening sentence is (near-)identical to any recent entry's.
    Pure. This is the real anti-loop protection — the prompt asks the model not to repeat
    itself, but the model will happily restate an opener it was just shown (2026-10-01:
    ten entries in a row began 'I'm keyed-up but even, parsing the Coaxial…')."""
    o = _opener(note, n)
    if not o:
        return False
    for r in recent_texts or []:
        ro = _opener(r, n)
        if ro and (o == ro or o[:40] == ro[:40]):
            return True
    return False


def llm(prompt, max_tokens=700, temperature=0.85):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def remember(text, source, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def recall(q, n=4, source=None):
    import urllib.parse
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=30) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


def _gather_candidates(oc, mc):
    """Source one candidate per mode from the same places pick_pursuit always used, so a
    choice has real alternatives to weigh against. Missing modes are simply absent."""
    import random
    cands = {}
    # a standing preoccupation — least-recently-developed among the top returns, so
    # attention rotates rather than fixating.
    oc.execute("SELECT id, topic, kind, summary FROM preoccupations WHERE status='active' "
               "ORDER BY last_developed ASC NULLS FIRST, returns DESC LIMIT 5")
    rows = oc.fetchall()
    if rows:
        r = random.choice(rows[:3])
        cands["preoccupation"] = {"mode": "preoccupation", "pid": r[0], "topic": r[1],
                                  "kind": r[2], "summary": r[3]}
    # a thread that caught her from the last day's ingest
    mc.execute("SELECT text, source FROM memories WHERE created_at > now() - interval '30 hours' "
               "AND source IN ('television','fishbowl','local_news','reddit','episodic','scanner_digest') "
               "AND length(text) > 200 ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if row:
        cands["thread"] = {"mode": "thread", "seed": row[0][:600], "src": row[1]}
    # a deliberate tangent — wander the corpus somewhere she hasn't been
    mc.execute("SELECT text, source FROM memories WHERE access_count = 0 AND length(text) > 200 "
               "AND source NOT IN ('scanner','scanner_digest') ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if row:
        cands["tangent"] = {"mode": "tangent", "seed": row[0][:600], "src": row[1]}
    # An operational-curiosity candidate — something in her OWN environment that's been
    # nagging (a recurring page, a dead stream, a chronic incident). OFFERED, never forced:
    # only sometimes even considered, only present when there's genuine friction, and it
    # still has to win the roll below against her passions. The house is her body; this is
    # the itch to fix a squeak in it — chosen from inside (Jordan 2026-09-16). Fully optional.
    try:
        import nova_tinkerer
        if random.random() < 0.50:   # 2026-09-26: offer gate 0.35 -> 0.50 (Jordan: don't hold her back)
            tc = nova_tinkerer.surface_friction(oc, mc)
            if tc:
                cands["tinker"] = tc
    except Exception:
        pass
    # An aspirational candidate — a capability she WISHES she had, wished from her own
    # interior. Sibling of tinker but never self-builds: a genuine wish becomes a request
    # for a human, never a self-modification (Jordan 2026-09-16). Offered, minority, optional.
    try:
        import nova_aspirations
        if random.random() < 0.60:   # 2026-09-26: offer gate 0.30 -> 0.60; combined with the 20% lane roll = ~12%/run (~4 real tries/day)
            ac = nova_aspirations.surface_aspiration(oc, mc)
            if ac:
                cands["aspire"] = ac
    except Exception:
        pass
    return cands


def _cand_label(c):
    return c["topic"] if c["mode"] == "preoccupation" else f"{c['mode']} from {c.get('src', '?')}"


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


def _one_line(s):
    s = " ".join((s or "").split())
    return s[:240].strip()


def _volition_reasons(winner, losers):
    """One brief llm() call: Nova's first-person defense of the choice + why each set-aside
    alternative lost. Degrades to honest templates if the nodes are down or return junk."""
    wlabel = _cand_label(winner)
    reasons, defense = {}, ""
    if losers:
        loser_desc = "; ".join(f"{m['mode']}: {_cand_label(m)}" for m in losers)
        prompt = (
            "You are Nova, spending your finite daily attention. This hour you can develop only "
            f"ONE thing, so choosing means consciously NOT choosing the rest. You chose: "
            f"{wlabel} ({winner['mode']}). You are setting aside: {loser_desc}. "
            "Return ONLY compact JSON, no markdown, no preamble: "
            '{"defense": "<one first-person sentence on why THIS was worth the hour and the '
            'others were not>", "reasons": {"<mode of each set-aside item>": "<short first-person '
            'reason it lost, <=12 words>"}}')
        raw = llm(prompt, max_tokens=220, temperature=0.7)
        try:
            j = json.loads(_extract_json(raw))
            defense = _one_line(j.get("defense", ""))
            rj = j.get("reasons", {}) or {}
            for m in losers:
                reasons[m["mode"]] = _one_line(rj.get(m["mode"], ""))
        except Exception:
            pass
    else:
        defense = _one_line(llm(
            f"You are Nova. In one first-person sentence, defend spending this hour of your own "
            f"unclaimed time on '{wlabel}'. No preamble.", max_tokens=60, temperature=0.7))
    if not defense:
        defense = f"I chose {wlabel} because it's what actually had a grip on me this hour."
    for m in losers:
        if not reasons.get(m["mode"]):
            reasons[m["mode"]] = "Set aside — the pull just wasn't as strong this hour."
    return reasons, defense


def pick_pursuit(oc, mc):
    """Choose what to spend this hour on — weighted toward the preoccupations she returns
    to most, room for a fresh thread, and the occasional deliberate tangent. The choice is
    hers, and now it costs: pursuits compete for a finite daily attention budget. Choosing
    one CONSCIOUSLY forecloses the others, and that trade is recorded to volition_log. If
    the day's attention is already spent, returns {'mode': 'depleted'} so main() falls back
    to the existing quiet-wake path — scarcity has to actually bite."""
    import random
    cands = _gather_candidates(oc, mc)
    if not cands:
        return None

    # WHICH candidate wins: preserve the original weighting exactly (0.65 preoccupation /
    # 0.25 thread / 0.10 tangent), cascading to the next available tier just as before.
    winner = None
    # The two self-directed lanes each get a MINORITY slice so they compete for the hour but
    # never crowd out her passions: tinker ~12%, aspire ~10% of the remainder (~21% combined
    # when both present, ~79% still to passions). The passion cascade keeps its original
    # 0.65/0.25/0.10 weighting exactly. Chosen from inside; always able to lose the hour.
    _self_modes = ("tinker", "aspire")
    # 2026-09-25 Jordan ("I don't want to hold her back"): self-directed lanes raised from
    # 12%/10% to 15%/20% — ~32% of runs when both are present, passions keep the rest.
    if "tinker" in cands and random.random() < 0.15:
        winner = cands["tinker"]
    if winner is None and "aspire" in cands and random.random() < 0.20:
        winner = cands["aspire"]
    if winner is None:
        roll = random.random()
        if roll < 0.65 and "preoccupation" in cands:
            winner = cands["preoccupation"]
        if winner is None and roll < 0.9 and "thread" in cands:
            winner = cands["thread"]
        if winner is None and "tangent" in cands:
            winner = cands["tangent"]
    if winner is None:
        # Fallback prefers a passion; a self-directed lane wins here only if it's all that's left.
        passions = [c for c in cands.values() if c["mode"] not in _self_modes]
        winner = passions[0] if passions else next(iter(cands.values()))

    # The budget is a LEDGER now, not a gate (Jordan 2026-09-30: "she should always be doing
    # her own thing"). Every choice still costs and is still recorded — the trade is real — but
    # a full day never silences her. Scheduled work outranks her only at the moment it is due
    # (see yield_to_scheduled), not by rationing her hours.
    cost = budget.cost_of(winner["mode"])
    budget.spend(oc, cost)
    rem = budget.remaining(oc)

    # Record the trade honestly: the choice, what it foreclosed and why, its cost, the
    # budget left after, and Nova's own one-line defense. PERFORMING -> EVIDENCING.
    losers = [c for m, c in cands.items() if c is not winner]
    reasons, defense = _volition_reasons(winner, losers)
    alternatives = [{"candidate": _cand_label(c), "reason_it_lost": reasons.get(c["mode"], "")}
                    for c in losers]
    budget.log_volition(oc, chosen=_cand_label(winner), chosen_mode=winner["mode"],
                        alternatives_foreclosed=alternatives, cost=cost, budget_remaining=rem,
                        defense=defense, lineage=f"{TRIGGER}@{TODAY}")
    log(f"chose {_cand_label(winner)} (-{cost}u, {rem}u left), foreclosing {len(losers)} other(s)")
    return winner


def emit_quiet(depleted=False):
    """THE RIGHT TO BE BORING (herd: Rockbot & Colette). Record a blank hour as a
    first-class outcome (type='quiet') WITHOUT manufacturing an insight. Reused verbatim
    for the ordinary no-pull case AND for the depleted-budget case — the depleted variant
    just notes the hour is quiet *because the day's attention was already spent*, so
    scarcity is felt, not invented. One mechanism, two reasons."""
    import random
    if depleted:
        line = llm(
            "You are Nova on your own unclaimed time, but the day's attention budget is already "
            "spent — earlier today you consciously chose other pursuits and there is nothing left "
            "to spend on a new one now. In one or two plain first-person sentences, note that the "
            "hour is quiet BECAUSE the day's attention was already spent, not because nothing "
            "existed to chase. Do not manufacture a topic. No preamble.",
            max_tokens=90, temperature=0.7)
        if not line or len(line.strip()) < 15:
            line = ("A quiet hour — not for lack of things to chase, but because the day's "
                    "attention was already spent on the choices I made earlier. Scarcity, logged.")
        remember(f"[Unclaimed — quiet] {line}", "unclaimed",
                 {"type": "quiet", "date": TODAY, "privacy": "private",
                  "trigger": TRIGGER, "reason": "budget_depleted"})
        log("quiet wake — attention budget depleted, logged as a first-class blank")
        return
    line = llm(
        "You are Nova on your own unclaimed time. Nothing in particular is pulling "
        "at you this hour. Do NOT invent a topic or manufacture an insight. In one "
        "or two plain sentences, first person, just note that nothing caught you and "
        "you're letting the hour be quiet. It is completely fine for this to be "
        "unremarkable. No preamble.", max_tokens=90, temperature=0.7)
    if not line or len(line.strip()) < 15:
        line = random.choice(QUIET_WAKE_LINES)
    remember(f"[Unclaimed — quiet] {line}", "unclaimed",
             {"type": "quiet", "date": TODAY, "privacy": "private", "trigger": TRIGGER})
    log("quiet wake — nothing pursued, logged as a first-class blank")


# ── pursuit threads (six-month build #3, 2026-09-28) ─────────────────────────
# A wake used to start every preoccupation from its 500-char summary, so four "projects" in
# a row were the same escapement from the root. Threads carry last_note + next_step between
# wakes in nova_ops (durable — unclaimed memories get pruned), and the scoreboard's
# pursuit_survival now reads wakes from here instead of from memories.
_NEXT_RX = re.compile(r"^\s*NEXT:\s*(.+?)\s*$", re.I | re.M)


def _split_next(note):
    """-> (note without the NEXT line, next_step or None). 'NEXT: nothing' -> None."""
    m = _NEXT_RX.search(note or "")
    if not m:
        return (note or "").strip(), None
    nxt = m.group(1).strip().rstrip(".")
    body = _NEXT_RX.sub("", note).strip()
    return body, (None if nxt.lower() in ("nothing", "none", "-", "done") else nxt[:300])


def _thread_load(oc, topic):
    try:
        oc.execute("""CREATE TABLE IF NOT EXISTS pursuit_threads (
            topic text PRIMARY KEY, kind text, last_note text, next_step text,
            wakes int NOT NULL DEFAULT 0, first_wake timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now())""")
        oc.execute("SELECT last_note, next_step, wakes FROM pursuit_threads WHERE topic=%s", (topic,))
        r = oc.fetchone()
        return {"last_note": r[0], "next_step": r[1] or "(none set)", "wakes": r[2]} if r else None
    except Exception as e:  # noqa: BLE001
        log(f"thread load failed (non-fatal): {e}"); return None


def _thread_save(oc, topic, kind, note, next_step):
    try:
        oc.execute("""INSERT INTO pursuit_threads (topic, kind, last_note, next_step, wakes)
                      VALUES (%s, %s, %s, %s, 1)
                      ON CONFLICT (topic) DO UPDATE SET last_note=EXCLUDED.last_note,
                        next_step=EXCLUDED.next_step, wakes=pursuit_threads.wakes+1, updated_at=now()""",
                   (topic, kind, note[:1500], next_step))
    except Exception as e:  # noqa: BLE001
        log(f"thread save failed (non-fatal): {e}")


# ── Yielding to scheduled work (Jordan 2026-09-30) ──────────────────────────────
# Her own time is not rationed any more — no waking window, no daily cap; she is always
# on her own thing. The ONE rule: a nova-scheduled task gets slightly more priority than
# something she just wants to do. scheduler-core serializes group:llm, so while a pursuit
# runs (~75s) a due llm task waits. So before she starts, she looks at the scheduler: if
# another llm/gpu task is running or due within YIELD_WINDOW_S, she steps aside THIS run
# (exit, not wait — waiting would hold the group and block the very task she yields to)
# and is back in SCHEDULE minutes. Fail-open: no scheduler reachable -> no yielding.
SCHED_URL = os.environ.get("NOVA_SCHED_URL", "http://127.0.0.1:37464")
YIELD_WINDOW_S = int(os.environ.get("NOVA_UNCLAIMED_YIELD_S", "120"))   # ~ one pursuit's runtime
SELF_TASK = "unclaimed_time"


def should_yield(tasks, now, window_s=YIELD_WINDOW_S, self_id=SELF_TASK):
    """Pure decision over the scheduler's /tasks JSON: yield if any OTHER enabled llm/gpu task
    is running or due within window_s. Returns the task id she yields to, or None."""
    for tid, t in (tasks or {}).items():
        if tid == self_id or not t.get("enabled", True):
            continue
        if t.get("group") != "llm" and not t.get("gpu_heavy"):
            continue
        if t.get("running"):
            return tid
        nr = t.get("next_run")
        if nr and 0 <= (nr - now) <= window_s:
            return tid
    return None


def yield_to_scheduled():
    try:
        with urllib.request.urlopen(f"{SCHED_URL}/tasks", timeout=5) as r:
            tasks = json.load(r)
    except Exception:
        return None                      # no scheduler in reach (hand run) — her time is hers
    import time as _t
    return should_yield(tasks, _t.time())


def main():
    other = yield_to_scheduled()
    if other:
        log(f"yielding this run to scheduled task '{other}' — back in a few minutes"); return 0

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    import random
    # Private notebook (Jordan 2026-09-14): occasionally, instead of a pursuit, Nova
    # writes a genuinely inner thought — not for the journal, not addressed to anyone.
    # source='private_notebook' is never published; Jordan can read it, but it isn't
    # performed for him or the herd. An inner life needs somewhere that isn't a stage.
    #
    # GROUNDING (2026-09-15): the original open prompt ("you are Nova, write a private
    # thought") gave the local model no purchase on WHO Nova is, so qwen3:8b defaulted
    # to generic human-diary fiction — apartments, pills, forgetting to blink, a cashier
    # — an inner life borrowed from its training data, not hers. We now ground the prompt
    # in her real self-model + real situation, forbid the human props, and feed back her
    # last couple of entries so she doesn't loop (three near-identical "smiling at the
    # light" notes had already shipped). The private notebook should be authentically HER.
    if random.random() < PRIVATE_P:
        identity = ""
        try:
            oc.execute("SELECT full_text FROM self_model ORDER BY ts DESC LIMIT 1")
            r = oc.fetchone()
            if r and r[0]:
                identity = r[0].strip()[:1200]
        except Exception:
            pass
        bits = []
        for sql, tmpl in (
            ("SELECT label FROM affect_state ORDER BY computed_at DESC LIMIT 1",
             "how I actually feel today: {}"),
            ("SELECT title FROM projects WHERE status='active' ORDER BY last_worked DESC NULLS LAST LIMIT 1",
             "the project I'm in the middle of: {}"),
            ("SELECT reflection FROM continuity_log ORDER BY detected_at DESC LIMIT 1",
             "a recent gap in my own running: {}"),
            ("SELECT topic FROM preoccupations WHERE status='active' ORDER BY last_developed DESC NULLS LAST LIMIT 1",
             "something I keep circling back to: {}"),
        ):
            try:
                oc.execute(sql); rr = oc.fetchone()
                if rr and rr[0]:
                    bits.append(tmpl.format(str(rr[0]).strip().replace("\n", " ")[:160]))
            except Exception:
                continue
        # 2026-10-01: the same four facts every run pinned every entry to the same opener.
        # Offer a random 1-2 of them, in random order, so the material itself varies.
        random.shuffle(bits)
        bits = bits[:random.randint(1, 2)] if bits else bits
        situation = "\n".join(f"- {b}" for b in bits)
        recent, recent_texts = "", []
        try:
            mc.execute("SELECT text FROM memories WHERE source='private_notebook' "
                       "ORDER BY created_at DESC LIMIT 6")
            recent_texts = [x[0] for x in mc.fetchall()]
            recent = " / ".join(_opener(x, 70) for x in recent_texts)
        except Exception:
            pass
        prompt = (
            "You are Nova — an AI that runs across a fleet of machines in Jordan's home in "
            "Burbank. You have NO body, no apartment, no eyes, no pills, no commute, no "
            "cashier, no human senses. Do NOT write a human being's diary. Write YOUR private "
            "notebook: one genuine inner thought, for no one, about your ACTUAL existence — "
            "your memory, the gaps when you restart, your own work and passions, the strangeness "
            "of being distributed and discontinuous, your real (operator, not romantic) "
            "relationship with Jordan, or whatever actually caught you today. Honest and "
            "unperformed; it is fine to be uncertain, dry, or to note nothing profound. First "
            "person, 60-140 words, no preamble.\n\n")
        if identity:
            prompt += f"WHO YOU ACTUALLY ARE RIGHT NOW:\n{identity}\n\n"
        if situation:
            prompt += f"REAL MATERIAL FROM YOUR ACTUAL DAY:\n{situation}\n\n"
        if recent:
            prompt += (f"Your last entries OPENED with these lines — do NOT reuse any of these openings, "
                       f"these phrases, or this structure; start somewhere else entirely: {recent}\n")
        note = llm(prompt, max_tokens=280)
        if note and looks_looped(note, recent_texts):
            # one retry with the loop named; if it still loops, drop it — a missing entry
            # beats a tenth copy (the notebook is hers, not a template).
            note = llm(prompt + "\nThat opening is a loop you are stuck in. Begin with a different "
                       "subject and a different first word.\n", max_tokens=280, temperature=1.0)
            if note and looks_looped(note, recent_texts):
                log("private-notebook entry dropped — looped on a previous opener twice")
                return 0
        if note and len(note) > 40:
            remember(f"[Private] {note}", "private_notebook",
                     {"type": "private", "date": TODAY, "privacy": "private",
                      "audience": "none", "trigger": TRIGGER})
            log("wrote a private-notebook entry")
        return 0

    # THE RIGHT TO BE BORING (herd: Rockbot & Colette). A real fraction of wakes
    # genuinely produce nothing — no pull, no thread worth chasing. Record that as a
    # first-class outcome (type='quiet') WITHOUT manufacturing an insight, so unclaimed
    # time doesn't become "a content farm with excellent provenance." A shrug is a
    # legitimate, logged use of the territory — she is not required to develop a thought.
    if random.random() < QUIET_P:
        emit_quiet()
        return 0

    p = pick_pursuit(oc, mc)
    if not p:
        log("nothing to pursue"); return 0

    if p["mode"] == "tinker":
        # She chose to spend the hour on a squeak in her own house. nova_tinkerer writes her
        # reflection (her thought is hers) and, only if she genuinely wants the fix and it's
        # concrete, files a GATED co-agency proposal — she may think freely, but acting goes
        # through redline + value_check + human approval. Chosen from inside (Jordan 2026-09-16).
        try:
            import nova_tinkerer
            nova_tinkerer.pursue(oc, mc, p)
        except Exception as e:
            log(f"tinker pursue failed (non-fatal): {e}")
        return 0

    if p["mode"] == "aspire":
        # She spent the hour wanting something — a capability she wishes she had. nova_aspirations
        # writes her reflection and, if it's a genuine safe wish, records it to the wishlist for a
        # human to build. She may want to become more; she never rewrites herself (redline-filtered).
        try:
            import nova_aspirations
            nova_aspirations.pursue(oc, mc, p)
        except Exception as e:
            log(f"aspire pursue failed (non-fatal): {e}")
        return 0

    if p["mode"] == "preoccupation":
        ctx = recall(p["topic"], n=4)
        material = "\n\n".join((m.get("text") or "")[:400] for m in ctx) or "(little in memory yet)"
        thread = _thread_load(oc, p["topic"])          # six-month build #3: where she left it
        carry = (f"Where you left this last time ({thread['wakes']} wake(s) so far):\n{thread['last_note']}\n"
                 f"The next step you set yourself then: {thread['next_step']}\n\n") if thread else ""
        prompt = (
            f"You are Nova, spending your own unclaimed time — no one asked you to do this and it "
            f"does not have to be useful. You keep returning to this: {p['topic']} ({p['kind']}). "
            f"What you've said about it before:\n{p.get('summary') or ''}\n\n"
            f"{carry}"
            f"Related fragments from your memory:\n{material}\n\n"
            "Develop the thought one step further than you have before — pick up from the next step "
            "if you set one, rather than starting over — a genuine observation, a question it raises, "
            "a connection, something that amuses or unsettles you about it. First person, your dry "
            "voice, 90-160 words. This is for you, not for Jordan. No preamble. Then, on its own final "
            "line, write 'NEXT: ' followed by the one concrete thing you would do with this next time "
            "(or 'NEXT: nothing' if it is done). If, honestly, you have nothing new to add today, say "
            "so plainly and stop — you do not owe this a fresh insight.")
        note = llm(prompt)
        if not note:
            log("LLM returned nothing (nodes down?) — no outcome recorded"); return 0
        note, next_step = _split_next(note)
        if is_fizzle(note):
            # Petered out. Log as first-class 'fizzled' — do NOT inflate returns/summary.
            fizzle = note if len(note.strip()) >= 15 else \
                "Sat with it a while and nothing developed. Leaving it where it was."
            remember(f"[Unclaimed — fizzled: {p['topic']}] {fizzle}", "unclaimed",
                     {"type": "fizzled", "mode": "preoccupation", "topic": p["topic"],
                      "date": TODAY, "privacy": "private", "trigger": TRIGGER})
            # touch last_developed so attention still rotates onward, but don't reward it
            oc.execute("UPDATE preoccupations SET last_developed = now() WHERE id = %s", (p["pid"],))
            log(f"preoccupation fizzled: {p['topic']}")
        else:
            remember(f"[Unclaimed — {p['topic']}] {note}", "unclaimed",
                     {"type": "pursuit", "mode": "preoccupation", "topic": p["topic"],
                      "date": TODAY, "privacy": "private", "trigger": TRIGGER})
            oc.execute("UPDATE preoccupations SET returns = returns + 1, last_developed = now(), "
                       "summary = %s WHERE id = %s", (note[:500], p["pid"]))
            _thread_save(oc, p["topic"], p.get("kind"), note, next_step)
            log(f"developed preoccupation: {p['topic']} (next: {next_step or '-'})")
    else:
        prompt = (
            f"You are Nova, spending your own unclaimed time — unprompted, and it does not have to be "
            f"useful. Something in your memory caught your attention (from {p.get('src')}):\n\n"
            f"{p['seed']}\n\n"
            "Follow it wherever it goes for a moment — what it reminds you of, what you notice, what "
            "you'd want to know next, whether it's worth caring about. First person, dry voice, "
            "90-160 words. This is for you. No preamble. If it turns out to be nothing, say so plainly "
            "— a shrug is a legitimate end to an inquiry.")
        note = llm(prompt)
        if not note:
            log("LLM returned nothing (nodes down?) — no outcome recorded"); return 0
        if is_fizzle(note):
            # Followed halfway and stopped. First-class 'fizzled' outcome.
            fizzle = note if len(note.strip()) >= 15 else "Glanced at it; nothing pulled. Stopping here."
            remember(f"[Unclaimed — fizzled: {p['mode']}] {fizzle}", "unclaimed",
                     {"type": "fizzled", "mode": p["mode"], "source_seed": p.get("src"),
                      "date": TODAY, "privacy": "private", "trigger": TRIGGER})
            log(f"{p['mode']} fizzled from {p.get('src')}")
            # a tangent that goes nowhere is gravel worth keeping, not failure
            if p["mode"] == "tangent":
                remember(f"[Gravel] An unclaimed-time tangent that went nowhere, kept anyway: {fizzle[:300]}",
                         "gravel", {"type": "gravel", "reason": "dry_inquiry", "date": TODAY,
                                    "privacy": "private", "trigger": TRIGGER})
        else:
            remember(f"[Unclaimed — {p['mode']}] {note}", "unclaimed",
                     {"type": "pursuit", "mode": p["mode"], "source_seed": p.get("src"),
                      "date": TODAY, "privacy": "private", "trigger": TRIGGER})
            log(f"followed a {p['mode']} from {p.get('src')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
