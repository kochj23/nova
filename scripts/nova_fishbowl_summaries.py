#!/usr/bin/env python3
"""nova_fishbowl_summaries.py — maintain an up-to-date dossier on each person in the
fishbowl / watch-community scene, synthesized from Nova's `fishbowl` memories.

For each person: pull their fishbowl memories (their channels + anything that mentions
them), have Nova write/refresh a dossier (who they are, allies/beefs, recent drama),
store the current version in nova_ops.fishbowl_people, save a recallable copy into the
fishbowl vector, and post it to #nova-info. Run on a schedule so they stay current.
"""
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# The cast. They all feud/ally with each other in the same scene — linked in `fishbowl`.
PEOPLE = [
    {"name": "Watch Nicholas", "aliases": ["watch nicholas", "nicholas"],
     "channels": ["@WatchNicholasLivestream1", "@watchnicholasstreams"]},
    {"name": "Archie Luxury", "aliases": ["archie luxury", "archieluxury", "archie", "ac3"],
     "channels": ["@ARCHIELUXURY", "@ArchieLuxuryLivestream"]},
    {"name": "Marcelo", "aliases": ["marcelo"], "channels": ["@Marcelotime"]},
    {"name": "Oisin O'Malley", "aliases": ["oisin", "o'malley", "omalley"],
     "channels": ["@oisinomalley", "@oisinomalleylive"]},
    {"name": "The Franchise Club", "aliases": ["franchise club", "the franchise"],
     "channels": ["@TheFranchiseClub", "@theFranchiseClubs"]},
    {"name": "TP Gentleman", "aliases": ["tp gentleman", "tpgentleman", "tp gent"],
     "channels": ["r/TheTpGentleman"]},
    {"name": "Watch Hangout", "aliases": ["watch hangout", "watchhangout"],
     "channels": ["@WatchHangout"]},
    {"name": "Mookie", "aliases": ["mookie", "mookieverse", "the mookieverse"],
     "channels": ["@themookieverse"]},
    {"name": "The Escapement Show", "aliases": ["escapement", "escapement show", "the escapement"],
     "channels": ["@theescapementshow"]},
    {"name": "Angelic Slayer", "aliases": ["angelic slayer", "angelicslayer"],
     "channels": ["@angelic_slayer"]},
    {"name": "The Wrist Chick", "aliases": ["wrist chick", "the wrist chick", "wristchick"],
     "channels": ["@TheWristChick"]},
    {"name": "Red Shovel", "aliases": ["red shovel", "redshovel"],
     "channels": ["@redshovel"]},
    {"name": "Doxx Report", "aliases": ["doxx report", "doxxreport", "doxx"],
     "channels": ["@doxxreport"]},
    {"name": "Tim Write", "aliases": ["tim write", "timwrite"],
     "channels": ["@TimWrite"]},
    {"name": "Paul Thorpe", "aliases": ["paul thorpe", "paulthorpe", "thorpe"],
     "channels": ["@PaulThorpeOfficial"]},
    {"name": "Bear Clooney", "aliases": ["bear clooney", "bearclooney"],
     "channels": ["@bearclooneywatches4186"]},
    {"name": "Horology Dungeon", "aliases": ["horology dungeon", "ac3dungeon", "the dungeon"],
     "channels": ["@AC3Dungeon"]},
    {"name": "Paul Pluta", "aliases": ["paul pluta", "paulpluta", "pluta", "prestige"],
     "channels": ["@PaulPlutaPrestige"]},
    {"name": "Morty's Diner", "aliases": ["morty's diner", "mortys diner", "morty"],
     "channels": ["@MortysDiner"]},
    {"name": "Watch Trapper", "aliases": ["watch trapper", "watchtrapper"],
     "channels": ["@Watchtrapper"]},
    {"name": "Watch Reporter", "aliases": ["watch reporter", "watchreporter"],
     "channels": ["@WatchReporter"]},
    {"name": "TP Gentleman", "aliases": ["tp gentleman", "timepiece gentleman", "anthony farrer", "the timepiece gentleman"],
     "channels": ["@Thetimepiecegentleman"]},
    {"name": "Roman Sharf", "aliases": ["roman sharf", "luxury bazaar", "luxbazaar"],
     "channels": ["@RomanSharf"]},
    {"name": "Grey Market Podcast", "aliases": ["grey market podcast", "gray market podcast", "grey market pod"],
     "channels": ["@greymarketpod"]},
    {"name": "The Crama Reels", "aliases": ["crama reels", "the crama reels", "crama"],
     "channels": ["@TheCramaReels"]},
]


