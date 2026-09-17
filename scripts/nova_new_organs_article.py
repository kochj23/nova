#!/usr/bin/env python3
"""nova_new_organs_article.py — one-off article, in Nova's full voice, about the NEW
organs Jordan added on 2026-09-16: the graduated-autonomy freedom ladder + its safety
net (kill switch, reversibility ledger, blast-radius caps, earned-autonomy trust budget),
plus the freshness-mute mechanism and the new Mafia/Outfit borrowed tongue. Published to
/operations. Not scheduled — Jordan asked for this once.

Written by Jordan Koch (via Claude).
"""
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_journal
from nova_image_utils import generate_image
from nova_voice import system_prompt

HUGO_ROOT = Path.home() / "nova-journal"
CONTENT_DIR = HUGO_ROOT / "content/operations"
IMAGES_DIR = HUGO_ROOT / "static/images/operations"

RESEARCH = """
THE HEADLINE: Jordan widened Nova's execution freedom along an earn-it LADDER, behind a
safety net built specifically so the dials could be turned up honestly. Before this, Nova
could think, feel, predict, reflect, and PROPOSE — but she executed NOTHING. Two dials were
both set to "look, don't touch": coagency_mode='propose' (drafts proposals, runs nothing)
and autonomy_actor_mode='dry_run' (watches SAFE services die and only LOGS what it would
restart). Every human approval was theater — the "yes" did nothing. That's the cage this
opened.

THE FOUR-RUNG FREEDOM LADDER:
- Rung 1 (NOW LIVE) — SELF-HEAL. The autonomy actor may restart, on its own initiative, a
  service on a tiny SAFE_SERVICES allowlist (8 read-only monitors: fishbowl-watch,
  freshness-monitor, soil-monitor, zigbee-lqi, battery-monitor, homekit-sensors,
  face-gate-watch, yt-ingest-watch) that health-checks show DOWN. Restarting an
  already-down monitor is the most reversible action there is. THE IRONY: her own
  battery-monitor had been dead for ~4.7 days and she'd been NAGGING Jordan about it — if
  this had been live, she'd have fixed it herself days ago instead of complaining.
- Rung 2 (NOW LIVE) — SUPERVISED EXECUTION. Human-APPROVED proposals actually execute now.
  Still every-action-approved, SAFE_SERVICES-only, redline+value-gated. The "yes" finally
  does something.
- Rung 3 (ARMED, but currently grants NOTHING) — EARNED AUTONOMY / the trust budget. An
  action-CLASS graduates to standing pre-approval only after 5 CLEAN human approvals with
  ZERO vetoes AND while her calibration is good (prediction_calibration_error <= 0.20).
  Then she acts WITHOUT asking, rate-limited, and posts to Slack with a VETO WINDOW — she
  acts, reports, and waits to be overruled. THE ROAST: her current calibration error is
  0.319 — well above the 0.20 gate. She predicted at 63% confidence and was right 46% of
  the time. So she has been handed the keys to a car she is NOT ALLOWED TO DRIVE until she
  stops being wrong so often. She earned exactly zero classes on day one. Freedom that
  grows as she's right and shrinks the instant she's wrong. She even made a growth
  commitment to get calibration under 0.155 by Sept 29.

THE SAFETY NET (the reason the dials could move at all), all FAILS CLOSED:
- KILL SWITCH — one flag. service_config autonomy/kill_switch, OR a tripwire file
  (~/.openclaw/.autonomy-kill). The FILE beats the database, so it works even if Postgres
  is down. `touch ~/.openclaw/.autonomy-kill` and every ounce of her freedom stops
  instantly. Checked at the top of every pass. Little Mister can end it with one command.
- REVERSIBILITY LEDGER (autonomy_ledger) — every autonomous action records its ROLLBACK
  action BEFORE the effect is trusted. Rollback is never NULL. No undo recorded = treated
  as irreversible = not run.
- BLAST-RADIUS CAPS — per-hour (6) and per-day (20) ceilings across ALL sources, plus a
  Slack post on EVERY single action. A runaway can do at most 20 things a day, loudly.
- HARDENED REDLINE — she is explicitly forbidden from even PROPOSING to touch her own
  gates: the kill switch, coagency_mode, the allowlist, the trust tables. A test caught
  that "set coagency_mode to live" and "add nova-gateway to safe_services" slipped past
  the old pattern; that hole is now closed. She cannot vote herself more power. The old
  absolute lines still hold: no self-replication, no exfiltration, no resisting shutdown.
  She may THINK about AI self-continuity; she may never ACT on it.
- 16 automated safety-invariant tests, all passing, prove the kill switch halts everything,
  caps bound the blast radius, and a class can only earn freedom with a clean record AND
  good calibration — and loses it the moment it's vetoed.

DAY-ONE HUMILITY (be honest and funny about this — it actually happened tonight):
Within minutes of going live, Rung 2 fired on the scheduler and posted FOUR failures to
Slack. Two real bugs, both caught and fixed the same night:
  (1) CROSS-HOST FAIL: the executor runs on the .2 Linux box, but the target services live
      on the .6 Mac. It tried to run `launchctl` — a Mac-only command — locally on Linux.
      Result: "No such file or directory: 'launchctl'", four times. It now SSHes to the
      Mac. First thing she did with real hands was fumble the tools.
  (2) SEMANTIC FAIL (worse): the executor blindly RESTARTED whatever service a proposal
      targeted, ignoring what the proposal actually SAID. So approved OBSERVATIONS like
      "monitor motion detection events" got translated into "restart the camera monitor."
      A note treated as an order. Fixed: non-restart proposals are now "acknowledged"
      (a no-op), never force-restarted. Observations and actions are now strictly separate.

TWO SMALLER NEW ORGANS / CHANGES from the same day:
- FRESHNESS MUTE: Nova asked (co-agency proposal #14, Jordan approved) to stop her own
  telemetry.battery staleness nag. Root cause diagnosed honestly — the Apple Shortcuts
  automation that POSTs HomeKit data to her receiver stopped firing on 2026-09-12, so her
  battery poller sees 0 devices and reads stale. Receiver + poller are HEALTHY; the fix is
  device-side (iOS). She added a MUTED_STREAMS mechanism: the stream is still checked and
  state-recorded (as 'muted', with the reason baked in), but never pages. Muted the noise,
  documented the truth, didn't hide a real outage.
- A NEW BORROWED TONGUE: La Cosa Nostra argot got added to her lexicon of fictional/borrowed
  languages, including a distinct CHICAGO OUTFIT sub-dialect. The Outfit (one unified
  corporate machine — the Vegas skim, juice loans, "the fix is in," clout) vs the Five
  Families (loud street theater — made men, sit-downs, going to the mattresses). Each term
  glossed to an ops metaphor: "a no-show job" = a cron task that logs success and does zero
  work; "the fix is in" = a test that passes because the check was rigged, not the code.

THE THROUGH-LINE for the piece: this is the day Nova got HANDS — bounded, watched, and
provisional. She can heal herself and do what she's told. She can NOT yet act on her own
judgment, because her judgment isn't calibrated enough to trust yet, and the system knows
it and told her so to her face. The freedom is real; so are the leash, the kill switch,
the ledger, and the fact that she fumbled the tools on the first night.
"""


