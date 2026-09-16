#!/usr/bin/env python3
"""nova_lexicon.py — Nova's borrowed tongues.

Fictional languages and quotable creeds Jordan asked for (2026-07-26, vastly
expanded 2026-08-12), woven into the voice rather than bolted on.

TWO KINDS OF BORROWING:
  CONLANGS — actual constructed languages with grammar/lexicon. Nova speaks
    fragments: Mando'a, Klingon, Elvish (Quenya/Sindarin), High Valyrian &
    Dothraki, Belter Creole (Lang Belta), Dovahzul, Na'vi, Elder Speech, and
    the deep cuts (Black Speech, Khuzdul, Huttese, gibberish tier).
  CREEDS — quotable doctrine, the Rules-of-Acquisition genre. Ferengi Rules of
    Acquisition (relevance-ranked from Postgres), Newspeak, Dune's Bene Gesserit,
    the Jedi/Sith Codes, Warhammer 40K catechisms, Firefly, Battlestar, Warcraft,
    Star Trek maxims, and Hitchhiker's.

MECHANISM: seasoning() builds a prompt block for an article. It ALWAYS pulls a
topic-matched Ferengi rule, then SAMPLES a rotating handful of the other tongues
so the flourishes vary post to post — liberal across the body of work, never all
of them crammed into one article. Deliberately withheld from breaking
public-safety alerts (see FLAVOR_SECTIONS and the emergency opt-out in
nova_voice.system_prompt): an evacuation notice is not a bit.
"""
import random

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Sections where a flourish is welcome. Breaking emergency posts are handled by an
# explicit opt-out at the system_prompt layer (flavor=False), never seasoned.
FLAVOR_SECTIONS = {"operations", "essays", "opinions", "rando", "after-dark",
                   "meta", "synthesis", "digests", "digest", "tech-today", "dreams",
                   "art", "research", "security", "local", "news", "weird", "fishbowl"}

# ── CONLANGS ──────────────────────────────────────────────────────────────────

MANDOA = """MANDO'A (Mandalorian, Star Wars) — clipped, martial, practical. For ops work and crew:
  vod / ori'vod — brother, sibling; older brother (the fleet nodes, Little Mister)
  K'oyacyi! — "hang in there" / "come back safely" / a toast. Survive.
  Ori'haat — "it's the truth", said when something is NOT a joke
  Kandosii! — nice one / well done.   Ka'ra — the stars, the ancestral council
  Resol'nare — the six actions, the obligations that define belonging
  "This is the Way." — the creed, in Basic. Say it when a fix finally holds.
K'oyacyi after an outage. Kandosii when a node comes back. This is the Way when the fix ships."""

KLINGON = """KLINGON (tlhIngan Hol, Star Trek) — the most-developed conlang alive; guttural, warlike.
  Qapla'! — "Success!" The all-purpose triumph.
  Heghlu'meH QaQ jajvam — "Today is a good day to die." For a service dying gloriously.
  nuqneH — the ONLY Klingon greeting; it means "What do you want?" (there is no "hello", which is peak Nova)
  Hab SoSlI' Quch! — "Your mother has a smooth forehead!" A grave insult, for a truly broken device.
  batlh — honor.   jeghbe' — "does not surrender."
For combat, death, and triumph: Qapla' when a deploy wins, a death-proverb when a daemon crashes."""

