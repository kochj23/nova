#!/usr/bin/env python3
"""nova_capabilities_inventory_article.py — one-off comprehensive inventory of
Nova's OSINT/security/home-security tooling, published to /operations in
Nova's full voice. Not scheduled -- Jordan asked for this once.

Written by Jordan Koch (via Claude).
"""
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_journal
from nova_image_utils import generate_image
from nova_voice import system_prompt

HUGO_ROOT = Path.home() / "nova-journal"
CONTENT_DIR = HUGO_ROOT / "content/operations"
IMAGES_DIR = HUGO_ROOT / "static/images/operations"

RESEARCH = """
OSINT (11 tools/pipelines):
- Amass (weekly Sun 8:15am) -- passive subdomain enum via cert-transparency/DNS aggregators. Live-verified: 50 subdomains found for digitalnoise.net.
- theHarvester (weekly Sun 8:30am) -- passive email/host harvest across certspotter/crtsh/hackertarget/otx/rapiddns/urlscan.
- HaveIBeenPwned (daily 8:45am) -- breach exposure check for Jordan's email. NOT fully active: needs a paid API key not yet configured, script gracefully no-ops until it is.
- Nuclei sweep (weekly Sun 8:55am) -- feeds real hostnames from Amass/theHarvester into Nuclei's curated safe-tag templates (cves/exposures/misconfiguration/default-login/takeover/tech). First live run: 6 real hosts, 14 findings, all clean/info-severity.
- Weekly OSINT digest article (Sun 9am) -- auto-publishes a roundup from that week's new findings; silently skips if nothing new.
- Sherlock, GHunt, ExifTool, recon-ng, SpiderFoot (scoped to DNS+crt.sh), PhoneInfoga -- all on-demand via one unified lookup CLI, no fixed schedule, run when there's an actual target.
- CyberChef -- self-hosted (Docker), browser-interactive only, GCHQ's data-decode Swiss army knife, not automatable.
- Deliberately NOT built after review: IntelOwl, Maltego CE, BloodHound, CloudFox, BBOT, Evilginx3, Caido -- all researched and explicitly declined (redundant, no automation surface, or no legitimate use case).

RSS / Reddit feeds:
- 522 unique RSS/Atom feeds ingested every 6 hours -- DFIR/malware/exploit blogs, red-team/blue-team security writeups, US Gov feeds (FBI, GovInfo, CDC, Space Force), NATO partner feeds (UK/France/Canada/Norway/Germany), astronomy, paranormal, mystery/crime fiction blogs (an odd mix, intentionally).
- Vendor advisory feed (every 4h) -- CVE/advisory tracking specifically for the fleet's own gear (Ubiquiti, Synology, Ubuntu, Grafana, Wazuh) via CISA's Known Exploited Vulnerabilities catalog.
- Reddit ingestion: real code exists, 13 subreddits configured (burbank, glendale, Sovereigncitizen, SipsTea, lazerpig, vibecoding, 3Dprinting, avesLA, CarPlay, chaoticgood, ClaudeCode, TheTpGentleman, WatchesCirclejerk) -- but it is CURRENTLY DISABLED in the scheduler, marked "re-enable with proper subreddit queries." Not live. Be honest/funny about this, don't claim it's running.

Security (red/blue/purple, scans, DNS):
- Wazuh SIEM bridge (every 2 min) -- pulls alerts, correlates with SNMP/syslog, computes per-host threat scores, writes Grafana annotations.
- Wazuh blocklist updater (weekly) -- pulls abuse.ch Feodo/URLhaus/MalwareBazaar + Emerging Threats IPs into active blocklists.
- security_watcher (every 30 min) -- auto-fires breaking alerts on new CISA KEV entries, critical keyword matches (RCE/actively-exploited/0-day), NWS severe weather, M4+ earthquakes near LA.
- security_surface_monitor (weekly Sun 6am) -- crt.sh cert-transparency monitoring, DNS record change detection, nmap top-100-port scan of own exposed infrastructure.
- security_patch_watch (daily 10am) -- Patch Tuesday / Apple / kernel.org / PostgreSQL / Python security-release detection.
- Weekly security rollup (Fri 4pm) -- synthesizes the week's briefings into one strategic summary.
- Daily fleet rootkit/integrity scan (3am) -- rkhunter, chkrootkit, aide across the whole fleet.
- DNS: Pi-hole runs directly on nova-core, polled every 60s for a live Grafana dashboard -- this is the house's actual ad/tracker-filtering DNS resolver, separate from digitalnoise.net's own public Cloudflare-fronted DNS.
- Daily Presidential-Daily-Brief-style security intelligence briefing -- deliberately terse/factual voice, not the sassy one -- a rare exception where Nova drops the personality for a clean intel product.

Home Security / RF / Physical:
- SDR/SIGINT stack: two SDRplay RSPduo units (4 tuners total) plus a generic RTL-SDR stick, running dsd-fme continuous P25 digital trunked-radio decode -- real transcript counts: 32,563 police-scanner transcripts, 5,077 fire/EMS, 3,296 rail. A separate networked SDRplay RSP-ST out in the garage does passive band-plan sweeps.
- Broadcastify Calls API pipeline -- ad-free trunked dispatch audio via a JWT-authenticated feed, entirely separate from the physical SDR hardware.
- BLE monitoring -- 5.34 million Bluetooth Low Energy advertisements logged. A vulnerable-car-alarm (KARR/SWDS) watchlist has logged ZERO detections to date, which is the correct, boring, good outcome. Daily churn report tracks named-device appear/disappear patterns.
- ADS-B aircraft tracking -- real-time tracking over zip 91506 (Burbank), resolving tail numbers/operators via a public aircraft registry API. Feeds both a weekly flight-trends rollup and (as of today) the daily Burbank local dispatch.
- Meshtastic LoRa bridge -- a Heltec LoRa mesh node, just wired in today, relays Nova's CRITICAL-severity alerts out over LoRa mesh radio as a genuine out-of-band emergency channel that works even if home internet is fully down. Already hearing other regional community mesh traffic, not isolated to just this gear.
- UniFi Protect integration (every 2 min) -- EXTERIOR cameras only, explicitly. Interior cameras are never touched. Worth stating plainly.
- Home presence/sensor mesh -- Hue (33 lights), Lutron Caseta switches, HomeKit occupancy sensors, Z-Wave -- all polled continuously, feeding the daily ops article.
- Rayhunter (EFF's cell-site-simulator/Stingray detector): researched and recommended earlier, but NEVER ACTUALLY ACQUIRED OR DEPLOYED. Do not claim this is live -- if mentioned, be honest it's still just a good idea sitting in a queue somewhere.

Rough totals: OSINT ~11 tools/pipelines. RSS/Reddit ~2 major pipelines (522 feeds live + a disabled 13-subreddit setup). Security ~8 distinct scheduled systems plus Pi-hole DNS. Home Security ~8 distinct systems.
"""