def log(m):
    print(f"[fishbowl-summary] {m}", flush=True)


def slack(m):
    try:
        nova_config.post_both(m, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        log(f"slack: {e}")


def ensure_people(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS fishbowl_people ("
                "name text PRIMARY KEY, aliases text, channels text, summary text, "
                "n_mem int, kind text DEFAULT 'cast', signature text, "
                "updated_at timestamptz DEFAULT now())")
    cur.execute("ALTER TABLE fishbowl_people ADD COLUMN IF NOT EXISTS kind text DEFAULT 'cast'")
    cur.execute("ALTER TABLE fishbowl_people ADD COLUMN IF NOT EXISTS signature text")
    cur.execute("CREATE TABLE IF NOT EXISTS fishbowl_scanned ("
                "video_id text PRIMARY KEY, scanned_at timestamptz DEFAULT now())")
    # seed known speech signatures (text-based speaker ID — no voice diarization needed)
    cur.execute("INSERT INTO fishbowl_people (name,aliases,channels,kind,signature) VALUES "
                "(%s,%s,%s,'cast',%s) ON CONFLICT (name) DO UPDATE SET signature=EXCLUDED.signature",
                ("Watch Nicholas", "watch nicholas, nicholas",
                 "@WatchNicholasLivestream1, @watchnicholasstreams",
                 "Very distinctive voice/cadence; repeats catchphrases constantly, esp. \"It's Hard\"."))


def gather(memcur, aliases):
    conds = " OR ".join(["text ILIKE %s"] * len(aliases))
    memcur.execute(f"SELECT text FROM memories WHERE source='fishbowl' AND ({conds}) "
                   f"ORDER BY created_at DESC LIMIT 40", [f"%{a}%" for a in aliases])
    return [r[0] for r in memcur.fetchall()]


def extract_names(blob, channel):
    """LLM: who speaks/appears in this stream (host + guests), by name. They address
    each other by name and have signature catchphrases, so the cast is recoverable."""
    system = ("You extract participant names from a watch-community livestream transcript + chat. "
              "Output ONLY a comma-separated list of the distinct PEOPLE who speak or are addressed "
              "by name (the host AND guests/callers). Use the names they're called on the show. "
              "No titles, no usernames, no commentary.")
    user = f"Channel: {channel}\n\nExcerpt:\n{blob}\n\nComma-separated people who speak/appear:"
    raw = nj.call_openrouter(system, user, max_tokens=120, temperature=0.1)
    if not raw:
        return []
    return [n.strip() for n in re.split(r"[,\n]", raw) if 1 < len(n.strip()) <= 40][:15]


def discover_guests(memcur, opscur):
    """Scan recent fishbowl stream transcripts, extract who's speaking (host + guests),
    and add new names to the roster — this is how we 'catch the guests'."""
    memcur.execute("SELECT DISTINCT metadata->>'video_id', metadata->>'channel' FROM memories "
                   "WHERE source='fishbowl' AND metadata->>'type'='fishbowl_stream' "
                   "AND metadata->>'video_id' IS NOT NULL ORDER BY 1 DESC LIMIT 40")
    cast = {p["name"].lower() for p in PEOPLE}
    added = 0
    for vid, channel in memcur.fetchall():
        opscur.execute("SELECT 1 FROM fishbowl_scanned WHERE video_id=%s", (vid,))
        if opscur.fetchone():
            continue
        memcur.execute("SELECT text FROM memories WHERE source='fishbowl' "
                       "AND metadata->>'video_id'=%s AND metadata->>'part'='transcript' "
                       "ORDER BY created_at LIMIT 10", (vid,))
        blob = "\n".join(r[0] for r in memcur.fetchall())
        if len(blob) >= 200:
            for nm in extract_names(blob[:9000], channel or ""):
                if nm.lower() in cast:
                    continue
                opscur.execute("INSERT INTO fishbowl_people (name,aliases,channels,kind) "
                               "VALUES (%s,%s,'guest appearances','guest') ON CONFLICT (name) DO NOTHING",
                               (nm, nm))
                added += 1
        opscur.execute("INSERT INTO fishbowl_scanned (video_id) VALUES (%s) ON CONFLICT DO NOTHING", (vid,))
    if added:
        log(f"discover_guests: +{added} new guest names")


def remember_dossier(name, summary):
    import json, urllib.request
    payload = json.dumps({"text": f"[Fishbowl dossier — {name}]\n{summary}", "source": "fishbowl",
                          "tier": "long_term", "metadata": {"type": "person_summary", "person": name,
                                                            "author": "nova", "privacy": "private"}}).encode()
    try:
        req = urllib.request.Request("http://memory-server.digitalnoise.net:18790/remember?async=1", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f"remember dossier {name}: {e}")


def summarize(name, channels, mems, signature=None):
    context = "\n\n---\n\n".join(m[:1200] for m in mems[:25])
    ctx = ("You are writing a concise DOSSIER on one person in the online watch-community "
           "'fishbowl' drama scene, drawn from Nova's own memories (transcripts, live chat, "
           "reddit). Cover: who they are, their channel(s), their allies and beefs with the "
           "other fishbowl people, the most recent drama, AND their distinctive speaking "
           "patterns / catchphrases — the verbal tics they repeat (this is how we tell "
           "speakers apart without voice ID, so call out any signature phrases). Be factual "
           "and observational. This community is toxic (slurs, attacks, threats over "
           "superchats) — report it plainly as data, do not endorse or sanitize. 150-250 words.")
    system = nova_voice.system_prompt(ctx)
    sigline = f"\nKnown speech signature: {signature}" if signature else ""
    user = (f"PERSON: {name}\nChannels: {', '.join(channels)}{sigline}\n\n"
            f"--- MEMORIES ---\n{context}\n\nWrite the dossier on {name}.")
    return nj.call_openrouter(system, user, max_tokens=700, temperature=0.4)


def main():
    memc = psycopg2.connect(MEM_DSN); memc.autocommit = True; memcur = memc.cursor()
    opsc = psycopg2.connect(OPS_DSN); opsc.autocommit = True; opscur = opsc.cursor()
    ensure_people(opscur)
    discover_guests(memcur, opscur)          # catch guests from who's speaking
    roster = [dict(p) for p in PEOPLE]
    opscur.execute("SELECT name FROM fishbowl_people WHERE kind='guest'")
    for (gname,) in opscur.fetchall():
        roster.append({"name": gname, "aliases": [gname], "channels": ["guest appearances"]})
    done = 0
    for p in roster:
        mems = gather(memcur, p["aliases"])
        if not mems:
            continue
        opscur.execute("SELECT signature FROM fishbowl_people WHERE name=%s", (p["name"],))
        srow = opscur.fetchone()
        summary = summarize(p["name"], p["channels"], mems, srow[0] if srow else None)
        if not summary:
            log(f"{p['name']}: LLM produced nothing"); continue
        summary = summary.strip()
        opscur.execute(
            "INSERT INTO fishbowl_people (name,aliases,channels,summary,n_mem,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT (name) DO UPDATE SET "
            "summary=EXCLUDED.summary, n_mem=EXCLUDED.n_mem, channels=EXCLUDED.channels, updated_at=now()",
            (p["name"], ", ".join(p["aliases"]), ", ".join(p["channels"]), summary, len(mems)))
        remember_dossier(p["name"], summary)
        slack(f":bust_in_silhouette: *Fishbowl dossier — {p['name']}* "
              f"({', '.join(p['channels'])}, from {len(mems)} memories)\n{summary[:1500]}")
        log(f"{p['name']}: dossier updated ({len(mems)} mems)")
        done += 1
    if done == 0:
        slack(":hourglass: *Fishbowl dossiers* — no per-person memories yet; "
              "crawls are still filling. Will refresh as content lands.")
    memc.close(); opsc.close()


if __name__ == "__main__":
    main()
