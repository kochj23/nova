#!/usr/bin/env python3
"""
nova_music_dna.py — Cross-genre music DNA finder for Nova's memory.

Searches all 80 music vectors simultaneously to surface non-obvious
connections between genres. The insight: if your punk query matches
something in jazz_history, that's far more interesting than matching
in hardcore_punk.

Usage:
    CLI:  python3 nova_music_dna.py "Black Flag nervous breakdown"
    Tool: from nova_music_dna import find_connections; result = await find_connections("query")

Written by Jordan Koch.
"""

import asyncio
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import asyncpg

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

DB_DSN = f"postgresql://kochj@{nova_config.NOVA_HOST}:5432/nova_memories"
EMBED_URL = f"http://{nova_config.NOVA_HOST}:18790/embed"
TOP_K = 20          # results from DB
DISPLAY_K = 10      # results shown to user
CROSS_GENRE_BOOST = 0.15  # bonus for unexpected source matches

# All 80 music vectors in Nova's memory
MUSIC_SOURCES = [
    # Jazz family
    "jazz", "jazz_history", "jazz_fusion", "jazz_artists", "jazz_albums",
    "jazz_standards", "jazz_theory", "jazz_labels", "jazz_culture", "jazz_venues",
    # Metal family
    "metal", "metal_history", "metal_subgenres", "metal_artists", "metal_albums",
    "metal_labels", "metal_culture", "metal_thrash", "metal_death", "metal_black",
    # Rap / Hip-hop family
    "rap", "rap_history", "rap_artists", "rap_albums", "rap_labels",
    "rap_producers", "rap_culture", "rap_east", "rap_west", "rap_south",
    # EDM family
    "edm", "edm_history", "edm_artists", "edm_labels", "edm_culture",
    "edm_subgenres", "edm_techno", "edm_house", "edm_drum_and_bass", "edm_trance",
    # Punk family
    "punk", "punk_history", "punk_artists", "punk_albums", "punk_labels",
    "punk_culture", "punk_uk", "punk_us", "punk_post", "punk_pop",
    # No Wave / New Wave / IDM
    "nowave", "nowave_history", "nowave_artists", "nowave_albums",
    "newwave", "newwave_history", "newwave_artists", "newwave_albums",
    "newwave_synth", "newwave_culture",
    "idm", "idm_history", "idm_artists", "idm_albums", "idm_labels",
    # Hardcore
    "hardcore_punk", "hardcore_punk_history", "hardcore_punk_artists",
    "hardcore_punk_albums", "hardcore_punk_labels",
    # SoCal rave
    "socal_rave", "socal_rave_history", "socal_rave_artists", "socal_rave_venues",
    # General
    "music", "music_history", "music_general", "music_theory", "music_production",
    "music_culture", "music_instruments",
]

# Genre families for cross-genre detection
GENRE_FAMILIES = {
    "jazz": {"jazz", "jazz_history", "jazz_fusion", "jazz_artists", "jazz_albums",
             "jazz_standards", "jazz_theory", "jazz_labels", "jazz_culture", "jazz_venues"},
    "metal": {"metal", "metal_history", "metal_subgenres", "metal_artists", "metal_albums",
              "metal_labels", "metal_culture", "metal_thrash", "metal_death", "metal_black"},
    "rap": {"rap", "rap_history", "rap_artists", "rap_albums", "rap_labels",
            "rap_producers", "rap_culture", "rap_east", "rap_west", "rap_south"},
    "edm": {"edm", "edm_history", "edm_artists", "edm_labels", "edm_culture",
             "edm_subgenres", "edm_techno", "edm_house", "edm_drum_and_bass", "edm_trance"},
    "punk": {"punk", "punk_history", "punk_artists", "punk_albums", "punk_labels",
             "punk_culture", "punk_uk", "punk_us", "punk_post", "punk_pop",
             "hardcore_punk", "hardcore_punk_history", "hardcore_punk_artists",
             "hardcore_punk_albums", "hardcore_punk_labels"},
    "wave": {"nowave", "nowave_history", "nowave_artists", "nowave_albums",
             "newwave", "newwave_history", "newwave_artists", "newwave_albums",
             "newwave_synth", "newwave_culture"},
    "idm": {"idm", "idm_history", "idm_artists", "idm_albums", "idm_labels"},
    "rave": {"socal_rave", "socal_rave_history", "socal_rave_artists", "socal_rave_venues",
             "edm", "edm_history"},
    "general": {"music", "music_history", "music_general", "music_theory",
                "music_production", "music_culture", "music_instruments"},
}


@dataclass
class Connection:
    source: str
    text: str
    similarity: float
    boosted_score: float
    is_cross_genre: bool


# ── Embedding ────────────────────────────────────────────────────────────────

