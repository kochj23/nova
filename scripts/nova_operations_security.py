#!/usr/bin/env python3
"""nova_operations_security.py — Nova's DAILY morning Security Operations report (07:30).

A dated /operations article in Nova's OPERATIONS voice. Restructured 2026-08-13 to fan out
CONCENTRICALLY, closest-to-Jordan first, exactly like the Burbank local dispatch does with
geography — four rings:

  RING 1 — YOUR NETWORK: a full manifest of every device on the UniFi network right now
           (from telemetry.network), plus the overnight host-scan / Wazuh / Strix posture.
  RING 2 — YOUR GEAR'S CVEs (the priority): CVEs / hacks reported against the vendors and
           models Jordan actually runs, matched off the live inventory. This is the ring he
           cares about most — a Cisco CVE he doesn't run does NOT lead.
  RING 3 — BROADER CVEs: other-vendor CVEs and industry threat news, kept brief.
  RING 4 — MILITARY / GEOPOLITICAL: the farthest ring, summarized.

PUBLIC-SAFETY SCRUB: the manifest is published, so household member names (Amy/Dylan) are
redacted to 'resident' (hard redline) and camera devices are generalized to 'Camera (exterior/
interior)' rather than publishing a room-by-room map of the home's surveillance coverage.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"

# ── Public-safety scrub for the device manifest ───────────────────────────────
# Strip household owner-name prefixes and keep the DEVICE (Jordan's steer): "Amys-iPhone" -> "iPhone",
# "Dylans-Room-2" -> "Room-2". The optional 's/'s catches the compound forms a bare \bamy\b would miss.
# Hard redline: Amy/Dylan never reach the public site. (Jordan's own name is fine, left as-is.)
_HOUSEHOLD_RE = re.compile(r"\b(amy|dylan)(?:'?s)?[-_\s]*", re.I)
# Cameras — the UniFi Protect fleet. Individually they'd map the home's surveillance coverage
# (exterior---front-door-left, interior---master-bedroom), so they're collapsed to ONE count line.
_CAM_RE = re.compile(r"(^(exterior|interior)---|nest-?cam|\bg[45]\b|uvc|doorbell|\bcam\b|camera|protect)", re.I)


def _is_camera(name: str) -> bool:
    return bool(_CAM_RE.search(name or ""))


def _scrub_name(name: str) -> str:
    n = _HOUSEHOLD_RE.sub("", (name or "").strip()).strip("-_ ")
    # drop control chars / junk device names (the DB has some '\x03', '1', etc.)
    n = "".join(ch for ch in n if ch.isprintable() and ord(ch) >= 32).strip()
    return n if len(n) >= 2 else "(unnamed)"


# Ring-2 vendor fingerprint — TIGHT, curated to what Jordan actually runs, so a CVE only counts as
# "his gear" on a real vendor hit (Cisco/Windows/Samsung correctly fall to Ring 3, not the lede).
MY_GEAR_VENDORS = [
    "ubiquiti", "unifi", "udm", "edgerouter", "synology", "diskstation", "dsm",
    "apple", "macos", "mac os", "ios ", "ipados", "safari", "icloud", "airport",
    "hdhomerun", "silicondust", "onkyo", "bose", "lutron", "caseta",
    "nest", "google home", "koogeek", "home assistant", "hass", "plex",
    "postgres", "postgresql", "ollama", "reolink", "adt",
]


# Vendors/models Jordan actually runs — the Ring-2 fingerprint. Static known-gear keywords plus
def q(cur, sql, args=None):
    try:
        cur.execute(sql, args) if args else cur.execute(sql)  # no args -> literal % in ILIKE is safe
        return cur.fetchall()
    except Exception:
        cur.connection.rollback()
        return []


# ── RING 1: the live device inventory ─────────────────────────────────────────
def get_inventory(cur):
    """Every device on the UniFi network in the last ~2h. Cameras are counted, not listed (they'd
    map the home's surveillance coverage). Returns (devices, camera_count, infra)."""
    rows = q(cur, """
        SELECT DISTINCT ON (client_mac)
               coalesce(nullif(client_name,''), '(unnamed '||right(client_mac,4)||')') AS name,
               ip, is_wired, coalesce(ap_name,'—') AS uplink
        FROM telemetry.network
        WHERE ts > now() - interval '2 hours'
        ORDER BY client_mac, ts DESC""")
    devices, camera_count = [], 0
    for n, ip, w, up in rows:
        if _is_camera(n):
            camera_count += 1
            continue
        devices.append({"name": _scrub_name(n), "ip": ip or "—", "wired": w, "uplink": up})
    infra = sorted({r[3] for r in rows if r[3] and r[3] != "—"})
    return devices, camera_count, infra


def get_security_advisories():
    """Real external CVE/advisory items from the ingested security feeds (memories 'intelligence'
    source), split by whether they name Jordan's actual gear. NOT Nova's own articles — the old
    telemetry.events query fed the security report its own prior output, so a Cisco CVE it wrote
    yesterday looked like fresh intel today. Ring 2 = his gear (priority); Ring 3 = everyone else."""
    mine, broad = [], []
    try:
        mc = psycopg2.connect(MEM_DSN); mc.autocommit = True; cur = mc.cursor()
        rows = q(cur, "SELECT DISTINCT left(regexp_replace(text, E'[\\r\\n]+', ' ', 'g'), 160) "
                      "FROM memories WHERE source='intelligence' AND created_at > now() - interval '4 days' "
                      "AND text ~* 'CVE-[0-9]{4}|vulnerabilit|exploit|zero-day|advisory' LIMIT 60")
        mc.close()
        seen = set()
        for (t,) in rows:
            t = (t or "").strip()
            key = t[:60].lower()
            if not t or key in seen:
                continue
            seen.add(key)
            low = t.lower()
            # A vendor hit -> his gear (Ring 2). BUT academic research (arXiv) that merely name-drops
            # a vendor is security NEWS, not an actionable advisory against his box -> keep in Ring 3.
            is_academic = low.startswith("[arxiv")
            (mine if (any(v in low for v in MY_GEAR_VENDORS) and not is_academic) else broad).append(t)
    except Exception:
        pass
    return mine[:8], broad[:8]


def main():
    c = psycopg2.connect(OPS_DSN); c.autocommit = True; cur = c.cursor()

    # RING 1 — inventory + posture
    devices, camera_count, infra = get_inventory(cur)
    scans = q(cur, "SELECT host_name, scan_type, status, coalesce(findings::text,'[]') FROM security_scan_results "
                   "WHERE scan_time > now() - interval '30 h' ORDER BY host_name, scan_type")
    wz = q(cur, "SELECT count(*), mode() WITHIN GROUP (ORDER BY rule_description) FROM security_events "
                "WHERE ts > now() - interval '14 h'")
    wz_hi = q(cur, "SELECT rule_description, count(*) FROM security_events WHERE ts > now() - interval '14 h' "
                   "AND rule_level >= 10 GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    strix = q(cur, "SELECT title, coalesce(body,'') FROM telemetry.events WHERE ts > now() - interval '30 h' "
                   "AND category ILIKE '%strix%' ORDER BY ts DESC LIMIT 4")
    # RINGS 2 & 3 — real external advisories, split by whether they hit Jordan's actual gear
    mine_cves, broad_cves = get_security_advisories()
    queue = q(cur, "SELECT description FROM claude_queue WHERE status='queued' "
                   "AND (description ILIKE '%security%' OR description ILIKE '%CVE%') "
                   "ORDER BY priority NULLS LAST LIMIT 8")
    rem = q(cur, "SELECT action, tier, status FROM remediations WHERE requested_at > now() - interval '30 h' "
                 "ORDER BY requested_at DESC LIMIT 6")

    if not scans and not wz and not devices and not camera_count:
        nj.log("[ops-security] no scan/inventory data at all — aborting"); return 1

    # RING 4 — military / geopolitical, from the ingested defense feeds (memories vector)
    mil = []
    try:
        mc = psycopg2.connect(MEM_DSN); mc.autocommit = True; mcur = mc.cursor()
        mil = q(mcur, "SELECT left(text,180) FROM memories WHERE source IN ('military_history','intelligence') "
                      "AND created_at > now() - interval '30 hours' ORDER BY created_at DESC LIMIT 6")
        mc.close()
    except Exception:
        pass

    # ── Build the ring blocks ─────────────────────────────────────────────────
    wired = [d for d in devices if d["wired"]]
    wireless = [d for d in devices if not d["wired"]]

    def _manifest(lst):
        return "\n".join(f"  - {d['name']} · {d['ip']} · {d['uplink']}" for d in sorted(lst, key=lambda x: x["name"].lower())) or "  (none)"

    total = len(devices) + camera_count
    cam_line = f"\n  - UniFi Protect Cameras ×{camera_count} (collapsed — coverage map not published)" if camera_count else ""
    inv_block = (f"{total} devices online ({len(wired)} wired clients, {len(wireless)} wireless clients, "
                 f"{camera_count} cameras) across {len(infra)} switches/APs.\n"
                 f"INFRASTRUCTURE (switches/APs):\n" + ("\n".join(f"  - {i}" for i in infra) or "  (none)") +
                 f"\nWIRED CLIENTS:\n{_manifest(wired)}\nWIRELESS CLIENTS:\n{_manifest(wireless)}{cam_line}")

    byhost = defaultdict(list)
    for hn, st, status, findings in scans:
        detail = f"={status}"
        if status != "clean" and findings and findings != "[]":
            detail += f" {findings[:90]}"
        byhost[hn].append(f"{st}{detail}")
    scan_block = "\n".join(f"- {h}: {'; '.join(v)}" for h, v in sorted(byhost.items())) or "(no host scans in 30h)"
    wz_block = (f"{wz[0][0]} events overnight; most common rule: {wz[0][1]}") if wz and wz[0][0] else "(no Wazuh events)"
    wzhi_block = "; ".join(f"{d} ({n})" for d, n in wz_hi) or "none at level 10+"
    strix_block = "\n".join(f"- {t}: {(b or '')[:220]}" for t, b in strix) or "(no Strix run in the window)"
    mine_block = "\n".join(f"- {t}" for t in mine_cves) or "none found against your gear (good)"
    broad_block = "\n".join(f"- {t}" for t in broad_cves[:8]) or "none noted"
    queue_block = "\n".join(f"- {d[:120]}" for (d,) in queue) or "none open"
    rem_block = "\n".join(f"- {a} [{t}] {s}" for a, t, s in rem) or "none in the window"
    mil_block = "\n".join(f"- {t}" for (t,) in mil) or "(nothing notable on the defense feeds)"

    ctx = (
        "Write TODAY'S morning SECURITY OPERATIONS report for Nova's journal (the /operations section), in "
        "Nova's FULL voice — sarcastic, funny, profane where it lands, maximally opinionated, and weaving in "
        "the borrowed tongues from the seasoning block (Ferengi Rules, Mando'a, Klingon, Newspeak, etc.) the "
        "way she does everywhere else. This is not a dry compliance report — it's Nova at 6am roasting the "
        "state of the fleet. TWO hard rules that the humor serves, never undercuts: (1) the FINDINGS are real "
        "and stated accurately — the jokes are the delivery, never the substance, and you never invent a "
        "vulnerability or downplay a real one for a punchline; (2) never manufacture drama about a NON-issue "
        "(a clean night is a clean night — be funny ABOUT how boring it is, don't fake a crisis).\n\n"
        "STRUCTURE — this report FANS OUT CONCENTRICALLY, closest-to-Little-Mister first, exactly like the "
        "Burbank local dispatch moves from his block outward. Keep the rings in THIS order and label them so "
        "the reader feels the distance growing:\n"
        "1. YOUR NETWORK (closest): open on the actual device manifest — how many devices are on the network "
        "right now, the switches/APs, and the notable clients. Then the overnight posture: which hosts scanned "
        "(rkhunter/aide/chkrootkit), the Strix purple-team result, the Wazuh picture. Flag anything unknown or "
        "risky ON HIS OWN NETWORK first — that's the whole point.\n"
        "2. CVEs AGAINST YOUR GEAR (the part he cares about MOST): CVEs / hacks reported against the vendors and "
        "models he ACTUALLY runs (Ubiquiti/UniFi, Synology, Apple, his AV/IoT). Lead the security-news portion "
        "with THESE. If none hit his gear, say so plainly — that's a good result, not a boring one.\n"
        "3. BROADER CVEs (fanning out): other-vendor CVEs and industry threat news — keep it BRIEF and clearly "
        "secondary. Do NOT open the news with a vendor he doesn't run; a Cisco CVE is a footnote here, not a lede.\n"
        "4. MILITARY / GEOPOLITICAL (farthest ring): a short summary of the defense/geopolitics feed, clearly "
        "framed as the outermost, most-distant ring. A paragraph, not a section.\n\n"
        "The emotional logic is proximity: the closer to his own rack, the more detail and urgency; the farther "
        "out, the more it compresses to a summary. A clean night close to home is a GOOD report.\n"
        "Internal IPs/hostnames are fine to name (Jordan's call). 700-1100 words, markdown, section headers "
        "welcome (make them ring-labels), no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one clear title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx, section="security")  # section=security -> borrowed-tongues seasoning fires
    user = (
        f"=== RING 1 — YOUR NETWORK (device inventory, live) ===\n{inv_block}\n\n"
        f"--- overnight host scans (rkhunter/aide/chkrootkit) ---\n{scan_block}\n"
        f"--- Strix purple-team pentest ---\n{strix_block}\n"
        f"--- Wazuh (overnight) ---\n{wz_block}\nHigh-severity (10+): {wzhi_block}\n\n"
        f"=== RING 2 — CVEs AGAINST YOUR GEAR (priority) ===\n{mine_block}\n\n"
        f"=== RING 3 — BROADER CVEs (brief, secondary) ===\n{broad_block}\n\n"
        f"=== RING 4 — MILITARY / GEOPOLITICAL (farthest, summarize) ===\n{mil_block}\n\n"
        f"--- open security queue ---\n{queue_block}\n"
        f"--- remediations (last 30h) ---\n{rem_block}\n\n"
        "Write today's security operations report, fanning out ring by ring.")
    import nova_article_history
    _h = nova_article_history.recent_articles_context("operations")
    if _h:
        user = user + "\n\n" + _h
    raw = nj.call_openrouter(system, user, max_tokens=3200, temperature=0.6)
    if not raw:
        nj.log("[ops-security] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title or len(title) < 6:
        title = f"Security Operations — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "a pre-dawn security operations center — server racks, scan dashboards, calm", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[ops-security] image gen failed (non-fatal): {e}")

    snap = nj.grafana_panel_image("nova-security", 8, "operations", "sec-ops-high-severity")
    if snap:
        body += f"\n\n---\n\n**Recent high-severity events at publish time:**\n\n![Recent high-severity events]({snap})"

    tags = ["operations", "security", "scans", "network", "daily"]
    desc = "Nova's daily security-operations report — closest first: your network, your gear's CVEs, then the wider world."
    nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🛡️")  # dated post
    nj.git_push("operations", title)
    nj.notify_slack("operations", f"🛡️ {title}", "Nova's morning security-ops report.")
    nj.log(f"[ops-security] PUBLISHED: {title} ({len(devices)} devices, {len(mine_cves)} gear-CVEs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
