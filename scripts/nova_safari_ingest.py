#!/usr/bin/env python3
"""
nova_safari_ingest.py — Ingest Safari browsing history into Nova's vector memory.

Reads macOS Safari History.db, groups visits by domain and date into
meaningful memory chunks, and stores them via Nova's /remember API.

Features:
  - Groups visits by domain+date for coherent memory chunks
  - Filters tracking pixels, ad domains, redirects, blank titles
  - Checkpoint/resume (survives interruption, safe to re-run)
  - 5-minute Slack status reports to #nova-notifications
  - Idempotent: skips already-ingested visits via checkpoint
  - All secrets from macOS Keychain — nothing hardcoded

Usage:
  python3 nova_safari_ingest.py                    # Ingest last 5 years
  python3 nova_safari_ingest.py --since 2024-01-01 # Custom cutoff
  python3 nova_safari_ingest.py --dry-run          # Count only, don't ingest
  python3 nova_safari_ingest.py --status           # Show progress from checkpoint

Written by Jordan Koch.
"""

import argparse
import json
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ───────────────────────────────────────────────────────────────────

HISTORY_DB = Path.home() / "Library/Safari/History.db"
VECTOR_URL = "http://127.0.0.1:18790/remember?async=1"
SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_NOTIFY  # #nova-notifications
CHECKPOINT_FILE = Path.home() / ".openclaw/workspace/state/safari_ingest_checkpoint.json"
STATUS_INTERVAL = 300  # 5 minutes
MAX_URLS_PER_CHUNK = 30  # Cap URLs in a single memory chunk
MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)  # Mac absolute time epoch

# ── Noise filters ────────────────────────────────────────────────────────────

# Ad/tracking domains to skip entirely
AD_TRACKING_DOMAINS = {
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "google-analytics.com", "googletagmanager.com", "googletagservices.com",
    "facebook.com/tr", "connect.facebook.net", "pixel.facebook.com",
    "analytics.twitter.com", "t.co", "bat.bing.com",
    "ad.doubleclick.net", "adservice.google.com", "ads.linkedin.com",
    "px.ads.linkedin.com", "insight.adsrvr.org", "adsymptotic.com",
    "amazon-adsystem.com", "aax.amazon-adsystem.com",
    "scorecardresearch.com", "sb.scorecardresearch.com",
    "quantserve.com", "pixel.quantserve.com",
    "demdex.net", "dpm.demdex.net", "omtrdc.net",
    "krxd.net", "cdn.krxd.net", "bluekai.com",
    "taboola.com", "widgets.outbrain.com", "outbrain.com",
    "criteo.com", "static.criteo.net",
    "hotjar.com", "static.hotjar.com",
    "newrelic.com", "nr-data.net", "bam.nr-data.net",
    "segment.io", "cdn.segment.com",
    "mixpanel.com", "cdn.mxpnl.com",
    "optimizely.com", "cdn.optimizely.com",
    "mouseflow.com", "crazyegg.com",
    "chartbeat.com", "static.chartbeat.com",
    "branch.io", "app.link", "adjust.com",
    "appsflyer.com", "kochava.com",
    "amplitude.com", "cdn.amplitude.com",
    "rubiconproject.com", "fastclick.net",
    "pubmatic.com", "openx.net", "casalemedia.com",
    "indexexchange.com", "sharethrough.com",
    "moatads.com", "z.moatads.com",
    "adsafeprotected.com", "fw.adsafeprotected.com",
    "doubleverify.com",
}

# URL path patterns that are tracking/noise
NOISE_URL_PATTERNS = [
    "/pixel", "/beacon", "/track", "/collect",
    "/imp?", "/impression", "/__utm.gif",
    "/pagead/", "/aclk?", "/adsense/",
    "favicon.ico", "/apple-touch-icon",
    "about:blank", "about:newtab",
]

# Domains to always skip (not useful browsing content)
SKIP_DOMAINS = {
    "localhost", "127.0.0.1", "0.0.0.0",
    "", "local", "broadcasthost",
}


# ── Globals ──────────────────────────────────────────────────────────────────