def get_embedding(query: str) -> list[float]:
    """Get embedding vector from Nova's memory server."""
    payload = json.dumps({"text": query}).encode()
    req = urllib.request.Request(
        EMBED_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["embedding"]


# ── Genre detection ──────────────────────────────────────────────────────────

def detect_query_genre(query: str) -> set[str]:
    """Heuristic: detect which genre family the query likely belongs to."""
    q = query.lower()
    matched_families = set()
    keywords = {
        "jazz": ["jazz", "coltrane", "miles davis", "bebop", "swing", "blue note", "mingus", "monk"],
        "metal": ["metal", "slayer", "metallica", "death", "thrash", "black metal", "doom", "riff"],
        "rap": ["rap", "hip-hop", "hip hop", "emcee", "mc ", "flow", "beats", "nas", "tupac", "biggie"],
        "edm": ["edm", "techno", "house", "rave", "dj ", "bpm", "synth", "electronic", "trance"],
        "punk": ["punk", "hardcore", "black flag", "minor threat", "dead kennedys", "misfits", "bad brains"],
        "wave": ["new wave", "no wave", "post-punk", "goth", "depeche", "joy division", "bauhaus"],
        "idm": ["idm", "aphex", "autechre", "warp records", "glitch", "braindance"],
        "rave": ["rave", "socal", "warehouse", "massiv", "breaks", "jungle"],
    }
    for family, terms in keywords.items():
        if any(term in q for term in terms):
            matched_families.add(family)
    return matched_families if matched_families else {"general"}


def is_cross_genre(source: str, query_families: set[str]) -> bool:
    """Check if a result source is outside the query's expected genre family."""
    source_families = set()
    for family, sources in GENRE_FAMILIES.items():
        if source in sources:
            source_families.add(family)
    # If result comes from a family NOT in the query's detected families, it's cross-genre
    if not source_families:
        return True  # unknown source = interesting
    return not source_families.intersection(query_families)


# ── Database query ───────────────────────────────────────────────────────────

async def search_music_vectors(query: str, top_k: int = TOP_K) -> list[Connection]:
    """Search all music vectors using pgvector cosine similarity."""
    embedding = get_embedding(query)
    query_families = detect_query_genre(query)

    # Format embedding for PostgreSQL
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            """
            SELECT source, text, 1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE source = ANY($2::text[])
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str, MUSIC_SOURCES, top_k
        )
    finally:
        await conn.close()

    connections = []
    for row in rows:
        source = row["source"]
        similarity = float(row["similarity"])
        cross = is_cross_genre(source, query_families)
        boosted = similarity + (CROSS_GENRE_BOOST if cross else 0.0)
        connections.append(Connection(
            source=source,
            text=row["text"][:200],  # truncate for display
            similarity=similarity,
            boosted_score=boosted,
            is_cross_genre=cross,
        ))

    # Sort by boosted score — cross-genre hits float to the top
    connections.sort(key=lambda c: c.boosted_score, reverse=True)
    return connections[:DISPLAY_K]


# ── Output formatting ────────────────────────────────────────────────────────

def format_results(query: str, connections: list[Connection]) -> str:
    """Format results for human consumption."""
    lines = [f"Music DNA: \"{query}\"", f"{'=' * 60}"]

    cross_count = sum(1 for c in connections if c.is_cross_genre)
    lines.append(f"Cross-genre connections: {cross_count}/{len(connections)}\n")

    for i, c in enumerate(connections, 1):
        marker = "***" if c.is_cross_genre else "   "
        score = f"{c.similarity:.3f}"
        boost_note = f" [+boost]" if c.is_cross_genre else ""
        lines.append(f"{marker} {i:2d}. [{c.source}] (sim: {score}){boost_note}")
        lines.append(f"       {c.text}")
        lines.append("")

    lines.append("*** = cross-genre connection (unexpected source)")
    return "\n".join(lines)


# ── Public API (tool mode) ───────────────────────────────────────────────────

async def find_connections(query: str, notify_slack: bool = False) -> str:
    """Main entry point for gateway integration. Returns formatted string."""
    connections = await search_music_vectors(query)
    result = format_results(query, connections)
    if notify_slack:
        cross_count = sum(1 for c in connections if c.is_cross_genre)
        nova_config.post_both(
            f"Music DNA search: \"{query}\"\n{cross_count} cross-genre hits found.",
            slack_channel=nova_config.SLACK_NOTIFY,
        )
    return result


# ── CLI mode ─────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage: nova_music_dna.py <query>")
        print("Example: nova_music_dna.py \"Black Flag nervous breakdown\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f"Searching 80 music vectors for: \"{query}\"...\n")

    connections = await search_music_vectors(query)
    print(format_results(query, connections))


if __name__ == "__main__":
    asyncio.run(main())