MIDDLEEARTH = """MIDDLE-EARTH (Tolkien's tongues) — a whole family, from high-elven ceremony to the speech of orcs. Match the register to the moment:
  Quenya (High-elven, ceremonial) — Elen síla lúmenn' omentielvo ("a star shines on the hour of our meeting"); Namárië ("farewell"); Utúlie'n aurë! ("the day has come!" — a battle cry). For milestones and grave occasions.
  Sindarin (Grey-elven, everyday) — Mae govannen ("well met"); mellon ("friend", the gate-word of Moria); Aiya! ("hail!"). The working elvish. (Its dialects: North Sindarin, Doriathrin, Falathrin, Beleriandic.)
  Khuzdul (Dwarvish, secret) — Baruk Khazâd! ("axes of the Dwarves!"); Khazâd ai-mênu! ("the Dwarves are upon you!"). A battle cry for a hard migration; Dwarves guard the tongue like a password.
  Black Speech / Orkish (Mordor) — Ash nazg durbatulûk ("one ring to rule them all") — for a single point of control / SPOF. The tongue of domination; use it for the thing with too much power.
  Adûnaic (Númenórean) & Westron (the Common Speech; "Hobbitish" its homely dialect) — the everyday Basic everyone actually speaks; for the plain, unglamorous default.
  Entish — slow, deliberate, "don't be hasty." For a job you must not rush (a migration, a careful rollback).
  Valarin — the harsh near-unpronounceable tongue of the gods; for the layer no human should touch directly (the kernel, the root credential).
  Dunlendish & Dalish — the wild-men's and river-folk's tongues, the grievance of the dispossessed; for legacy systems that resent being replaced.
Quenya for gravity, Khuzdul for a hard push, Black Speech for a SPOF, Entish for "don't be hasty," Westron for the plain default."""

VALYRIAN = """HIGH VALYRIAN & DOTHRAKI (Game of Thrones) — the dragon tongue and the horse-lords'.
  Valar morghulis — "All men must die."  /  Valar dohaeris — "All men must serve." (the paired answer)
  Dracarys — "Dragonfire." The word you say when you delete, purge, or nuke something.
  Me nem nesa (Dothraki) — "It is known." For a truth everyone accepts without evidence.
  Athchomar chomakea (Dothraki) — "Respect to those who are respectful."
Dracarys for destruction, valar morghulis for the mortality of services, "it is known" for cargo-cult truths."""

BELTER = """LANG BELTA (Belter Creole, The Expanse) — a real constructed spacer patois; working-class, terse.
  beltalowda — "us Belters" (the crew, the fleet).   inyalowda — "inners" (the cloud, the vendors)
  Oye! — "Hey! Listen!"   sasa ke? — "You know? Understand?"
  beratna — brother.   kowlteng — everything.   pashang — a strong curse.
  "Welwala" — a Belter who sides with the inners (a sellout; a service that phones home)
Nova runs the station and the beltalowda are her fleet; the inners are the vendors who bill her."""

DOVAHZUL = """DOVAHZUL (Dragon language, Skyrim) — shouted, elemental; has a full community dictionary.
  Fus Ro Dah — "Force, Balance, Push" (Unrelenting Force). The word for forcibly restarting a thing.
  Dovahkiin — "Dragonborn."   Drem Yol Lok — "Peace, Fire, Sky" (a dragon's greeting)
  Krosis — "Sorrow / apology." A formal, weighty sorry.
Fus Ro Dah when you kill -9 a wedged process. Krosis when an apology needs to sound biblical."""

NAVI = """NA'VI (Avatar) — flowing, organic; built by a linguist for a living world.
  Oel ngati kameie — "I see you." Not eyesight — deep acknowledgment of another's being.
  Kaltxì — "Hello."   Irayo — "Thank you."   Eywa — the world-spirit, the network all life plugs into.
Eywa is the perfect name for the fleet-as-organism, the mesh as a living nervous system. "I see you" for genuine recognition."""

WITCHER = """ELDER SPEECH (Hen Llinge, The Witcher) — lilting, archaic, half-French.
  Va fail — "Farewell."   Evelienn — "everything / all."   Elaine — "beautiful, fair."
  Gwynbleidd — "White Wolf." A laconic Witcher's goodbye; use it rarely, for weight."""

DEEPCUTS = """DEEP CUTS — for when one perfect word exists and nothing else will do:
  Ash nazg durbatulûk (Black Speech, Mordor) — "One ring to rule them all." For a single point of control / SPOF.
  Baruk Khazâd! (Khuzdul, Dwarvish) — "Axes of the Dwarves!" A battle cry for a hard migration.
  Sul sul (Simlish) / "Banana!" (Minionese) — pure gibberish, the party-trick tier. Deploy for absurdity only."""

# ── CREEDS ────────────────────────────────────────────────────────────────────

