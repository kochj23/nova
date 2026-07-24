#!/opt/homebrew/bin/python3
"""Third pass on the rack-rebuild ops column: append everything that happened
AFTER it was published — Wave 3 migration finish line, two real production bugs
found and fixed, raw_classification, the Fishbowl pipeline, a full queue backlog
sweep, a live pentest with a real vuln found and fixed, and five external
open-source PRs. Updates the SAME published post in place (same slug/URL)."""
import os
import sys, time, re, shutil, subprocess
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_rando_daily_ops import call_llm, HUGO_ROOT, CONTENT_DIR
from nova_image_utils import generate_image
from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS

LOG = Path.home() / ".openclaw/logs/ops_article_update3.log"
def log(m): LOG.open("a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); print(m, flush=True)

EXISTING_SLUG = "2026-07-19-please-update-your-records-the-house-is-now-haunted-by-a-man"
EXISTING_TITLE = "PLEASE UPDATE YOUR RECORDS: THE HOUSE IS NOW HAUNTED BY A MAN WHO CHANGED HIS OWN IP AND TOLD NO ONE"

MATERIAL = """
FRAME: this is a THIRD pass on the same article. The rack rebuild is done, the NVR came home, the five siblings
are introduced. What follows happened in the hours AFTER that article published — the same night, rolling
straight into a marathon session that finished the actual software migration the rebuild had been setting up for,
found two real bugs, and then went well past infrastructure into contributing to other people's open-source
projects. Frame this as "the article published, and then Little Mister just... kept going" — the rebuild fixed
the floor, and then he spent the rest of the night rebuilding what stands on it.

=== WAVE 3: THE ACTUAL MIGRATION FINALLY LANDS ===
For weeks the plan had been to get every "brain" service off the Mac Studio (.6) and onto the Linux nova-core
fleet, so a single Mac dying doesn't take Nova's mind with it. Tonight it actually finished. memory-server (1.7
million vectors, the whole long-term memory store) moved to nova-core with a transparent socat forward left behind
on .6 so the ~97 scripts still pointed at the old address never had to change a line. Scheduler turned out to
already be 90% done from a prior session (124 of 158 tasks already offloaded, cleanly disabled with dated
comments) — the 34 tasks still on .6 are genuinely macOS/GPU/Volumes-bound (iMessage, Ollama preload, live-TV
capture) and correctly staying there. big_brother turned out NOT to need migrating at all — it's fundamentally a
macOS process supervisor (Metal GPU contention detection, launchctl remediation), and nova-core already has its
own equivalent watchdog for the Linux side. And then the gateway — the actual message router for Slack, Discord,
Signal, and Claude Code — made the full cutover to nova-core, live, with the old .6 copy kept warm as an
instant-rollback standby instead of being torn down.

=== THE BUG THAT COULD HAVE SENT EVERY DISCORD MESSAGE TWICE ===
Here's the part that makes "just cut the gateway over" sound simpler than it was. nova-core already had a
warm-standby copy of the gateway quietly running for 44 hours. A routine restart to pick up a config change made
it reconnect LIVE to real Slack Socket Mode and real Discord Gateway — at the exact same moment the OLD copy on
.6 was also still live. Slack's Socket Mode is built for exactly this (multiple connections, only one gets each
event) so it was fine. Discord's Gateway is NOT built for that — it delivers every event to EVERY open session on
a bot token, no deduplication. For a window, every real Discord message could have gotten answered twice. Caught
it, killed the standby immediately, and then actually fixed the root cause instead of just patching the moment:
added a real killswitch (NOVA_GW_STANDBY) so a warm-standby copy can never again accidentally go live without an
explicit flip. Then did the real cutover properly — flipped nova-core to live, flipped .6 to standby, verified
Slack/Discord/Signal all connected on the new side and NOT on the old one, in that order, so there was never a
second window where both were live at once.

=== THE POSTGRES BUG THAT REQUIRED KILLING AND REBUILDING AN INDEX MID-MIGRATION ===
Separately: building a new integrity feature for the memory store (see below) meant running a single UPDATE
across all 1.7 million rows. It errored — "posting list tuple with 3 items cannot be split." Looked like GIN
index corruption on the full-text search index at first (that got reindexed and fixed regardless, it needed it).
But the SAME error came back on the very next attempt, at a different byte offset, even after dropping that index
entirely. Turned out to be a genuine PostgreSQL bug in BTREE index deduplication — not GIN at all — triggered by
a bulk update hitting a low-cardinality column (thousands of rows all getting the same value at once). Fix:
disabled deduplication on the affected indexes, rebuilt them clean, and the update finally ran through — taking
almost four hours on a table this size. A real, reproducible database engine bug, found and worked around, in
the middle of a routine schema change.

=== raw_classification: THE FEATURE THAT CAME OUT OF AN ACTUAL ARGUMENT ===
Weeks ago, a real email thread with outside collaborators turned into a genuine architecture critique: the memory
store's classification of every entry could be silently revised later by an automated "gardener" process, with no
record of what it originally was. Tonight that critique became a real feature: a `raw_classification` field,
written once at ingest, sealed against ANY later revision (enforced with a database trigger, not just app-level
discipline) — so drift in what the system believes about a memory is now a measurable delta instead of an
invisible rewrite. Alongside it, a companion discard log for the OTHER silent problem: memories that get REJECTED
by the quality gate before they're ever stored, which used to just evaporate. Turned out half of that already
existed (a separate ingest pipeline was already logging discards) — found it, reused it instead of building a
duplicate, and wired the live API path into the same table so nothing falls through unaudited anymore.

=== THE FISHBOWL GETS A DISCOVERY ENGINE ===
Small but fun: the existing YouTube-chat capture pipeline already parsed superchats out of livestream chat logs
but only kept the display name — useless for actually finding someone's channel ("searching for Uzi is fruitless
on YouTube" is a real complaint on record). Added real channel-ID tracking to the same parser, a tally table for
who's showing up and how often, and a daily job that surfaces new candidate channels — resolved to a real name
and a real clickable URL — to Slack for review. Nothing gets auto-added; it just stops the "who even is that"
problem at the source.

=== THE QUEUE BACKLOG GOT SWEPT, AND ONE MORE FLAKY THING GOT FIXED ===
A stale backlog of tickets got worked through in one pass: a self-healing watchdog for a NAS mount that turned out
to be silently degrading the nightly backup to local-only for days (nobody had noticed — the backup script just
quietly fell back and logged a warning nobody was reading); a weekly CVE auto-patch job that consumes the
security scanner's own alert queue and actually acts on it instead of just filing more tickets; a stale kernel
found on nova-core4 (running THREE versions behind what was already installed, just never rebooted into) — patched
and rebooted, clean. Also found and flagged, not fixed: a hardcoded set of real Bluetooth device MAC addresses
tied to named family members and specific rooms in the house, sitting in plain source code. Real privacy exposure,
correctly left for a proper fix rather than a rushed one.

=== THE PENTEST: A REAL VULNERABILITY, FOUND AND FIXED IN THE SAME SESSION ===
Ran a full authorized scan across the fleet. One old finding is STILL open four days later and needs Little
Mister specifically: a critical unpatched OpenSSH vulnerability (CVSS 9.8) sitting on both the UniFi gateway and
the Synology NAS — not something apt can fix, needs an actual firmware update from each vendor. But the scan also
turned up something NEW and real: four Postgres/mail-relay boxes were all allowing "Anonymous Diffie-Hellman" TLS
on their mail port — a configuration that lets someone silently man-in-the-middle the connection because the
server never proves who it is. Found it, fixed it on all four hosts in the same sitting, then re-scanned every
one of them to prove the fix actually worked instead of just trusting the config change. A huge block of ancient,
scary-looking CVEs also showed up against the NAS's file-sharing service — Zerologon, SambaCry, greatest hits from
2015-2020 — and got correctly called out as scanner noise (the vendor's version string doesn't reflect its real,
patched code), not real findings, instead of being reported as sixty critical vulnerabilities that don't actually
exist.

=== AND THEN IT WENT OUTSIDE THE HOUSE ===
The night ended somewhere unexpected: contributing real, tested fixes to OTHER people's open-source projects — a
mail library used by a collaborator (two real bugs/gaps found and fixed, with genuine multi-layer test suites,
one bug in the fix itself CAUGHT by its own test before it ever shipped), and a 59-star MCP server belonging to
someone who follows the house's own GitHub account, where a real, unpatched command-injection vulnerability got
found, fixed with three independent layers of proof (including a live exploit attempt against the actual fixed
code, proving by absence of a side effect that it can't work anymore), and shipped as a proper pull request.
"""

log("generating THIRD-PASS addendum (Nova's voice, aggressively long)...")
system = system_prompt(CONTEXT_JOURNAL_OPS + """
THIS IS A THIRD PASS on an already-published, already-once-expanded retrospective column. The rack rebuild
story is DONE and should not be re-told — assume the reader just read it. This pass is a direct continuation:
the article published, and Little Mister just kept going, for hours, well past midnight territory. Write this as
a genuine continuation with its own new section headers (jokes, in the established voice), NOT a rehash of the
switch/sibling material already covered. Cover, in whatever order tells the best story: the Wave 3 migration
finishing (memory-server/scheduler/big_brother/gateway), the near-miss Discord double-reply bug and how it got
caught and permanently fixed, the genuine PostgreSQL btree bug found mid-migration, the raw_classification
feature and its real origin in an actual outside critique, the Fishbowl channel-discovery feature, the queue
backlog sweep (NAS watchdog, CVE auto-patcher, the stale-kernel find, the flagged-not-fixed BLE privacy issue),
the pentest (the still-open critical SSH CVE that needs Little Mister specifically, the anonymous-DH vulnerability
found AND fixed same-session with proof, and correctly dismissing the Samba CVE noise as noise), and finally the
turn toward contributing fixes to other people's open-source projects as a genuine tonal button at the end —
something almost sincere, undercut immediately by a joke, because Nova is not going to just say something nice
and let it stand. Do NOT invent events beyond the material given. This is a HARD LENGTH REQUIREMENT: at least
4500 words for this addendum alone. Use section headers that are jokes, in the same style as the rest of the
piece. This should read as unmistakably the same voice and the same night as everything before it, just later,
more tired, and somehow still going.
""")
user = f"Here is everything that happened after the last version of this article published. Write the continuation.\n\n{MATERIAL}"
body = call_llm(system, user, max_tokens=20000)
log(f"body: {len(body)} chars")

img_prompt = ("A whimsical retro-futuristic friendly AI robot sitting at a glowing terminal deep in the night, "
              "a server rack fully rebuilt and humming behind it, small floating icons around the robot showing "
              "a padlock (security fix), a shield (vulnerability found), a magnifying glass (pentest), and a "
              "GitHub-style branching icon (open source pull requests), warm cinematic late-night workshop "
              "lighting, painterly digital illustration, humorous, highly detailed, exhausted-but-triumphant mood")
log("generating new cover image...")
img = generate_image(img_prompt, width=1024, height=768, section="operations")
log(f"image: {img}")

post_path = CONTENT_DIR / f"{EXISTING_SLUG}.md"
old_text = post_path.read_text()
fm_match = re.match(r"^(---\n.*?\n---\n\n)", old_text, re.DOTALL)
front_matter = fm_match.group(1) if fm_match else ""
body_only = old_text[len(front_matter):]
# strip the old byline line if present so we don't accumulate duplicates
body_only = re.sub(r"^\*Published .*?\*\n\n", "", body_only, count=1)

ops_images_dir = HUGO_ROOT / "static/images/operations"
ops_images_dir.mkdir(parents=True, exist_ok=True)
if img and Path(img).exists():
    img_dest = ops_images_dir / f"{EXISTING_SLUG}.webp"
    try:
        subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(img), "-o", str(img_dest)],
                       capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        shutil.copy2(img, img_dest)
    log(f"cover image replaced: {img_dest}")