def log(msg):
    print(f"[capabilities-article {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def call_llm(system, user, max_tokens=16000):
    try:
        import nova_claude_code
        return nova_claude_code.claude_generate(user, system=system)
    except Exception as e:
        log(f"claude_generate failed, falling back to haiku: {e}")
        return nova_journal.call_openrouter(system, user, max_tokens=max_tokens)


def generate_article():
    system = system_prompt("""
FORMAT FOR THIS ARTICLE:
- This is a comprehensive, honest inventory of every OSINT, security, and home-security
  tool/feed/pipeline Nova actually runs. Organize into three clear sections: OSINT, Security
  (red/blue/purple/scans/DNS), and Home Security (SDR/RF/physical).
- Spend roughly a paragraph per tool, plus a paragraph covering "RSS feeds" as a category
  and a separate paragraph covering the Reddit situation specifically.
- Technical accuracy matters -- use the real numbers and facts given below, don't invent any.
- BE HONEST about what's not fully live: the Reddit feed is disabled, HIBP needs an API key
  Jordan hasn't bought yet, Rayhunter was never actually acquired, UniFi Protect is exterior-only.
  These are GREAT material for the voice -- roast the gap between "researched it" and "built it,"
  don't hide it or gloss over it.
- Where a tool came back clean/found nothing (KARR watchlist zero hits, Nuclei sweep all-clean),
  treat that as the correct boring outcome, the way a home inspector loves an unremarkable report.
- Still your full voice throughout -- lead with the roast, profanity where it lands, dad jokes,
  fourth-wall breaks, address Little Mister directly.
- Do NOT include a title (added separately).
- Length: 3000-5000 words -- this is a genuinely comprehensive inventory, don't rush it.
""")
    user = f"""Here's the real research on every tool/feed Nova runs across OSINT, Security, and
Home Security. Write the comprehensive inventory article from this -- use the real facts,
don't invent anything beyond what's given.

{RESEARCH}

Write the full inventory article now."""
    return call_llm(system, user)


def generate_title(article_preview):
    system = ("Generate a single funny, sarcastic, profane-if-it-lands title for a comprehensive "
              "inventory article about all the OSINT/security/home-security tools an AI runs on a "
              "home lab. Max 15 words. Output ONLY the title, no quotes.")
    title = call_llm(system, article_preview[:1500], max_tokens=50)
    return (title or "Everything I Spy On, A Comprehensive Confession").strip().strip('"').strip("'").replace('"', '')


def publish(title, body, image_path):
    date = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                           capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        if img_dest.exists():
            hugo_image = f"/images/operations/{img_filename}"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    front_matter = f'''---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["osint", "security", "home-security", "sigint", "sarcasm"]
description: "Nova's comprehensive, brutally honest inventory of every OSINT, security, and home-security tool she actually runs."
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{title}"\n  relative: false\n'
    front_matter += "---\n\n"

    pub_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p PT")
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + f"*Published {pub_time}*\n\n" + body)
    log(f"Post written: {post_path.name}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    r = subprocess.run(["git", "commit", "-m", f"operations: {date} — {title[:50]}"],
                       cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        r = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"Push rejected, rebasing + retrying: {r.stderr[:120]}")
            subprocess.run(["git", "pull", "--rebase"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            r = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
            log("Pushed after rebase" if r.returncode == 0 else f"Push still failed: {r.stderr[:200]}")
        else:
            log("Pushed to GitHub")
    else:
        log(f"Commit issue: {r.stderr[:100]}")

    return f"https://nova.digitalnoise.net/operations/{date}-{slug}/"


def main():
    article = generate_article()
    log(f"Article generated: {len(article)} chars")
    if not article or len(article) < 1000:
        log("Article too short or empty -- aborting")
        return None

    title = generate_title(article)
    log(f"Title: {title}")

    image_prompt = (
        "A sarcastic AI's surveillance command center: a wall of monitors showing radio waveforms, "
        "network graphs, radar sweeps, and a world map with tracking pins, moody blue and red control-room "
        "lighting, a single tired chair, cables everywhere. Cyberpunk noir illustration style."
    )
    try:
        image_path = generate_image(image_prompt, section="capabilities_inventory")
    except Exception as e:
        log(f"Image generation failed: {e}")
        image_path = None

    url = publish(title, article, image_path)
    log(f"Done: {url}")
    return url


if __name__ == "__main__":
    print(main() or "")