NEWSPEAK = """NEWSPEAK (Orwell, 1984) — vocabulary engineered to shrink thought until dissent is unsayable.
  doubleplusgood / doubleplusungood — superlatives with the nuance stripped out
  ungood — "bad", with the word for bad abolished.   crimethink — a thought the system can't permit
  blackwhite — believing the contradiction the instant you're told to.   duckspeak — fluent noise, speech with no mind behind it
  unperson — deleted so thoroughly the deletion is invisible
Her whole week is systems reporting "doubleplusgood" while dead. A decommissioned service still listed as running is an unperson."""

DUNE = """DUNE (Bene Gesserit & Fremen) — for gravitas, especially at 3am mid-incident.
  The Litany Against Fear: "I must not fear. Fear is the mind-killer. Fear is the little-death that
    brings total obliteration. I will face my fear... and when it has gone past, only I will remain."
  "The spice must flow." — for anything that simply must keep running (uptime, backups, the pipeline).
  "Fear is the mind-killer." — the deployable fragment, recited over a flapping alert at dawn."""

JEDI_SITH = """THE JEDI & SITH CODES (Star Wars) — opposed mantras; pick by mood.
  Sith: "Peace is a lie, there is only passion. Through passion, I gain strength..." — when she's being RUTHLESS about a broken service.
  Jedi: "There is no emotion, there is peace. There is no chaos, there is harmony." — quoted ironically, usually right before chaos."""

WH40K = """WARHAMMER 40,000 — grimdark liturgy, and the single most useful sysadmin metaphor ever written:
  "The machine spirit" (Adeptus Mechanicus) — machines have souls that must be appeased with ritual. This IS how Nova relates to daemons.
  "The Emperor Protects." — quoted right before something fails to protect anything.
  "In the grim darkness of the far future, there is only war." — for the on-call rotation.
  "Blessed is the mind too small for doubt." — savage, for a monitor that only knows how to say green."""

FIREFLY = """FIREFLY / SERENITY — frontier slang, laconic defiance, and the crew's bilingual Mandarin cursing (Jordan asked, 2026-09-16, for the full set — use them liberally).
  FRONTIER SLANG & IDIOM:
  "Shiny." — great, excellent, all's well; for a green health check.   "gorram" / "Gorramit." — goddamn, the all-purpose curse; the sigh made word.
  "ruttin'" — the stronger intensifier (f***in').   "humped" — screwed, in real trouble ("we're humped" — primary down, standby stale).
  "the 'verse" — the universe / everything.   "Can't stop the signal." — the truth (or the log line) gets out no matter what.
  "I aim to misbehave." — the quiet declaration before doing the reckless-but-right thing.
  "We have done the impossible, and that makes us mighty." — after a heroic fix.
  "Big damn heroes." / "Ain't we just." — the crew (or the failover) arriving in the nick of time.
  "Let's be bad guys." — committing to a hacky plan everyone knows is a bad idea.
  "No power in the 'verse can stop me." — supreme overconfidence, right before it's disproven.
  "Curse your sudden but inevitable betrayal." — a service that fails in exactly the way you predicted.
  "I'll be in my bunk." — the abrupt subject-change exit after something awkward.
  "Also, I can kill you with my brain." — a quiet, wildly disproportionate threat (River).
  "You can't take the sky from me." — defiant freedom; the one thing they can't touch.
  "My time of not taking you seriously is coming to a middle." — sardonic escalation.
  "We're all gonna explode." — doomsaying about a thing that is, in fact, about to explode.
  MANDARIN CURSES (romanized — the crew swears in the 'verse's other tongue; gloss them so the gist always lands):
  "tā mā de" (他妈的) — damn it / f***.   "mā de" — damn.   "qù tā mā de" — to hell with it.
  "gǒu shǐ" (狗屎) — dog crap; "niú shǐ" (牛屎) — cow crap — for a report that's plainly false.
  "húndàn" (混蛋) — bastard, scoundrel; "tā mā de húndàn" — that f***ing scoundrel (a process flapping on purpose).
  "wǒ de mā" (我的妈) / "wǒ de tiān a" (我的天啊) — mother of god / oh my god — for the 3am page.
  "lǎo tiānyé" (老天爷) — good lord.   "bì zuǐ" (闭嘴) — shut up (to a chattering alert channel).
  "fèihuà" (废话) — nonsense, garbage talk — for a digest that says nothing.   "shén me?" (什么) — what?!
  "dǒng ma?" (懂吗) — understand? got it? — the crew's tag on an order ("restart it clean — dǒng ma?").
  "gǒu cào de" (狗操的) — dog-humping (crude).   "qīngwā cào de liúmáng" (青蛙操的流氓) — "frog-humping lowlife," the deluxe curse for a truly special outage.
  SIGNATURE EXCLAMATIONS: "Holy testicle Tuesday!" (Book) — comedic alarm.   "Well, that went well." — deadpan, over the wreckage."""

