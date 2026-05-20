#!/usr/bin/env python3
"""
nova_reclassify_vectors.py — One-time migration to rename poorly-named memory sources.

All operations are UPDATE statements on the 'source' column — no re-embedding needed.
Run dry-run first, then live.

Written by Jordan Koch.
"""

import subprocess
import sys
from datetime import datetime

DRY_RUN = "--live" not in sys.argv

def psql(sql: str) -> str:
    r = subprocess.run(
        ["psql", "-p", "5432", "-U", "kochj", "-d", "nova_memories", "-t", "-A", "-c", sql],
        capture_output=True, text=True
    )
    return r.stdout.strip()

def rename(old: str, new: str, condition: str = "") -> int:
    where = f"source = '{old}'" + (f" AND ({condition})" if condition else "")
    count_sql = f"SELECT COUNT(*) FROM memories WHERE {where};"
    count = int(psql(count_sql) or 0)
    if count == 0:
        return 0
    action = f"UPDATE memories SET source = '{new}' WHERE {where};"
    if DRY_RUN:
        print(f"  DRY RUN: {count:>6,}  {old!r:45} → {new!r}")
    else:
        psql(action)
        print(f"  DONE:    {count:>6,}  {old!r:45} → {new!r}")
    return count

def fix_privacy(source: str) -> int:
    """Tag all rows in a source with privacy=local-only in metadata."""
    count_sql = f"SELECT COUNT(*) FROM memories WHERE source='{source}' AND metadata->>'privacy' IS NULL;"
    count = int(psql(count_sql) or 0)
    if count == 0:
        return 0
    action = f"""
UPDATE memories
SET metadata = jsonb_set(metadata, '{{privacy}}', '"local-only"')
WHERE source = '{source}' AND metadata->>'privacy' IS NULL;
"""
    if DRY_RUN:
        print(f"  DRY RUN: {count:>6,}  tag privacy=local-only on {source!r}")
    else:
        psql(action.strip())
        print(f"  DONE:    {count:>6,}  tagged privacy=local-only on {source!r}")
    return count

def count(source: str) -> int:
    return int(psql(f"SELECT COUNT(*) FROM memories WHERE source='{source}';") or 0)

NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
mode = "DRY RUN" if DRY_RUN else "LIVE"
print(f"\n{'='*70}")
print(f"  Nova Vector Reclassification — {NOW} [{mode}]")
print(f"{'='*70}\n")

total = 0

# ── 1. Straight renames ────────────────────────────────────────────────────

print("── Straight renames ─────────────────────────────────────────────────")
total += rename("document",              "livejournal")
total += rename("tv_transcript",         "television")
total += rename("gdrive-ingest",         "financial_documents")
total += rename("work_knowledge",        "work_internal")
total += rename("project_docs",          "nova_project_docs")
total += rename("security",              "camera_events")
total += rename("health",                "wiki_health")
total += rename("culture",               "media_culture")
total += rename("community",             "gang_culture")
total += rename("gaming",                "wiki_gaming")
total += rename("swift_dev",             "swift_development")
total += rename("swift_macos",           "swift_development")
total += rename("personal_media",        "personal_videos")
total += rename("personal",              "family_contacts")
total += rename("local",                 "burbank_local")
total += rename("internet_history",      "wiki_technology")

# ── 2. Good Eats fragments → cooking ─────────────────────────────────────

print("\n── Good Eats fragments → cooking ────────────────────────────────────")
for src in ["science", "food", "lifestyle", "commerce", "biography", "archive", "media"]:
    total += rename(src, "cooking")

# ── 3. Slack consolidation → slack ────────────────────────────────────────

print("\n── Slack consolidation → slack ──────────────────────────────────────")
for src in ["slack_general", "slack_conversation", "slack_jordan",
            "slack_todo", "slack_homerepair", "slack_random", "slack_house"]:
    total += rename(src, "slack")

# ── 4. OneOnOne consolidation ─────────────────────────────────────────────

print("\n── OneOnOne consolidation ───────────────────────────────────────────")
total += rename("oneonone", "oneonone_meetings")

# ── 5. LiveTV consolidation ───────────────────────────────────────────────

print("\n── LiveTV consolidation ─────────────────────────────────────────────")
for src in ["livetv_ambiance", "livetv_breaking_news", "livetv_novas_time"]:
    total += rename(src, "livetv_news")

# ── 6. Programming books consolidation ───────────────────────────────────

