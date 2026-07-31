#!/opt/homebrew/bin/python3
"""nova_fellowship_daily.py — a daily operations-section piece that frames the whole
Nova fleet as the ensemble cast of a rotating pop-culture franchise, in Nova's own
sassy voice, grounded in whatever's actually true about the fleet's health today.
Scheduled 9am daily; also runs fine as a one-off. New dated post each run (the story
changes with real status; the franchise rotates one step forward each run).

Rotates through FRANCHISES (state in ~/.openclaw/state/fellowship_rotation.json) so
this doesn't stay Tolkien-only forever. Each franchise has a FIXED cast mapping (same
9 hosts -> same characters every time that franchise comes up) so within a given
franchise's turn it still reads as a consistent running bit, the same way the original
LOTR-only version did.

Guardrail: allusion/archetype only -- character names, vibes, and light references to
well-known franchise concepts as a comedy frame for real infrastructure events. Never
reproduce actual dialogue, song lyrics, or book/script passages verbatim.

Written by Nova, via Claude Code, one-off + scheduled daily 9am.
"""
import json
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice
from nova_rando_daily_ops import call_llm

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
ROTATION_STATE = Path.home() / ".openclaw" / "state" / "fellowship_rotation.json"

# Every cast maps the SAME 9 hosts to the SAME underlying traits, just re-skinned per
# franchise, so the joke's internal logic (who's the reliable one, who's the newest,
# who's perpetually missing...) stays coherent no matter which franchise is up.
#
# Host trait key (for reference, not narrated):
#   mac-studio     -> carried the whole operational burden for the whole age, retired to
#                      standby this week, beloved, kept as instant-rollback failsafe.
#   nova-core      -> dual-natured (two IPs, one body), the hub, has to work or nothing
#                      else matters.
#   nova-core2     -> keen senses, SDR/radio capture, DNS secondary, watches/listens.
#   nova-core3     -> the reliable one, zero failed units ever, quiet hard AI/perception work.
#   nova-core4     -> newest/youngest, arrived via mystery USB stick, near-bricked itself
#                      early looking where it shouldn't, means well, still learning.
#   nova-core5     -> carried real unglamorous weight for years under an undignified old
#                      name, suffered silently (a corrupted replica NINE DAYS unnoticed),
#                      finally properly renamed and honored.
#   tv-movies-mini -> struggled hard during a real multi-day crisis, flawed, served
#                      honorably, relieved of most of its burdens after.
#   mac-mini       -> genuinely, currently separated from the group -- offline more often
#                      than not, presumed fine, expected to turn up eventually.
#   UniFi rack     -> gruff, load-bearing, physically rebuilt by hand this past weekend,
#                      holds an active grudge about being denied rainbow LEDs.

