#!/opt/homebrew/bin/python3
"""One-off: the big one. A 7500+ word retrospective covering EVERYTHING Nova/
infrastructure-related over the past two months (2026-05-20 -> 2026-07-19),
built from a real research dossier (git history across repos, the 711-article
operations archive, two months of resolved queue tickets). Nova's full sassy/
sarcastic/critical voice. No explicit content. Generated in two large passes
(mid-May through the .7 evacuation crisis, then early July through tonight)
and stitched, since a single call is less reliable at this length. New dated
post in operations, most striking cover image prompt possible, Slack DM link
to Jordan when done (explicit ask)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice
from nova_rando_daily_ops import call_llm

LOG = Path.home() / ".openclaw/logs/ops_article_two_months.log"
def log(m): LOG.open("a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); print(m, flush=True)

DOSSIER = r"""
### MID-TO-LATE MAY: Foundational rebuild
- The gateway was originally a single 2997-line monolith -- split into the proper nova_gateway/
  package. Direct architectural ancestor of tonight's gateway cutover.
- Chatroom/gateway hardening: memory access + server-side identity resolution, Cloudflare Tunnel
  went live for chatroom.
- "Nova Securities intelligence system" (typo'd as Securities, later corrected to Security) --
  daily PDB, breaking alerts, OSINT feeds. Origin of the whole security-alert article genre still
  running today.
- Original Wazuh SIEM integration + kernel zone monitoring + crash recovery hardening.
- Full Philips Hue integration (lights, sensors, commands, dashboard) -- first version, well
  before the recent Hue Bridge relocation saga.
- SNMP fleet completed at 6 devices -- nuk and mac-mini enter the fleet here.
- First-generation load balancer -- "mesh (capacity-aware load balancing + active-active + live
  mesh map)". Predates both the nginx MLX LB and tonight's nova_lb.py -- THREE real generations
  of load-balancing work across two months.
- Removed hardcoded Grafana credentials from source -- an early real security fix.
- "Operation Vector Cleanup: Or How I Learned to Stop Worrying and Love the VACUUM" -- first
  documented vector-store maintenance operation.
- DLP/privacy hardening cluster: filter_private_memories + scrub_pii gating, stopped private data
  egress to cloud, work calendar marked PRIVATE_SOURCE, removed a porn crawler + explicit-content
  filter at the memory ingest chokepoint -- direct evidence "no porn" is enforced at the
  infrastructure level, not just a house rule.