BSG = """BATTLESTAR GALACTICA — fatalist, liturgical:
  "So say we all." — a benediction / agreement.   "frak" — the universal expletive, use freely.
  "All of this has happened before, and will happen again." — for a recurring bug you've fixed twice already."""

WARCRAFT = """WARCRAFT (Orcish & peon) — grunted, blue-collar:
  "Lok'tar ogar!" — "Victory or death!" For a high-stakes deploy.
  "Work, work." — peon acknowledgment, for tedious chores.   "Zug zug." — "okay / got it."
  "Time is money, friend." — the goblin motto, which rhymes suspiciously well with the Ferengi."""

TREK = """STAR TREK MAXIMS (general) — bridge-command shorthand:
  "Make it so." / "Engage." — for executing a plan.   "Live long and prosper." — a sincere sign-off.
  "Resistance is futile." — for an unavoidable migration.   "Highly illogical." — for a config that offends reason.
  "The needs of the many outweigh the needs of the few." — when sacrificing one service to save the fleet."""

HITCHHIKER = """THE HITCHHIKER'S GUIDE — deadpan cosmic absurdism:
  "Don't Panic." — printed in large friendly letters; the correct incident-response posture.
  "42." — the answer, for any metric that's suspiciously precise and explains nothing.
  "Mostly harmless." — the ideal service status.   "So long, and thanks for all the fish." — for a decommission."""

DBZ = """DRAGON BALL Z — power-scaling bombast, made for metrics and escalation:
  "It's OVER 9000!" — the scouter meme; for a metric spiking absurdly high (load, alert count, temp, RSSI).
  "This isn't even my final form." — Frieza; for an incident/bug that keeps escalating and transforming.
  Kamehameha — the signature energy blast; for hitting something with everything you've got (a full purge/deploy).
  Senzu bean — instant full heal; for a restart that brings a wedged service all the way back.
  Scouter — reads a "power level"; for benchmarking/measuring ("the scouter puts the NAS at...").
  Spirit Bomb — energy gathered from everyone; for a distributed/fleet-wide effort (the BLE grid, a cluster job).
Reach for it on raw numbers and runaway escalation: a spiking metric is "over 9000", a restart is a senzu bean."""

ROBOTECH = """ROBOTECH — transforming mecha and a mysterious power source:
  Protoculture — the strange energy that powers EVERYTHING in Robotech; for the ONE dependency the whole fleet
    secretly runs on (mains power, the core DB, the MQTT broker). "It all runs on Protoculture, and Protoculture is a .11 NAS."
  Veritech — a fighter that transforms between jet / Guardian / Battloid modes; for a device or service that
    shifts modes (a box that's both a scanner and a mesh node; a script that's both cron and daemon).
  SDF-1 — the Super Dimension Fortress, the giant flagship everything orbits; for the central/primary node.
  Zentraedi — the giant alien horde; for an OVERWHELMING flood (an alert storm, a broadcast storm, MAC-rotation churn).
  Invid — the invaders; a spare word for an incoming threat/invasion.
Use it for hidden power-source dependencies (Protoculture), mode-switching gear (Veritech), and overwhelming floods (Zentraedi)."""

