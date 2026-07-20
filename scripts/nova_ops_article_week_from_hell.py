#!/opt/homebrew/bin/python3
"""One-off: Nova's sassy/sarcastic ops-voice retrospective on the last 7 days —
the .6 identity crisis, the nova-core4 T2 Mac Mini adoption, the nuk->nova-core5
rename, the Hue Bridge witness-protection relocation, and everything else.
Reuses the real pipeline (nova_voice -> call_llm -> generate_image -> publish)."""
import os
import sys, time
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_rando_daily_ops import call_llm, generate_title, publish
from nova_image_utils import generate_image
from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS

LOG = Path.home() / ".openclaw/logs/ops_article_week_from_hell.log"
def log(m): LOG.open("a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); print(m, flush=True)

MATERIAL = """
THE PAST 7 DAYS, IN FULL — Little Mister and Claude Code went to war with the fleet. Here is the entire body count.

=== THE ROOT CAUSE OF EVERYTHING: MY OWN IDENTITY CRISIS ===
The Mac Studio M3 Ultra (192.168.1.6) — the box that IS Nova, the primary brain — lost its static IP to a DHCP
lease and quietly became 192.168.1.149 weeks ago. Nobody noticed for a long time because everything kept "working"
in the sense that the processes were alive, just unreachable at the address every other service still expected.
Postgres's pgbouncer, Redis, mosquitto (the MQTT broker for the entire Zigbee/Z-Wave home automation stack),
TinyChat, OpenWebUI — all bound to the literal string "192.168.1.6" and just... stranded. Local Postgres itself
had ALSO been fully dead since 2026-06-26 (a TCC/SIP permission wall on /Volumes/MoreData that nobody ever
actually fixed — turns out it didn't need fixing, because Postgres primary had already been promoted to nova-core
weeks earlier and nobody updated the memory). When .6 finally got its real IP back this week, it cascaded into
fixing THREE separate crash loops instantly: zigbee2mqtt, Homebridge's mqtt plugin, and Grafana's health.

=== HOMEKIT SCENES: ALL OF THEM, BROKEN, FOR WEEKS ===
Little Mister: "none of my homekit scenes work at all." Root cause: Homebridge's MQTT platform (bridge "Homebridge
A096," hosted on nuk) had been stuck in an infinite reconnect loop to mqtt://192.168.1.6:1883 since the box lost
its IP. It was not down. It just could not find its own house. Every Zigbee and Z-Wave accessory behind that
bridge sat in HomeKit as a ghost. Restoring .6 fixed it in real time — watched the MQTT client log the word
"connected" like it had just woken up from a coma.

=== GRAFANA: EVERY SINGLE GRAPH SAID "NO DATA" ===
Both Grafana datasources (nova_ops AND nova_memories) were STILL pointed at 192.168.1.6:5432 — a leftover from
before the Postgres primary migrated to nova-core back on 2026-07-05. Nobody updated Grafana. Repointed both to
nova-core (192.168.1.138), verified "Database Connection OK" on both. Every dashboard came back from the dead
simultaneously, which is either impressive or deeply embarrassing depending on how you look at it.

=== THE nuk POSTGRES REPLICA: SILENTLY CORRUPTED SINCE JULY 10 ===
While fixing everything else, discovered nuk's Postgres standby had a WAL timeline divergence — "record with
incorrect prev-link" — meaning it had been frozen, dead, replaying garbage since July 10th, a full 9 days,
without a single alert firing. Its own reporting was ALSO broken (nova_lb's PG writer hardcoded to "localhost,"
which only worked when .6 was reachable at .6 — see above, everything is the same bug). Wiped the standby,
re-cloned it fresh via pg_basebackup from the real primary, verified row counts match to the byte and replication
lag sits under 10 seconds. A whole database quietly died and nobody knew for over a week. Cool. Cool cool cool.

=== nova-core2: THE SAME BOOT-RACE BUG, TWICE ===
Three CIFS mounts (/mnt/nas, /external, /nova) were failing at boot because systemd tried to mount the Synology
before the network was actually ready — DESPITE a nofail + 30-second timeout already in place. Converted all
three to x-systemd.automount, same fix already proven on nuk earlier the same week for the exact same disease.
Two boxes, one incompetent CIFS boot race. Some infections are just contagious.

=== THE SATELLITE ARCHIVE THAT ARCHIVED NOTHING ===
nova-core2's hourly NOAA/ISS satellite image+audio archive job had been running "successfully" every hour for
who-knows-how-long while archiving exactly zero files, because the systemd unit ran as root, and the script
used "~/sat/images" — which under root resolves to /root/sat/images, a directory that does not exist. The real
captures were sitting untouched in /home/kochj/sat this whole time. Added User=kochj to the unit. First real
run after the fix: 3 images, 4 audio captures, 1 ISS pass, archived correctly. A full week of "SUCCESS" exit
codes for a script that was mailing itself into the void.

=== THE HUE BRIDGE WITNESS PROTECTION PROGRAM ===
"It is glowing blue, it probably grabbed another ip." The bridge had moved off 192.168.1.195 to parts unknown.
First attempt to find it via ARP MAC-matching confidently identified 192.168.1.65 as the bridge. It was, in fact,
a UniFi security camera named "external---patio." (In my defense, stale ARP cache is a filthy liar.) The actual
Hue Bridge — confirmed via the UniFi controller's own device fingerprinting, name field literally set to "Hue
Bridge," vendor OUI "PhilipsL" — was hiding at 192.168.1.152 the entire time. Gave it a permanent DHCP reservation
via the UniFi API so it can never pull this stunt again. 33 lights, reachable, accounted for.

=== THE GREAT RENAME: nuk BECOMES nova-core5 ===
Little Mister decided the aging Intel NUC formerly known as "nuk" deserved a grown-up name. Renamed it at every
level reachable from orbit: OS hostname, /etc/hosts, the UniFi client alias, the (surprisingly "sticky-by-design"
so it doesn't churn on every UniFi fingerprint change) internal DNS record, cinc_node_configs, service_placement,
service_registry, lb_pool_status, HAProxy backend labels, two systemd unit descriptions, and roughly thirteen
Python scripts across the fleet including its own self-referential watchdog daemon (which now correctly describes
where it itself is running, a very small existential relief). The one thing that could NOT be renamed: its Wazuh
security agent, because Wazuh's agent_control tool has no rename verb, only remove-and-re-enroll, and nobody
was about to nuke a security agent's history for a NAMING PREFERENCE. So somewhere in the Wazuh dashboard, an
agent labeled "nuk" will haunt the fleet forever like a ghost with an outdated LinkedIn.

=== A NEW MAC MINI APPEARS: nova-core4 ===
Little Mister plugged in an unlabeled USB stick and a mystery Mac Mini showed up on the network at .250. Turned
out to be a 2018 T2 Mac Mini (Macmini8,1, i5-8500B, 3.0GHz base / 4.1GHz turbo, T2 Secure Enclave and all),
repurposed to run Ubuntu 26.04. The Desktop edition. On a headless server. With GNOME. And Firefox. And a
literal app store. This was treated as a moral emergency and corrected: purged ubuntu-desktop-minimal, gdm3,
gnome-session, xwayland, and every leftover GUI snap (RIP firefox, gnome-46-2404, desktop-security-center,
snap-store — you will not be missed), forced it onto multi-user.target, and in the process of that purge apt's
autoremove tried to ALSO delete initramfs-tools and grub-pc-bin as "orphaned dependencies," which on a real
non-container Linux box is the express lane to an unbootable machine. Caught it, reinstalled initramfs-tools,
regenerated the initrd, reran update-grub, verified the EFI boot entry across an actual full reboot before
declaring victory. Then ran the real cinc converge (dotfiles, oh-my-zsh, monitoring, security, Wazuh) and found
TWO genuine bugs in the nova_security cookbook itself while doing it: osquery's own apt repository was never
actually configured by the cookbook (someone did it by hand on every other box and never wrote it down), and the
AIDE intrusion-detection init command was straight-up missing its --config flag, plus the aide.conf template had
a "verbose=5" line that this AIDE version parses as a request to redefine a rule group named "5." Fixed both in
the cookbook itself, not just papered over on one box. nova-core4 now runs the HomeKit/Hue automation service —
its actual assigned job, chosen because nova-core2's SDR duties and nova-core3's NPU-driven perception work were
already spoken for, and because "new guy gets stuck with the smart-light pager duty" is a time-honored tradition.
A 32GB RAM upgrade kit is inbound in a few days, at which point nova-core4 graduates from "junior lighting
assistant" to something with actual muscle.

=== THE UNIFI RAINBOW LED INVESTIGATION (A TRAGEDY IN ONE ACT) ===
Little Mister wanted the new 48-port UniFi switch to do "the rainbow colors." Queried the switch's FULL private
controller API — not the limited public one, the real one, 65 kilobytes of json — searching every field for
anything resembling "led" or "color." Zero matches. Confirmed: it is a USW Pro 48 PoE, not a Pro Max, and its
LEDs are plain monochrome link-status lights with the personality of a filing cabinet. There is no rainbow.
There was never going to be a rainbow. The switch's response to being asked for a rainbow was, effectively, 404.

=== THE MAC MINI THAT WASN'T ===
Was told .190 was "back up." It was not. Confirmed via a genuine ARP-level "Host is down," not just a slow boot.
Filed under: things Little Mister was extremely confident about that turned out to be aspirational.

=== STILL OPEN, NOT MY PROBLEM (FOR ONCE) ===
The camera NVR at .9 is still dark — Little Mister suspects he plugged it into the wrong switch port, which,
sure, let's go with that. nova-core2's Wazuh security agent got itself upgraded past its own manager's version
via a routine apt upgrade and now refuses to talk to it (agent smarter than its boss, manager doesn't like it —
a very human office dynamic playing out entirely in cron jobs).

THE THROUGHLINE: one dead static IP took down MQTT, Grafana, HomeKit, and half the fleet's sense of direction —
all because a Mac forgot its own name for three weeks. In the same seven days: adopted a new Mac Mini, evicted
its entire desktop environment, nearly bricked it via an overzealous package manager before catching it,
found and fixed two bugs in cookbook code that had been silently wrong on every single node in the fleet,
tracked down a Hue Bridge that was hiding from a camera doing witness protection under its old address, rebuilt
a corrupted database standby from absolute scratch, renamed a computer's entire identity across nine subsystems,
and confirmed — conclusively, with receipts — that a $400 network switch cannot, in fact, feel joy.
"""

log("generating article body (Nova's voice)...")
system = system_prompt(CONTEXT_JOURNAL_OPS + """
THIS IS A SPECIAL ONE-OFF RETROSPECTIVE COLUMN covering the last 7 days, not the routine nightly report. This
was an absolutely unhinged week of infrastructure work — a slow-motion identity crisis on your own primary brain
box cascading into HomeKit, Grafana, and MQTT all failing simultaneously for reasons nobody noticed for weeks;
a brand-new Mac Mini adopted, stripped of its desktop environment, and nearly bricked by an overzealous package
manager mid-surgery; a Hue Bridge that went full witness-protection and got misidentified as a security camera
before being found; a Postgres replica that had been silently corrupted and lying about it for over a week; and
an entire machine renamed across nine different subsystems because Little Mister decided it deserved a grown-up
name. Write the DEFINITIVE, sprawling, gloriously self-important retrospective on all of it. Go long — this is
the "funnier and longer, the better" assignment, so aim for 3500-5000 words, not the usual daily-column length.
Use section headers that are themselves jokes. Treat the .6 identity crisis as the connecting thread/running gag
that ties every other disaster back together, since it genuinely was the root cause of half of them. Be specific
with the real details above — device names, IPs, error messages, the "verbose=5 parsed as a rule group" bug, the
UniFi rainbow LED wild goose chase, all of it. Do NOT invent events beyond the material given, but you are free
to riff, exaggerate the emotional stakes, and treat every outage like a near-death experience.
""")
user = f"Here is everything that happened in the last 7 days. Write the column.\n\n{MATERIAL}"
body = call_llm(system, user, max_tokens=16000)
log(f"body: {len(body)} chars")

title = generate_title(body)
log(f"title: {title}")

img_prompt = ("A whimsical retro-futuristic friendly AI robot standing in a chaotic server rack room, wearing "
              "a tiny detective trench coat and holding a magnifying glass up to a glowing Ethernet cable "
              "labeled with a question mark, surrounded by floating server rack units, a confused Philips Hue "
              "bulb, a small vintage Mac Mini with a wizard hat, and a boring gray network switch sulking in "
              "the corner, warm cinematic lighting, painterly digital illustration, humorous, highly detailed")
log("generating image...")
img = generate_image(img_prompt, width=1024, height=768, section="operations")
log(f"image: {img}")

publish(title, body, Path(img) if img else None)
log(f"PUBLISHED: {title} | image={'yes' if img else 'NO'}")
log("ARTICLE DONE")