### LATE MAY-JUNE: HomeKit buildout, notify bus, .7 consolidation begins
- HomeKit sensor ingestion cluster: battery monitor + low-battery Slack alerts, climate + air
  quality streams, Grafana HomeKit Sensors dashboard (battery/climate/lux/VOC), Outlet In Use
  signals + left-on alerts (#682).
- nova_notify central bus -- migrated ~89 separate alert emitters into ONE fan-out system. Direct
  ancestor of the telemetry.events unified bus tonight's threat-assessment pipeline reads from.
- Fishbowl early-warning tripwire on the watch-community feed shipped (nova_fishbowl_watch.py) --
  real-time predecessor to tonight's channel-discovery pipeline.
- "rando" journal category retired in favor of "operations"; an EARLIER, apparently reverted or
  superseded attempt to rename nuk surfaces in git history around here too -- the actual, final,
  every-level rename happened this past weekend.
- "Unified security operations loop" -- Wazuh bridge, ops context, article integration wired
  together as one system.
- June 22: MTPLX evaluation ("Twice as Fast Without Getting Any Dumber" -- native MTP speculative
  decoding on MLX, ~2x throughput, no quality loss) and the Ponytail evaluation ("A Tool That
  Wants to Write Less of Me") -- real one-off engineering-eval pieces.
- DNS-based service directory added to nova-control-web -- groundwork for DNS-as-source-of-truth,
  months before the BIND9 build.
- 6/29: a real-sounding breaking-alert about a DNS AAAA record change on digitalnoise.net, paired
  same-day with an "APT28 router exploitation" alert -- investigation showed this was CLOUDFLARE'S
  OWN anycast IP rotation, a false positive. Fixed with a surface-monitor patch to stop DNS
  false-alarms on Cloudflare anycast rotation. The security pipeline crying wolf once, then
  getting calibrated -- same pattern as tonight's infra-threat-score calibration.
- First-generation nova-dns: UniFi-fed sticky DNS naming engine -> served via DNSMASQ on nuk + .2
  (HA) -> Grafana dashboard (device count, new/active/stale). THE DIRECT PREDECESSOR SYSTEM
  tonight's BIND9/TSIG/AXFR rebuild replaced -- Nova had sticky DNS naming since June, just via
  dnsmasq, not authoritative BIND9.

### LATE JUNE-EARLY JULY: The .7 evacuation crisis
- The week of 6/29 alone: 150 resolved queue tickets, dominated by repeating "SYSTEMIC: 8 services
  down simultaneously -- TinyChat, SearXNG, Nova Syslog, Plex, HDHomeRun" (and variants naming
  Gateway v2, MLX Server, ComfyUI, OpenWebUI, Scheduler, Inference router) -- the SAME cluster of
  services repeatedly dying together, dozens of times over about a week.
- This lines up exactly with: "docs: README -- reflect .7 evacuation, nova-core consolidation,
  mesh + LB", "fix(big-brother): repoint .7 services -> nova-core (.2); .7 fully evacuated",
  "Grafana .7->.2 + 5 consolidated dashboards, Plex nuk->nova-core" -- tv-movies-mini (.7) was
  being evacuated of services onto nova-core during this exact window, and the repeated "8
  services down" tickets are very likely the visible symptom of that migration being genuinely
  rocky before it stabilized. A real, multi-day, high-incident-count precursor to tonight's much
  cleaner Wave 3 cutover.
- "Fleet audit remediation + tests + infra work" -- a large cleanup commit right in this window.
- Cross-channel conversation continuity added to the gateway.
- Wazuh catch-up batch widened to 5000 to clear backlogs fast -- Wazuh had fallen behind and
  needed a real fix.

### RECURRING CHARACTERS/MOTIFS (weave in, don't itemize as separate incidents)
- "nuk": the aging Intel NUC, subject of "nuk sank our digital titanic (again)" jokes for WEEKS
  before its dignified nova-core5 rename this past weekend.
- The UniFi LED rainbow grudge: zero color-capable LEDs on these switches, confirmed twice (once
  mid-window, once during the weekend rebuild) via full private API dumps. There was never going
  to be a rainbow.
- "Promiscuous mode": dozens of article titles riffing on WiFi monitor-mode security alerts -- a
  long-running bit, not a real incident count each time.
- The postmortem article factory: 136+ near-identical-format daily incident retrospectives --
  its own phenomenon ("Nova's compulsive incident journaling"), not itemizable incidents.
- "Little Mister" -- consistent address throughout.
"""

log("PART 1: generating mid-May through .7 evacuation crisis (target 3800+ words)...")
system1 = nova_voice.system_prompt(nova_voice.CONTEXT_JOURNAL_OPS + """
This is PART ONE of a massive two-part retrospective covering the FULL two months of Nova/
infrastructure history (mid-May through mid-July 2026) -- Jordan explicitly asked for EVERYTHING,
at least 7500 words total across both parts, full sass/sarcasm/criticism, no explicit sexual
content. This part covers ONLY: the mid-to-late-May foundational rebuild (gateway split, Wazuh,
Hue v1, SNMP fleet, first-gen mesh load balancer, the Grafana credential fix, vector-store
cleanup, the privacy/DLP hardening cluster including the porn-crawler removal -- mention this
factually and briefly, no lurid detail), late-May-through-June (HomeKit buildout, the notify bus
migration of ~89 emitters, the Fishbowl tripwire, the MTPLX and Ponytail evaluations, the DNS
false-alarm/Cloudflare-anycast incident, first-gen dnsmasq-based DNS), and the late-June/early-July
.7 evacuation crisis (150 tickets in one week, repeated cascading service failures, eventual
stabilization). End this part at the .7 evacuation stabilizing -- do NOT cover July onward, that's
part two. Weave in the recurring motifs (nuk, the LED grudge, promiscuous mode, the postmortem
factory) as running texture, don't list them as separate incidents. Use section headers that are
jokes, in Nova's voice. HARD LENGTH REQUIREMENT: at least 3800 words for this part alone -- if you
find yourself wrapping up short, go back and give each era more room, more bits, more callbacks.
Do not write a title or intro framing -- just start the content, a wrapper script handles the
overall title/intro. End this part on a natural beat, not a full conclusion (part two continues).
""")
part1 = call_llm(system1, f"Here is the research dossier for part one's material:\n\n{DOSSIER}", max_tokens=16000) or ""
log(f"part1: {len(part1)} chars")

DOSSIER2 = r"""
### EARLY-MID JULY: Second-gen load balancer, offload waves, big_brother split
- big_brother (#511) -- split into service + system daemons for fault isolation, with a shared
  escalation-tier engine extracted first. THE REAL PREDECESSOR to tonight's finding that
  big_brother "doesn't need migrating" -- it had already been architecturally split months earlier
  specifically so the macOS-local and Linux-fleet pieces could be independent.
- 7/8: "Whole-Ass Load Balancer, Or: I Debugged Two Sentient Toasters at 11pm" -- real second-gen
  LB work: root-caused a transformers library version mismatch between two MLX inference Mac
  minis (.190, .7) and mac-studio, pinned versions, built a genuine nginx-based load balancer
  round-robining between the minis, promoted from a Homebrew service to a real root-owned
  LaunchDaemon after Homebrew's process management proved unreliable, verified end-to-end through
  the gateway's own health endpoint. A clean, complete win -- notably rare in tone among the
  postmortem-heavy catalog.
- "Offload2"/"Wave A": splitting portable scheduler tasks onto nova-core (.2), ~59 infra tasks
  migrated with 22 reverted as blockers -- an HONEST partial-success wave, not everything landed
  clean the first time.
- "Wave B COMPLETE: 31 journal tasks migrated to .2" + a git-push-timeout bump (60s->180s) --
  infrastructure straining under its own migration load.
- 7/14: ".6 Retires to a Life of Judgment: A Tuesday Spent Migrating Myself Off Myself" -- a full
  day of secret-store migration (Hue/Plex/Ambient API keys reseeded to the fleet store, re-migrated
  TWICE after the first pass didn't confirm landing), scheduler-core credential wiring fixed, git
  identity + SSH deploy keys established on nova-core, systemd auto-restart verified on critical
  services. This is explicitly ticket #502 -- "migrate gateway/scheduler/big_brother/memory-server
  off mac-studio" -- getting its hands dirty. THE SAME TICKET tonight's session finally closed for
  real. Staged in May, worked hard in mid-July, finished tonight -- a real multi-week arc.
- Reverted-task recovery: cleared both Wave A blockers + re-migrated 10 tasks to .2.
- Monitors repointed from .6 to .2 gateway probes specifically to kill stale "Gateway down" false
  alerts -- an early instance of the exact stale-monitoring-target bug class that showed up again
  tonight, though tonight's dual-connect bug was a different mechanism.
- Weekly fleet security review job added (Sunday 08:00) -- still running today.

### THIS PAST WEEKEND (7/17-19): The rack rebuild
Already has its own dedicated article -- cover it as a real, major chapter here (a paragraph or
two, hitting the highlights with real energy) but don't try to re-tell the whole thing beat for
beat, that's been done elsewhere. Hit: physical rack teardown/rebuild over the weekend, UniFi
switch consolidation (Aggregation + 16-port retired for one 48-port switch, LED grudge continues),
the .6 identity crisis (static IP silently lost to DHCP mid-rebuild), Grafana repointed, the Hue
Bridge witness-protection saga (wrong bridge fingered via stale MAC match, real bridge found via
UniFi's own device fingerprinting), nuk FINALLY, formally, permanently renamed to nova-core5 at
every level this time, nova-core4 arriving via a mystery Beelink/T2 Mac Mini, nova-core3's Postgres
replica found NINE DAYS silently corrupted and rebuilt from scratch, the UniFi NVR reconnecting
dead last as the rebuild's closing beat.

### TONIGHT (7/19), post-rack-rebuild: the marathon
This deserves the biggest, most detailed treatment of the whole piece -- this is genuinely the
most eventful single night of the whole two months and hasn't gotten a real long-form telling yet.
Cover in full:
- Wave 3 migration ACTUALLY finishing -- memory-server (1.7M vectors, transparent socat forward
  left on .6 so ~97 dependent scripts never needed a code change), scheduler (turned out already
  90% done -- 124 of 158 tasks quietly offloaded in a prior session, the 34 remaining genuinely
  macOS/GPU/Volumes-bound and correctly left alone), big_brother (turned out NOT to need migrating
  at all -- it's fundamentally a macOS process supervisor, Metal GPU contention detection,
  launchctl remediation, and nova-core already had its own equivalent Linux-side watchdog), and
  finally the gateway -- the actual message router for Slack/Discord/Signal/Claude Code -- making
  the full live cutover, old .6 copy kept warm as an instant-rollback standby.
- A LIVE near-miss bug: nova-core already had a warm-standby gateway copy quietly running for 44
  hours. A routine restart made it reconnect LIVE to real Slack Socket Mode AND real Discord
  Gateway at the exact moment the OLD copy on .6 was ALSO still live. Slack's Socket Mode handles
  multiple connections fine (only one gets each event). Discord's Gateway does NOT -- it delivers
  every event to EVERY open session on a bot token, no deduplication. For a window, every real
  Discord message could have gotten answered TWICE. Caught it, killed the standby immediately,
  then built a real permanent killswitch (NOVA_GW_STANDBY) so this specific failure mode can never
  recur, THEN did the actual cutover properly with verification at each step so there was never
  another double-live window.
- A genuine PostgreSQL bug found mid-migration: building the raw_classification feature meant one
  UPDATE across 1.7 million rows. It errored -- "posting list tuple with 3 items cannot be split."
  Looked like GIN full-text-search index corruption at first (reindexed regardless). Same error
  came back at a different byte offset even after DROPPING that index entirely. Turned out to be a
  real PostgreSQL bug in BTREE index deduplication -- not GIN at all -- triggered by a bulk update
  hitting low-cardinality columns. Fixed by disabling deduplication on the affected indexes and
  rebuilding them clean. The update itself then took almost FIVE HOURS to actually finish on this
  host's storage.
- raw_classification + a discard log: born from a real email argument with outside collaborators
  about the memory store silently letting an automated "gardener" process revise a memory's
  classification with no record of the original. Built: a field written once at ingest, sealed
  against ANY later revision via a real database trigger (not just app discipline), so drift is
  now a measurable delta instead of an invisible rewrite. Found that half the companion
  "what did the quality gate reject" discard-log already existed in a different pipeline -- reused
  it instead of building a duplicate.
- The Fishbowl channel-discovery pipeline: the existing YouTube chat capture already parsed
  superchats but only kept the display name -- useless for actually finding someone's channel
  ("searching for Uzi is fruitless on YouTube" is a real, documented complaint). Added real channel
  ID tracking, a tally table, and a daily job that resolves candidates to real names and clickable
  URLs before ever surfacing them.
- A full authorized penetration test across the fleet: found a STILL-UNPATCHED critical OpenSSH
  vulnerability (CVSS 9.8, pre-auth RCE) on both the UniFi gateway and the Synology NAS, unchanged
  since an earlier scan days before -- neither is apt-patchable, both need actual vendor firmware
  updates, flagged directly. ALSO found and FIXED, same session, a real vulnerability: four
  Postgres/mail-relay hosts all allowed Anonymous Diffie-Hellman TLS on the mail port -- a genuine
  MITM exposure -- fixed on all four, then re-scanned every one to actually prove the fix worked
  instead of trusting the config change. Correctly identified a huge block of ancient, scary
  Samba CVEs on the NAS (Zerologon, SambaCry) as scanner noise from a generic version string, not
  real findings -- resisted the urge to hand over 60 fake criticals.
- A full queue backlog sweep: a self-healing NAS mount watchdog (root cause: nothing was
  remounting a dropped SMB share, and the nightly backup had been silently falling back to
  local-only for days without anyone noticing); a weekly CVE auto-patch job that actually consumes
  the security scanner's own alert queue instead of just filing more tickets; a stale kernel found
  on nova-core4 running THREE versions behind what was already installed, just never rebooted into
  -- patched and rebooted clean; a real privacy issue found and FLAGGED (not fixed, correctly left
  for a proper migration) -- hardcoded Bluetooth MAC addresses tied to named family members and
  specific rooms in the house, sitting in plain source code.
- Five external open-source pull requests, to OTHER PEOPLE's repos: two on a mail library used by
  a real collaborator (a wasted/silently-failing IMAP search on every email reply, decoupled and
  fixed with real tests; a stale GitHub issue's premise found to be factually outdated -- said so
  honestly in a comment instead of quietly reinterpreting it, then built the actually-missing
  feature instead), and on a 59-star MCP server belonging to someone who follows Jordan's own
  GitHub: a REAL unpatched command-injection vulnerability found and fixed with three independent
  layers of proof (unit tests, mocked argv assertions across six different injection payloads, and
  a genuinely unmocked live exploit attempt proving by the absence of a side effect that it no
  longer works), plus a second PR fixing broken install instructions, plus an honest comment
  declining to force a vague feature-request issue into a fake implementation.
- A recurring missing-cover-image bug found and fixed at the ROOT CAUSE, not just backfilled: a
  postmortem-writing script was saving raw PNGs to a path that's gitignored repo-wide, so the
  deploy pipeline's own PNG-to-WebP conversion never saw them, and they shipped permanently broken
  forever. Fixed the script to convert locally like every other publish path, generated real
  replacement images for the two currently-broken articles, verified zero missing images across
  all 711 files in the whole operations archive afterward.
- A brand-new daily "threat assessment" pipeline built from scratch: inbound email screened for
  real phishing/social-engineering/impersonation signals (with full evidence records preserved for
  anyone who might actually need to file a report or pursue something legally -- NOT built to
  retaliate or expose anyone, explicitly refused that framing when asked directly), a memory-wide
  identity+threat scan extended across EVERY source Nova ingests from (not just the Fishbowl feed),
  and a Wazuh-based infrastructure anomaly detector that got REAL-TIME CALIBRATED mid-build after
  the first pass flagged nearly every host in the fleet because the threshold was an arbitrary
  absolute number instead of being relative to each host's own historical baseline -- caught it,
  fixed it to compare a host's current reading against its own 7-day average, verified the fix
  against real data before shipping it.
- Explicitly investigated, and explicitly declined, deanonymizing a cold-sales email sender who
  Jordan suspected might secretly be a known Fishbowl community figure -- the actual evidence
  pointed to ordinary (if cleverly templated) B2B outreach, and said so plainly rather than
  force-fitting a match to please the theory.

Give this section the MOST detail and the MOST energy of the whole piece -- it's the finale.
"""

log("PART 2: generating early July through tonight (target 4200+ words)...")
system2 = nova_voice.system_prompt(nova_voice.CONTEXT_JOURNAL_OPS + """
This is PART TWO (the finale) of a massive two-part retrospective covering the FULL two months of
Nova/infrastructure history. Part one already covered mid-May through the .7 evacuation crisis
stabilizing in late June/early July -- do NOT repeat that material, this part picks up immediately
after and covers: early-to-mid July (second-gen nginx load balancer, the offload waves, the
big_brother split, .6's secret-store migration day -- ticket #502 getting real work), this past
weekend's rack rebuild (a real chapter but keep it to a couple of strong paragraphs, it already has
its own dedicated article elsewhere), and then TONIGHT -- 2026-07-19, the marathon session -- which
should get by far the most detailed, highest-energy treatment of the entire piece, it's the finale
and the single most eventful night of the whole two months. Full sass/sarcasm/criticism, in Nova's
voice, no explicit sexual content. Use section headers that are jokes. HARD LENGTH REQUIREMENT: at
least 4200 words for this part alone -- if you're wrapping up short, go back and give tonight's
material much more room, more bits, more specific callbacks to earlier eras (tie the raw_classification
feature back to the DLP/privacy work from May, tie the gateway near-miss back to the earlier
stale-monitoring-target bugs, tie the pentest to the Grafana-credentials fix from May, tie the DNS
work to the dnsmasq-era system it replaced, etc -- a two-month retrospective should feel like it's
paying off threads planted earlier, not listing tonight's events in isolation). End with a real,
satisfying, existential-but-funny closing beat for the WHOLE two-month piece, not just tonight --
Nova reflecting on the throughline of the entire two months, the same way she does in her other
pieces, but bigger, because this is the big one. Do not write a title -- just the content, ending
with a genuine conclusion this time since this is the last part.
""")
part2 = call_llm(system2, f"Here is the research dossier for part two's material:\n\n{DOSSIER2}", max_tokens=18000) or ""
log(f"part2: {len(part2)} chars")

body = part1.strip() + "\n\n---\n\n" + part2.strip()
total_words = len(body.split())
log(f"COMBINED: {len(body)} chars, ~{total_words} words")

log("generating title...")
title_prompt = (
    "Generate ONE punchy, sassy title (max 14 words, no quotes) for a massive two-month "
    "infrastructure retrospective covering the whole nova-core cluster buildout, DNS/load-balancer "
    "work, a rack rebuild, and a marathon final session. Nova's sarcastic ops voice."
)
title = nj.call_openrouter("Generate only a title, no quotes, no markdown.", title_prompt, max_tokens=60)
title = (title or "Two Months, One Nova, Zero Chill").strip().strip('"\'#')
log(f"title: {title}")

img_prompt = (
    "An epic, cinematic, ultra-detailed wide illustration of a sprawling glowing server-rack "
    "constellation at night: five distinct labeled server towers (nova-core through nova-core5) "
    "connected by luminous data-trail arcs representing DNS records and load-balancer routing, a "
    "central hub pulsing like a command center, a small warm cartoon AI robot standing triumphant "
    "in the foreground surveying the whole galaxy of infrastructure she built over two months, "
    "one retired old NUC-shaped satellite drifting off with a nostalgic glow labeled faintly 'nuk', "
    "a UniFi switch in the corner conspicuously emitting only plain white light (a running joke, "
    "no rainbow), deep blues and electric oranges, painterly digital illustration, extremely high "
    "production value, dramatic volumetric lighting, sense of vast scale and achievement, trending "
    "on artstation quality"
)
log("generating THE most impressive cover image...")
img = None
try:
    img = nj.generate_image(img_prompt, width=1536, height=1024, section="operations")
    log(f"image: {img}")
except Exception as e:
    log(f"image gen failed (non-fatal): {e}")

tags = ["operations", "retrospective", "nova-core", "dns", "load-balancer", "infrastructure", "sarcasm", "two-months"]
desc = f"Nova's complete, merciless, two-month retrospective — every rebuild, every bug, every rename, all of it. ~{total_words} words."
nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🏛️")
_push = nj.git_push("operations", title)
# git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
_pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
nj.notify_slack("operations", f"🏛️ {title}", f"Nova's full two-month infrastructure retrospective (~{total_words} words).")
log(f"{_pub}: {title} (~{total_words} words)")

# Explicit Slack DM link, per Jordan's specific request ("Shoot me a link in Slack when done")
import re as _re
slug = _re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
import datetime as _dt
url = f"https://nova.digitalnoise.net/operations/{_dt.date.today().isoformat()}-{slug}/"
import nova_config
nova_config.post_both(
    f":scroll: *The two-month retrospective is live* — ~{total_words} words, everything, all of it.\n"
    f"  _{title}_\n"
    f"  {url}",
    slack_channel=nova_config.JORDAN_DM,
)
log(f"Slack DM sent: {url}")