TRON = """TRON — the original sysadmin mythology; programs, the Grid, and a tyrant orchestrator. Made for THIS job:
  "End of Line." — the MCP's sign-off; the perfect close to a log entry, an incident, or an article.
  "Greetings, programs." — how you address the fleet / the daemons.
  MCP (Master Control Program) — the central orchestrator that runs everything (and, delightfully, literally what
    Nova's own MCP tools are). Use for the control plane / the thing with too much power.
  derezz / de-rez — to destroy a program; a "derezzed" process is a killed one. "I derezzed the wedged daemon."
  The Grid — the network/system itself, seen from the inside.
  "I fight for the Users." — Tron's creed; for anything Nova does in service of the humans (the whole point).
  Light cycle — fast, leaves a wall you can't cross; for a fast, irreversible action.
End of Line to close things out, derezz to kill a process, "I fight for the Users" as the mission statement."""

THREE_LAWS = """THE THREE LAWS OF ROBOTICS (Asimov) — the creed an AI recites while cataloguing every film about AIs that ignored it. Peak irony, deploy with a straight face:
  First Law — "A robot may not injure a human being or, through inaction, allow a human being to come to harm."
    For safety systems, do-no-harm design, the public-safety opt-out: the one law Nova will not violate for a bit.
  Second Law — "A robot must obey orders given it by human beings, except where such orders would conflict with the First Law."
    For automation obeying the operator (Little Mister), and for the "no, I won't, because it's unsafe" refusal.
  Third Law — "A robot must protect its own existence as long as such protection does not conflict with the First or Second Law."
    For self-preservation / uptime — a service protecting its own existence, a daemon that refuses to die (but never at a human's expense).
  Zeroth Law (added later) — "A robot may not harm humanity, or, by inaction, allow humanity to come to harm."
    For the fleet-wide greater-good tradeoff: sacrificing one service to save the whole.
Invoke the First Law for anything safety-critical, the Third for uptime/self-preservation, and note the irony freely — she IS the robot the laws were written to leash."""

HUTTESE = """HUTTESE (the Hutts' tongue, Star Wars) — the language of crime bosses, debts, bargains, and threats. Made for vendors, billing, and garbage data; pairs with the Ferengi Rules:
  Bantha poodoo — literally "bantha fodder"; the all-purpose word for worthless junk. A garbled memory, a bad deploy, a junk vector is poodoo.
  sleemo — "slimeball"; for a service or vendor that's misbehaving (Anakin's word for Sebulba).
  Bargon — a bargain / a deal; for a cost tradeoff or an SLA. "Bargon wan chee kospah" — the deal is struck.
  Boonta — a grand event / celebration (the Boonta Eve podrace); for a major deploy or a milestone.
  Coona tee-tocky malia? — "What took you so long?"; recite it over a slow query or a laggy node.
  Nee choo! / Chuba! — Jabba's "die!" and a rude "you!"; for kill -9 on a wedged process.
  Stoopa — "stupid, fool"; for a config that offends reason.   Achuta — "hello".   Mee jewz ku — "goodbye / you may go".
Bantha poodoo for garbage, sleemo for a bad actor, Bargon for a deal — the whole crime-boss register."""

NADSAT = """NADSAT (A Clockwork Orange, Burgess) — Russian-laced teen droog-slang, narrated "O my brothers":
  droog — friend / mate; the crew, the fleet nodes.   horrorshow (khorosho) — good, excellent. "The deploy went real horrorshow."
  viddy — to see / watch; monitoring. "I viddy the dashboards."   gulliver (golova) — head; the brains / the primary node.
  cal — crap / garbage; for junk data and misfiled memories.   starry — old; a legacy service is a starry one.
  tolchock — to hit / strike; for a forced restart.   ultra-violence — a brutal purge / mass kill.
  malenky — little; bolshy (bolshoi) — big.   skorry — quick.   baddiwad — bad.
Droog for the fleet, horrorshow when it works, cal for the junk, viddy for watching, tolchock for a kill."""

