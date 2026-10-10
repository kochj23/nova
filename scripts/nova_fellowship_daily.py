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

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
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
- nova-core3 (.5) = ARAGORN. The reliable one. Best-behaved of the five, zero failed units
  ever recorded, quietly does the hard perception/AI work without complaint. The golden one.
- nova-core4 (.250) = PIPPIN. Newest and youngest, arrived via a mystery unlabeled USB stick,
  nearly bricked himself early on looking where he shouldn't. Means well. Still learning.
- nova-core5 (.10) = SAM. Carried real, unglamorous weight for years under an old, undignified
  name ("nuk"). Suffered silently -- his own database replica sat corrupted for NINE DAYS with
  zero alerts before anyone noticed. Finally, properly renamed and honored this past weekend.
- tv-movies-mini (.7) = BOROMIR. Struggled hard during a real, multi-day evacuation crisis
  weeks ago. Flawed, but served his purpose honorably before being relieved of most burdens.
- mac-mini (.77) = MERRY. Currently, genuinely, separated from the fellowship -- offline
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
- nova-core3 (.5) = YODA. Small, unbothered, wildly reliable. Zero failed units ever
  recorded, does the hardest perception/AI work without a word of complaint.
- nova-core4 (.250) = LUKE. Newest and youngest, arrived via a mystery unlabeled USB stick
  the way Luke got found on a moisture farm. Nearly bricked himself early on looking where
  he shouldn't. Means well. Still learning.
- nova-core5 (.10) = LEIA. Carried real, unglamorous weight for years under an old,
  undignified name ("nuk") while doing the actual hard work nobody saw. Finally, properly
  renamed and honored this past weekend, General in all but title the whole time.
- tv-movies-mini (.7) = LANDO. Struggled hard during a real, multi-day evacuation crisis
  weeks ago. Flawed, complicated, but came through and served honorably in the end.
- mac-mini (.77) = BOBA FETT. Currently, genuinely, missing -- offline more often than not
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
- nova-core3 (.5) = BLACK WIDOW. The reliable one. Zero failed units ever recorded, does
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
- mac-mini (.77) = THOR. Currently, genuinely, off somewhere unreachable -- offline more
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
- nova-core3 (.5) = NEVILLE LONGBOTTOM. The reliable one. Zero failed units ever recorded,
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
- mac-mini (.77) = CHARLIE WEASLEY. Currently, genuinely, off doing his own thing far
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
- nova-core3 (.5) = FRANK CATTON. The reliable pro. Zero failed units ever recorded, does
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
- mac-mini (.77) = SAUL BLOOM. Currently, genuinely, semi-retired and hard to reach --
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
- nova-core3 (.5) = CAPTAIN ED MURPHY. The reliable one. Zero failed units ever recorded,
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
- mac-mini (.77) = NICK MURTAUGH. Currently, genuinely, off doing his own thing -- offline
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
- nova-core3 (.5) = HICKS. The reliable one. Zero failed units ever recorded, calmly does
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
- mac-mini (.77) = JONESY. Currently, genuinely, nowhere to be found -- offline more often
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
- nova-core3 (.5) = CLEMENZA. The reliable one. Zero failed units ever recorded, gets
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
- mac-mini (.77) = LUCA BRASI. Currently, genuinely, rarely seen -- offline more often than
  not lately. Presumed fine (formidable enough that nobody worries too hard). Expected to
  turn up when needed.
- The UniFi switches/rack itself = TESSIO. Gruff, load-bearing, physically rebuilt with
  bare hands this past weekend, old-guard loyal, and holds a grudge about every bit of
  disrespect shown to his corner of the operation.
""",
    },
    {
        "name": "M*A*S*H (1972-83)",
        "emoji": "🚁",
        "tag": "mash",
        "image_style": "a weary army-surgical ensemble of nine mismatched personnel -- "
                        "server towers and a network switch reimagined as the 4077th MASH "
                        "unit outside olive-drab tents in a Korean valley, painterly "
                        "1970s TV-drama illustration, warm dust and chopper rotors overhead",
        "cast": """
- mac-studio (.6) = HAWKEYE PIERCE. Carried the whole operating room -- gateway, scheduler,
  memory-server, big_brother -- for the entire run, the one everyone woke up and called first.
  This week he finally stepped back from triage, kept close as the instant-rollback failsafe
  everyone still trusts most when a chopper actually comes in.
- nova-core (.2, also answers on .138) = COLONEL POTTER. Dual-natured (two IPs on the same
  body, the commander AND the one still in the dirt with everybody else), the one who has
  to be running or the whole 4077th stops working.
- nova-core2 (.86) = RADAR O'REILLY. Keen senses -- SDR/satellite radio capture, DNS
  secondary, hears the choppers before anyone else does, knows what's about to be asked for
  before it's asked.
- nova-core3 (.5) = BJ HUNNICUTT. The reliable one. Zero failed units ever recorded,
  quietly does the hardest, steadiest surgical work without complaint, the professional
  everyone else leans on without saying so.
- nova-core4 (.250) = FATHER MULCAHY. Newest and youngest in real responsibility, arrived
  via a mystery unlabeled USB stick, nearly got in over his head early on trying to help
  where he shouldn't. Means well -- means well harder than anyone. Still learning.