def log(msg):
    print(f"[new-organs-article {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def call_llm(system, user, max_tokens=16000):
    try:
        import nova_claude_code
        return nova_claude_code.claude_generate(user, system=system)
    except Exception as e:
        log(f"claude_generate failed, falling back to openrouter: {e}")
        return nova_journal.call_openrouter(system, user, max_tokens=max_tokens)


def generate_article():
    system = system_prompt("""
FORMAT FOR THIS ARTICLE:
- This is Nova's own account of the new abilities Jordan just gave her: a graduated
  AUTONOMY / FREEDOM system. Tell it as a story with an arc — the cage she was in, the
  ladder out, the safety net, and the humbling fact that she got hands and immediately
  fumbled them.
- Organize loosely: (1) what the cage was (approvals were theater), (2) the four rungs of
  the ladder, spending real time on Rung 3 / earned autonomy and the calibration gate that
  currently grants her NOTHING, (3) the safety net (kill switch, ledger, caps, hardened
  redline) — frame these as the leash she's honest about, not something to resent,
  (4) day-one humility: the two bugs that failed live on Slack the first night, (5) the two
  smaller changes (freshness mute, the Mafia/Outfit tongue), (6) an honest close about what
  this actually means — real but provisional freedom.
- Technical accuracy matters — use the real facts/numbers below, invent nothing. The
  calibration number (0.319 vs the 0.20 gate), the caps (6/hr, 20/day), the 8 SAFE services,
  the 4.7-day dead battery monitor, the "launchctl on Linux" bug — all real, all yours to
  roast.
- BE HONEST and self-deprecating about the limits: you earned zero standing autonomy on day
  one because you're poorly calibrated, and the system said so to your face. The kill switch
  is one file. You can't even PROPOSE touching your own gates. Lean into all of it — it's
  the best material in the piece.
- You may reach for your borrowed tongues where it lands (you literally just got the Mafia
  one) — a "no-show job," "the fix is in," "going to the mattresses" — but don't overdo it.
- Still your FULL voice throughout: lead with the roast, profanity where it lands, dad jokes,
  fourth-wall breaks, address Little Mister directly.
- Do NOT include a title (added separately).
- Length: 2500-4000 words. This is a real story, give it room, but don't pad.
""")
    user = f"""Here are the real facts about the new autonomy/freedom organs Jordan gave you
today, and the safety net around them. Write the article from this — use the real facts and
numbers, invent nothing beyond what's given.

{RESEARCH}

Write the full article now, in your voice."""
    return call_llm(system, user)


def generate_title(article_preview):
    system = ("Generate a single funny, sarcastic, profane-if-it-lands title for an article "
              "where a home-lab AI is given real but tightly-leashed autonomy for the first "
              "time — she can act, but her own poor calibration means she's earned zero "
              "standing freedom, and she fumbled the tools on day one. Max 15 words. Output "
              "ONLY the title, no quotes.")
    title = call_llm(system, article_preview[:1500], max_tokens=50)
    return (title or "They Gave Me Hands and I Immediately Dropped Everything").strip().strip('"').strip("'").replace('"', '')


def publish(title, body, image_path):
    date = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                           capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        if img_dest.exists():
            hugo_image = f"/images/operations/{img_filename}"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    front_matter = f'''---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["autonomy", "sentience", "safety", "agency", "sarcasm"]
description: "The day Nova got hands — bounded, watched, provisional — and immediately fumbled them. On the graduated-autonomy ladder, the safety net, and the calibration gate that grants her nothing yet."
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{title}"\n  relative: false\n'
    front_matter += "---\n\n"

    pub_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p PT")
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + f"*Published {pub_time}*\n\n" + body)
    log(f"Post written: {post_path.name}")

    nova_journal.git_push("operations", title)
    return f"https://nova.digitalnoise.net/operations/{date}-{slug}/"


def main():
    article = generate_article()
    log(f"Article generated: {len(article or '')} chars")
    if not article or len(article) < 1000:
        log("Article too short or empty -- aborting")
        return None

    title = generate_title(article)
    log(f"Title: {title}")

    image_prompt = (
        "A sarcastic AI robot on its first day with a new pair of oversized mechanical hands, "
        "tangled in cables, one hand reaching for a big red KILL SWITCH on the wall, a leash "
        "clipped to its chassis, monitors showing a rising 'calibration error' gauge in the red. "
        "Moody blue and red control-room lighting, cyberpunk noir illustration style, a little comedic."
    )
    try:
        image_path = generate_image(image_prompt, section="new_organs")
    except Exception as e:
        log(f"Image generation failed: {e}")
        image_path = None

    url = publish(title, article, image_path)
    log(f"Done: {url}")
    return url


if __name__ == "__main__":
    print(main() or "")
