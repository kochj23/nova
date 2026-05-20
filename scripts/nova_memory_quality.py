#!/usr/bin/env python3
"""
nova_memory_quality.py — Weekly audit of Nova's vector memory database.

Detects garbage entries across five categories:
  1. Repetitive content (same phrase 5+ times in one memory)
  2. Near-empty chunks (<30 chars after stripping)
  3. Misclassified memories (keyword heuristics vs source domain)
  4. Duplicate text_hash collisions (exact dupes that slipped through)
  5. Transcription artifacts (gibberish repetition patterns)

Modes:
  --dry-run (default): Report findings, no modifications
  --clean: Quarantine bad memories (sets source to 'quarantine:{original_source}')

Reports summary to Slack #nova-notifications.
Logs to ~/.openclaw/logs/nova_memory_quality.log

Written by Jordan Koch.
"""

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ───────────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_memories"
LOG_DIR = Path.home() / ".openclaw" / "logs"
LOG_FILE = LOG_DIR / "nova_memory_quality.log"
BATCH_SIZE = 5000
QUARANTINE_PREFIX = "quarantine"

# Misclassification heuristics: source -> keywords that should NOT be there
DOMAIN_KEYWORDS = {
    "jazz_history": ["military", "battle", "infantry", "artillery", "regiment", "warfare", "battalion"],
    "classical_music": ["touchdown", "quarterback", "slam dunk", "home run", "penalty kick"],
    "automotive": ["recipe", "baking", "flour", "oven temperature", "tablespoon"],
    "cooking": ["carburetor", "horsepower", "turbocharger", "cylinder", "crankshaft"],
    "astronomy": ["recipe", "cookbook", "ingredient", "marinate", "sauté"],
    "world_history": ["guitar tab", "chord progression", "verse chorus", "BPM", "time signature"],
    "biology": ["stock market", "dividend", "nasdaq", "bull market", "portfolio allocation"],
    "game_show": ["surgical procedure", "chemotherapy", "dialysis", "biopsy", "anesthesia"],
}

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("memory_quality")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

sh = logging.StreamHandler()
sh.setLevel(logging.INFO)
sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(sh)


# ── Detection Functions ──────────────────────────────────────────────────────

def detect_repetitive(text: str) -> bool:
    """Same word/phrase repeated 5+ times consecutively."""
    # Split into words, look for 5+ consecutive identical tokens
    words = text.lower().split()
    if len(words) < 5:
        return False
    count = 1
    for i in range(1, len(words)):
        if words[i] == words[i - 1]:
            count += 1
            if count >= 5:
                return True
        else:
            count = 1
    # Also check for repeated short phrases (2-3 word patterns)
    for phrase_len in (2, 3):
        phrases = [" ".join(words[i:i + phrase_len]) for i in range(len(words) - phrase_len + 1)]
        if len(phrases) < 5:
            continue
        count = 1
        for i in range(1, len(phrases)):
            if phrases[i] == phrases[i - 1]:
                count += 1
                if count >= 5:
                    return True
            else:
                count = 1
    return False


def detect_near_empty(text: str) -> bool:
    """Less than 30 chars of actual content after stripping."""
    stripped = re.sub(r"\s+", "", text)
    return len(stripped) < 30


def detect_misclassified(text: str, source: str) -> bool:
    """Source domain contains keywords that don't belong."""
    keywords = DOMAIN_KEYWORDS.get(source)
    if not keywords:
        return False
    lower = text.lower()
    matches = sum(1 for kw in keywords if kw in lower)
    return matches >= 3


def detect_transcription_artifact(text: str) -> bool:
    """Gibberish repetition typical of bad ASR output."""
    words = text.lower().split()
    if len(words) < 6:
        return False
    # Check if >60% of words are the same token
    if words:
        from collections import Counter
        most_common_count = Counter(words).most_common(1)[0][1]
        if most_common_count / len(words) > 0.6 and len(words) >= 6:
            return True
    # Check for non-dictionary gibberish patterns (consonant clusters)
    gibberish_pattern = re.compile(r"([bcdfghjklmnpqrstvwxyz]{5,})")
    gibberish_words = sum(1 for w in words if gibberish_pattern.search(w))
    if len(words) >= 8 and gibberish_words / len(words) > 0.5:
        return True
    return False


# ── Main Audit ───────────────────────────────────────────────────────────────