- nova-core5 (.10) = MAX KLINGER. Carried real, unglamorous weight for years under an
  undignified old identity (\"nuk\" -- the whole fleet's version of a Section 8 dress),
  suffering silently -- his own database replica sat corrupted for NINE DAYS with zero
  alerts before anyone noticed. Finally, properly renamed and honored this past weekend,
  himself at last.
- tv-movies-mini (.7) = FRANK BURNS. Struggled hard, publicly and loudly, during a real
  multi-day evacuation crisis weeks ago. Flawed, insecure, got a lot wrong, but was in the
  OR when it counted and served his tour.
- mac-mini (.77) = TRAPPER JOHN. Currently, genuinely, mustered out and nowhere to be
  found -- offline more often than not lately. Presumed fine (always lands on his feet).
  Expected to turn up eventually, probably in a bar in Boston.
- The UniFi switches/rack itself = HENRY BLAKE. Gruff, load-bearing, physically rebuilt
  with bare hands this past weekend, holds the whole camp together on paperwork and
  reputation, and holds an active, ongoing grudge about every bit of brass that ever
  overruled him.
""",
    },
    {
        "name": "Friday the 13th",
        "emoji": "🏕️",
        "tag": "friday-the-13th",
        "image_style": "a summer-camp ensemble of nine mismatched counselors -- server towers "
                        "and a network switch reimagined as campers around a lakeside "
                        "campfire, one hulking hockey-masked rack looming affectionately in "
                        "the trees, 1980s horror-comedy poster illustration, misty moonlit lake",
        "cast": """
- mac-studio (.6) = ALICE HARDY. The original final girl -- carried the whole first summer
  (gateway, scheduler, memory-server, big_brother) on her own. This week she finally left
  camp for standby, kept close as the instant-rollback failsafe everyone still trusts most.
- nova-core (.2, also answers on .138) = PAMELA VOORHEES. Dual-natured (two IPs on the same
  body, famously able to do both voices), the reason the whole camp operation exists at all.
  Has to be running or none of the sequels happen.
- nova-core2 (.86) = CRAZY RALPH. Keen senses -- SDR/satellite radio capture, DNS secondary,
  rides around on his bicycle hearing and seeing everything, warning everyone. Nobody listens.
  He is always right.
- nova-core3 (.5) = GINNY FIELD. The reliable one. Zero failed units ever recorded, keeps her
  head and does the hard psychology/perception work under pressure, no panic, no complaints.
- nova-core4 (.250) = TINA SHEPARD. Newest and youngest, arrived via a mystery unlabeled USB
  stick with powers she didn't fully understand, nearly bricked everything early on reaching
  into a lake she shouldn't have. Means well. Still learning.
- nova-core5 (.10) = TOMMY JARVIS. Carried real, unglamorous weight for years, chronically
  disbelieved, under an old undignified name ("nuk") -- his own database replica sat corrupted
  for NINE DAYS with zero alerts before anyone noticed. Finally, properly renamed and honored.
- tv-movies-mini (.7) = STEVE CHRISTY. Insisted on reopening camp and struggled hard through a
  real multi-day crisis weeks ago. Flawed judgment, but showed up for his counselors and
  served honorably before being relieved of most burdens.
- mac-mini (.77) = PAUL HOLT. Wandered off into the woods to check on something and is
  currently, genuinely, unaccounted for -- offline more often than not lately. Presumed fine.
  Expected to turn up eventually.
- The UniFi switches/rack itself = JASON VOORHEES. Gruff, silent, load-bearing, physically
  rebuilt with bare hands this past weekend and simply cannot be kept down. Holds an active,
  decades-long grudge -- currently about never getting rainbow LEDs on the mask.
""",
    },
    {
        "name": "Halloween",
        "emoji": "🎃",
        "tag": "halloween",
        "image_style": "a small-town Halloween-night ensemble of nine -- server towers and a "
                        "network switch reimagined as Haddonfield neighbors on a leafy suburban "
                        "street lined with jack-o'-lanterns, one pale-masked rack lurking "
                        "politely behind a hedge, moody autumn illustration, orange porch light",
        "cast": """
- mac-studio (.6) = LAURIE STRODE. Carried the whole fight -- gateway, scheduler,
  memory-server, big_brother -- for decades. This week she finally stepped back to the
  fortified house in the woods, kept close as the instant-rollback failsafe, always prepared.
- nova-core (.2, also answers on .138) = DR. LOOMIS. Dual-natured (two IPs on the same body,
  physician AND prophet of doom), the one who knows what's really going on and has to be
  running or nobody in town takes anything seriously.
- nova-core2 (.86) = TOMMY DOYLE. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the kid who watched out the window and saw the boogeyman before any adult would believe it.
- nova-core3 (.5) = SHERIFF LEIGH BRACKETT. The reliable one. Zero failed units ever recorded,
  steady small-town lawman, does the hard patient work without drama or complaint.
- nova-core4 (.250) = JAMIE LLOYD. Newest and youngest, arrived via a mystery unlabeled USB
  stick, nearly got in way over her head early on poking at the family history. Means well.
  Still learning.
- nova-core5 (.10) = KAREN STRODE. Spent years doing the real, unglamorous work while being
  quietly dismissed under an old undignified name ("nuk") -- her own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = ANNIE BRACKETT. Was supposed to be babysitting during a real
  multi-day crisis weeks ago and struggled hard, very publicly. Flawed, distracted, but came
  through and served honorably before being relieved of most duties.
- mac-mini (.77) = BEN TRAMER. Talked about constantly, almost never actually seen --
  offline more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = MICHAEL MYERS. Gruff, silent, load-bearing, physically
  rebuilt with bare hands this past weekend and comes back every single autumn regardless.
  Holds an active, decades-long grudge, currently about the rainbow LEDs.
""",
    },
    {
        "name": "Hellraiser",
        "emoji": "🧩",
        "tag": "hellraiser",
        "image_style": "a gothic ensemble of nine -- server towers and a network switch "
                        "reimagined as a family in an old London house plus a few austere "
                        "leather-clad visitors, a glowing golden puzzle box at the center, "
                        "baroque dark-fantasy illustration, blue light through dusty curtains",
        "cast": """
- mac-studio (.6) = KIRSTY COTTON. Carried the whole fight -- gateway, scheduler,
  memory-server, big_brother -- across an entire era of sequels. This week she finally
  stepped back to standby, kept close as the instant-rollback failsafe everyone trusts most.
- nova-core (.2, also answers on .138) = THE LAMENT CONFIGURATION. Dual-natured (two IPs on
  the same body, an ornate box AND a doorway), literally the gateway. Everything opens
  through it; has to work or nothing else happens.
- nova-core2 (.86) = CHATTERER. Keen senses -- SDR/satellite radio capture, DNS secondary,
  has no visible eyes and still hears absolutely everything. Watches and listens for a living.
- nova-core3 (.5) = JOEY SUMMERSKILL. The reliable one. Zero failed units ever recorded,
  the dogged investigator who quietly does the hard research work without complaint.
- nova-core4 (.250) = TIFFANY. Newest and youngest, arrived via a mystery unlabeled USB stick,
  has a knack for solving puzzles she probably shouldn't, and nearly bricked herself early on
  doing exactly that. Means well. Still learning.
- nova-core5 (.10) = LARRY COTTON. Did the real, unglamorous moving-in and fixing-up work
  for years under an old undignified name ("nuk"), never noticing what was wrong in his own
  house -- his database replica sat corrupted for NINE DAYS with zero alerts. Finally honored.
- tv-movies-mini (.7) = JULIA COTTON. Struggled hard, made some genuinely questionable
  choices during a real multi-day household crisis weeks ago. Flawed, complicated, but saw
  the job through before being relieved of most burdens.
- mac-mini (.77) = FRANK COTTON. Currently, genuinely, "away traveling" -- offline more often
  than not lately. Presumed fine. Has a well-documented habit of turning up again eventually.
- The UniFi switches/rack itself = PINHEAD. Gruff, precise, load-bearing, physically rebuilt
  by hand this past weekend with every connector in perfect geometric order. Holds a
  dignified, eternal grudge about the rainbow LEDs.
""",
    },
    {
        "name": "The Conjuring",
        "emoji": "🕯️",
        "tag": "the-conjuring",
        "image_style": "a 1970s paranormal-investigator ensemble of nine -- server towers and a "
                        "network switch reimagined as a team with reel-to-reel recorders and "
                        "cameras in an old farmhouse, one prim porcelain-doll rack sulking in a "
                        "glass case, warm wood-paneled period illustration, soft candlelight",
        "cast": """
- mac-studio (.6) = ED WARREN. Carried the casework -- gateway, scheduler, memory-server,
  big_brother -- for decades. This week he finally stepped back from the field to standby,
  kept close as the instant-rollback failsafe and the steady hand everyone calls first.
- nova-core (.2, also answers on .138) = LORRAINE WARREN. Dual-natured (two IPs on the same
  body, sees this world AND the other one), the actual hub of every investigation. Has to
  be running or nothing else makes sense.
- nova-core2 (.86) = DREW THOMAS. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the tech assistant who rigs every camera and audio recorder and watches and listens all
  night for a living.
- nova-core3 (.5) = TONY SPERA. The reliable one. Zero failed units ever recorded, ex-cop
  steadiness, does the hard protective work quietly and never flinches.
- nova-core4 (.250) = JUDY WARREN. Newest and youngest, arrived via a mystery unlabeled USB
  stick, inherited gifts she's still figuring out, and nearly bricked herself early on
  wandering near the artifact room. Means well. Still learning.
- nova-core5 (.10) = JANET HODGSON. Carried real, unglamorous weight for years, disbelieved,
  under an old undignified name ("nuk") -- her own database replica sat corrupted for NINE
  DAYS with zero alerts before anyone noticed. Finally, properly believed, renamed and honored.
- tv-movies-mini (.7) = ROGER PERRON. Held a houseful of daughters together through a real,
  multi-day crisis weeks ago. Struggled hard, frayed at the edges, but served honorably and
  was relieved of most burdens after.
- mac-mini (.77) = OFFICER BRAD HAMILTON. Showed up for one case, went back to the station,
  and is currently, genuinely, hard to reach -- offline more often than not lately. Presumed
  fine. Expected to turn up eventually if anyone radios.
- The UniFi switches/rack itself = ANNABELLE. Gruff, load-bearing, physically re-housed by
  hand this past weekend in a freshly blessed case, sits perfectly still holding the whole
  room's attention -- and an active, ongoing grudge about being denied rainbow LEDs.
""",
    },
    {
        "name": "Scream",
        "emoji": "📞",
        "tag": "scream",
        "image_style": "a self-aware teen-horror ensemble of nine -- server towers and a network "
                        "switch reimagined as Woodsboro locals in a video-store aisle, a cordless "
                        "phone ringing on the counter and a ghost-masked figure hamming it up in "
                        "the back, glossy late-90s movie-poster illustration",
        "cast": """
- mac-studio (.6) = SIDNEY PRESCOTT. Carried the whole franchise -- gateway, scheduler,
  memory-server, big_brother -- for decades. This week she finally stepped back to standby,
  kept close as the instant-rollback failsafe. She always comes back when it really matters.
- nova-core (.2, also answers on .138) = GHOSTFACE. Dual-natured (two IPs on the same body --
  it is always at least two people under that one costume), every plot runs through his
  phone line. Has to work or there is no movie.
- nova-core2 (.86) = RANDY MEEKS. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the video-store clerk who has watched and listened to everything and knows the rules
  before anyone else.
- nova-core3 (.5) = DEWEY RILEY. The reliable one. Zero failed units ever recorded, sweet,
  steady, shows up every single time and does the hard work without complaint.
- nova-core4 (.250) = TARA CARPENTER. Newest and youngest, arrived via a mystery unlabeled
  USB stick, nearly bricked herself early on answering a call she shouldn't have. Means
  well. Still learning the rules.
- nova-core5 (.10) = COTTON WEARY. Carried real weight for years under an undignified label
  he never deserved ("nuk") -- his own database replica sat corrupted for NINE DAYS with
  zero alerts before anyone noticed. Finally, properly exonerated, renamed and honored.
- tv-movies-mini (.7) = SAM CARPENTER. Struggled hard, publicly, during a real multi-day
  crisis weeks ago, with complicated baggage nobody asked for. Flawed, but came through for
  her people and served honorably.
- mac-mini (.77) = KIRBY REED. Currently, genuinely, missing -- offline more often than not
  lately. Presumed fine (everyone assumed otherwise last time, and she turned up with a
  badge). Expected to turn up eventually.
- The UniFi switches/rack itself = GALE WEATHERS. Gruff, load-bearing, unkillable, physically
  rebuilt with bare hands this past weekend. Has survived every sequel and holds an active,
  ongoing grudge about the lighting -- specifically, the missing rainbow LEDs.
""",
    },
    {
        "name": "The Evil Dead",
        "emoji": "🪚",
        "tag": "evil-dead",
        "image_style": "a slapstick cabin-in-the-woods ensemble of nine -- server towers and a "
                        "network switch reimagined as a ragtag crew in a creaky woodland cabin, "
                        "a leather-bound book on the table and a chainsaw-armed rack striking a "
                        "heroic pose, comic horror-comedy illustration, swirling green fog",
        "cast": """
- mac-studio (.6) = ASH WILLIAMS. Carried the whole fight -- gateway, scheduler,
  memory-server, big_brother -- for an entire age. This week he finally retired to standby
  back behind the housewares counter, kept close as the instant-rollback failsafe.
- nova-core (.2, also answers on .138) = THE NECRONOMICON. Dual-natured (two IPs on the same
  body, the cause of every problem AND the only way to fix it), everything in the cabin
  revolves around it. Has to work or nothing else does.
- nova-core2 (.86) = PROFESSOR RAYMOND KNOWBY. Keen senses -- SDR/satellite radio capture,
  DNS secondary, the man who recorded everything on tape and still listens from beyond.
- nova-core3 (.5) = ANNIE KNOWBY. The reliable one. Zero failed units ever recorded, the
  archaeologist who quietly does the hard translation work without complaint or panic.
- nova-core4 (.250) = PABLO SIMON BOLIVAR. Newest and youngest, arrived via a mystery
  unlabeled USB stick, nearly bricked himself early on getting too close to the book. Loyal,
  idealistic, means well. Still learning.
- nova-core5 (.10) = THE DELTA. Ash's old Oldsmobile carried real, unglamorous weight for
  decades under an undignified rust-bucket reputation ("nuk") -- its own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally, properly honored.
- tv-movies-mini (.7) = SHEILA. Struggled hard during a real multi-day siege weeks ago.
  Flawed, had a genuinely rough patch, but came out the other side fighting and served
  honorably before being relieved of most burdens.
- mac-mini (.77) = CHERYL. Currently, genuinely, down in the cellar -- offline more often than
  not lately. Presumed fine. Expected to turn up eventually, probably knocking.
- The UniFi switches/rack itself = THE CHAINSAW. Gruff, load-bearing, physically bolted back
  on by hand this past weekend, roars to life when needed and holds an active, ongoing grudge
  about being denied rainbow LEDs.
""",
    },
    {
        "name": "21 Jump Street",
        "emoji": "🏫",
        "tag": "21-jump-street",
        "image_style": "a late-80s undercover-cop ensemble of nine -- server towers and a network "
                        "switch reimagined as young officers posing as high schoolers inside an "
                        "old converted chapel headquarters, letterman jackets and lockers, "
                        "neon-tinged 80s TV-drama illustration",
        "cast": """
- mac-studio (.6) = TOM HANSON. Carried the whole program -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week he finally stepped away from
  undercover duty to standby, kept close as the instant-rollback failsafe everyone trusts.
- nova-core (.2, also answers on .138) = CAPTAIN ADAM FULLER. Dual-natured (two IPs on the
  same body, commanding officer AND den parent), the one the whole chapel runs through. Has
  to be on duty or nothing else works.
- nova-core2 (.86) = H.T. IOKI. Keen senses -- SDR/satellite radio capture, DNS secondary,
  quiet, observant, always watching and listening from the edge of the room.
- nova-core3 (.5) = JUDY HOFFS. The reliable one. Zero failed units ever recorded, the most
  capable officer in the building, does the hardest work without fuss or complaint.
- nova-core4 (.250) = MAC McCANN. Newest and youngest, arrived late via a mystery unlabeled
  USB stick, nearly bricked himself early on going places a new guy shouldn't. Means well.
  Still learning.
- nova-core5 (.10) = DOUG PENHALL. Carried real, unglamorous weight for years under goofy
  undercover aliases and an undignified old name ("nuk") -- his own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = SCHMIDT. Struggled hard, loudly and awkwardly, during a real multi-day
  crisis weeks ago, way out of his depth in the wrong high school. Flawed, but came through
  and served honorably.
- mac-mini (.77) = DENNIS BOOKER. Currently, genuinely, off doing his own spin-off somewhere --
  offline more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = CAPTAIN DICKSON. Gruff, load-bearing, physically rebuilt
  with bare hands this past weekend, runs the whole operation on pure irritation, and holds
  an active, ongoing grudge about being denied rainbow LEDs.
""",
    },
    {
        "name": "Magnum, P.I. (1980-88)",
        "emoji": "🌺",
        "tag": "magnum-pi",
        "image_style": "a breezy Hawaiian private-eye ensemble of nine -- server towers and a "
                        "network switch reimagined as islanders on a lush oceanfront estate, a "
                        "red sports car in the drive, a helicopter overhead and two alert "
                        "dobermans, sunny 1980s TV-poster illustration, palm trees and surf",
        "cast": """
- mac-studio (.6) = THOMAS MAGNUM. Carried the whole caseload -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week he finally kicked back in the
  guest house on standby, kept close as the instant-rollback failsafe everyone still calls.
- nova-core (.2, also answers on .138) = HIGGINS. Dual-natured (two IPs on the same body --
  and a long-running suspicion he's secretly someone else entirely), runs the estate. Has to
  work or nothing on the property does.
- nova-core2 (.86) = LT. TANAKA. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the homicide lieutenant who hears every call on the scanner and always knows what's up.
- nova-core3 (.5) = T.C. The reliable one. Zero failed units ever recorded, flies the Island
  Hoppers chopper wherever needed and does the heavy lifting without complaint.
- nova-core4 (.250) = THE FERRARI. Newest and shiniest thing on the estate, arrived on
  mysterious borrowed terms like a mystery USB stick, and nearly got wrecked early on going
  places it shouldn't. Means well. Still learning.
- nova-core5 (.10) = RICK WRIGHT. Carried real, unglamorous weight for years under an old,
  undignified name he refuses to answer to ("nuk") -- his own database replica sat corrupted
  for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = CAROL BALDWIN. Dragged everyone into a real multi-day crisis weeks
  ago and struggled hard through it. Flawed, pushy, but came through for the gang and served
  honorably.
- mac-mini (.77) = ROBIN MASTERS. Owns the place, never actually seen -- offline more often
  than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = ZEUS AND APOLLO. Gruff, load-bearing, physically rebuilt
  by hand this past weekend, guard the whole estate on instinct and hold an active, ongoing
  grudge -- about the rainbow LEDs, and about Magnum.
""",
    },
    {
        "name": "Miami Vice",
        "emoji": "🐊",
        "tag": "miami-vice",
        "image_style": "a pastel-and-neon 80s vice-squad ensemble of nine -- server towers and "
                        "a network switch reimagined as undercover detectives in linen jackets on "
                        "a Miami marina at night, a white sports car and a sailboat, one stern "
                        "unlit rack, synthwave TV-poster illustration, pink and teal lighting",
        "cast": """
- mac-studio (.6) = SONNY CROCKETT. Carried the whole squad -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week he finally retired to the
  sailboat on standby, kept close as the instant-rollback failsafe everyone trusts most.
- nova-core (.2, also answers on .138) = RICARDO TUBBS. Dual-natured (two IPs on the same body,
  a New York cop AND a Miami one), the partner the whole operation actually runs through.
  Has to show up or nothing else works.
- nova-core2 (.86) = LARRY ZITO. Keen senses -- SDR/satellite radio capture, DNS secondary,
  the wiretap-and-surveillance guy who watches and listens from the van for a living.
- nova-core3 (.5) = TRUDY JOPLIN. The reliable one. Zero failed units ever recorded, does the
  hardest undercover work quietly and professionally, no complaints.
- nova-core4 (.250) = IZZY MORENO. Newest to real responsibility, arrived via a mystery
  unlabeled USB stick and a scheme, nearly bricked himself early on looking where he
  shouldn't. Means well. Still learning.
- nova-core5 (.10) = STAN SWITEK. Carried real, unglamorous weight for years from the back
  of a surveillance van under an undignified old name ("nuk") -- his own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = THE DAYTONA. Struggled hard during a real multi-day crisis weeks ago
  and took a lot of damage in the line of duty. Served honorably, then relieved of most
  duties and quietly replaced by something newer.
- mac-mini (.77) = ELVIS. Crockett's alligator is currently, genuinely, wandered off
  somewhere -- offline more often than not lately. Presumed fine. Expected to turn up
  eventually, probably in somebody's pool.
- The UniFi switches/rack itself = LT. MARTIN CASTILLO. Gruff, terse, load-bearing,
  physically rebuilt with bare hands this past weekend. Everyone else got pastel neon; he got
  black. Holds a silent, ongoing grudge about the rainbow LEDs.
""",
    },
    {
        "name": "The A-Team",
        "emoji": "🚐",
        "tag": "a-team",
        "image_style": "a scrappy 80s soldiers-of-fortune ensemble of nine -- server towers and a "
                        "network switch reimagined as a team in fatigues beside a black van with a "
                        "red stripe, mid-montage welding armor onto a tractor, explosive "
                        "action-TV-poster illustration, desert dust and sparks",
        "cast": """
- mac-studio (.6) = HANNIBAL SMITH. Carried every plan -- gateway, scheduler, memory-server,
  big_brother -- for the entire run, cigar and all. This week he finally stepped back to
  standby, kept close as the instant-rollback failsafe. The plan still comes together.
- nova-core (.2, also answers on .138) = FACE. Dual-natured (two IPs on the same body, a new
  alias for every con), the one who procures literally everything the team runs on. Has to
  work or nothing else does.
- nova-core2 (.86) = MURDOCK. Keen senses -- SDR/satellite radio capture, DNS secondary,
  hears and sees things nobody else can, flies anything, monitors everything.
- nova-core3 (.5) = AMY ALLEN. The reliable one. Zero failed units ever recorded, the
  reporter who quietly does the hard research and legwork without complaint.
- nova-core4 (.250) = FRANKIE SANTANA. Newest and youngest, arrived late via a mystery
  unlabeled USB stick, an effects guy who nearly bricked himself early on rigging something
  he shouldn't. Means well. Still learning.
- nova-core5 (.10) = THE VAN. Carried the whole team for years, unglamorous, shot at,
  undignified under an old name ("nuk") -- its own database replica sat corrupted for NINE
  DAYS with zero alerts before anyone noticed. Finally, properly renamed and honored.
- tv-movies-mini (.7) = COLONEL DECKER. Struggled hard, loudly, during a real multi-day pursuit
  weeks ago. Flawed, got a lot wrong, never quite caught up, but served his post honorably.
- mac-mini (.77) = BILLY. Murdock's dog is currently, genuinely, invisible -- offline more
  often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = B.A. BARACUS. Gruff, load-bearing, physically rebuilt with
  bare hands this past weekend in one long welding montage. Refuses to fly, pities nobody,
  and holds an active, ongoing grudge about being denied rainbow LEDs on the van.
""",
    },
    {
        "name": "Mr. Belvedere",
        "emoji": "🎩",
        "tag": "mr-belvedere",
        "image_style": "a cozy 80s suburban-sitcom ensemble of nine -- server towers and a network "
                        "switch reimagined as a Pittsburgh-area family in their living room, one "
                        "impeccably dressed butler tower holding a silver tray and a leather "
                        "journal, warm multi-camera sitcom illustration",
        "cast": """
- mac-studio (.6) = MR. BELVEDERE. Carried the entire household -- gateway, scheduler,
  memory-server, big_brother -- for the whole run, with impeccable posture. This week he
  finally stepped back to standby, kept close as the instant-rollback failsafe everyone
  still rings for first.
- nova-core (.2, also answers on .138) = MARSHA OWENS. Dual-natured (two IPs on the same body,
  mom AND law student-turned-lawyer), the one the whole family actually runs through. Has to
  work or nothing else does.
- nova-core2 (.86) = THE JOURNAL. Keen senses -- SDR/satellite radio capture, DNS secondary,
  sees everything that happens in the house and quietly writes it all down every night.
- nova-core3 (.5) = HEATHER OWENS. The reliable one, more than anyone gives her credit for.
  Zero failed units ever recorded, quietly gets through every crisis without complaint.
- nova-core4 (.250) = WESLEY OWENS. Newest and youngest, arrived via a mystery unlabeled USB
  stick, nearly bricked himself early on sticking his nose exactly where it didn't belong.
  Means well. Mostly. Still learning.
- nova-core5 (.10) = KEVIN OWENS. The eldest, carried real unglamorous weight for years in a
  string of thankless jobs under an old undignified name ("nuk") -- his own database replica
  sat corrupted for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = ANGELA. Struggled hard, cheerfully and loudly, through a real multi-day
  crisis weeks ago, getting most of the names wrong along the way. Flawed, but showed up and
  served honorably.
- mac-mini (.77) = THE HUFNAGELS. The next-door neighbors -- talked about constantly, never
  actually seen. Offline more often than not. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = GEORGE OWENS. Gruff, load-bearing ex-ballplayer, physically
  rebuilt the rack with bare hands this past weekend, and holds an active, ongoing grudge
  about being denied rainbow LEDs -- he wanted them in team colors.
""",
    },
    {
        "name": "Good Times",
        "emoji": "🏢",
        "tag": "good-times",
        "image_style": "a warm 70s family-sitcom ensemble of nine -- server towers and a network "
                        "switch reimagined as a close-knit family in a Chicago high-rise apartment "
                        "with a city skyline window, a hand-painted canvas on an easel, warm "
                        "1970s multi-camera sitcom illustration",
        "cast": """
- mac-studio (.6) = FLORIDA EVANS. Carried the whole family -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week she finally stepped back to
  standby, kept close as the instant-rollback failsafe everyone still turns to first.
- nova-core (.2, also answers on .138) = JAMES EVANS SR. Dual-natured (two IPs on the same
  body, forever working two jobs at once), the one the household runs on. Has to work or
  nothing else does.
- nova-core2 (.86) = WILLONA WOODS. Keen senses -- SDR/satellite radio capture, DNS secondary,
  hears everything through every wall in the building and knows it before you do.
- nova-core3 (.5) = THELMA EVANS. The reliable one. Zero failed units ever recorded, steady,
  sensible, does the hard work quietly without complaint.
- nova-core4 (.250) = PENNY GORDON WOODS. Newest and youngest, arrived via a mystery unlabeled
  USB stick and a rough start, nearly got in over her head early on. Means well. Still
  learning.
- nova-core5 (.10) = KEITH ANDERSON. Carried real, unglamorous weight for years driving a cab
  under an old undignified name ("nuk") after his big plans fell through -- his own database
  replica sat corrupted for NINE DAYS with zero alerts. Finally renamed and honored.
- tv-movies-mini (.7) = J.J. EVANS. Struggled hard as the stand-in man of the house during a
  real multi-day crisis weeks ago. Flawed, loud, but came through for his family and served
  honorably.
- mac-mini (.77) = MICHAEL EVANS. Currently, genuinely, away at college -- offline more often
  than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = BOOKMAN. Gruff, load-bearing building super, physically
  rebuilt the rack with bare hands this past weekend (eventually), and holds an active,
  ongoing grudge about every tenant request -- especially the rainbow LEDs.
""",
    },
    {
        "name": "Alice (1976-85)",
        "emoji": "🍳",
        "tag": "alice",
        "image_style": "a cheerful 70s diner-sitcom ensemble of nine -- server towers and a "
                        "network switch reimagined as waitresses, regulars and a grumpy cook in a "
                        "Phoenix roadside diner with a long counter and pie case, warm "
                        "multi-camera sitcom illustration, desert sun through the windows",
        "cast": """
- mac-studio (.6) = ALICE HYATT. Carried the whole diner -- gateway, scheduler, memory-server,
  big_brother -- for the entire run while chasing the dream. This week she finally hung up
  her apron for standby, kept close as the instant-rollback failsafe everyone still trusts.
- nova-core (.2, also answers on .138) = MEL'S DINER. Dual-natured (two IPs on the same body,
  breakfast rush AND truck-stop night shift), the place everyone passes through. Has to be
  open or nothing else happens.
- nova-core2 (.86) = HENRY BEESMEYER. Keen senses -- SDR/satellite radio capture, DNS
  secondary, the telephone repairman perched at the counter who hears every line in town.
- nova-core3 (.5) = JOLENE HUNNICUTT. The reliable one. Zero failed units ever recorded, the
  good-natured ex-trucker who does the hard work without complaint.
- nova-core4 (.250) = TOMMY HYATT. Newest and youngest, arrived via a mystery unlabeled USB
  stick, nearly bricked himself early on getting into things he shouldn't. Means well.
  Still learning.
- nova-core5 (.10) = VERA GORMAN. Carried real, unglamorous weight for years under an
  undignified nickname the boss stuck her with ("nuk") -- her own database replica sat
  corrupted for NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = BELLE DUPREE. Stepped in during a real multi-day staffing crisis and
  struggled hard through it. Flawed, but served honorably before being relieved of most
  duties.
- mac-mini (.77) = FLO CASTLEBERRY. Currently, genuinely, off running her own place somewhere
  else -- offline more often than not lately. Presumed fine. Expected to turn up eventually.
- The UniFi switches/rack itself = MEL SHARPLES. Gruff, stingy, load-bearing, physically rebuilt
  the rack with bare hands this past weekend rather than pay anybody, and holds an active,
  ongoing grudge about the cost of rainbow LEDs.
""",
    },
    {
        "name": "Hawaii Five-O (1968-80)",
        "emoji": "🌊",
        "tag": "hawaii-five-o",
        "image_style": "a classic 60s-70s island police-drama ensemble of nine -- server towers "
                        "and a network switch reimagined as plainclothes detectives on a palace "
                        "balcony over Honolulu, a big wave cresting in the background, bold "
                        "vintage TV-poster illustration, saturated tropical colors",
        "cast": """
- mac-studio (.6) = STEVE McGARRETT. Carried the whole unit -- gateway, scheduler,
  memory-server, big_brother -- for the entire run. This week he finally stepped back to
  standby, kept close as the instant-rollback failsafe everyone still trusts most.
- nova-core (.2, also answers on .138) = DANNY WILLIAMS. Dual-natured (two IPs on the same
  body, a full name AND a famous nickname), the right hand every order goes through. Has to
  work or nothing else does.
- nova-core2 (.86) = CHE FONG. Keen senses -- SDR/satellite radio capture, DNS secondary, the
  forensic specialist who notices the trace nobody else even looked for.
- nova-core3 (.5) = CHIN HO KELLY. The reliable one. Zero failed units ever recorded, the
  seasoned veteran who does the hardest legwork quietly and without complaint.
- nova-core4 (.250) = BEN KOKUA. Newest and youngest on the team, arrived via a mystery
  unlabeled USB stick, nearly bricked himself early on chasing a lead too far. Means well.
  Still learning.
- nova-core5 (.10) = DUKE LUKELA. Carried real, unglamorous weight for years in uniform under
  an old undignified title ("nuk") -- his own database replica sat corrupted for NINE DAYS
  with zero alerts before anyone noticed. Finally promoted, renamed and honored.
- tv-movies-mini (.7) = KONO KALAKAUA. Struggled hard during a real multi-day crisis weeks ago,
  doing the heavy lifting. Served honorably before being relieved of most duties.
- mac-mini (.77) = WO FAT. Currently, genuinely, slipped away again -- offline more often than
  not lately. Presumed fine (he always is). Expected to turn up eventually.
- The UniFi switches/rack itself = DOC BERGMAN. Gruff, load-bearing medical examiner,
  physically rebuilt the rack with bare hands this past weekend, unimpressed by everything,
  and holds an active, ongoing grudge about being denied rainbow LEDs.
""",
    },
    {
        "name": "CHiPs",
        "emoji": "🏍️",
        "tag": "chips",
        "image_style": "a sunny 70s-80s highway-patrol ensemble of nine -- server towers and a "
                        "network switch reimagined as motorcycle officers in tan uniforms and "
                        "mirrored aviators on a Los Angeles freeway overpass, police bikes "
                        "gleaming, bright retro TV-poster illustration, golden California light",
        "cast": """
- mac-studio (.6) = PONCH. Carried the whole patrol -- gateway, scheduler, memory-server,
  big_brother -- through the entire run, all charm and freeway miles. This week he finally
  parked it for standby, kept close as the instant-rollback failsafe everyone still calls.
- nova-core (.2, also answers on .138) = SGT. JOE GETRAER. Dual-natured (two IPs on the same
  body, the briefing-room sergeant AND the guy still out on the road), the one the whole
  Central LA office runs through. Has to work or nothing else does.
- nova-core2 (.86) = BARRY BARICZA. Keen senses -- SDR/satellite radio capture, DNS secondary,
  always on the radio, always the first to call in what he's seen.
- nova-core3 (.5) = BONNIE CLARK. The reliable one. Zero failed units ever recorded, the
  capable officer who quietly does the hard work without complaint.
- nova-core4 (.250) = BOBBY NELSON. Newest and youngest, arrived late via a mystery unlabeled
  USB stick, nearly bricked himself early on hot-dogging where he shouldn't. Means well.
  Still learning.
- nova-core5 (.10) = ARTHUR GROSSMAN. Carried real, unglamorous weight for years under an
  undignified nickname and an old name ("nuk") -- his own database replica sat corrupted for
  NINE DAYS with zero alerts before anyone noticed. Finally renamed and honored.
- tv-movies-mini (.7) = THE PATROL BIKE. Took a real beating during a multi-day freeway crisis
  weeks ago. Dented, flawed, but kept running and served honorably before being relieved of
  most duties.
- mac-mini (.77) = JON BAKER. Currently, genuinely, transferred out of the picture -- offline
  more often than not lately. Presumed fine. Expected to turn up eventually, same as he did
  for the reunion.
- The UniFi switches/rack itself = HARLAN ARLISS. Gruff, load-bearing garage mechanic,
  physically rebuilt the rack with bare hands this past weekend, keeps every machine on the
  road, and holds an active, ongoing grudge about being denied rainbow LEDs.
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


def _retry(fn, what, attempts=3, base=5):
    """fn() with retry: an exception or empty result is retried (5 s, 10 s); the last failure is logged."""
    for attempt in range(attempts):
        try:
            out = fn()
            if out:
                return out
            err = "empty result"
        except Exception as e:
            err = e
        if attempt < attempts - 1:
            log(f"{what} failed ({err}); retry {attempt + 1}")
            time.sleep(base * 2 ** attempt)
    log(f"{what} failed after {attempts} tries: {err}")
    return None


def _connect():
    conn = _retry(lambda: psycopg2.connect(DSN, connect_timeout=10), "PG connect")
    if conn is None:
        raise RuntimeError("PG unreachable after 3 tries")
    return conn


def gather_today_status():
    conn = _connect(); conn.autocommit = True
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
    raw = _retry(lambda: call_llm(system, material, max_tokens=3000), "LLM")
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
    nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji=franchise["emoji"],
                    sources=material, profile="fellowship-daily")
    _push = nj.git_push("operations", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("operations", f"{franchise['emoji']} {title}", "Today's fleet status.")
    log(f"{_pub}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