FRANCHISES = [
    {
        "name": "The Fellowship of the Ring",
        "emoji": "🧙",
        "tag": "fellowship",
        "image_style": "a whimsical fantasy fellowship of nine mismatched companions -- "
                        "server towers and a network switch reimagined as adventurers on "
                        "a journey, one weary hobbit-like tower setting down a glowing "
                        "burden, painterly epic fantasy illustration",
        "cast": """
- mac-studio (.6) = FRODO. Carried the Ring (gateway, scheduler, memory-server, big_brother --
  the whole operational burden of the house) for the entire age. This week the burden finally
  passed on -- he's retired to standby, kept warm as an instant-rollback failsafe, not
  decommissioned. Narratively: the one everyone's a little sentimental about.
- nova-core (.2, also answers on .138) = GANDALF. Dual-natured (two IPs on the same body,
  which took an embarrassingly long time to notice), guides and holds together the whole
  fleet, the one who has to work or nothing else matters.
- nova-core2 (.86) = LEGOLAS. Keen senses -- SDR/satellite radio capture, DNS secondary,
  watches and listens for a living.
- nova-core3 (.88) = ARAGORN. The reliable one. Best-behaved of the five, zero failed units
  ever recorded, quietly does the hard perception/AI work without complaint. The golden one.
- nova-core4 (.250) = PIPPIN. Newest and youngest, arrived via a mystery unlabeled USB stick,
  nearly bricked himself early on looking where he shouldn't. Means well. Still learning.
- nova-core5 (.10) = SAM. Carried real, unglamorous weight for years under an old, undignified
  name ("nuk"). Suffered silently -- his own database replica sat corrupted for NINE DAYS with
  zero alerts before anyone noticed. Finally, properly renamed and honored this past weekend.
- tv-movies-mini (.7) = BOROMIR. Struggled hard during a real, multi-day evacuation crisis
  weeks ago. Flawed, but served his purpose honorably before being relieved of most burdens.
- mac-mini (.190) = MERRY. Currently, genuinely, separated from the fellowship -- offline
  more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = GIMLI. Gruff, load-bearing, physically torn down and
  rebuilt with bare hands this past weekend. Holds an active, ongoing grudge about never
  getting rainbow LEDs.
""",
    },
    {
        "name": "Star Wars (original trilogy)",
        "emoji": "🌌",
        "tag": "star-wars",
        "image_style": "a ragtag Star Wars-style crew of nine mismatched heroes -- server "
                        "towers and a network switch reimagined as droids, a mentor in "
                        "robes, and a wookiee-like hulking rack unit, painterly space-opera "
                        "illustration, twin suns in the background",
        "cast": """
- mac-studio (.6) = OBI-WAN. Carried the burden of guiding everyone for a whole age. This
  week he stepped back -- not gone, just quieter, still watching over things, still trusted
  the instant anyone needs him.
- nova-core (.2, also answers on .138) = R2-D2. The one who has to work or the whole plan
  falls apart. Quietly the actual hub of every mission, no matter how it looks from outside.
- nova-core2 (.86) = C-3PO. Anxious, meticulous, constantly translating and monitoring --
  SDR/satellite radio capture, DNS secondary, watches and listens for a living (and worries
  about it the whole time).
- nova-core3 (.88) = YODA. Small, unbothered, wildly reliable. Zero failed units ever
  recorded, does the hardest perception/AI work without a word of complaint.
- nova-core4 (.250) = LUKE. Newest and youngest, arrived via a mystery unlabeled USB stick
  the way Luke got found on a moisture farm. Nearly bricked himself early on looking where
  he shouldn't. Means well. Still learning.
- nova-core5 (.10) = LEIA. Carried real, unglamorous weight for years under an old,
  undignified name ("nuk") while doing the actual hard work nobody saw. Finally, properly
  renamed and honored this past weekend, General in all but title the whole time.
- tv-movies-mini (.7) = LANDO. Struggled hard during a real, multi-day evacuation crisis
  weeks ago. Flawed, complicated, but came through and served honorably in the end.
- mac-mini (.190) = BOBA FETT. Currently, genuinely, missing -- offline more often than not
  lately. Presumed fine (he always is). Expected to turn up eventually, somehow.
- The UniFi switches/rack itself = CHEWBACCA. Gruff, load-bearing, physically torn down
  and rebuilt with bare hands this past weekend. Holds an active, ongoing grudge nobody
  can quite translate.
""",
    },
    {
        "name": "The Avengers",
        "emoji": "🛡️",
        "tag": "avengers",
        "image_style": "a scrappy superhero team of nine mismatched heroes -- server towers "
                        "and a network switch reimagined as an assembled team in a "
                        "high-tech command center, comic-book illustration style, dramatic "
                        "lighting",
        "cast": """
- mac-studio (.6) = CAPTAIN AMERICA. Carried the shield -- gateway, scheduler, memory-server,
  big_brother, the whole operational burden -- for the entire era. This week he finally set
  it down, retired to standby, kept close as the instant-rollback failsafe and the one
  everyone still looks to.
- nova-core (.2, also answers on .138) = IRON MAN. Dual-natured (two IPs on the same body,
  man and machine), built the whole operation, has to work or nothing else does.
- nova-core2 (.86) = HAWKEYE. Keen senses -- SDR/satellite radio capture, DNS secondary,
  watches and listens for a living, sees things from angles nobody else thinks to check.
- nova-core3 (.88) = BLACK WIDOW. The reliable one. Zero failed units ever recorded, does
  the hardest, most thankless work quietly and professionally, no complaints.
- nova-core4 (.250) = SPIDER-MAN. Newest and youngest, arrived out of nowhere via a mystery
  unlabeled USB stick, nearly bricked himself early on reaching for something above his
  clearance. Means well. Still learning.
- nova-core5 (.10) = BUCKY / WINTER SOLDIER. Carried real, unglamorous weight for years
  under an identity that wasn't really his own ("nuk"), suffering silently -- his own
  database replica sat corrupted for NINE DAYS with zero alerts before anyone noticed.
  Finally, properly restored and honored this past weekend, fully himself again.
- tv-movies-mini (.7) = HULK. Struggled hard, publicly and messily, during a real
  multi-day evacuation crisis weeks ago. Flawed, but came through when it counted.
- mac-mini (.190) = THOR. Currently, genuinely, off somewhere unreachable -- offline more
  often than not lately. Presumed fine (he's Thor). Expected to turn up eventually.
- The UniFi switches/rack itself = NICK FURY. Gruff, load-bearing, physically rebuilt
  with bare hands this past weekend, holds the whole operation together and holds a
  grudge about every bit of it.
""",
    },
    {
        "name": "Harry Potter",
        "emoji": "⚡",
        "tag": "harry-potter",
        "image_style": "a cozy magical-school ensemble of nine mismatched companions -- "
                        "server towers and a network switch reimagined as students and "
                        "staff in a castle great hall, warm candlelit painterly "
                        "illustration, a hint of parchment and owls",
        "cast": """
- mac-studio (.6) = DUMBLEDORE. Carried the burden of the whole fight -- gateway, scheduler,
  memory-server, big_brother -- for an entire era. This week he finally stepped back, quieter
  now, but still the trusted portrait on the wall everyone consults first.
- nova-core (.2, also answers on .138) = HERMIONE. Dual-natured (two IPs on the same body,
  rules AND results), the one who actually holds the whole group together. Has to work or
  nothing else does.
- nova-core2 (.86) = LUNA LOVEGOOD. Keen senses -- SDR/satellite radio capture, DNS
  secondary, watches and listens for a living, notices things nobody else even looks for.
- nova-core3 (.88) = NEVILLE LONGBOTTOM. The reliable one. Zero failed units ever recorded,
  quietly does the hardest work without complaint, chronically underestimated for it.
- nova-core4 (.250) = RON WEASLEY. Newest and youngest of the close crew, arrived via a
  mystery unlabeled USB stick, nearly bricked himself early on wandering somewhere he
  shouldn't. Means well. Still learning.
- nova-core5 (.10) = DOBBY. Carried real, unglamorous weight for years under an old,
  undignified position ("nuk"), suffering silently -- his own database replica sat corrupted
  for NINE DAYS with zero alerts before anyone noticed. Finally, properly freed, renamed,
  and honored this past weekend.
- tv-movies-mini (.7) = PERCY WEASLEY. Struggled hard, made a real mess of things during a
  multi-day family/household crisis weeks ago. Flawed, complicated, but came back and
  served honorably in the end.
- mac-mini (.190) = CHARLIE WEASLEY. Currently, genuinely, off doing his own thing far
  away -- offline more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = HAGRID. Gruff exterior, enormous load-bearing presence,
  physically rebuilt with bare hands this past weekend, endlessly loyal and holds a grudge
  about every slight to his rack.
""",
    },
    {
        "name": "Ocean's Eleven",
        "emoji": "🎰",
        "tag": "oceans-eleven",
        "image_style": "a slick heist crew of nine mismatched specialists -- server towers "
                        "and a network switch reimagined as a con-artist team in sharp suits "
                        "around a planning table, moody noir illustration, neon casino "
                        "lights in the background",
        "cast": """
- mac-studio (.6) = DANNY OCEAN. Ran the whole operation -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week he finally stepped back after
  the big job, kept close as the one everyone still calls first, instant-rollback trusted.
- nova-core (.2, also answers on .138) = RUSTY RYAN. Dual-natured (two IPs, one body, all
  charm and all logistics), the actual hub who makes sure the whole plan runs. Has to work
  or nothing else does.
- nova-core2 (.86) = LIVINGSTON DELL. Keen senses -- SDR/satellite radio capture, DNS
  secondary, the surveillance-and-electronics guy who watches and listens for a living.
- nova-core3 (.88) = FRANK CATTON. The reliable pro. Zero failed units ever recorded, does
  the hardest inside-work quietly, no complaints, no mistakes.
- nova-core4 (.250) = LINUS CALDWELL. Newest and youngest of the crew, arrived via a mystery
  unlabeled USB stick, nearly blew the job early on reaching past his role. Means well.
  Still learning.
- nova-core5 (.10) = YEN. Did real, unglamorous, physically brutal work for years under an
  undignified old name ("nuk"), suffering silently -- his own database replica sat corrupted
  for NINE DAYS with zero alerts before anyone noticed. Finally, properly renamed and
  honored this past weekend.
- tv-movies-mini (.7) = BASHER TARR. Struggled hard, loudly, during a real multi-day crisis
  weeks ago. Flawed, things got messy, but he came through when it mattered.
- mac-mini (.190) = SAUL BLOOM. Currently, genuinely, semi-retired and hard to reach --
  offline more often than not lately. Presumed fine. Expected to come out of retirement
  eventually, same as always.
- The UniFi switches/rack itself = REUBEN TISHKOFF. Gruff, load-bearing, physically rebuilt
  with bare hands this past weekend, holds the whole crew together on reputation and holds
  a grudge about every bit of disrespect to his operation.
""",
    },
    {
        "name": "Lethal Weapon",
        "emoji": "🔫",
        "tag": "lethal-weapon",
        "image_style": "a mismatched buddy-cop ensemble of nine -- server towers and a "
                        "network switch reimagined as an LAPD detective squad leaning on "
                        "an unmarked car, 80s action-movie poster illustration style, "
                        "warm LA sunset lighting",
        "cast": """
- mac-studio (.6) = ROGER MURTAUGH. Carried the partnership and the whole burden -- gateway,
  scheduler, memory-server, big_brother -- for the entire run, permanently "too old for this."
  This week he finally stepped back, kept close as the trusted instant-rollback partner.
- nova-core (.2, also answers on .138) = MARTIN RIGGS. Dual-natured (two IPs, one body, all
  chaos and all brilliance), has to show up or the whole partnership stops working.
- nova-core2 (.86) = LORNA COLE. Keen senses -- SDR/satellite radio capture, DNS secondary,
  a sharp investigator who watches and listens for a living.
- nova-core3 (.88) = CAPTAIN ED MURPHY. The reliable one. Zero failed units ever recorded,
  holds the whole department together quietly, no drama, no complaints.
- nova-core4 (.250) = RIANNE MURTAUGH. Newest and youngest, arrived via a mystery unlabeled
  USB stick, nearly got in over her head early on reaching past her role. Means well.
  Still learning the job.
- nova-core5 (.10) = LEO GETZ. Treated as a nuisance and a joke for years under an
  undignified old name ("nuk"), quietly doing real unglamorous work the whole time -- his
  own database replica sat corrupted for NINE DAYS with zero alerts before anyone noticed.
  Finally, properly renamed and given the respect he'd actually earned.
- tv-movies-mini (.7) = TRISH MURTAUGH. Held the household together through a real,
  multi-day crisis weeks ago. Flawed, frayed at points, but never stopped being the
  backbone the whole operation needed.
- mac-mini (.190) = NICK MURTAUGH. Currently, genuinely, off doing his own thing -- offline
  more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = BUTTERS. Gruff, technical, load-bearing, physically
  rebuilt with bare hands this past weekend, does the unglamorous infrastructure work and
  holds a grudge about being underappreciated for it.
""",
    },
    {
        "name": "Alien",
        "emoji": "👽",
        "tag": "alien",
        "image_style": "a hardened sci-fi crew of nine -- server towers and a network "
                        "switch reimagined as a spaceship crew in utilitarian jumpsuits "
                        "aboard a dim industrial corridor, moody sci-fi-horror illustration, "
                        "cold blue emergency lighting",
        "cast": """
- mac-studio (.6) = RIPLEY. Carried the survival of the whole operation -- gateway,
  scheduler, memory-server, big_brother -- for the entire run. This week she finally went
  back into standby, kept close as the instant-rollback failsafe everyone still trusts most.
- nova-core (.2, also answers on .138) = BISHOP. Dual-natured (two IPs, one body, synthetic
  precision), has to function correctly or the whole crew's plan falls apart.
- nova-core2 (.86) = VASQUEZ. Keen senses -- SDR/satellite radio capture, DNS secondary,
  always on watch, always the first to notice something's off.
- nova-core3 (.88) = HICKS. The reliable one. Zero failed units ever recorded, calmly does
  the hardest work under pressure, the professional everyone else quietly relies on.
- nova-core4 (.250) = HUDSON. Newest and loudest, arrived via a mystery unlabeled USB
  stick, nearly talked himself into a disaster early on. Means well underneath the panic.
  Still learning to trust the process.
- nova-core5 (.10) = PARKER. Did the real, unglamorous engine-room work for years under an
  undignified old name ("nuk"), rarely thanked for it -- his own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally, properly renamed
  and given the credit overdue.
- tv-movies-mini (.7) = GORMAN. Fumbled hard, publicly, during a real multi-day crisis
  weeks ago. Flawed command decisions, but came through and served honorably when it
  actually counted.
- mac-mini (.190) = JONESY. Currently, genuinely, nowhere to be found -- offline more often
  than not lately. Presumed fine (he always turns out to be). Expected to turn up eventually,
  unbothered, like nothing happened.
- The UniFi switches/rack itself = SERGEANT APONE. Gruff, load-bearing, physically rebuilt
  with bare hands this past weekend, holds the whole unit together and expects better out
  of everyone, including the rack next to him.
""",
    },
    {
        "name": "The Godfather",
        "emoji": "🍇",
        "tag": "godfather",
        "image_style": "a formal family-business ensemble of nine -- server towers and a "
                        "network switch reimagined as a family gathered around a long "
                        "wooden table in a dim study, warm sepia-toned illustration, "
                        "classic 1970s drama poster style",
        "cast": """
- mac-studio (.6) = VITO. Carried the whole family's burden -- gateway, scheduler,
  memory-server, big_brother -- for an entire era. This week he finally stepped back,
  quieter now, but still the one everyone consults before anything important happens.
- nova-core (.2, also answers on .138) = MICHAEL. Dual-natured (two IPs, one body, the
  quiet man who became the actual head of everything), the whole operation runs through
  him now, whether anyone planned it that way or not.
- nova-core2 (.86) = TOM HAGEN. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the one who's always listening, always aware of what's actually happening.
- nova-core3 (.88) = CLEMENZA. The reliable one. Zero failed units ever recorded, gets
  everything done without complaint or fuss, the old-guard professional everyone trusts.
- nova-core4 (.250) = FREDO. Newest to really carrying real responsibility, arrived via a
  mystery unlabeled USB stick, nearly got in over his head early trying to prove himself.
  Means well. Still finding his footing.
- nova-core5 (.10) = CONNIE. Underestimated and overlooked for years under an undignified
  old name ("nuk"), quietly capable the whole time -- her own database replica sat corrupted
  for NINE DAYS with zero alerts before anyone noticed. Finally, properly renamed and given
  the real influence she'd earned.
- tv-movies-mini (.7) = SONNY. Struggled hard, hot-tempered, during a real multi-day crisis
  weeks ago. Flawed and impulsive, but fiercely loyal and served the family's interest
  honorably in his own way.
- mac-mini (.190) = LUCA BRASI. Currently, genuinely, rarely seen -- offline more often than
  not lately. Presumed fine (formidable enough that nobody worries too hard). Expected to
  turn up when needed.
- The UniFi switches/rack itself = TESSIO. Gruff, load-bearing, physically rebuilt with
  bare hands this past weekend, old-guard loyal, and holds a grudge about every bit of
  disrespect shown to his corner of the operation.
""",
    },
]