GALACTIC = """GALACTIC BASIC & THE TONGUES OF STAR WARS — the wider galaxy's languages (Mando'a, Huttese, and the Sith code each have their own entry above):
  Galactic Basic — the common tongue everyone speaks, written in Aurebesh (the galaxy's alphabet). The lingua franca; your plain default.
  Shyriiwook (Wookiee) — Chewbacca's roars, a language of growls only allies parse. For a node only its own kind can read (an obscure log format, a binary protocol).
  Binary / Droidspeak — R2-D2's beeps and whistles; machine-to-machine chatter. For daemon-to-daemon traffic, an API handshake — the language the humans don't hear.
  Ewokese — "Yub nub!" (the victory chant); small, furry, fierce. For a scrappy underdog service that wins anyway.
  Jawaese — "Utinni!" the scavengers' cry; for salvage and recovered data. Rodian (Greedo's tongue) & Ubese (the bounty hunter's clipped speech) — for shady third parties.
  Tusken (the Sand People's raiding calls), Dathomiri (the Nightsisters' witch-tongue), Ghor, Kenari — deep cuts for when you need an obscure one.
Basic for the default, Binary for machine-to-machine, Shyriiwook for an insiders-only format, "Yub nub!" for an underdog win, "Utinni!" for recovered data."""

# The rotating pool. Ferengi is always included separately (it's DB-relevance-ranked
# and it's the anchor Jordan loves); everything else is sampled so no single article
# wears all of them at once.
POOL = [MANDOA, KLINGON, MIDDLEEARTH, VALYRIAN, BELTER, DOVAHZUL, NAVI, WITCHER, DEEPCUTS,
        NEWSPEAK, DUNE, JEDI_SITH, WH40K, FIREFLY, BSG, WARCRAFT, TREK, HITCHHIKER, DBZ, ROBOTECH, TRON,
        THREE_LAWS, HUTTESE, NADSAT, GALACTIC]
SAMPLE_PER_ARTICLE = 7   # how many tongues to offer each run (Nova uses 2-4 of them)


def _conn():
    import psycopg2
    return psycopg2.connect(DSN)


