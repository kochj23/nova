#!/usr/bin/env python3
"""
nova_vector_taxonomy.py — Define hierarchical taxonomy for memory sources and
backfill the category ltree column.

Designed for nohup background operation:
    nohup python3 nova_vector_taxonomy.py > ~/.openclaw/logs/vector_taxonomy.log 2>&1 &

Maps each source value to an ltree category path, then runs one UPDATE per source.
Fast because it uses the existing source index.

Written by Jordan Koch.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

try:
    import asyncpg
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "asyncpg"])
    import asyncpg

# ── Configuration ─────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_memories"

# ── Source → ltree Taxonomy ───────────────────────────────────────────────────
# Keys are source values, values are ltree paths (dot-separated)

SOURCE_TAXONOMY: dict[str, str] = {
    # ── music.* ───────────────────────────────────────────────────────────────
    "jazz_history": "music.jazz.history",
    "jazz_theory": "music.jazz.theory",
    "jazz_artists": "music.jazz.artists",
    "metal_history": "music.metal.history",
    "metal_bands": "music.metal.bands",
    "rap_history": "music.rap.history",
    "rap_artists": "music.rap.artists",
    "edm_history": "music.edm.history",
    "edm_artists": "music.edm.artists",
    "punk_history": "music.punk.history",
    "punk_bands": "music.punk.bands",
    "nowave_history": "music.nowave",
    "newwave_history": "music.newwave",
    "idm_history": "music.idm",
    "idm_artists": "music.idm",
    "music": "music.general",
    "music_history": "music.history",
    "music_general": "music.general",
    "hardcore_punk": "music.punk.hardcore",
    "socal_rave": "music.edm.socal_rave",

    # ── history.military.* ────────────────────────────────────────────────────
    "ww2_history": "history.military.ww2",
    "ww2_europe": "history.military.ww2.europe",
    "ww2_pacific": "history.military.ww2.pacific",
    "ww2_weapons": "history.military.ww2.weapons",
    "ww2_intelligence": "history.military.ww2.intelligence",
    "military_history": "history.military.general",
    "vietnam_war": "history.military.vietnam",
    "korean_war": "history.military.korea",
    "american_revolution": "history.military.american_revolution",
    "civil_war": "history.military.civil_war",
    "cold_war": "history.military.cold_war",
    "weapons_history": "history.military.weapons",
    "forgotten_weapons": "history.military.weapons",
    "nuclear_weapons": "history.military.nuclear",
    "space_history": "history.space",

    # ── science.* ─────────────────────────────────────────────────────────────
    "physics_general": "science.physics.general",
    "physics_quantum": "science.physics.quantum",
    "physics_relativity": "science.physics.relativity",
    "chemistry_general": "science.chemistry.general",
    "chemistry_organic": "science.chemistry.organic",
    "biology_general": "science.biology.general",
    "biology_evolution": "science.biology.evolution",
    "math_general": "science.math.general",
    "math_statistics": "science.math.statistics",
    "geology": "science.geology",
    "neuroscience": "science.neuroscience",
    "medicine_general": "science.medicine.general",
    "medicine_pharmacology": "science.medicine.pharmacology",
    "wiki_health": "science.medicine.general",

    # ── technology.* ──────────────────────────────────────────────────────────
    "computing_history": "technology.computing.history",
    "computing_general": "technology.computing.general",
    "sre_practices": "technology.sre",
    "sre_incidents": "technology.sre.incidents",
    "devops_general": "technology.devops",
    "compsec_general": "technology.security.general",
    "compsec_offensive": "technology.security.offensive",
    "compsec_defensive": "technology.security.defensive",
    "swift_development": "technology.development.swift",
    "fastapi": "technology.development.python",
    "postgresql": "technology.databases.postgresql",
    "programming_books": "technology.development.books",
    "wiki_technology": "technology.general",
    "nova_project_docs": "technology.nova",
    "nova_operational": "technology.nova.operations",
    "homekit": "technology.homekit",
    "education": "technology.education",

    # ── personal.* ────────────────────────────────────────────────────────────
    "email_archive": "personal.email",
    "imessage": "personal.imessage",
    "slack": "personal.slack",
    "livejournal": "personal.livejournal",
    "private_document": "personal.documents",
    "financial_documents": "personal.finance",
    "apple_health": "personal.health",
    "healthkit": "personal.health",
    "calendar": "personal.calendar",
    "family_contacts": "personal.contacts",
    "herd": "personal.herd",
    "personal_videos": "personal.media",
    "home_address": "personal.private",
    "oneonone_meetings": "personal.work.meetings",
    "work_internal": "personal.work.internal",
    "morning_brief": "personal.work.briefs",
    "camera_events": "personal.security",

    # ── entertainment.media.* ─────────────────────────────────────────────────
    "television": "entertainment.media.television",
    "documentary": "entertainment.media.documentary",
    "comedy": "entertainment.media.comedy",
    "crime_drama": "entertainment.media.crime_drama",
    "game_show": "entertainment.media.game_show",
    "horror": "entertainment.media.horror",
    "drama": "entertainment.media.drama",
    "action": "entertainment.media.action",
    "sci_fi": "entertainment.media.sci_fi",
    "blockbuster_films": "entertainment.media.blockbuster",
    "film_criticism": "entertainment.media.criticism",
    "livetv_news": "entertainment.media.live_news",
    "media_culture": "entertainment.media.culture",

    # ── entertainment.animation.* ─────────────────────────────────────────────
    "robotech": "entertainment.animation.robotech",
    "thundercats": "entertainment.animation.thundercats",
    "she_ra": "entertainment.animation.she_ra",
    "he_man": "entertainment.animation.he_man",
    "fist_of_north_star": "entertainment.animation.fist_of_north_star",
    "anime_films": "entertainment.animation.anime",
    "cartoons": "entertainment.animation.cartoons",

    # ── automotive.* ──────────────────────────────────────────────────────────
    "automotive": "automotive.general",
    "corvette_workshop_manual": "automotive.corvette",

    # ── geography.* ───────────────────────────────────────────────────────────
    "geography_general": "geography.general",
    "geography_physical": "geography.physical",
    "local_knowledge": "geography.local",
    "burbank_local": "geography.local.burbank",
    "wiki_los_angeles": "geography.local.los_angeles",
    "gang_culture": "geography.local.gangs",

    # ── philosophy.* ──────────────────────────────────────────────────────────
    "philosophy_general": "philosophy.general",
    "philosophy_existential": "philosophy.existential",
    "philosophy_eastern": "philosophy.eastern",
    "occult": "philosophy.occult",
    "gnostic_texts": "philosophy.gnostic",
    "psychedelic_research": "philosophy.psychedelic",
    "pihkal": "philosophy.psychedelic.pihkal",
    "tihkal": "philosophy.psychedelic.tihkal",
    "religion": "philosophy.religion",

    # ── literature.* ──────────────────────────────────────────────────────────
    "literature_general": "literature.general",
    "literature_fiction": "literature.fiction",
    "literature_nonfiction": "literature.nonfiction",
    "literature_poetry": "literature.poetry",

    # ── law.* ─────────────────────────────────────────────────────────────────
    "law_general": "law.general",
    "law_criminal": "law.criminal",
    "law_constitutional": "law.constitutional",

    # ── art.* ─────────────────────────────────────────────────────────────────
    "art_general": "art.general",
    "art_modern": "art.modern",
    "art_history": "art.history",

    # ── economics.* ───────────────────────────────────────────────────────────
    "economics_general": "economics.general",
    "economics_macro": "economics.macro",
    "economics_micro": "economics.micro",

    # ── food/cooking ──────────────────────────────────────────────────────────
    "cooking": "lifestyle.cooking",
    "wiki_gaming": "entertainment.gaming",
    "general_knowledge": "uncategorized",
}


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def main():
    nova_config.post_both(
        ":gear: *nova_vector_taxonomy* starting — backfilling category ltree column",
        slack_channel=nova_config.SLACK_NOTIFY,
    )
    log("Connecting to database...")
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)

    total_updated = 0
    sources_processed = 0
    start_time = time.time()

    try:
        # Get all distinct sources in the table
        async with pool.acquire() as conn:
            db_sources = await conn.fetch(
                "SELECT DISTINCT source FROM memories WHERE category IS NULL AND source IS NOT NULL"
            )

        source_list = [r["source"] for r in db_sources]
        log(f"Found {len(source_list)} distinct sources needing category assignment")

        for source in source_list:
            category = SOURCE_TAXONOMY.get(source, "uncategorized")

            async with pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE memories SET category = $1::ltree WHERE source = $2 AND category IS NULL",
                    category,
                    source,
                )
                count = int(result.split()[-1]) if result else 0

            if count > 0:
                total_updated += count
                log(f"  {source:40} -> {category:40} ({count:,} rows)")

            sources_processed += 1

            if sources_processed % 50 == 0:
                log(f"  ... {sources_processed}/{len(source_list)} sources done, {total_updated:,} rows updated")

    finally:
        await pool.close()

    elapsed = time.time() - start_time
    summary = (
        f":white_check_mark: *nova_vector_taxonomy* complete\n"
        f"- Sources processed: {sources_processed:,}\n"
        f"- Rows categorized: {total_updated:,}\n"
        f"- Duration: {elapsed / 60:.1f} minutes"
    )
    log(summary)
    nova_config.post_both(summary, slack_channel=nova_config.SLACK_NOTIFY)


if __name__ == "__main__":
    asyncio.run(main())