print("\n── Programming books → programming_books ────────────────────────────")
for src in ["software_architecture", "git_training"]:
    total += rename(src, "programming_books")

# ── 7. Nova operational consolidation ────────────────────────────────────

print("\n── Nova operational → nova_operational ──────────────────────────────")
for src in ["subagent.briefer", "subagent.analyst", "app_watchdog",
            "system", "system_status"]:
    total += rename(src, "nova_operational")

# ── 8. Herd consolidation ─────────────────────────────────────────────────

print("\n── Herd → herd ──────────────────────────────────────────────────────")
for src in ["herd_blog", "herd_correspondence"]:
    total += rename(src, "herd")

# ── 9. Burbank/local → burbank_local ─────────────────────────────────────

print("\n── Burbank/local → burbank_local ────────────────────────────────────")
for src in ["reddit", "burbank"]:
    total += rename(src, "burbank_local")

# ── 10. Gang culture consolidation ───────────────────────────────────────

print("\n── Gang culture → gang_culture ──────────────────────────────────────")
for src in ["community", "gang_data", "LA County gangs",
            "r/homelab"]:        # r/homelab sample was gang/security related
    total += rename(src, "gang_culture")

# ── 11. Reddit tech subreddits → appropriate sources ─────────────────────

print("\n── Reddit subreddits → specific sources ─────────────────────────────")
total += rename("r/HomeKit",   "homekit")
total += rename("r/BambuLab",  "nova_project_docs")

# ── 12. KDAY Radio history → music_history ───────────────────────────────

print("\n── KDAY Radio history → music_history ───────────────────────────────")
total += rename("KDAY Radio history", "music_history")

# ── 13. Orphaned single entries ───────────────────────────────────────────

print("\n── Orphaned single entries ───────────────────────────────────────────")
total += rename("user_request",    "local_knowledge")
total += rename("user",            "work_internal")
total += rename("meeting",         "work_internal")
total += rename("law",             "gang_culture")
total += rename("archive",         "cooking")   # already caught above, safe to re-run
total += rename("research_summary","local_knowledge")
total += rename("self_update",     "nova_operational")
total += rename("work_reports",    "work_internal")
total += rename("public service",  "local_knowledge")

# ── 14. local_knowledge split: keep local_knowledge for local info,
#        move documentary transcripts to documentary ─────────────────────

print("\n── local_knowledge: move Documentary entries → documentary ──────────")
total += rename(
    "local_knowledge", "documentary",
    "text ILIKE '%[Documentary:%' OR text ILIKE '%documentary transcript%'"
)

# ── 15. YouTube classification ────────────────────────────────────────────

print("\n── YouTube classification ────────────────────────────────────────────")

# youtube_transcript: all CrashCourse playlists
# Literature → education  |  Organic Chemistry → education
# Business/Entrepreneurship → education  |  AI → education
total += rename("youtube_transcript", "education")   # 100% CrashCourse = education

# youtube-channel-ingest by channel
total += rename(
    "youtube-channel-ingest", "space_history",
    "metadata->>'channel' = 'The Vintage Space'"
)
total += rename(
    "youtube-channel-ingest", "automotive",
    "metadata->>'channel' IN ('Jay Leno''s Garage', 'JasonCammisa')"
)
total += rename(
    "youtube-channel-ingest", "education",
    "metadata->>'channel' = 'CrashCourse'"
)
# Any remaining youtube-channel-ingest (shouldn't be any)
remaining = count("youtube-channel-ingest")
if remaining > 0:
    total += rename("youtube-channel-ingest", "television")

# ── 16. Fix mail source names (apostrophe/space issues) ──────────────────

print("\n── Fix mail source names ─────────────────────────────────────────────")
total += rename("mail_don't_fight_the_crazy", "email_archive")
total += rename("mail_consumer_whore",        "email_archive")
total += rename("mail_home_rennovations",      "email_archive")
total += rename("mail_automotive",             "email_archive")

# ── 17. Privacy tagging for sensitive sources ─────────────────────────────

print("\n── Privacy tagging (local-only) ─────────────────────────────────────")
for src in ["home_address", "personal", "family_contacts",
            "gdrive-ingest", "financial_documents",
            "personal_media", "personal_videos",
            "threat-documentation", "imessage", "email_archive",
            "family_contacts", "livejournal"]:
    fix_privacy(src)

print(f"\n{'='*70}")
print(f"  Total rows affected: {total:,}")
if DRY_RUN:
    print(f"\n  Run with --live to apply changes.")
print(f"{'='*70}\n")