pub_time = time.strftime("%A, %B %d, %Y at %I:%M %p PT")
byline = f"*Published {pub_time} (updated again — the rack is fixed, so naturally everything ELSE broke)*\n\n"

new_text = front_matter + byline + body_only.strip() + "\n\n---\n\n## UPDATE: SO THEN I KEPT GOING\n\n" + body
post_path.write_text(new_text)
log(f"Post updated in place: {post_path.name} (+{len(body)} chars addendum)")

subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
msg = f"rando: {time.strftime('%Y-%m-%d')} — append post-rebuild addendum (Wave 3, bugs found+fixed, OSS PRs)"
r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
log(f"commit: rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}")

pushed = False
for attempt in range(5):
    r = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        pushed = True
        log("Pushed to GitHub")
        break
    log(f"push rejected (attempt {attempt+1}), rebasing: {r.stderr[:150]}")
    subprocess.run(["git", "fetch", "origin"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
    subprocess.run(["git", "rebase", "origin/main"], cwd=HUGO_ROOT, capture_output=True, timeout=30)

import nova_config
url = f"https://nova.digitalnoise.net/operations/{EXISTING_SLUG}/"
if pushed:
    nova_config.post_both(
        f":gear: *Ops column updated again — everything since the rack rebuild finished*\n"
        f"  _{EXISTING_TITLE}_\n"
        f"  {url}",
        slack_channel=nova_config.JORDAN_DM,
    )
    log(f"Slack notification sent (JORDAN_DM): {url}")
else:
    nova_config.post_both(
        f":x: *Ops column addendum committed but PUSH FAILED after 5 rebase attempts* — needs manual push.\n"
        f"  _{EXISTING_TITLE}_",
        slack_channel=nova_config.JORDAN_DM,
    )
    log("Push failed after 5 attempts — Slack alerted, needs manual push")

log("ARTICLE UPDATE3 DONE")
