#!/usr/bin/env python3
"""
nova_gov_rss_ingest.py — Ingest government, OSINT, & interest RSS/Atom feeds into Nova's vector memory.

Covers: US Gov (FBI, GovInfo, CDC, EAC, Space Force), NATO partners (UK NCSC, UK Legislation,
European Parliament, French Senate/Assembly, Canadian Supreme Court, Norwegian Parliament),
OSINT (Bellingcat, RAND, Krebs, Talos, MITRE ATT&CK, Schneier, BleepingComputer, etc),
and Mystery/Crime Fiction (60+ blogs, magazines, and review sites).

Runs every 6 hours via scheduler. Tracks seen URLs to avoid duplicates.
Supports both RSS 2.0 (<item>) and Atom (<entry>) feed formats.

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# Feed definitions: (url, vector, label)
FEEDS = [
    # ══════════════════════════════════════════════════════════════
    # US GOVERNMENT
    # ══════════════════════════════════════════════════════════════
    # FBI
    ("https://www.fbi.gov/feeds/fbi-top-stories/rss.xml", "law", "FBI Top Stories"),
    ("https://www.fbi.gov/feeds/news-blog/rss.xml", "law", "FBI News"),
    ("https://www.fbi.gov/feeds/toptenwanted/rss.xml", "law", "FBI Most Wanted"),
    ("https://www.fbi.gov/feeds/congressional-testimony/rss.xml", "law", "FBI Testimony"),
    ("https://www.fbi.gov/feeds/executive-speeches/rss.xml", "law", "FBI Speeches"),
    # GovInfo — Legislative
    ("https://www.govinfo.gov/rss/plaw.xml", "law", "Public Laws"),
    ("https://www.govinfo.gov/rss/crec.xml", "politics", "Congressional Record"),
    ("https://www.govinfo.gov/rss/chrg.xml", "politics", "Congressional Hearings"),
    ("https://www.govinfo.gov/rss/crpt.xml", "politics", "Congressional Reports"),
    ("https://www.govinfo.gov/rss/bills-enr.xml", "law", "Enrolled Bills"),
    # GovInfo — Executive
    ("https://www.govinfo.gov/rss/fr.xml", "law", "Federal Register"),
    ("https://www.govinfo.gov/rss/dcpd.xml", "politics", "Presidential Documents"),
    ("https://www.govinfo.gov/rss/budget.xml", "economics", "US Budget"),
    # GovInfo — Oversight
    ("https://www.govinfo.gov/rss/gaoreports.xml", "operations", "GAO Reports"),
    ("https://www.govinfo.gov/rss/cmr.xml", "politics", "Mandated Reports"),
    # GovInfo — Judicial
    ("https://www.govinfo.gov/rss/usreports.xml", "law", "Supreme Court"),
    ("https://www.govinfo.gov/rss/uscourts-ca9.xml", "law", "9th Circuit"),
    ("https://www.govinfo.gov/rss/uscourts-cadc.xml", "law", "DC Circuit"),
    # US Space Force
    ("https://www.spaceforce.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1060&max=10", "military_history", "US Space Force"),
    # CDC
    ("https://tools.cdc.gov/api/v2/resources/media/342778.rss", "medicine", "CDC MMWR Weekly"),
    # Elections
    ("https://www.eac.gov/rss.xml", "politics", "US Election Assistance Commission"),
    # SoCal Emergency / Physical Security
    ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.atom", "infrastructure", "USGS Earthquakes 2.5+ Day"),
    ("https://alerts.weather.gov/cap/ca.php?x=0", "infrastructure", "NWS California Alerts"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — UK
    # ══════════════════════════════════════════════════════════════
    # UK NCSC (GCHQ-adjacent cyber threat intelligence)
    ("https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml", "intelligence", "UK NCSC All Resources"),
    ("https://www.ncsc.gov.uk/api/1/services/v1/news-rss-feed.xml", "intelligence", "UK NCSC News"),
    ("https://www.ncsc.gov.uk/api/1/services/v1/guidance-rss-feed.xml", "intelligence", "UK NCSC Guidance"),
    # UK Legislation
    ("https://www.legislation.gov.uk/new/data.feed", "law", "UK Legislation New"),
    # UK Government
    ("https://www.gov.uk/search/news-and-communications.atom", "politics", "UK Gov News"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — EUROPEAN PARLIAMENT
    # ══════════════════════════════════════════════════════════════
    ("https://www.europarl.europa.eu/rss/doc/top-stories/en.xml", "politics", "EU Parliament Top Stories"),
    ("https://www.europarl.europa.eu/rss/doc/press-releases/en.xml", "politics", "EU Parliament Press"),
    ("https://www.europarl.europa.eu/rss/doc/texts-adopted/en.xml", "law", "EU Parliament Texts Adopted"),
    ("https://www.europarl.europa.eu/rss/committee/afet/en.xml", "politics", "EU Foreign Affairs Committee"),
    ("https://www.europarl.europa.eu/rss/committee/sede/en.xml", "military_history", "EU Security & Defence Committee"),
    ("https://www.europarl.europa.eu/rss/committee/libe/en.xml", "law", "EU Civil Liberties Committee"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — FRANCE
    # ══════════════════════════════════════════════════════════════
    ("https://www.senat.fr/rss/rapports.xml", "law", "French Senate Reports"),
    ("https://www.senat.fr/themes/rss/therss29.xml", "military_history", "French Senate Defense"),
    ("https://www.senat.fr/themes/rss/therss4.xml", "politics", "French Senate Foreign Affairs"),
    ("https://www.senat.fr/rss/presse.xml", "politics", "French Senate Press"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — CANADA
    # ══════════════════════════════════════════════════════════════
    ("https://www.scc-csc.ca/case-dossier/rss/rss-eng.xml", "law", "Canadian Supreme Court"),
    ("https://www.scc-csc.ca/case-dossier/rss/rss-leave-autorisation-eng.xml", "law", "Canadian SC Leave Applications"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — NORWAY
    # ══════════════════════════════════════════════════════════════
    ("https://www.stortinget.no/no/Stottemeny/RSS/Rss-lister-for-hovedtema/Forsvar/", "military_history", "Norwegian Parliament Defense"),
    ("https://www.stortinget.no/no/Stottemeny/RSS/Rss-lister-for-hovedtema/Utenriks/", "politics", "Norwegian Parliament Foreign Affairs"),
    ("https://www.stortinget.no/no/Stottemeny/RSS/Rss-lister-for-hovedtema/Samfunnssikkerhet/", "intelligence", "Norwegian Parliament Security"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — GERMANY
    # ══════════════════════════════════════════════════════════════
    ("https://www.destatis.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/Aktuell.xml", "economics", "German Federal Statistics"),

    # ══════════════════════════════════════════════════════════════
    # NATO PARTNERS — EUROPOL
    # ══════════════════════════════════════════════════════════════
    ("https://www.europol.europa.eu/rss.xml", "law", "Europol"),

    # ══════════════════════════════════════════════════════════════
    # OSINT — CYBER THREAT INTELLIGENCE (TOP 25)
    # ══════════════════════════════════════════════════════════════
    # Tier 1: Government / Authoritative
    ("https://www.us-cert.gov/ncas/alerts.xml", "intelligence", "CISA Alerts"),
    ("https://www.us-cert.gov/ncas/current-activity.xml", "intelligence", "CISA Current Activity"),
    ("https://isc.sans.edu/rssfeed.xml", "intelligence", "SANS ISC Diary"),
    # Tier 2: Vendor Threat Research (primary sources)
    ("https://www.crowdstrike.com/blog/feed/", "intelligence", "CrowdStrike"),
    ("https://www.mandiant.com/resources/blog/rss.xml", "intelligence", "Mandiant"),
    ("https://unit42.paloaltonetworks.com/feed/", "intelligence", "Unit 42 Palo Alto"),
    ("https://blog.talosintelligence.com/rss/", "intelligence", "Cisco Talos"),
    ("https://www.sentinelone.com/blog/feed/", "intelligence", "SentinelOne Labs"),
    ("https://research.checkpoint.com/feed/", "intelligence", "Check Point Research"),
    ("https://www.huntress.com/blog/rss.xml", "intelligence", "Huntress"),
    ("https://www.elastic.co/security-labs/rss/feed.xml", "intelligence", "Elastic Security Labs"),
    ("https://www.rapid7.com/blog/rss/", "intelligence", "Rapid7"),
    ("https://blog.qualys.com/feed", "intelligence", "Qualys Threat Research"),
    ("https://www.microsoft.com/en-us/security/blog/feed/", "intelligence", "Microsoft Security"),
    ("https://www.welivesecurity.com/en/feed/", "intelligence", "WeLiveSecurity ESET"),
    ("https://www.malwarebytes.com/blog/feed", "intelligence", "Malwarebytes Labs"),
    # Tier 3: Incident Response / Forensics
    ("https://thedfirreport.com/feed/", "intelligence", "DFIR Report"),
    ("https://therecord.media/feed", "intelligence", "The Record"),
    # Tier 4: Journalism / Analysis
    ("https://krebsonsecurity.com/feed/", "intelligence", "Krebs on Security"),
    ("https://feeds.feedburner.com/TheHackersNews", "intelligence", "The Hacker News"),
    ("https://www.bleepingcomputer.com/feed/", "intelligence", "BleepingComputer"),
    ("https://www.darkreading.com/rss.xml", "intelligence", "Dark Reading"),
    ("https://www.securityweek.com/feed/", "intelligence", "SecurityWeek"),
    ("https://www.schneier.com/feed/atom/", "intelligence", "Schneier on Security"),
    ("https://grahamcluley.com/feed/", "intelligence", "Graham Cluley"),
    # Tier 5: Exploit / Vuln Databases
    ("https://www.exploit-db.com/rss.xml", "intelligence", "Exploit-DB"),
    ("https://aws.amazon.com/security/security-bulletins/rss/feed/", "intelligence", "AWS Security Bulletins"),
    # Tier 6: Frameworks / Research
    ("https://medium.com/feed/mitre-attack", "intelligence", "MITRE ATT&CK"),
    ("https://www.recordedfuture.com/feed", "intelligence", "Recorded Future"),
    ("https://cloudblog.withgoogle.com/topics/threat-intelligence/rss/", "intelligence", "Google Threat Intelligence"),

    # ══════════════════════════════════════════════════════════════
    # OSINT — GEOPOLITICAL / MILITARY ANALYSIS
    # ══════════════════════════════════════════════════════════════
    ("https://warontherocks.com/feed/", "military_history", "War on the Rocks"),
    ("https://www.bellingcat.com/feed/", "intelligence", "Bellingcat"),
    ("https://www.rand.org/blog.xml", "politics", "RAND Commentary"),
    ("https://www.rand.org/content/rand/pubs/research_reports.xml", "politics", "RAND Research Reports"),
    ("https://www.rand.org/topics/national-security-and-terrorism.xml", "military_history", "RAND National Security"),
    ("https://gcaptain.com/feed/", "economics", "gCaptain Maritime Intelligence"),

    # ══════════════════════════════════════════════════════════════
    # OSINT — NUCLEAR / WMD / ARMS CONTROL
    # ══════════════════════════════════════════════════════════════
    ("https://www.iaea.org/feeds/news", "politics", "IAEA News"),
    ("https://www.armscontrol.org/rss.xml", "military_history", "Arms Control Association"),
    ("https://fas.org/feed/", "military_history", "Federation of American Scientists"),

    # ══════════════════════════════════════════════════════════════
    # OSINT — SPACE INTELLIGENCE
    # ══════════════════════════════════════════════════════════════
    ("https://www.nasa.gov/rss/dyn/breaking_news.rss", "computing", "NASA Breaking News"),
    ("https://www.esa.int/rssfeed/Our_Activities/Space_Safety", "intelligence", "ESA Space Safety"),
    ("https://www.esa.int/rssfeed/Our_Activities/Navigation", "intelligence", "ESA Satellite Navigation"),

    # ══════════════════════════════════════════════════════════════
    # MYSTERY / CRIME FICTION & TRUE CRIME
    # ══════════════════════════════════════════════════════════════
    # Magazines & Publications
    ("https://strandmag.com/feed/", "mystery", "The Strand Magazine"),
    ("https://mysterytribune.com/feed/", "mystery", "MysteryTribune"),
    ("https://crimereads.com/category/genres/mystery/feed/", "mystery", "CrimeReads Mystery"),
    ("https://www.criminalelement.com/feed/", "mystery", "Criminal Element"),
    ("https://unsolved.com/feed/", "mystery", "Unsolved Mysteries"),
    ("https://www.mysterywire.com/feed/", "mystery", "Mystery Wire"),
    # Blogs & Reviews
    ("http://feeds.feedburner.com/TheCozyMysteryListBlog", "mystery", "The Cozy Mystery List Blog"),
    ("https://mysteryfile.com/blog/?feed=rss2", "mystery", "Mystery*File Blog"),
    ("https://listverse.com/bizarre/mysteries/feed/", "mystery", "Listverse Mysteries"),
    ("https://elizabethspanncraig.com/feed/", "mystery", "Elizabeth Spann Craig"),
    ("https://blog.world-mysteries.com/feed/", "mystery", "World Mysteries Blog"),
    ("https://jsydneyjones.wordpress.com/feed/", "mystery", "Scene of the Crime"),
    ("https://feeds.feedburner.com/feedburner/MkNK", "mystery", "The Bunburyist"),
    ("https://feeds.feedburner.com/blogspot/therapsheet", "mystery", "The Rap Sheet"),
    ("https://www.escapewithdollycas.com/feed/", "mystery", "Escape With Dollycas"),
    ("http://thepassingtramp.blogspot.com/feeds/posts/default", "mystery", "The Passing Tramp"),
    ("https://www.marilynsmysteryreads.com/feed/", "mystery", "Marilyn's Mystery Reads"),
    ("https://robin-stevens.co.uk/feed/", "mystery", "Robin Stevens Blog"),
    ("https://drusbookmusing.com/feed/", "mystery", "Dru's Book Musings"),
    ("https://shortmystery.blogspot.com/feeds/posts/default?alt=rss", "mystery", "Short Mystery Fiction Society"),
    ("https://ladiesofmystery.com/feed/", "mystery", "Ladies of Mystery"),
    ("https://unmyst3.blogspot.com/feeds/posts/default?alt=rss", "mystery", "Unsolved Mysteries In The World"),
    ("https://feeds.feedburner.com/MysteriesInParadise", "mystery", "Mysteries in Paradise"),
    ("https://somethingisgoingtohappen.net/feed/", "mystery", "Something Is Going To Happen"),
    ("http://bitterteaandmystery.blogspot.com/feeds/posts/default", "mystery", "Bitter Tea and Mystery"),
    ("https://vancouvermysteries.com/blog-vancouver-mysteries/feed/", "mystery", "Vancouver Mysteries"),
    ("https://killerhobbies.blogspot.com/feeds/posts/default", "mystery", "Killer Hobbies"),
    ("https://lesasbookcritiques.com/feed/", "mystery", "Lesa's Book Critiques"),
    ("https://cuddleupwithacozymysteryandadachshund.blog/feed/", "mystery", "Cuddle Up With a Cozy Mystery"),
    ("http://feeds.feedblitz.com/omnimysterynews", "mystery", "Omnimystery News"),
    ("https://www.lesliebudewitz.com/blog/feed/", "mystery", "Leslie Budewitz Blog"),
    ("http://mysterysuspence.blogspot.com/feeds/posts/default", "mystery", "Mysteries and My Musings"),
    ("https://classicmystery.blog/feed/", "mystery", "Classic Mystery Novel Blog"),
    ("https://chicksonthecase.com/feed/", "mystery", "Chicks on the Case"),
    ("https://ahsweetmystery.com/feed/", "mystery", "Ah Sweet Mystery"),
    ("https://writerswhokill.blogspot.com/feeds/posts/default", "mystery", "Writers Who Kill"),
    ("https://mysteryreadersinc.blogspot.com/feeds/posts/default", "mystery", "Mystery Fanfare"),
    ("https://www.missdemeanors.com/feed/", "mystery", "Miss Demeanors"),
    ("https://mainecrimewriters.com/feed/", "mystery", "Maine Crime Writers"),
    ("https://mastersofmystery.com/blogs/latest.atom", "mystery", "Masters of Mystery"),
    ("https://www.lainaturner.com/blog/feed/", "mystery", "Laina Turner Blog"),
    ("https://mysteryofmurder.wordpress.com/feed/", "mystery", "Mystery of Murder"),
    ("https://mysteriesahoy.com/feed/", "mystery", "Mysteries Ahoy!"),
    ("https://www.suzannewinterly.com/blog?format=rss", "mystery", "Suzanne Winterly Blog"),
    ("https://www.broadwaymurdermysteries.com/blogs/news.atom", "mystery", "Broadway Murder Mysteries"),
    ("https://theinvisibleevent.com/feed/", "mystery", "The Invisible Event"),
    ("https://cozymysterycafe.com/feed/", "mystery", "Cozy Mystery Cafe"),
    ("https://www.mysterycenter.com/feed/", "mystery", "Mystery Center"),
    ("https://mru.ink/feed/", "mystery", "MRU.INK"),
    ("http://lisaksbookthoughts.blogspot.com/feeds/posts/default?alt=rss", "mystery", "Lisa K's Book Reviews"),
    ("https://mysterypeople.wordpress.com/feed/", "mystery", "Mystery People"),
    ("https://www.reviewingtheevidence.com/rte_rss.xml", "mystery", "Reviewing The Evidence"),
    ("https://feeds.feedblitz.com/inreferencetomurder", "mystery", "In Reference to Murder"),
    ("https://shriploring.wordpress.com/feed/", "mystery", "Explore With Me"),
    ("https://feeds.feedburner.com/MarilynsMusings", "mystery", "Marilyn's Musings"),
    ("https://mbtb-books.blogspot.com/feeds/posts/default?alt=rss", "mystery", "MBTB's Mystery Book Blog"),
    ("https://heresthefuckingtwist.com/feed/", "mystery", "Here's the Fucking Twist"),
    ("https://stephbroadribb.com/feed/", "mystery", "Steph Broadribb"),
    ("https://theplainspokenpen.com/category/mystery/feed/", "mystery", "The Plain-Spoken Pen Mystery"),
    ("http://feeds.feedburner.com/KingsRiverLife", "mystery", "Kings River Life Magazine"),
    ("https://mysterywriters.org/feed/", "mystery", "Mystery Writers of America"),
]

MEMORY_URL = "http://192.168.1.6:18790/remember?async=1"
STATE_FILE = Path.home() / ".openclaw/workspace/state/gov_rss_seen.json"
CHUNK_SIZE = 1500


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[gov-rss {ts}] {msg}", flush=True)


def load_seen() -> set:
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep last 10000 entries (expanded for 70+ feeds)
    STATE_FILE.write_text(json.dumps(list(seen)[-10000:]))


def truncate_at_boundary(text, max_chars=2000):
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_space = cut.rfind(' ')
    if last_space > max_chars * 0.8:
        return cut[:last_space]
    return cut


def fetch_feed(url: str) -> list:
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Nova-OSINT/2.0 (nova.digitalnoise.net)")
        req.add_header("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  FETCH FAILED: {url[:60]} — {e}")
        return []

    items = []

    # Try RSS 2.0 format (<item> tags)
    for match in re.finditer(r'<item>(.*?)</item>', body, re.DOTALL):
        item_xml = match.group(1)
        title = re.search(r'<title>(.*?)</title>', item_xml, re.DOTALL)
        link = re.search(r'<link>(.*?)</link>', item_xml, re.DOTALL)
        guid = re.search(r'<guid>(.*?)</guid>', item_xml, re.DOTALL)
        desc = re.search(r'<description>(.*?)</description>', item_xml, re.DOTALL)
        pub = re.search(r'<pubDate>(.*?)</pubDate>', item_xml, re.DOTALL)

        item_link = (link.group(1).strip() if link else "") or (guid.group(1).strip() if guid else "")
        items.append({
            "title": (title.group(1).strip() if title else "")[:300],
            "link": item_link,
            "description": truncate_at_boundary((desc.group(1).strip() if desc else ""), 2000),
            "pubDate": (pub.group(1).strip() if pub else ""),
        })

    # Try Atom format (<entry> tags) if no RSS items found
    if not items:
        for match in re.finditer(r'<entry>(.*?)</entry>', body, re.DOTALL):
            entry_xml = match.group(1)
            title = re.search(r'<title[^>]*>(.*?)</title>', entry_xml, re.DOTALL)
            # Atom uses <link href="..."/> or <link href="..."></link>
            link = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', entry_xml)
            summary = re.search(r'<summary[^>]*>(.*?)</summary>', entry_xml, re.DOTALL)
            content = re.search(r'<content[^>]*>(.*?)</content>', entry_xml, re.DOTALL)
            updated = re.search(r'<updated>(.*?)</updated>', entry_xml, re.DOTALL)
            published = re.search(r'<published>(.*?)</published>', entry_xml, re.DOTALL)

            item_link = link.group(1).strip() if link else ""
            desc_text = (summary.group(1).strip() if summary else "") or (content.group(1).strip() if content else "")
            pub_date = (published.group(1).strip() if published else "") or (updated.group(1).strip() if updated else "")

            items.append({
                "title": (title.group(1).strip() if title else "")[:300],
                "link": item_link,
                "description": truncate_at_boundary(desc_text, 2000),
                "pubDate": pub_date,
            })

    return items


def clean_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#39;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def chunk_text(text: str, prefix: str) -> list:
    chunks = []
    words = text.split()
    current = f"{prefix}: "
    for word in words:
        if len(current) + len(word) + 1 > CHUNK_SIZE:
            chunks.append(current.strip())
            current = f"{prefix} (cont): "
        current += word + " "
    if current.strip() and len(current.strip()) > 50:
        chunks.append(current.strip())
    return chunks


def ingest_chunk(text: str, vector: str, metadata: dict) -> bool:
    payload = json.dumps({"text": text, "source": vector, "metadata": metadata}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def run():
    log(f"Starting government RSS ingest ({len(FEEDS)} feeds)...")
    seen = load_seen()
    total_new = 0
    total_ingested = 0

    for feed_url, vector, label in FEEDS:
        items = fetch_feed(feed_url)
        new_items = 0

        for item in items:
            url_hash = hashlib.md5((item["link"] or item["title"]).encode()).hexdigest()[:12]
            if url_hash in seen:
                continue

            seen.add(url_hash)
            new_items += 1

            title = clean_html(item["title"])
            desc = clean_html(item["description"])
            content = f"{title}. {desc}" if desc else title

            if len(content) < 30:
                continue

            prefix = f"[{label}] {title}"
            chunks = chunk_text(content, prefix)
            metadata = {
                "type": "gov_rss",
                "feed": label,
                "title": title[:200],
                "url": item["link"],
                "published": item["pubDate"],
                "ingested_at": datetime.now().isoformat(),
            }

            for chunk in chunks:
                if ingest_chunk(chunk, vector, metadata):
                    total_ingested += 1

        if new_items:
            log(f"  {label}: {new_items} new items")
            total_new += new_items

        time.sleep(0.5)

    save_seen(seen)
    log(f"Done: {total_new} new items, {total_ingested} chunks ingested")

    if total_new > 0:
        nova_config.post_both(
            f":globe_with_meridians: *OSINT/Gov RSS Ingest* — {total_new} new items across {len(FEEDS)} feeds, "
            f"{total_ingested} chunks ingested",
            slack_channel=nova_config.SLACK_NOTIFY
        )


if __name__ == "__main__":
    run()