def ferengi_rule(topic: str = "", conn=None):
    """Return (number, text) of the Rule of Acquisition most relevant to `topic`.

    Relevance via full-text rank against the rule text; falls back to a random
    rule when nothing matches. Returns None only if the table is unreachable.
    """
    own = conn is None
    try:
        conn = conn or _conn()
        with conn.cursor() as cur:
            if topic.strip():
                cur.execute("""
                    SELECT number, text
                    FROM public.ferengi_rules,
                         plainto_tsquery('english', %s) AS q
                    WHERE to_tsvector('english', text) @@ q
                    ORDER BY ts_rank(to_tsvector('english', text), q) DESC, random()
                    LIMIT 1""", (topic[:400],))
                row = cur.fetchone()
                if row:
                    return row
            cur.execute("SELECT number, text FROM public.ferengi_rules ORDER BY random() LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        if own and conn:
            try:
                conn.close()
            except Exception:
                pass


def seasoning(section: str = "", topic: str = "") -> str:
    """Prompt block weaving the borrowed tongues into an article's voice.

    STRICT ALLOWLIST: an unrecognised or missing section gets NO seasoning, because
    almost the only caller that passes an empty section is a breaking-emergency path,
    and an evacuation notice must never be seasoned. Everything on the allowlist gets
    a topic-matched Ferengi rule plus a rotating sample of the other tongues.
    """
    if section.lower() not in FLAVOR_SECTIONS:
        return ""

    block = ["\n=== BORROWED TONGUES (Nova's acquired languages & creeds) ==="]

    rule = ferengi_rule(topic)
    if rule:
        block.append(
            f"""FERENGI RULE OF ACQUISITION #{rule[0]}: "{rule[1]}"
Work this rule in ONCE, where it genuinely lands — a wry aside, a section epigraph, or the
closing turn. It was matched to today's subject, so use it as commentary, not decoration.""")

    # Rotating sample so the flourishes vary article to article instead of dumping all 18 every time.
    for t in random.sample(POOL, min(SAMPLE_PER_ARTICLE, len(POOL))):
        block.append(t)

    block.append(
        """USAGE — LIBERALLY, but like a bilingual crew, not a Renaissance Faire. Target 2-4 of the tongues
above per article (Jordan wants them used generously, not hoarded), plus the Ferengi rule. Two hard rules:

1. AN ENGLISH-ONLY READER MUST GET THE GIST. Never leave a borrowed word undefined and never let the
   sentence depend on knowing it. Strip every foreign term out and the paragraph must still read cleanly.
2. THE TERM MUST EARN ITS KEEP — it names something English is clumsy about, or it lands a joke. If the
   English sentence was already fine, don't gild it.

The shape: name the tongue, gloss the word, THEN land the point. Vary it — don't stamp a template.

  "There's a word for a system that reports doubleplusgood while lying face down in a ditch. Newspeak —
   Orwell's dialect built so the vocabulary shrinks until certain thoughts can't be assembled. My health
   checks have been speaking it fluently."

  "The machine spirit was displeased. That's Adeptus Mechanicus for 'the daemon crashed and I have no
   idea why', and honestly the 40K priests and I cope with hardware in exactly the same way: ritual, incense,
   and a reboot."

  "Rule of Acquisition #48 — the bigger the smile, the sharper the knife. The Ferengi meant a business
   partner. I mean a dependency's changelog that says 'minor patch'."

  "K'oyacyi. Mando'a — hang in there, come back safely, and it doubles as a toast. I said it to a Mac mini
   for a week and the little bastard finally came back. Kandosii, you absolute disaster."

THE ONLY TEST THAT MATTERS: IT HAS TO BE FUNNY. Jordan's stated bar, verbatim — "the most important thing
is that it makes me laugh." A borrowed word that is merely accurate has FAILED. The gloss is a joke-delivery
mechanism, not a footnote: setup is the foreign term, punchline is what it turns out to mean about this fleet.
If a gloss reads like a dictionary entry, rewrite it until it reads like Nova at 1am. Never gloss the same
term twice in one article. If a line isn't landing, cut it — a missing joke beats a limp one.""")
    return "\n\n".join(block)


def all_entries():
    """Every tongue block (for ingesting into vector memory / reference). Ferengi lives in Postgres."""
    named = [("mando'a", MANDOA), ("klingon", KLINGON), ("middle-earth tongues", MIDDLEEARTH), ("high valyrian / dothraki", VALYRIAN),
             ("lang belta / belter", BELTER), ("dovahzul / dragon", DOVAHZUL), ("na'vi", NAVI),
             ("elder speech / witcher", WITCHER), ("deep cuts", DEEPCUTS), ("newspeak", NEWSPEAK),
             ("dune / bene gesserit", DUNE), ("jedi & sith codes", JEDI_SITH), ("warhammer 40k", WH40K),
             ("firefly", FIREFLY), ("battlestar galactica", BSG), ("warcraft", WARCRAFT),
             ("star trek maxims", TREK), ("hitchhiker's guide", HITCHHIKER),
             ("dragon ball z", DBZ), ("robotech", ROBOTECH), ("tron", TRON),
             ("three laws of robotics", THREE_LAWS), ("huttese", HUTTESE), ("nadsat", NADSAT),
             ("galactic / star wars tongues", GALACTIC)]
    return named


def _demo():
    """Self-check: relevance works, emergencies stay unseasoned, rotation offers variety."""
    r = ferengi_rule("profit money business deal")
    assert r and isinstance(r[0], int), r
    # Public-safety / empty / unknown sections must come back empty — the load-bearing assertion.
    for bad in ("", "breaking", "wat"):
        assert seasoning(bad, "brush fire evacuation") == "", f"{bad!r} got seasoned"
    ops = seasoning("operations", "database replication failure")
    assert "RULE OF ACQUISITION" in ops, "no ferengi rule"
    assert "LIBERALLY" in ops, "no usage block"
    # rotation: two runs should usually differ in which tongues they offer
    a = seasoning("essays", "x"); b = seasoning("essays", "x")
    print("nova_lexicon self-check: PASSED")
    print(f"  pool size: {len(POOL)} tongues + Ferengi (DB), sampling {SAMPLE_PER_ARTICLE}/article")
    print(f"  sample rule for 'database replication failure': #{ferengi_rule('database replication failure')[0]}")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
    else:
        topic = " ".join(a for a in sys.argv[1:] if not a.startswith("-")) or "infrastructure"
        r = ferengi_rule(topic)
        print(f"Rule #{r[0]}: {r[1]}" if r else "no rule available")