shutdown_requested = False
stats = {
    "total_visits": 0,
    "groups_formed": 0,
    "groups_ingested": 0,
    "visits_ingested": 0,
    "skipped_noise": 0,
    "skipped_checkpoint": 0,
    "skipped_no_title": 0,
    "errors": 0,
    "start_time": 0,
    "last_status": 0,
}


def handle_signal(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    log("Shutdown requested, finishing current batch...")


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[safari_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def mac_timestamp_to_datetime(mac_time):
    """Convert Mac absolute time (seconds since 2001-01-01) to datetime."""
    return MAC_EPOCH + timedelta(seconds=mac_time)


def slack_post(text):
    """Post a message to #nova-notifications."""
    if not SLACK_TOKEN:
        return
    try:
        payload = json.dumps({
            "channel": SLACK_CHAN,
            "text": text,
            "mrkdwn": True,
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {SLACK_TOKEN}",
            }
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Slack post failed: {e}")


def vector_remember(text, metadata):
    """Store a memory chunk in Nova's vector memory."""
    payload = json.dumps({
        "text": text,
        "source": "safari_history",
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        log(f"Memory write failed: {e}")
        return False


# ── Checkpoint management ────────────────────────────────────────────────────

def load_checkpoint():
    """Load checkpoint: set of already-ingested group keys (domain::date)."""
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return set(data.get("ingested_keys", []))
        except (json.JSONDecodeError, KeyError):
            log("Corrupt checkpoint file, starting fresh")
    return set()


def save_checkpoint(ingested_keys):
    """Save checkpoint atomically."""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "ingested_keys": sorted(ingested_keys),
        "last_updated": datetime.now().isoformat(),
        "total_groups": len(ingested_keys),
    }, indent=2))
    tmp.replace(CHECKPOINT_FILE)


# ── Noise detection ──────────────────────────────────────────────────────────

def is_noise_url(url, domain):
    """Return True if this URL is tracking, ad, or noise."""
    if not url:
        return True

    # Skip known ad/tracking domains
    domain_lower = (domain or "").lower()
    for ad_domain in AD_TRACKING_DOMAINS:
        if domain_lower == ad_domain or domain_lower.endswith("." + ad_domain):
            return True

    # Skip localhost and empty domains
    if domain_lower in SKIP_DOMAINS:
        return True

    # Skip tracking URL patterns
    url_lower = url.lower()
    for pattern in NOISE_URL_PATTERNS:
        if pattern in url_lower:
            return True

    # Skip data: and javascript: URLs
    if url_lower.startswith(("data:", "javascript:", "blob:", "about:")):
        return True

    # Skip very short URLs (likely redirects)
    if len(url) < 12:
        return True

    return False


def clean_domain(domain):
    """Normalize domain for grouping (strip www. prefix)."""
    if not domain:
        return "unknown"
    d = domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d


# ── Database reading ─────────────────────────────────────────────────────────

def read_history(since_date):
    """Read Safari history from a copy of History.db.

    Safari locks the database while running, so we copy it first.
    Returns list of (url, domain, title, visit_time_mac, visit_count) tuples.
    """
    if not HISTORY_DB.exists():
        log(f"History.db not found at {HISTORY_DB}")
        sys.exit(1)

    # Copy the database to a temp file (Safari may have it locked)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        shutil.copy2(HISTORY_DB, tmp_path)
        # Also copy the WAL and SHM files if they exist for consistency
        for suffix in ["-wal", "-shm"]:
            wal = HISTORY_DB.parent / (HISTORY_DB.name + suffix)
            if wal.exists():
                shutil.copy2(wal, tmp_path.parent / (tmp_path.name + suffix))
    except PermissionError:
        log("Permission denied reading History.db. Grant Full Disk Access to Terminal.")
        log("System Preferences > Privacy & Security > Full Disk Access > Terminal")
        sys.exit(1)

    since_mac = (since_date - MAC_EPOCH).total_seconds()

    visits = []
    try:
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute("""
            SELECT
                hi.url,
                hi.domain_expansion,
                hv.title,
                hv.visit_time,
                hi.visit_count
            FROM history_visits hv
            JOIN history_items hi ON hv.history_item = hi.id
            WHERE hv.visit_time >= ?
            ORDER BY hv.visit_time ASC
        """, (since_mac,))

        for row in cursor:
            visits.append(row)

        conn.close()
    except sqlite3.Error as e:
        log(f"SQLite error: {e}")
        sys.exit(1)
    finally:
        # Clean up temp files
        for suffix in ["", "-wal", "-shm"]:
            p = Path(str(tmp_path) + suffix)
            if p.exists():
                p.unlink()

    return visits


# ── Grouping ─────────────────────────────────────────────────────────────────

def group_visits(visits):
    """Group visits by domain and date for meaningful memory chunks.

    Returns dict of {(domain, date_str): [visit_info, ...]}
    """
    groups = defaultdict(list)

    for url, domain, title, visit_time_mac, visit_count in visits:
        domain_clean = clean_domain(domain)

        if is_noise_url(url, domain_clean):
            stats["skipped_noise"] += 1
            continue

        # Skip visits with no meaningful title
        if not title or title.strip() in ("", "Untitled", "undefined", "null"):
            stats["skipped_no_title"] += 1
            continue

        visit_dt = mac_timestamp_to_datetime(visit_time_mac)
        date_str = visit_dt.strftime("%Y-%m-%d")

        groups[(domain_clean, date_str)].append({
            "title": title.strip(),
            "url": url,
            "time": visit_dt.strftime("%H:%M"),
            "visit_count": visit_count,
        })

    return groups


def format_memory_text(domain, date_str, visit_list):
    """Format a group of visits into a readable memory chunk."""
    # Deduplicate by title (same page visited multiple times in a day)
    seen_titles = set()
    unique_visits = []
    for v in visit_list:
        title_key = v["title"].lower()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_visits.append(v)

    # Cap the number of URLs per chunk
    if len(unique_visits) > MAX_URLS_PER_CHUNK:
        unique_visits = unique_visits[:MAX_URLS_PER_CHUNK]
        truncated = True
    else:
        truncated = False

    lines = [f"Safari browsing on {domain} — {date_str}"]
    lines.append(f"({len(unique_visits)} pages visited)")
    lines.append("")

    for v in unique_visits:
        lines.append(f"- [{v['time']}] {v['title']}")
        lines.append(f"  {v['url']}")

    if truncated:
        remaining = len(visit_list) - MAX_URLS_PER_CHUNK
        lines.append(f"  ... and {remaining} more pages")

    return "\n".join(lines)


# ── Status reporting ─────────────────────────────────────────────────────────

def maybe_post_status(force=False):
    """Post a status update to Slack every STATUS_INTERVAL seconds."""
    now = time.time()
    if not force and (now - stats["last_status"]) < STATUS_INTERVAL:
        return

    stats["last_status"] = now
    elapsed = now - stats["start_time"]
    elapsed_min = elapsed / 60

    rate = stats["groups_ingested"] / elapsed_min if elapsed_min > 0 else 0

    remaining = stats["groups_formed"] - stats["groups_ingested"] - stats["skipped_checkpoint"]
    eta_min = remaining / rate if rate > 0 else 0

    text = (
        f":safari: *Safari History Ingest*\n"
        f"Groups: {stats['groups_ingested']}/{stats['groups_formed']} ingested"
        f" ({stats['skipped_checkpoint']} skipped from checkpoint)\n"
        f"Visits: {stats['visits_ingested']} stored, "
        f"{stats['skipped_noise']} noise, {stats['skipped_no_title']} no title\n"
        f"Rate: {rate:.1f} groups/min — ETA: {eta_min:.0f} min\n"
        f"Errors: {stats['errors']}"
    )
    slack_post(text)


# ── Main ─────────────────────────────────────────────────────────────────────

def show_status():
    """Show current progress from checkpoint file."""
    if not CHECKPOINT_FILE.exists():
        print("No checkpoint file found. No previous ingest run detected.")
        return

    data = json.loads(CHECKPOINT_FILE.read_text())
    print(f"Safari History Ingest Status")
    print(f"  Last updated: {data.get('last_updated', 'unknown')}")
    print(f"  Groups ingested: {data.get('total_groups', 0)}")


def run_ingest(since_date, dry_run=False):
    """Main ingest pipeline."""
    stats["start_time"] = time.time()
    stats["last_status"] = time.time()

    log(f"Reading Safari history since {since_date.strftime('%Y-%m-%d')}...")
    visits = read_history(since_date)
    stats["total_visits"] = len(visits)
    log(f"Found {len(visits)} visits in History.db")

    if not visits:
        log("No visits found. Nothing to do.")
        return

    log("Grouping visits by domain and date...")
    groups = group_visits(visits)
    stats["groups_formed"] = len(groups)
    log(f"Formed {len(groups)} domain/date groups "
        f"(filtered {stats['skipped_noise']} noise, {stats['skipped_no_title']} no-title)")

    if dry_run:
        log("DRY RUN — showing top 20 groups by visit count:")
        sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
        for (domain, date_str), visit_list in sorted_groups[:20]:
            log(f"  {domain} on {date_str}: {len(visit_list)} visits")
        log(f"\nTotal: {stats['groups_formed']} groups from {stats['total_visits']} visits")
        return

    # Load checkpoint
    ingested_keys = load_checkpoint()
    log(f"Checkpoint: {len(ingested_keys)} groups already ingested")

    # Post initial status
    slack_post(
        f":safari: *Safari History Ingest Starting*\n"
        f"Total visits: {stats['total_visits']}\n"
        f"Groups to process: {stats['groups_formed']}\n"
        f"Already ingested: {len(ingested_keys)}\n"
        f"Since: {since_date.strftime('%Y-%m-%d')}"
    )

    # Process groups
    for (domain, date_str), visit_list in sorted(groups.items()):
        if shutdown_requested:
            log("Shutdown requested, saving checkpoint...")
            break

        group_key = f"{domain}::{date_str}"

        # Skip already ingested
        if group_key in ingested_keys:
            stats["skipped_checkpoint"] += 1
            continue

        # Format the memory text
        text = format_memory_text(domain, date_str, visit_list)

        metadata = {
            "privacy": "local-only",
            "domain": domain,
            "date": date_str,
            "visit_count": len(visit_list),
            "ingest_time": datetime.now().isoformat(),
        }

        if vector_remember(text, metadata):
            stats["groups_ingested"] += 1
            stats["visits_ingested"] += len(visit_list)
            ingested_keys.add(group_key)

            # Save checkpoint every 50 groups
            if stats["groups_ingested"] % 50 == 0:
                save_checkpoint(ingested_keys)
        else:
            stats["errors"] += 1

        maybe_post_status()

    # Final save and status
    save_checkpoint(ingested_keys)

    elapsed = time.time() - stats["start_time"]
    log(f"Done in {elapsed/60:.1f} min — "
        f"{stats['groups_ingested']} groups ingested, "
        f"{stats['visits_ingested']} visits, "
        f"{stats['skipped_checkpoint']} skipped, "
        f"{stats['errors']} errors")

    slack_post(
        f":white_check_mark: *Safari History Ingest Complete*\n"
        f"Groups ingested: {stats['groups_ingested']}\n"
        f"Visits stored: {stats['visits_ingested']}\n"
        f"Skipped (checkpoint): {stats['skipped_checkpoint']}\n"
        f"Skipped (noise): {stats['skipped_noise']}\n"
        f"Skipped (no title): {stats['skipped_no_title']}\n"
        f"Errors: {stats['errors']}\n"
        f"Elapsed: {elapsed/60:.1f} min"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Safari browsing history into Nova's vector memory."
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Cutoff date in YYYY-MM-DD format (default: 5 years ago)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count and preview groups without ingesting",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show progress from checkpoint file",
    )
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Invalid date format: {args.since} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        since_date = datetime.now(timezone.utc) - timedelta(days=5 * 365)

    run_ingest(since_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