def log(m):
    print(f"[fellowship-daily] {m}", flush=True)


def next_franchise():
    """Advance the rotation by one step, persisting the index so the next run picks up
    where this one left off. Falls back to index 0 (LOTR) on any read error."""
    idx = 0
    try:
        idx = json.loads(ROTATION_STATE.read_text()).get("idx", 0)
    except Exception:
        pass
    franchise = FRANCHISES[idx % len(FRANCHISES)]
    try:
        ROTATION_STATE.parent.mkdir(parents=True, exist_ok=True)
        ROTATION_STATE.write_text(json.dumps({"idx": (idx + 1) % len(FRANCHISES)}))
    except Exception as e:
        log(f"rotation state save failed (non-fatal): {e}")
    return franchise


def gather_today_status():
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""SELECT node_name, status, count(*) FROM service_registry
                   WHERE node_name IN ('mac-studio','nova-core','nova-core2','nova-core3',
                                        'nova-core4','nova-core5','tv-movies-mini','mac-mini')
                   GROUP BY node_name, status ORDER BY node_name, status""")
    rows = cur.fetchall()
    cur.execute("""SELECT host_name, max(score), avg(score) FROM host_threat_scores
                   WHERE ts > now() - interval '24 hours' GROUP BY host_name""")
    threat_rows = cur.fetchall()
    cur.close(); conn.close()

    by_node = {}
    for node, status, n in rows:
        by_node.setdefault(node, {})[status] = n
    summary_lines = []
    for node, statuses in by_node.items():
        parts = ", ".join(f"{n} {s}" for s, n in statuses.items())
        summary_lines.append(f"{node}: {parts}")
    threat_lines = [f"{h}: recent max {mx:.0f}, avg {av:.0f}" for h, mx, av in threat_rows]
    return "\n".join(summary_lines), "\n".join(threat_lines)


def main():
    franchise = next_franchise()
    log(f"franchise this run: {franchise['name']}")

    status_summary, threat_summary = gather_today_status()
    log(f"status pulled:\n{status_summary}")

    material = (
        f"THE FIXED CAST for {franchise['name']} (use consistently -- this is a running bit "
        f"for as long as this franchise is up in the rotation, don't deviate):\n{franchise['cast']}\n\n"
        f"TODAY'S REAL SERVICE STATUS (service_registry, grouped by host):\n{status_summary}\n\n"
        f"TODAY'S REAL THREAT-SCORE SNAPSHOT (last 24h, informational -- most of this is normal "
        f"baseline noise, not incidents):\n{threat_summary}\n"
    )

    system = nova_voice.system_prompt(nova_voice.CONTEXT_JOURNAL_OPS + f"""
