#!/opt/homebrew/bin/python3
"""Rewrite/expand of the week-from-hell ops column: the REAL story is Little Mister
disassembled and rebuilt the entire server rack from Friday afternoon through the
UniFi NVR finally coming back up, and everything else was fallout from that. Covers
nova-core through nova-core5 explicitly, the switch consolidation, and goes 2x length.
Updates the SAME published post in place (same slug/URL) rather than posting new."""
import os
import sys, time, re, shutil
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_rando_daily_ops import call_llm, HUGO_ROOT, CONTENT_DIR
from nova_image_utils import generate_image
from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS

LOG = Path.home() / ".openclaw/logs/ops_article_week_from_hell_v2.log"
def log(m): LOG.open("a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); print(m, flush=True)

EXISTING_SLUG = "2026-07-19-please-update-your-records-the-house-is-now-haunted-by-a-man"
EXISTING_TITLE = "PLEASE UPDATE YOUR RECORDS: THE HOUSE IS NOW HAUNTED BY A MAN WHO CHANGED HIS OWN IP AND TOLD NO ONE"

MATERIAL = """
THE REAL STORY, WHICH CHANGES EVERYTHING: this was not just a random week of bugs surfacing. Little Mister spent
the ENTIRE WEEKEND — starting Friday afternoon — completely disassembling the physical server rack down to bare
metal and rebuilding it from scratch, rack unit by rack unit, cable by cable. Every single piece of chaos in this
retrospective — the identity crisis, the dead replicas, the missing Hue Bridge, the confused Homebridge, all of
it — was downstream of a human being physically ripping an entire rack apart with his bare hands over a weekend
and putting it back together in a different, better shape. The rebuild's final, triumphant closing beat: the
UniFi NVR (192.168.1.9) came back online literally minutes ago, the very last piece to rejoin the network, after
being dark for the entire ordeal. This retrospective needs to be reframed around THAT arc — Friday afternoon
teardown to Sunday-night NVR resurrection — with all the previously-covered material now understood as scenes
inside that larger story, not as a mysterious ambient bug. Roughly DOUBLE the previous draft's length.

=== NETWORK HARDWARE RETIREMENT: THE GREAT SWITCH CONSOLIDATION ===
As part of the rebuild, TWO pieces of networking hardware were formally retired: the UniFi Aggregation switch
and the UniFi 16-port rack switch. Both replaced by a single new UniFi 48-port switch (the same "USW Pro 48 PoE"
already investigated and found to have zero rainbow-capable LEDs) — this one switch has four 10-Gigabit SFP+
ports for the heavy uplinks (the aggregation switch's old job) plus forty-four 2.5-Gigabit copper ports for
everything else (the 16-port switch's old job, times almost three). Two aging boxes retired, one modern box doing
both jobs, and its LEDs are still exactly as emotionally unavailable as previously documented. This consolidation
is very likely WHY half the fleet's static IPs and DHCP reservations got shaken loose in the first place — moving
every cable in the rack to a brand-new switch, one port at a time, over a weekend, is exactly the kind of event
that would cause a Mac Studio to "forget" it's supposed to be 192.168.1.6.

=== THE FIVE nova-core SIBLINGS: A FAMILY PORTRAIT DURING THE REBUILD ===
nova-core (.2, dual-homed — also answers on .138 off a second NIC on the same physical box, which took an
embarrassingly long time to figure out): the hub. Postgres primary (promoted here back on July 5th, a fact half
the fleet's config files still don't know), Grafana, all three Wazuh containers, zigbee2mqtt, zwave-js-ui,
Homebridge, TinyChat, SearXNG, Frigate. The one that has to work or nothing else matters, survived the entire
rack rebuild as the load-bearing wall.

nova-core2 (.86): the SDR/observability box. RTL2838 dongle and SDRplay RSPduo doing satellite and radio capture,
Grafana/Wazuh candidates for the "monitoring should live off the box it monitors" doctrine. Survived the rebuild
with two boot-race CIFS mount bugs (same disease nova-core5 already had, because apparently this fleet believes
in sharing) and an hourly satellite-archive job that had been reporting SUCCESS for who knows how long while
archiving exactly nothing, because it ran as root and looked for files that only existed under a regular user's
home directory. Both fixed mid-rebuild.

nova-core3 (.88, database also lists it as .5 for reasons nobody has explained and everybody has decided not to
ask about): the perception/AI node, Beelink SER10 MAX with an 86-TOPS NPU and a Radeon 890M, meant for Frigate,
Whisper, embeddings, image generation. The best-behaved of the five. Zero failed units through the entire
rebuild. The golden child. Do not tell the other four.

nova-core4 (.250): did not exist a week ago. Appeared via an unlabeled USB stick plugged into a mystery machine
that turned out to be a 2018 T2 Mac Mini (Macmini8,1, i5-8500B, 3.0/4.1GHz, actual functioning Secure Enclave
under Linux). Was running Ubuntu 26.04 DESKTOP — full GNOME, Firefox, an app store — on what was supposed to be
a headless server, a crime against the rack rebuild's entire ethos. Corrected: purged the desktop environment
down to multi-user.target, and in the process apt's autoremove tried to ALSO delete initramfs-tools and
grub-pc-bin as "orphaned," which on real hardware (not a container) is how you turn a Mac Mini into a paperweight.
Caught it, reinstalled initramfs-tools, regenerated the boot image, verified across an actual real reboot. Then
ran the real cinc converge and found two genuine bugs baked into the nova_security cookbook itself — osquery's
apt repository was never configured by the cookbook (someone did it by hand on every other node and never wrote
it down), and the AIDE intrusion-detection init command was missing its --config flag entirely, plus its own
config template had a "verbose=5" line that this AIDE version parses as an attempt to redefine a rule GROUP named
"5," which is not a typo, that is a real error message that happened. Fixed both in the cookbook, not just on one
box. nova-core4 now runs the HomeKit/Hue automation service, its actual assigned job. A 32GB RAM kit is inbound
in a few days.

nova-core5 (formerly "nuk," an aging Intel NUC, renamed this week because it earned an adult name): survived a
Ubuntu Desktop installer getting written to and booted from a USB stick via manually forcing the EFI boot order
(because the keyboard on the box was dead — no local input, boot order had to be hacked over SSH via efibootmgr),
including one attempt where the firmware auto-registered a phantom boot entry labeled "Linpus lite" that turned
out to just be the stick's own bootloader with a weird generic name, not a foreign intrusion, a five-minute panic
for nothing. During the SAME rebuild weekend, its Postgres standby was discovered to have a WAL timeline
divergence — "record with incorrect prev-link" — meaning it had been silently dead and replaying corrupted WAL
since July 10th, NINE DAYS, with zero alerts. Wiped and re-cloned from the real primary via pg_basebackup;
replication lag now under 10 seconds. Renamed at every level physically reachable: OS hostname, /etc/hosts, the
UniFi client alias, the sticky-by-design internal DNS record (which required a direct database edit because the
DNS sync script deliberately ignores subsequent name changes to prevent churn — a very reasonable design decision
that was, this one time, extremely annoying), cinc_node_configs, service_placement, service_registry,
lb_pool_status, two systemd unit descriptions, HAProxy backend labels, and about thirteen Python scripts across
the fleet, including its own self-referential watchdog daemon. The one thing that could not be renamed: its Wazuh
security agent, because Wazuh's own agent-management tool has no rename verb, only remove-and-re-enroll, which
nobody was willing to do just to fix a LABEL. Somewhere in a dashboard, an agent named "nuk" will outlive the box
that was named that.

=== EVERYTHING ELSE THAT HAPPENED DURING THE REBUILD (unchanged from before, keep all of this) ===
The .6 identity crisis: static IP silently replaced by a DHCP lease weeks earlier (now understood to be rebuild
fallout, not a mystery), taking pgbouncer, Redis, mosquitto, TinyChat, and OpenWebUI down with it, all bound to
a literal address nothing answered to anymore.
HomeKit scenes: ALL of them broken because Homebridge's mqtt plugin (bridge "Homebridge A096") was stuck in an
infinite reconnect loop to a Mac Studio that had, for all practical purposes, changed its name and not told
anyone. Fixed instantly the moment .6 came home.
Grafana: every graph said "No data" because BOTH datasources were still pointed at .6, two weeks after the
Postgres primary had actually moved to nova-core. Nobody told Grafana either. Repointed both, watched every
dashboard come back to life at once.
The Hue Bridge witness protection saga: bridge went dark on its old address, first MAC-match investigation
confidently fingered 192.168.1.65, which turned out to be a UniFi security camera named "external---patio," not
a lighting appliance. The real bridge was found via the UniFi controller's own device fingerprinting at
192.168.1.152, name field literally reading "Hue Bridge," and was given a permanent DHCP reservation so it can
never pull this again.
The UniFi rainbow LED investigation: queried the full private controller API for the new 48-port switch, 65
kilobytes of JSON, zero fields containing "led" or "color." Confirmed: plain monochrome link-status lights. There
was never going to be a rainbow, before OR after the rebuild.
The Mac Mini that wasn't: told .190 was "back up." Genuinely was not — a real ARP-level "Host is down," not a
slow boot. Filed under things Little Mister was extremely confident about that were aspirational.
nova-core2's Wazuh agent quietly upgraded itself past its own manager's version via routine apt upgrade and now
refuses to talk to it — still unresolved, a bigger decision (downgrade the agent or the whole manager stack) left
for Little Mister.

=== THE FINALE: THE NVR COMES HOME ===
The UniFi NVR at 192.168.1.9 had been dark the entire ordeal — the very last device to reconnect after the full
rack teardown-and-rebuild, coming back online literally minutes before this rewrite was commissioned. Confirmed
via ping, HTTPS 200, and — the real proof — Homebridge's UniFi Protect plugin immediately started receiving live
motion and occupancy events from "Exterior - Front Door Left" and "Interior - Front Door" in real time, no more
EHOSTUNREACH, no more self-throttling. The rack rebuild is, as of that moment, actually, finally, completely done.
(One small unrelated wrinkle surfaced in that same log: Homebridge is failing to persist its accessory cache to
disk with an error about a "missing associated platform" — cosmetic for now, flagged, not yet fixed.)

THE THROUGHLINE, RESTATED: a human took an entire physical server rack apart with his hands over a weekend —
every cable, every switch, every rack unit — consolidated two aging network switches into one, and rebuilt it
from bare metal. Everything else in this saga was NOT random bad luck. It was the sound of about a dozen
computers, unable to communicate this fact to each other, individually and separately having the exact same
identity crisis at the exact same time, because the ground they stood on was, briefly, gone.
"""

log("generating EXPANDED article body (Nova's voice, ~2x length)...")
system = system_prompt(CONTEXT_JOURNAL_OPS + """
THIS IS A REWRITE/EXPANSION of an already-published one-off retrospective column, now with the REAL frame story:
Little Mister spent the entire weekend, starting Friday afternoon, completely disassembling and rebuilding the
physical server rack from bare metal, cable by cable, ending with the UniFi NVR finally reconnecting minutes ago
as the last device to come home. Every piece of software chaos covered previously (the .6 identity crisis, dead
replicas, the missing Hue Bridge, broken HomeKit, the works) should now be told as fallout from THAT physical
event, not as an unexplained mystery. Structure the piece around the Friday-to-NVR arc as the throughline, with
the various disasters as scenes/flashbacks within it. You MUST give nova-core, nova-core2, nova-core3, nova-core4,
and nova-core5 each their own explicit named section/beat — treat them like five siblings with distinct
personalities (the golden child, the new kid, the renamed one, etc.). Cover the network switch consolidation
(UniFi Aggregation switch + 16-port switch retired, replaced by the one 48-port switch with four 10GB and
forty-four 2.5GB ports) and tie it back to the already-established rainbow-LED joke. This is a HARD LENGTH
REQUIREMENT, not a suggestion: the piece MUST be at LEAST 6500 words, target 7000-7500. If you find yourself
wrapping up before that, you have NOT gone deep enough — go back and give each of the five nova-core siblings a
genuinely long, detailed, bit-filled section (400-700 words each minimum), expand the switch-consolidation
section with its own running gag, expand the Hue Bridge witness-protection section into a proper noir-detective
bit, add a longer existential-crisis outro, and add at least one additional flashback/aside per major section.
Use section headers that are themselves jokes — there should be at LEAST 12 distinct sections given the length
target. Do NOT invent events beyond the material given, but you are free to riff extensively on the
emotional/physical drama of a guy taking an entire rack apart with his bare hands over a weekend. Word count
under 6000 is a FAILED attempt at this assignment — pad with more jokes, more callbacks, more digressions in
voice, not filler, but genuinely MORE material per beat.
""")
user = f"Here is the full material, including the real frame story. Write the expanded column.\n\n{MATERIAL}"
body = call_llm(system, user, max_tokens=28000)
log(f"body: {len(body)} chars")

img_prompt = ("A whimsical retro-futuristic friendly AI robot standing amid a fully disassembled server rack "
              "with cables, rack units, and two old network switches scattered on the floor like a puzzle mid-"
              "solve, holding a single new sleek 48-port network switch up triumphantly like a trophy, a small "
              "security camera icon glowing back to life in the background, warm cinematic workshop lighting, "
              "painterly digital illustration, humorous, highly detailed")
log("generating new cover image...")
img = generate_image(img_prompt, width=1024, height=768, section="operations")
log(f"image: {img}")

# ── Update the EXISTING post in place (same slug/URL) ──────────────────────────
post_path = CONTENT_DIR / f"{EXISTING_SLUG}.md"
old_text = post_path.read_text()
fm_match = re.match(r"^(---\n.*?\n---\n\n)", old_text, re.DOTALL)
front_matter = fm_match.group(1) if fm_match else ""

ops_images_dir = HUGO_ROOT / "static/images/operations"
ops_images_dir.mkdir(parents=True, exist_ok=True)
if img and Path(img).exists():
    img_filename = f"{EXISTING_SLUG}.webp"
    img_dest = ops_images_dir / img_filename
    import subprocess
    try:
        subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(img), "-o", str(img_dest)],
                       capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        shutil.copy2(img, img_dest)
    log(f"cover image replaced: {img_dest}")

pub_time = time.strftime("%A, %B %d, %Y at %I:%M %p PT")
byline = f"*Published {pub_time} (updated — now with 100% more disassembled rack)*\n\n"

post_path.write_text(front_matter + byline + body)
log(f"Post updated in place: {post_path.name} ({len(body)} chars body)")

import subprocess
subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
msg = f"rando: {time.strftime('%Y-%m-%d')} — expand week-from-hell column (full rack rebuild frame)"
r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
log(f"commit: rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}")

# ── Push, rebasing against any other automated posts that landed meanwhile ─────
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
        f":gear: *Ops column rewritten — now 2x length with the full rack-rebuild frame story*\n"
        f"  _{EXISTING_TITLE}_\n"
        f"  {url}",
        slack_channel=nova_config.JORDAN_DM,
    )
    log(f"Slack notification sent (JORDAN_DM): {url}")
else:
    nova_config.post_both(
        f":x: *Ops column rewrite committed but PUSH FAILED after 5 rebase attempts* — needs manual push.\n"
        f"  _{EXISTING_TITLE}_",
        slack_channel=nova_config.JORDAN_DM,
    )
    log("Push failed after 5 attempts — Slack alerted, needs manual push")

log("ARTICLE V2 DONE")