async def run_audit(clean: bool = False):
    import asyncpg

    logger.info(f"Starting memory quality audit — mode={'CLEAN' if clean else 'DRY-RUN'}")

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5, command_timeout=60)

    findings = {
        "repetitive": [],
        "near_empty": [],
        "misclassified": [],
        "duplicate_hash": [],
        "transcription_artifact": [],
    }

    total_scanned = 0

    try:
        async with pool.acquire() as conn:
            # Phase 1-3, 5: Scan all memories in batches
            total_count = await conn.fetchval("SELECT count(*) FROM memories")
            logger.info(f"Total memories in database: {total_count:,}")

            offset = 0
            while offset < total_count:
                rows = await conn.fetch(
                    "SELECT id, text, source FROM memories ORDER BY created_at LIMIT $1 OFFSET $2",
                    BATCH_SIZE, offset
                )
                if not rows:
                    break

                for row in rows:
                    mid, text, source = row["id"], row["text"], row["source"]
                    if source.startswith(f"{QUARANTINE_PREFIX}:"):
                        continue

                    if detect_repetitive(text):
                        findings["repetitive"].append((mid, text[:80], source))
                    elif detect_near_empty(text):
                        findings["near_empty"].append((mid, text[:80], source))
                    elif detect_misclassified(text, source):
                        findings["misclassified"].append((mid, text[:80], source))
                    elif detect_transcription_artifact(text):
                        findings["transcription_artifact"].append((mid, text[:80], source))

                total_scanned += len(rows)
                if total_scanned % 50000 == 0:
                    logger.info(f"  Scanned {total_scanned:,}/{total_count:,} memories...")
                offset += BATCH_SIZE

            # Phase 4: Duplicate text_hash collisions
            dupes = await conn.fetch("""
                SELECT text_hash, count(*) as cnt
                FROM memories
                WHERE text_hash IS NOT NULL
                  AND source NOT LIKE 'quarantine:%'
                GROUP BY text_hash
                HAVING count(*) > 1
                ORDER BY count(*) DESC
                LIMIT 1000
            """)
            for dupe in dupes:
                dupe_rows = await conn.fetch(
                    "SELECT id, text, source FROM memories WHERE text_hash = $1 ORDER BY created_at LIMIT 10",
                    dupe["text_hash"]
                )
                # Keep the first (oldest), flag the rest
                for row in dupe_rows[1:]:
                    findings["duplicate_hash"].append((row["id"], row["text"][:80], row["source"]))

        # Summarize
        total_bad = sum(len(v) for v in findings.values())
        logger.info(f"Scan complete. Scanned: {total_scanned:,} | Issues found: {total_bad}")
        for category, items in findings.items():
            logger.info(f"  {category}: {len(items)}")

        # Quarantine if in clean mode
        quarantined = 0
        if clean and total_bad > 0:
            async with pool.acquire() as conn:
                for category, items in findings.items():
                    for mid, text_preview, source in items:
                        new_source = f"{QUARANTINE_PREFIX}:{source}"
                        await conn.execute(
                            "UPDATE memories SET source = $1 WHERE id = $2 AND source NOT LIKE 'quarantine:%'",
                            new_source, mid
                        )
                        quarantined += 1
                    if items:
                        logger.info(f"  Quarantined {len(items)} entries ({category})")
            logger.info(f"Total quarantined: {quarantined}")

        # Build Slack report
        report_lines = [
            f":mag: *Nova Memory Quality Audit* — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"Scanned: {total_scanned:,} memories",
            "",
            "*Findings:*",
            f"  Repetitive content: {len(findings['repetitive'])}",
            f"  Near-empty chunks: {len(findings['near_empty'])}",
            f"  Misclassified: {len(findings['misclassified'])}",
            f"  Duplicate hashes: {len(findings['duplicate_hash'])}",
            f"  Transcription artifacts: {len(findings['transcription_artifact'])}",
            f"  *Total issues: {total_bad}*",
            "",
        ]
        if clean:
            report_lines.append(f"Action: Quarantined {quarantined} entries")
        else:
            report_lines.append("Mode: Dry-run (no changes made)")

        # Add examples for non-empty categories
        for category, items in findings.items():
            if items and len(items) <= 5:
                report_lines.append(f"\n_{category} examples:_")
                for _, preview, src in items[:3]:
                    report_lines.append(f"  [{src}] {preview[:60]}")

        report = "\n".join(report_lines)
        logger.info(f"Posting report to Slack")
        nova_config.post_both(report, slack_channel=nova_config.SLACK_NOTIFY)

    except Exception as e:
        logger.error(f"Audit failed: {e}", exc_info=True)
        nova_config.post_both(
            f":x: *Memory Quality Audit FAILED*\n{e}",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        await pool.close()
        return 1

    await pool.close()
    logger.info("Audit complete.")
    return 0


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Nova Memory Quality Audit — detect and quarantine garbage entries"
    )
    parser.add_argument("--clean", action="store_true",
                        help="Quarantine bad memories (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only, no modifications (default)")
    args = parser.parse_args()

    clean = args.clean
    if clean:
        args.dry_run = False

    try:
        rc = asyncio.run(run_audit(clean=clean))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        rc = 1
    except Exception as e:
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        rc = 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