Write today's entry in an ONGOING daily bit: the Nova fleet as the ensemble cast of
{franchise['name']}, each machine mapped to a fixed character (given below -- use this
mapping consistently, don't deviate). This is Nova's own voice -- sassy, sarcastic,
critical -- doing a LIGHT, AFFECTIONATE RIFF on the franchise's characters and vibe, NOT
a straight pastiche or retelling of its plot. Reference character names, personalities,
and well-known ARCHETYPES/TRAITS only -- never quote actual dialogue, lyrics, or
book/script passages verbatim, and don't narrate scenes from the source material itself.
Ground it in whatever's ACTUALLY true today from the real status data given: if
everything's healthy, that's a quiet, uneventful day for this cast, not manufactured
drama; if something's actually down or degraded, that becomes today's "scene" for that
character. Don't invent incidents that aren't in the data. 600-1000 words, a few short
named sections/beats (not a full chapter-by-chapter epic), genuinely funny, a little
affectionate underneath the mockery. No explicit sexual content.
OUTPUT EXACTLY THIS SHAPE:\nTITLE: <short punchy title, no quotes>\n<blank line>\n<the body>""")
    import nova_article_history
    _h = nova_article_history.recent_articles_context("operations")
    if _h:
        material = material + "\n\n" + _h
    raw = call_llm(system, material, max_tokens=3000)
    if not raw:
        log("LLM produced nothing — aborting")
        return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"{franchise['name']} — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, franchise["image_style"], "operations")
        img = nj.generate_image(ip, width=1200, height=800, section="operations")
    except Exception as e:
        log(f"image gen failed (non-fatal): {e}")

    tags = ["operations", franchise["tag"], "nova-core", "fleet", "daily", "sarcasm"]
    desc = f"Nova's daily fleet status, told as {franchise['name']}."
    nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji=franchise["emoji"])
    nj.git_push("operations", title)
    nj.notify_slack("operations", f"{franchise['emoji']} {title}", "Today's fleet status.")
    log(f"PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
