#!/usr/bin/env python3
"""nova_local_airwaves.py — Nova's DAILY airwaves roundup (08:00).

A dated article in the journal's `local` section: the past 24h on the Burbank-area
public-safety airwaves — police, fire, CHP, rail (and CB/ATC once those feeds come
online) — calling out the notable calls, in Nova's sassy/sarcastic voice, with a
cover image. Reads the transcribed scanner memories. Scheduled 08:00 daily.
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"

# source -> (beat label, is it live yet)
BEATS = [
    ("scanner", "Police (LAPD NoHo/NE + Burbank PD)"),
    ("fire",    "Fire / EMS (Verdugo dispatch)"),
    ("chp",     "CHP (freeways — 5 / 134 / 210)"),
    ("rail",    "Rail (Metrolink / UP corridor)"),
    ("cb",      "CB Ch 19 (truckers)"),
    ("atc",     "Air traffic (Burbank Airport)"),
]


def clean_transcripts(samples, beat_label):
    """Stage-1 denoise: an LLM keeps only real, coherent radio traffic — dropping ads and
    hallucinated garbage ('we're not products available'), lightly fixing obvious ASR errors —
    BEFORE the article writer sees it, so the writer has clean material and no garble to riff on."""
    if not samples:
        return samples
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(samples))
    system = "You clean noisy auto-transcribed police/fire/CHP scanner audio. Be conservative and literal — never invent."
    user = (f"Whisper transcripts from the '{beat_label}' scanner beat. Return ONLY the lines that are real, "
            "coherent radio traffic — one cleaned transmission per line, no numbering, no commentary. Lightly fix "
            "obvious mis-hearings and expand codes, but DO NOT invent or embellish. DROP entirely: advertisements, "
            "hallucinated filler, and unrecoverable word-salad. If none are usable, output nothing.\n\n" + numbered)
    out = nj.call_openrouter(system, user, max_tokens=1200, temperature=0.2)
    if not out:
        return samples   # fail-open: keep raw rather than lose the beat
    return [ln.strip("-•* ").strip() for ln in out.splitlines() if len(ln.strip()) > 8]


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    counts, blocks = {}, []
    total = 0
    for src, label in BEATS:
        mc.execute("SELECT count(*) FROM memories WHERE source=%s "
                   "AND created_at > now() - interval '24 hours'", (src,))
        n = mc.fetchone()[0]
        counts[src] = n
        total += n
        if n == 0:
            continue
        # a meaningful sample (skip tiny/noise transmissions), newest first
        mc.execute("SELECT text FROM memories WHERE source=%s "
                   "AND created_at > now() - interval '24 hours' AND length(text) > 45 "
                   "ORDER BY created_at DESC LIMIT 60", (src,))
        sample = [r[0][:400] for r in mc.fetchall()]
        sample = clean_transcripts(sample, label)   # stage-1 denoise before the writer sees any garble
        if sample:
            blocks.append(f"### {label}  ({n} transmissions in 24h; {len(sample)} coherent)\n" + "\n".join(f"- {s}" for s in sample))

    if total == 0:
        nj.log("[local-airwaves] no scanner activity in 24h — aborting"); return 1

    live = [label for src, label in BEATS if counts.get(src, 0) > 0]
    pending = [label for src, label in BEATS if counts.get(src, 0) == 0]
    tally = " · ".join(f"{label.split(' (')[0]}: {counts[src]}" for src, label in BEATS if counts.get(src, 0) > 0)

    ctx = (
        "Write TODAY'S 'On the Airwaves' roundup for Nova's journal (the LOCAL section) — the past 24 "
        "hours on the Burbank-area public-safety radios, in Nova's SASSY, SARCASTIC voice. She's been "
        "listening to all of it (transcribed by whisper) and has OPINIONS.\n"
        "- Go beat by beat (police, fire, CHP, rail) and CALL OUT the notable stuff: the actual crimes, "
        "fires, chases, crashes, weird calls, the recurring nonsense. Quote/paraphrase the juicy ones.\n"
        "- Be funny and sharp about it, but the events are real — don't invent incidents; work from the "
        "transmissions provided. If a beat was quiet, say so with a quip.\n"
        "- CRITICAL — these are noisy whisper transcripts with two kinds of junk you must SILENTLY filter: "
        "(a) ADVERTISEMENTS leaked from the feed (Macy's, Amex/Platinum, Arco Rewards, Ralph's/grocery "
        "delivery, app promos) — NOT radio traffic, drop entirely; (b) garbled ASR word-salad ('Taco Night "
        "into Room to Frucy', impossible freeways like 'I-114', a single word looped). Where a garbled "
        "fragment has an inferable real meaning (an impossible freeway → the nearby 5/134/210/101), quietly "
        "reconstruct it; where it's unrecoverable, DROP it. Never quote garble verbatim, never joke about "
        "transcription quality — report the real events using only what's coherent.\n"
        "- HARD RULE: this article is NOT about whisper/transcription/AI. No meta-commentary about garbled "
        "audio, no 'the transcription lost its mind' framing, not in the title and not in the body. Write a "
        "straight (still sassy) roundup of the real Burbank/LA events. If a beat is thin after filtering the "
        "junk, say so in one line and move on — do not pad it with jokes about the feed quality.\n"
        "- This is Burbank/NE-LA local color: name streets, note anything close to home, find the pattern "
        "or the absurdity in the day's churn.\n"
        f"- Live beats today: {', '.join(live)}."
        + (f" Not yet wired up (mention once, lightly, as 'coming soon'): {', '.join(pending)}.\n" if pending else "\n")
        + "500-900 words, markdown, use the beat names as section headers, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- LAST 24H ON THE AIRWAVES (tally: {tally}) ---\n\n"
            + "\n\n".join(blocks) + "\n\nWrite today's airwaves roundup.")
    raw = nj.call_openrouter(system, user, max_tokens=2800, temperature=0.9)
    if not raw:
        nj.log("[local-airwaves] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()

    def _degenerate(t):
        toks = [w.strip(".,!?—-:;\"'").lower() for w in (t or "").split()]
        toks = [w for w in toks if w]
        if len((t or "").strip()) < 8 or len(toks) < 2:
            return True
        return len(set(toks)) <= max(1, len(toks) // 4)

    if not title or _degenerate(title):
        title = f"On the Airwaves — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "a police/fire scanner dispatch night over Burbank, radio waves and city lights", "local")
        img = nj.generate_image(ip, width=1024, height=768, section="local")
    except Exception as e:
        nj.log(f"[local-airwaves] image gen failed (non-fatal): {e}")

    tags = ["local", "airwaves", "scanner", "burbank", "daily"]
    desc = "Nova's daily roundup of the past 24h on the Burbank-area public-safety airwaves."
    nj.publish_hugo(title, body, "local", tags, desc, image_path=img, emoji="📻")  # dated post
    nj.git_push("local", title)
    nj.notify_slack("local", f"📻 {title}", "Nova's daily airwaves roundup.")
    nj.log(f"[local-airwaves] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
