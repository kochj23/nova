#!/usr/bin/env python3
"""
nova_gov_rss_ingest.py — Ingest government, OSINT, & interest RSS/Atom feeds into Nova's vector memory.

Covers: US Gov (FBI, GovInfo, CDC, EAC, Space Force, USAF), NATO partners (UK NCSC, UK Legislation,
European Parliament, French Senate/Assembly, Canadian Supreme Court, Norwegian Parliament),
OSINT Cyber Threat Intelligence (50+ vendor/research/journalism feeds),
Homeland Security (DHS, Just Security, Cipher Brief, INSA, etc),
Military (War Zone, Military Times, Task & Purpose, DefenseScoop, Oryx, etc),
Astronomy (NASA, ESO, Planetary Society, Space.com, EarthSky, etc),
Paranormal (Phantoms & Monsters, Anomalien, Fortean, Ghost Theory, etc),
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

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
    ("https://api.weather.gov/alerts/active.atom?area=CA", "infrastructure", "NWS California Alerts"),
    # US Army Corps of Engineers, LA District (Site=429). Akamai blocks the HTML
    # pages but NOT these RSS.ashx endpoints (plain UA gets 200). Covers SoCal
    # flood-control dams, levees, the LA River, dredging, emergency operations.
    ("https://www.spl.usace.army.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=429&max=20", "la_public_safety", "USACE LA District News"),
    ("https://www.spl.usace.army.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=429&max=20", "la_public_safety", "USACE LA District Releases"),

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
    ("https://nvd.nist.gov/feeds/xml/cve/misc/nvd-rss-analyzed.xml", "intelligence", "NIST NVD Analyzed"),
    ("https://packetstormsecurity.com/feeds/", "intelligence", "Packet Storm"),
    # Tier 6: Vendor Threat Research (expansion)
    ("https://nakedsecurity.sophos.com/feed/", "intelligence", "Sophos Naked Security"),
    ("https://securelist.com/feed/", "intelligence", "Kaspersky Securelist"),
    ("https://www.proofpoint.com/us/blog/rss.xml", "intelligence", "Proofpoint"),
    ("https://feeds.fortinet.com/fortinet/blog/threat-research", "intelligence", "Fortinet FortiGuard"),
    ("https://www.trendmicro.com/en_us/research.rss.html", "intelligence", "Trend Micro Research"),
    ("https://www.akamai.com/blog/security/feed", "intelligence", "Akamai Security"),
    ("https://blog.cloudflare.com/tag/security/rss/", "intelligence", "Cloudflare Security"),
    # Tier 7: Journalism / Podcast / Policy
    ("https://risky.biz/feeds/risky-business/", "intelligence", "Risky Business"),
    ("https://cyberscoop.com/feed/", "intelligence", "CyberScoop"),
    ("https://cert.europa.eu/publications/security-advisories/rss", "intelligence", "CERT-EU"),
    # Tier 8: Frameworks / Research
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

    # ══════════════════════════════════════════════════════════════
    # HOMELAND SECURITY (Feedspot homeland_security_rss_feeds)
    # ══════════════════════════════════════════════════════════════
    ("http://feeds.feedburner.com/dhs/zOAi", "intelligence", "DHS News"),
    ("https://www.justsecurity.org/feed/", "intelligence", "Just Security"),
    ("https://www.homelandsecuritynewswire.com/rss.xml", "intelligence", "Homeland Security News Wire"),
    ("https://homelandprepnews.com/feed/", "intelligence", "Homeland Preparedness News"),
    ("https://thehill.com/taxonomy/term/33007/feed/", "politics", "The Hill National Security"),
    ("https://www.theguardian.com/us-news/us-national-security/rss", "intelligence", "Guardian US National Security"),
    ("https://securitydebrief.com/feed/", "intelligence", "Security Debrief"),
    ("https://www.nationalreview.com/national-security-defense/feed/", "politics", "National Review NatSec"),
    ("https://wtop.com/national-security/feed/", "intelligence", "WTOP National Security"),
    ("https://www.hstoday.us/feed/", "intelligence", "Homeland Security Today"),
    ("https://www.longwarjournal.org/feed", "military_history", "Long War Journal"),
    ("https://www.thecipherbrief.com/feed", "intelligence", "The Cipher Brief"),
    ("https://intelnews.org/feed/", "intelligence", "intelNews"),
    ("https://www.hsaj.org/feed", "intelligence", "Homeland Security Affairs Journal"),
    ("https://www.coehsem.com/feed/", "intelligence", "CoE Homeland Security EM"),
    ("https://www.allsides.com/taxonomy/term/6928/feed", "politics", "AllSides National Security"),
    ("https://www.hsdl.org/c/feed/", "intelligence", "Homeland Security Digital Library"),
    ("https://www.insaonline.org/feed", "intelligence", "INSA Intelligence"),
    ("https://tcths.sanford.duke.edu/feed/", "intelligence", "Triangle Ctr Terrorism & HS"),

    # ══════════════════════════════════════════════════════════════
    # CYBERSECURITY EXPANSION (Feedspot cyber_security_rss_feeds)
    # ══════════════════════════════════════════════════════════════
    # Journalism & Research
    ("https://www.csoonline.com/feed/", "intelligence", "CSO Online"),
    ("https://www.theregister.com/security/headlines.atom", "intelligence", "The Register Security"),
    ("https://www.nist.gov/blogs/cybersecurity-insights/rss.xml", "intelligence", "NIST Cybersecurity Blog"),
    ("https://www.cyber.gov.au/rss/news", "intelligence", "Australian Cyber Security Centre"),
    ("https://www.cyber.gc.ca/api/cccs/rss/v1/get?feed=alerts_advisories&lang=en", "intelligence", "Canadian Cyber Centre"),
    # Vulnerability Research
    ("https://googleprojectzero.blogspot.com/feeds/posts/default", "intelligence", "Google Project Zero"),
    ("https://www.thezdi.com/blog?format=rss", "intelligence", "Zero Day Initiative"),
    ("https://portswigger.net/research/rss", "intelligence", "PortSwigger Research"),
    ("https://feeds.feedburner.com/tenable/qaXL", "intelligence", "Tenable Blog"),
    # Cloud & AppSec
    ("https://aws.amazon.com/blogs/security/feed/", "intelligence", "AWS Security Blog"),
    ("https://www.wiz.io/feed/rss.xml", "intelligence", "Wiz Cloud Security"),
    ("https://www.trustedsec.com/feed.rss", "intelligence", "TrustedSec"),
    ("https://snyk.io/blog/feed/", "intelligence", "Snyk Security"),
    ("https://www.invicti.com/blog/feed/", "intelligence", "Invicti Web Security"),
    ("https://www.imperva.com/blog/feed/", "intelligence", "Imperva"),
    ("https://www.netskope.com/feed", "intelligence", "Netskope"),
    # ICS / OT Security
    ("https://industrialcyber.co/feed/", "intelligence", "Industrial Cyber"),
    # Individual Researchers
    ("https://www.malwaretech.com/feed", "intelligence", "MalwareTech"),
    ("https://blog.didierstevens.com/feed/", "intelligence", "Didier Stevens"),
    ("https://www.hexacorn.com/blog/feed/", "intelligence", "Hexacorn"),
    ("https://doublepulsar.com/feed", "intelligence", "DoublePulsar"),
    ("https://www.troyhunt.com/rss/", "intelligence", "Troy Hunt"),
    ("https://scotthelme.co.uk/rss/", "intelligence", "Scott Helme"),
    ("https://danielmiessler.com/feed.rss", "intelligence", "Daniel Miessler"),
    ("https://rss.beehiiv.com/feeds/xgTKUmMmUm.xml", "intelligence", "tl;dr sec"),
    # Vendor Blogs (additional)
    ("http://feeds.feedburner.com/GoogleOnlineSecurityBlog", "intelligence", "Google Online Security"),
    ("https://cisoseries.com/feed/", "intelligence", "CISO Series"),
    ("https://www.tripwire.com/state-of-security/feed/", "intelligence", "Tripwire State of Security"),
    ("https://heimdalsecurity.com/blog/feed/", "intelligence", "Heimdal Security"),
    ("https://socprime.com/feed/", "intelligence", "SOC Prime"),
    ("https://www.upguard.com/blog/rss.xml", "intelligence", "UpGuard"),
    ("https://www.lastwatchdog.com/feed/", "intelligence", "The Last Watchdog"),
    ("https://www.hackthebox.com/rss/blog/all", "intelligence", "Hack The Box"),

    # ══════════════════════════════════════════════════════════════
    # MILITARY (Feedspot military_rss_feeds)
    # ══════════════════════════════════════════════════════════════
    ("https://militarywatchmagazine.com/feed/headlines.rss", "military_history", "Military Watch Magazine"),
    ("https://defence-blog.com/feed/", "military_history", "Defence Blog"),
    ("https://www.gov.uk/government/organisations/ministry-of-defence.atom", "military_history", "UK Ministry of Defence"),
    ("https://www.navy.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1067&max=10", "military_history", "US Navy"),
    ("https://taskandpurpose.com/feed/", "military_history", "Task & Purpose"),
    ("https://www.army-technology.com/feed/", "military_history", "Army Technology"),
    ("https://bulgarianmilitary.com/feed/", "military_history", "BulgarianMilitary"),
    ("https://www.dodreads.com/feed/", "military_history", "DODReads"),
    ("https://feeds.feedburner.com/strategy_bridge", "military_history", "The Bridge (Strategy)"),
    ("https://soldiersystems.net/feed/", "military_history", "Soldier Systems"),
    ("https://defensescoop.com/feed/", "military_history", "DefenseScoop"),
    ("https://www.twz.com/feed", "military_history", "The War Zone"),
    ("https://www.oryxspioenkop.com/feeds/posts/default", "military_history", "Oryx OSINT"),
    ("https://militaryleak.com/feed/", "military_history", "MilitaryLeak"),
    ("https://theaviationist.com/feed/", "military_history", "The Aviationist"),
    ("https://thewarhorse.org/feed/", "military_history", "The War Horse"),
    ("https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?max=10&ContentType=1&Site=945", "military_history", "DoDLive"),
    ("https://www.militarytimes.com/rss/", "military_history", "Military Times"),
    ("https://feeds.feedburner.com/InformationDissemination", "military_history", "Information Dissemination"),
    ("https://www.centcom.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=808&max=20/feed", "military_history", "US Central Command"),
    # Defence IQ (defenceiq.com)
    ("https://www.defenceiq.com/rss/editorials", "military_history", "Defence IQ Editorial"),
    ("https://www.defenceiq.com/rss/news-trends", "military_history", "Defence IQ News & Trends"),
    ("https://www.defenceiq.com/rss/categories/air-defence", "military_history", "Defence IQ Air Defence"),
    ("https://www.defenceiq.com/rss/categories/armoured-vehicles", "military_history", "Defence IQ Armoured Vehicles"),
    ("https://www.defenceiq.com/rss/categories/autonomous-uncrewed", "military_history", "Defence IQ Autonomous/UAS"),
    ("https://www.defenceiq.com/rss/categories/combat-air", "military_history", "Defence IQ Combat Air"),
    ("https://www.defenceiq.com/rss/categories/isr", "intelligence", "Defence IQ ISR"),
    ("https://www.defenceiq.com/rss/categories/naval-maritime-defence", "military_history", "Defence IQ Maritime"),
    ("https://www.defenceiq.com/rss/categories/indirect-fires", "military_history", "Defence IQ Indirect Fires"),
    # US Air Force (af.mil standard DNN RSS endpoints)
    ("https://www.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=715&max=10", "military_history", "US Air Force News"),
    ("https://www.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=715&Category=10903&max=10", "military_history", "USAF Press Releases"),
    ("https://www.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=715&Category=10901&max=10", "military_history", "USAF Commentaries"),
    ("https://www.amc.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=142&max=10", "military_history", "Air Mobility Command"),
    ("https://www.acc.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=119&max=10", "military_history", "Air Combat Command"),
    ("https://www.pacaf.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=230&max=10", "military_history", "Pacific Air Forces"),
    ("https://www.usafe.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=266&max=10", "military_history", "US Air Forces Europe"),
    ("https://www.afsoc.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=137&max=10", "military_history", "Air Force Special Ops"),
    ("https://www.afgsc.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=126&max=10", "military_history", "AF Global Strike Command"),

    # ══════════════════════════════════════════════════════════════
    # UKRAINE WAR (Pro-Ukrainian sources)
    # ══════════════════════════════════════════════════════════════
    ("https://www.pravda.com.ua/eng/rss/", "geopolitics", "Ukrainska Pravda (English)"),
    ("https://www.ukrinform.net/rss/block-lastnews", "geopolitics", "Ukrinform (State News Agency)"),
    ("https://english.nv.ua/rss/all.xml", "geopolitics", "NV (New Voice of Ukraine)"),
    ("https://euromaidanpress.com/feed/", "geopolitics", "Euromaidan Press"),
    ("https://www.atlanticcouncil.org/category/blogs/ukrainealert/feed/", "geopolitics", "Atlantic Council UkraineAlert"),
    ("https://cepa.org/feed/", "geopolitics", "CEPA (European Policy Analysis)"),
    ("http://theins.press/en/feed", "geopolitics", "The Insider (Anti-Kremlin Investigative)"),
    ("https://news.yahoo.com/rss/ukraine", "geopolitics", "Yahoo News Ukraine Aggregator"),
    ("https://www.ukrainianworldcongress.org/feed/", "geopolitics", "Ukrainian World Congress"),
    ("https://militarnyi.com/en/feed/", "geopolitics", "Militarnyi (Ukrainian Defense News)"),

    # ══════════════════════════════════════════════════════════════
    # ASTRONOMY (Feedspot astronomy_rss_feeds)
    # ══════════════════════════════════════════════════════════════
    ("https://www.space.com/feeds.xml", "computing", "Space.com"),
    ("https://skyandtelescope.org/astronomy-blogs/feed/", "computing", "Sky & Telescope"),
    ("https://public.nrao.edu/feed/", "computing", "NRAO"),
    ("https://www.astronomy.com/feed/", "computing", "Astronomy Magazine"),
    ("https://earthsky.org/feed/", "computing", "EarthSky"),
    ("https://www.nasa.gov/feed/", "computing", "NASA All"),
    ("https://www.planetary.org/rss/articles", "computing", "The Planetary Society"),
    ("https://www.spacedaily.com/spacedaily.xml", "computing", "SpaceDaily"),
    ("https://www.thespacereview.com/articles.xml", "computing", "The Space Review"),
    ("https://astrobites.org/feed/", "computing", "Astrobites"),
    ("https://feeds.feedburner.com/EsoTopNews", "computing", "European Southern Observatory"),
    ("https://dailygalaxy.com/category/astronomy/feed/", "computing", "The Daily Galaxy Astronomy"),
    ("https://astronomynow.com/feed/", "computing", "Astronomy Now"),
    ("https://www.sciencenews.org/topic/astronomy/feed", "computing", "Science News Astronomy"),
    ("https://www.aura-astronomy.org/feed/", "computing", "AURA Astronomy"),
    ("https://rss.beehiiv.com/feeds/t0Uscv6JDz.xml", "computing", "Bad Astronomy Newsletter"),
    ("https://nautil.us/topics/astronomy/feed/", "computing", "Nautilus Astronomy"),
    ("https://feeds.fireside.fm/universetoday/rss", "computing", "Universe Today"),
    ("https://feeds.libsyn.com/18112/rss", "computing", "Astronomy Cast Podcast"),
    ("https://sky-lights.org/feed/", "computing", "Sky Lights"),
    ("https://hirise.lpl.arizona.edu/togo/rss.php", "computing", "HiRISE Mars Images"),

    # ══════════════════════════════════════════════════════════════
    # PARANORMAL (Feedspot paranormal_rss_feeds)
    # ══════════════════════════════════════════════════════════════
    ("https://www.higgypop.com/feed.xml", "mystery", "Higgypop Paranormal"),
    ("https://www.ghosthuntingtheories.com/feeds/posts/default", "mystery", "Ghost Hunting Theories"),
    ("https://www.ghosttheory.com/feed", "mystery", "Ghost Theory"),
    ("https://anomalien.com/feed/", "mystery", "Anomalien"),
    ("https://www.singularfortean.com/news?format=rss", "mystery", "Singular Fortean Society"),
    ("https://paranormalhauntings.blog/feed/", "mystery", "Paranormal Hauntings"),
    ("https://paranormalschool.com/feed/", "mystery", "Paranormal School"),
    ("https://www.hauntjaunts.net/feed/", "mystery", "Haunt Jaunts"),
    ("https://phantomsandmonsters.com/rss.xml", "mystery", "Phantoms and Monsters"),
    ("https://www.unexplained.co/feed", "mystery", "Unexplained"),
    ("https://paranormaldailynews.com/feed/", "mystery", "Paranormal Daily News"),
    ("https://www.freaklore.com/feed", "mystery", "Freak Lore"),
    ("https://supernaturaltravel.com/feed/", "mystery", "Supernatural Travel"),
    ("https://www.myhauntedlifetoo.com/feed/", "mystery", "My Haunted Life Too"),
    ("https://usghostadventures.com/blog/feed/", "mystery", "US Ghost Adventures"),
    ("https://hauntedmontreal.com/feed", "mystery", "Haunted Montreal"),
    ("https://www.literaryescapism.com/feed", "mystery", "Literary Escapism"),
    ("https://feeds.simplecast.com/ftB6Gihc", "mystery", "Oh No Ross and Carrie Podcast"),

    # ══════════════════════════════════════════════════════════════
    # LOCAL — BURBANK, CA
    # ══════════════════════════════════════════════════════════════
    ("https://www.myburbank.com/feed/", "local_burbank", "myBurbank News"),
    ("https://bfrb.net/feed/", "local_burbank", "Burbank First Responders Blog"),
    ("https://www.burbankleader.com/arcio/rss/", "local_burbank", "Burbank Leader"),
    ("https://www.burbankca.gov/rss", "local_burbank", "City of Burbank News"),
    ("https://patch.com/california/burbank/feed", "local_burbank", "Patch Burbank"),
    # Glassell Park (NELA, just down the road from Burbank). No dedicated GP feed
    # exists; The Eastsider's keyword-search RSS is the best coverage of the area.
    ("https://www.theeastsiderla.com/search/?f=rss&t=article&l=40&s=start_time&sd=desc&q=glassell+park", "local_burbank", "The Eastsider — Glassell Park"),
    ("https://www.latimes.com/california/rss2.0.xml", "local_news", "LA Times California"),
    ("https://laist.com/feed", "local_news", "LAist"),
    ("https://abc7.com/feed/", "local_news", "ABC7 Los Angeles"),

    # ══════════════════════════════════════════════════════════════
    # HOME AUTOMATION / SMART HOME
    # ══════════════════════════════════════════════════════════════
    ("https://www.home-assistant.io/atom.xml", "home_automation", "Home Assistant Blog"),
    ("https://github.com/home-assistant/core/releases.atom", "home_automation", "HA Core Releases"),
    ("https://community.home-assistant.io/latest.rss", "home_automation", "HA Community Latest"),
    ("https://z-wavealliance.org/feed/", "home_automation", "Z-Wave Alliance"),
    ("https://github.com/zwave-js/node-zwave-js/releases.atom", "home_automation", "Z-Wave JS Releases"),
    ("https://github.com/Koenkk/zigbee2mqtt/releases.atom", "home_automation", "Zigbee2MQTT Releases"),
    ("https://www.theverge.com/rss/smart-home/index.xml", "home_automation", "Verge Smart Home"),
    ("https://matter-smarthome.de/en/feed/", "home_automation", "Matter Smart Home"),
    ("https://thehookup.home.blog/feed/", "home_automation", "The Hookup"),
    ("https://mostlychris.com/feed/", "home_automation", "MostlyChris"),
    ("https://blog.hubitat.com/rss/", "home_automation", "Hubitat Blog"),
    ("https://smarthomesolver.com/reviews/feed/", "home_automation", "SmartHomeSolver"),
    ("https://developer.apple.com/news/releases/rss/releases.rss", "home_automation", "Apple Developer Releases"),

    # ══════════════════════════════════════════════════════════════
    # TECH / SRE / DEVOPS
    # ══════════════════════════════════════════════════════════════
    ("https://arstechnica.com/feed/", "computing", "Ars Technica"),
    ("https://lwn.net/headlines/rss", "computing", "LWN.net"),
    ("https://lobste.rs/rss", "computing", "Lobste.rs"),
    ("https://www.brendangregg.com/blog/rss.xml", "computing", "Brendan Gregg"),
    ("https://jvns.ca/atom.xml", "computing", "Julia Evans"),
    ("https://sreweekly.com/feed/", "computing", "SRE Weekly"),
    ("https://newsletter.pragmaticengineer.com/feed", "computing", "Pragmatic Engineer"),

    # ══════════════════════════════════════════════════════════════
    # AI / ML
    # ══════════════════════════════════════════════════════════════
    ("https://huggingface.co/blog/feed.xml", "computing", "Hugging Face Blog"),
    ("https://simonwillison.net/atom/everything/", "computing", "Simon Willison"),
    ("https://paperswithcode.com/latest.rss", "computing", "Papers With Code"),
    ("https://github.com/ml-explore/mlx/releases.atom", "computing", "MLX Releases"),
    ("https://openai.com/blog/rss.xml", "computing", "OpenAI Blog"),

    # ══════════════════════════════════════════════════════════════
    # APPLE / macOS
    # ══════════════════════════════════════════════════════════════
    ("https://www.macrumors.com/macrumors.xml", "computing", "MacRumors"),
    ("https://9to5mac.com/feed/", "computing", "9to5Mac"),
    ("https://eclecticlight.co/feed/", "computing", "Howard Oakley"),
    ("https://mrmacintosh.com/feed/", "computing", "Mr. Macintosh"),

    # ══════════════════════════════════════════════════════════════
    # NETWORKING
    # ══════════════════════════════════════════════════════════════
    ("https://blog.apnic.net/feed/", "infrastructure", "APNIC Blog"),
    ("https://labs.ripe.net/rss/", "infrastructure", "RIPE Labs"),

    # ══════════════════════════════════════════════════════════════
    # HOROLOGY
    # ══════════════════════════════════════════════════════════════
    ("https://www.hodinkee.com/articles.rss", "horology", "Hodinkee"),
    ("https://wornandwound.com/feed/", "horology", "Worn & Wound"),
    ("https://www.fratellowatches.com/feed/", "horology", "Fratello"),
    ("https://timeandtidewatches.com/feed/", "horology", "Time+Tide"),
    ("https://revolutionwatch.com/feed/", "horology", "Revolution Watch"),
    ("https://monochrome-watches.com/feed/", "horology", "Monochrome Watches"),

    # ══════════════════════════════════════════════════════════════
    # SCIENCE (GENERAL)
    # ══════════════════════════════════════════════════════════════
    ("https://www.quantamagazine.org/feed/", "science", "Quanta Magazine"),
    ("https://arstechnica.com/science/feed/", "science", "Ars Science"),

    # ══════════════════════════════════════════════════════════════
    # ECONOMICS (DEEPER)
    # ══════════════════════════════════════════════════════════════
    ("https://fredblog.stlouisfed.org/feed/", "economics", "FRED Blog"),

    # ══════════════════════════════════════════════════════════════
    # PRIVACY / DIGITAL RIGHTS
    # ══════════════════════════════════════════════════════════════
    ("https://www.eff.org/rss/updates.xml", "intelligence", "EFF Deeplinks"),
    ("https://www.techdirt.com/feed/", "politics", "Techdirt"),

    # ══════════════════════════════════════════════════════════════
    # CALIFORNIA FIRE / WEATHER
    # ══════════════════════════════════════════════════════════════
    ("https://calfire.blogspot.com/feeds/posts/default", "infrastructure", "Cal Fire Blog"),
    ("https://api.weather.gov/alerts/active.atom?point=34.18,-118.31", "infrastructure", "NWS Burbank Alerts"),
    ("https://weatherwest.com/feed/", "infrastructure", "Weather West (Daniel Swain)"),

    # ── LA County Public Safety (Burbank/Glendale/Pasadena) ──
    ("https://feeds.feedburner.com/calfire", "la_public_safety", "CAL FIRE Incidents"),
    ("https://www.lafd.org/rss.xml", "la_public_safety", "LAFD News"),
    ("https://fire.lacounty.gov/feed/", "la_public_safety", "LA County Fire"),
    # ponytail: removed InciWeb (NATIONAL fire feed — leaked Utah/NM fires to /local/) and the two USGS feeds
    # (GLOBAL/national quakes — Venezuela, NorCal). CAL FIRE + LA County Fire cover local fires; LA news RSS
    # covers any newsworthy local quake. Re-add a geo-bounded USGS fdsnws query (lat/lon+maxradiuskm) only if a
    # dedicated local seismic feed is wanted — needs a dynamic starttime, so it'd be a small helper, not a static feed.
    ("https://api.weather.gov/alerts/active.atom?zone=CAC037", "la_public_safety", "NWS LA County Alerts"),
    ("https://dpw.lacounty.gov/adm/tools/rssFeed/feed2.aspx?xsltid=10&i=443", "la_public_safety", "LA County Public Works Road Closures"),
    ("https://www.lapdonline.org/newsroom/feed/", "la_public_safety", "LAPD Newsroom"),
    ("https://lasd.org/feed/", "la_public_safety", "LA County Sheriff"),
    ("https://ready.lacounty.gov/feed/", "la_public_safety", "Ready LA County"),
    ("https://lacounty.gov/feed/", "la_public_safety", "County of LA News"),
    ("https://www.cityofpasadena.net/public-health/feed/", "la_public_safety", "Pasadena Public Health"),
    ("https://tools.cdc.gov/api/v2/resources/media/132608.rss", "medicine", "CDC Newsroom"),
    ("https://www.cdc.gov/mmwr/rss/rss.html", "medicine", "CDC MMWR"),
    ("https://myburbank.com/feed/", "la_public_safety", "myBurbank"),
    ("https://myglendale.com/feed/", "la_public_safety", "MyGlendale"),
    ("https://www.crescentavalleyweekly.com/feed/", "la_public_safety", "Crescenta Valley Weekly"),
    ("https://www.cityofpasadena.net/feed/", "la_public_safety", "City of Pasadena"),
    ("https://pasadenanow.com/feed/", "la_public_safety", "Pasadena Now"),
    ("https://pasadenanow.com/category/crime/feed/", "la_public_safety", "Pasadena Now Crime/Fire/Courts"),
    ("https://laist.com/index.rss", "la_public_safety", "LAist"),
    ("https://www.latimes.com/california/rss2.0.xml", "la_public_safety", "LA Times California"),
    ("https://www.latimes.com/local/lanow/rss2.0.xml", "la_public_safety", "LA Times LA Now"),
    ("https://ktla.com/news/local-news/feed/", "la_public_safety", "KTLA Local News"),
    ("https://abc7.com/feed/", "la_public_safety", "ABC7 Los Angeles"),
    ("https://www.nbclosangeles.com/news/local/?rss=y", "la_public_safety", "NBC LA Local"),
    ("https://www.foxla.com/rss/category/news", "la_public_safety", "FOX 11 News"),
    ("https://mynewsla.com/feed/", "la_public_safety", "MyNewsLA (City News Service)"),
    # SoCal military (verified 2026-06-20)
    ("https://www.losangeles.spaceforce.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=122&max=20", "la_public_safety", "LA Space Force Base"),
    ("https://www.ssc.spaceforce.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1205&max=20", "la_public_safety", "Space Systems Command"),
    ("https://www.vandenberg.spaceforce.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=120&max=20", "la_public_safety", "Vandenberg SFB"),
    ("https://www.edwards.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=151&max=20", "la_public_safety", "Edwards AFB"),
    ("https://www.dvidshub.net/rss/unit/175", "la_public_safety", "Fort Irwin NTC"),
    ("https://www.dvidshub.net/rss/unit/1210", "la_public_safety", "Naval Base Ventura County"),
    ("https://www.dvidshub.net/rss/unit/366", "la_public_safety", "California National Guard"),

    # ── LA traffic conditions ──
    # Verified 2026-06-20: HTTP 200 + valid RSS 2.0 + >=1 <item>.
    # NOTE: CHP live LA-region incident data at https://media.chp.ca.gov/sa_xml/sa.xml is LIVE
    # but a CUSTOM XML schema (State>Center>Dispatch>Log), NOT RSS — needs a custom parser, not added here.
    # Caltrans District 7 (dot.ca.gov/news/rss and ?_format=rss) returns an HTML "Content Not Available"
    # page (no <item>/<entry>) — no working D7 RSS found.
    ("https://ktla.com/traffic/feed/", "la_public_safety", "KTLA Traffic"),
    ("https://www.foxla.com/rss/category/traffic", "la_public_safety", "FOX 11 LA Traffic"),
    ("https://thesource.metro.net/feed/", "la_public_safety", "Metro The Source (Transit/Service Updates)"),

    # ── LA CIVIC / GOVERNMENT — city council, planning, commissions (Granicus + WP) ──
    # Verified 2026-06-20: HTTP 200 + valid RSS 2.0 + >=100 <item> (Granicus agenda/video feeds).
    # Granicus URL pattern: https://<city>.granicus.com/ViewPublisherRSS.php?view_id=<N>&mode=agendas
    # NOTE: Burbank/Glendale CivicPlus city sites (burbankca.gov / glendaleca.gov) are Cloudflare/Akamai
    # walled (hard 403, UA-independent) — NO usable RSS; Granicus covers their agenda/meeting data instead.
    # NOTE: Legistar (burbank/glendale/pasadena.legistar.com) Feed.ashx returns "Invalid feed" placeholder —
    # working RSS needs per-body numeric IDs not publicly enumerable; treat as iCal/scrape-only.
    ("https://burbank.granicus.com/ViewPublisherRSS.php?view_id=29&mode=agendas", "la_public_safety", "Burbank City Council Agendas"),
    ("https://burbank.granicus.com/ViewPublisherRSS.php?view_id=30&mode=agendas", "la_public_safety", "Burbank Planning Board Agendas"),
    ("https://glendale.granicus.com/ViewPublisherRSS.php?view_id=12&mode=agendas", "la_public_safety", "Glendale City Council Agendas"),
    ("https://glendale.granicus.com/ViewPublisherRSS.php?view_id=27&mode=agendas", "la_public_safety", "Glendale Planning Commission Agendas"),
    ("https://pasadena.granicus.com/ViewPublisherRSS.php?view_id=25&mode=agendas", "la_public_safety", "Pasadena City Council Agendas"),
    ("https://pasadena.granicus.com/ViewPublisherRSS.php?view_id=32&mode=agendas", "la_public_safety", "Pasadena Commissions Agendas"),
    ("https://www.cityofpasadena.net/city-manager/feed/", "la_public_safety", "Pasadena City Manager (Weekly Newsletter)"),

    # ── LA LOCAL YOUTUBE — news/community video (Atom) ──
    # Verified 2026-06-20: HTTP 200 + 15 <entry>. Pattern: youtube.com/feeds/videos.xml?channel_id=UC...
    # channel_ids discovered from each channel's page "externalId":"UC...".
    # Glendale GTV6 / Pasadena KPAS had no resolvable UC id (gov video covered via Granicus above).
    # ponytail: removed the 4 TV-station YouTube channel feeds — they publish the stations' WORLD/national
    # uploads (Venezuela quakes, Colorado watch parties), not local segments. Their LOCAL RSS feeds (KTLA Local
    # News, ABC7, NBC LA Local, FOX 11) are already in la_public_safety above and stay.

    # ── LA AIR QUALITY ──
    # Verified 2026-06-20: HTTP 200 + 580 <item>. SCAQMD advisories incl. live smoke/particle advisories.
    # NOTE: AirNow has no static RSS for LA basin (EnviroFlash needs a per-area numeric ID; non-sequential,
    # must be pulled from AirNow's area picker) — supplemental real-time AQI = custom collector.
    ("https://www.aqmd.gov/Custom/RSS/LatestUpdates.aspx", "la_public_safety", "South Coast AQMD Advisories"),

    # ── LA EVENTS / COMMUNITY / CHAMBERS ──
    # Verified 2026-06-20: HTTP 200 + 10 <item> each (WordPress / chamber CMS).
    # NOTE: Burbank/Glendale city events + libraries (Communico/BiblioCommons) are edge-walled or iCal/JSON
    # only — no usable RSS; need custom collectors. Pasadena (cityofpasadena.net WordPress) is the exception.
    ("https://www.cityofpasadena.net/events/feed/", "la_public_safety", "City of Pasadena Events"),
    ("https://www.cityofpasadena.net/library/feed/", "la_public_safety", "Pasadena Public Library"),
    ("https://www.pasadena-chamber.org/rss.xml", "la_public_safety", "Pasadena Chamber of Commerce"),
    ("https://www.glendalechamber.com/feed", "la_public_safety", "Glendale Chamber of Commerce"),

    # ── LA CRIME / BLOTTER (beyond LAPD/LASD newsrooms) ──
    # Verified 2026-06-20: HTTP 200 + 10 <item>. Pasadena PD (cityofpasadena.net WordPress).
    # NOTE: Burbank PD (Liferay+Cloudflare) and Glendale PD (CivicEngage+Akamai WAF) have NO RSS — hard 403.
    # CrimeMapping.com = F5-WAF AJAX/map API only; Citizen = paid enterprise SSE/WS API. All need collectors.
    ("https://www.cityofpasadena.net/police/feed/", "la_public_safety", "Pasadena Police Department"),

    # ── LA UTILITIES / EMERGENCY MANAGEMENT ──
    # Verified 2026-06-20: HTTP 200 + valid RSS (10 <item>).
    # NOTE: Burbank Water & Power, Glendale Water & Power, SoCal Edison, SoCalGas have NO RSS — outage data
    # is map/JSON-API only (e.g. outageentry.com for GWP; PowerOutage.us util #765 for SCE) → custom collectors.
    # NOTE: Cal OES news (news.caloes.ca.gov/feed/) is valid RSS but its TLS chain fails urllib cert
    # verification (works in curl, not in this script) — excluded until an SSL fix is added.
    ("https://www.ladwpnews.com/feed/", "la_public_safety", "LADWP News"),

    # ── FOOTHILL / LA CRESCENTA AREA (where Nova's rack lives) ──
    # Verified 2026-06-20: Foothills Paper HTTP 200 + 10 <item> (Sunland-Tujunga/foothills, incl. fire/burn-area).
    # NWS San Gabriel Valley forecast zone (CAZ548) covers Pasadena/foothills — Atom alert endpoint (entries
    # appear only when alerts are active; intrinsically valid). Crescenta Valley Weekly already covered above.
    # NOTE: MediaNews Group papers (dailynews.com, sgvtribune.com, pasadenastarnews.com) hard-403 all UAs —
    # need a custom collector. Patch.com California feeds now 404 (platform changed).
    ("https://www.thefoothillspaper.com/feed/", "la_public_safety", "The Foothills Paper (Sunland-Tujunga)"),
    ("https://api.weather.gov/alerts/active.atom?zone=CAZ548", "la_public_safety", "NWS San Gabriel Valley/Foothills Alerts"),
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
    # Keep last 25000 entries (expanded for 400+ feeds)
    STATE_FILE.write_text(json.dumps(list(seen)[-25000:]))


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
        with urllib.request.urlopen(req, timeout=12) as resp:
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

    # ── Phase 1: Parallel fetch (network-bound, thread-safe per fetch_feed) ──
    # fetch_feed has its own per-feed try/except + 12s timeout, so one slow or
    # dead feed can never block the rest. Collect (vector, label, items) tuples.
    t_fetch = time.time()
    results = []  # list of (vector, label, items)
    with ThreadPoolExecutor(max_workers=24) as executor:
        future_map = {
            executor.submit(fetch_feed, feed_url): (vector, label)
            for feed_url, vector, label in FEEDS
        }
        for future in as_completed(future_map):
            vector, label = future_map[future]
            try:
                items = future.result()
            except Exception as e:
                # fetch_feed already swallows errors, but stay defensive.
                log(f"  FETCH FAILED (executor): {label} — {e}")
                items = []
            results.append((vector, label, items))
    log(f"Fetch phase complete in {time.time() - t_fetch:.1f}s "
        f"({len(results)} feeds, {sum(len(r[2]) for r in results)} raw items)")

    # ── Phase 2: Sequential dedup + ingest (seen set / ingest NOT thread-safe) ──
    feed_detail = {}  # label -> list of new item titles (for the per-feed Slack breakdown)
    for vector, label, items in results:
        new_items = 0

        for item in items:
            url_hash = hashlib.md5((item["link"] or item["title"]).encode()).hexdigest()[:12]
            if url_hash in seen:
                continue

            seen.add(url_hash)
            new_items += 1

            title = clean_html(item["title"])
            feed_detail.setdefault(label, []).append(title[:90])
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

    save_seen(seen)
    log(f"Done: {total_new} new items, {total_ingested} chunks ingested")

    if total_new > 0:
        # Per-feed breakdown with item titles — every feed that had new items appears,
        # with up to 3 titles each (so it's "all feeds + detail" without a per-item firehose).
        title = (f"OSINT/Gov RSS Ingest — {total_new} new items "
                 f"across {len(feed_detail)} feeds ({total_ingested} chunks)")
        lines = []
        for label in sorted(feed_detail, key=lambda k: -len(feed_detail[k])):
            titles = feed_detail[label]
            shown = "; ".join(t for t in titles[:3])
            more = f" _+{len(titles) - 3} more_" if len(titles) > 3 else ""
            lines.append(f":small_blue_diamond: *{label}* ({len(titles)}): {shown}{more}")
        body = "\n".join(lines)
        if len(body) > 3500:  # keep it digestible
            body = body[:3500] + "\n…(truncated)"
        notify(title, body=body, level="info", category="ingest",
               dedup_key="gov-rss-ingest")


if __name__ == "__main__":
    run()
