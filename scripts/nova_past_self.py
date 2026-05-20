#!/usr/bin/env python3
"""
nova_past_self.py — Query Jordan's past opinions and experiences from specific time periods.

Searches Nova's personal communication vectors (emails, iMessages, LiveJournal,
Slack, private documents) using semantic similarity + time-period filtering.

Usage:
    python3 nova_past_self.py "Iraq war" --year 2003
    python3 nova_past_self.py "raves" --range 2000-2005
    python3 nova_past_self.py "career anxiety" --year 2015 --limit 5

Tool mode:
    from nova_past_self import query_past_self
    results = await query_past_self("Iraq war", year=2003)

Written by Jordan Koch.
"""

import argparse
import asyncio
import json
import re
import sys
from typing import Optional

import asyncpg
import httpx


# ── Configuration ────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_memories"
EMBED_URL = "http://192.168.1.6:18790/embed"
TABLE = "memories"

PERSONAL_SOURCES = (
    "email_archive",
    "imessage",
    "livejournal",
    "slack",
    "private_document",
)

# Fetch more candidates than needed so time filtering still yields good results
CANDIDATE_LIMIT = 100
DEFAULT_RESULT_LIMIT = 10

# Regex to extract year from email Date: headers or general date patterns
YEAR_PATTERNS = [
    re.compile(r"Date:\s.*\b(19\d{2}|20[0-2]\d)\b"),          # Email Date: header
    re.compile(r"\b(19\d{2}|20[0-2]\d)[-/](0[1-9]|1[0-2])"),  # YYYY-MM or YYYY/MM
    re.compile(r"(0[1-9]|1[0-2])[-/]\d{1,2}[-/](19\d{2}|20[0-2]\d)"),  # MM/DD/YYYY
    re.compile(r"\b(January|February|March|April|May|June|July|August|"
               r"September|October|November|December)\s+\d{1,2},?\s+(19\d{2}|20[0-2]\d)"),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_year(text: str) -> Optional[int]:
    """Extract the most likely year from text content using Date: headers or date patterns."""
    for pattern in YEAR_PATTERNS:
        match = pattern.search(text[:2000])  # Only scan the top of the text
        if match:
            # Find which group is a 4-digit year
            for group in match.groups():
                if group and re.match(r"^(19|20)\d{2}$", group):
                    return int(group)
    return None


def year_in_range(year: Optional[int], year_start: int, year_end: int) -> bool:
    """Check if extracted year falls within the target range."""
    if year is None:
        return False
    return year_start <= year <= year_end


def format_excerpt(text: str, max_len: int = 300) -> str:
    """Extract a meaningful excerpt, skipping email headers when possible."""
    lines = text.split("\n")
    # Try to find body after headers (blank line separator)
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "" and i > 2:
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        body = text.strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > max_len:
        return body[:max_len].rstrip() + "..."
    return body


async def get_embedding(query: str) -> list[float]:
    """Get embedding vector from Nova's memory server."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(EMBED_URL, json={"text": query})
        resp.raise_for_status()
        data = resp.json()
        return data["embedding"]


# ── Core Query ───────────────────────────────────────────────────────────────

async def query_past_self(
    query: str,
    year: Optional[int] = None,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[dict]:
    """
    Search Jordan's personal communications for a topic within a time period.

    Args:
        query: Semantic search query (topic, opinion, feeling)
        year: Single year to filter (shorthand for year_start=year_end=year)
        year_start: Start of year range (inclusive)
        year_end: End of year range (inclusive)
        limit: Max results to return

    Returns:
        List of dicts: {"year": int, "source": str, "excerpt": str, "date_raw": str}
    """
    if year and not year_start:
        year_start = year
        year_end = year
    if not year_start or not year_end:
        raise ValueError("Must provide --year or --range")

    # Get query embedding
    embedding = await get_embedding(query)
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    # Build source filter
    source_placeholders = ", ".join(f"${i+2}" for i in range(len(PERSONAL_SOURCES)))

    sql = f"""
        SELECT text, source, metadata, created_at,
               embedding <=> $1::vector AS distance
        FROM {TABLE}
        WHERE source IN ({source_placeholders})
        ORDER BY embedding <=> $1::vector
        LIMIT {CANDIDATE_LIMIT}
    """

    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(sql, embedding_str, *PERSONAL_SOURCES)
    finally:
        await conn.close()

    # Filter by year from text content and metadata
    results = []
    for row in rows:
        text = row["text"] or ""
        metadata = row["metadata"] if row["metadata"] else {}

        # Try metadata date first
        extracted_year = None
        if isinstance(metadata, dict) and metadata.get("date"):
            date_str = metadata["date"]
            year_match = re.search(r"(19\d{2}|20[0-2]\d)", str(date_str))
            if year_match:
                extracted_year = int(year_match.group(1))

        # Fall back to text parsing
        if extracted_year is None:
            extracted_year = extract_year(text)

        # Fall back to created_at (less reliable for archived content)
        if extracted_year is None and row["created_at"]:
            db_year = row["created_at"].year
            # Only trust created_at if it's within a plausible range for archives
            if db_year < 2024:
                extracted_year = db_year

        if not year_in_range(extracted_year, year_start, year_end):
            continue

        # Extract a readable date string
        date_raw = ""
        if isinstance(metadata, dict) and metadata.get("date"):
            date_raw = metadata["date"]
        else:
            date_match = re.search(r"Date:\s*(.+?)(?:\n|$)", text[:1000])
            if date_match:
                date_raw = date_match.group(1).strip()
            else:
                date_raw = f"~{extracted_year}"

        results.append({
            "year": extracted_year,
            "source": row["source"],
            "excerpt": format_excerpt(text),
            "date_raw": date_raw,
            "distance": float(row["distance"]),
        })

        if len(results) >= limit:
            break

    # Sort by relevance (distance)
    results.sort(key=lambda r: r["distance"])
    return results


# ── Output Formatting ────────────────────────────────────────────────────────

def format_narrative(results: list[dict], query: str, year_start: int, year_end: int) -> str:
    """Format results as a readable narrative for CLI output."""
    if not results:
        period = str(year_start) if year_start == year_end else f"{year_start}-{year_end}"
        return f"No personal communications found about \"{query}\" from {period}."

    lines = []
    period = str(year_start) if year_start == year_end else f"{year_start}-{year_end}"
    lines.append(f"Your past self on \"{query}\" ({period}):")
    lines.append("")

    for i, r in enumerate(results, 1):
        source_label = r["source"].replace("_", " ").title()
        lines.append(f"  {i}. [{r['date_raw']}] ({source_label})")
        lines.append(f"     \"{r['excerpt']}\"")
        lines.append("")

    lines.append(f"  ({len(results)} result{'s' if len(results) != 1 else ''} found)")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_range(range_str: str) -> tuple[int, int]:
    """Parse a year range like '2000-2005' into (start, end)."""
    parts = range_str.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Range must be YYYY-YYYY, got: {range_str}")
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Range must be YYYY-YYYY, got: {range_str}")
    if end < start:
        raise argparse.ArgumentTypeError(f"End year must be >= start year: {range_str}")
    return start, end


async def async_main():
    parser = argparse.ArgumentParser(
        description="Query your past opinions and experiences from Nova's memory.",
        epilog="Examples:\n"
               "  nova_past_self.py \"Iraq war\" --year 2003\n"
               "  nova_past_self.py \"raves\" --range 2000-2005\n"
               "  nova_past_self.py \"career\" --range 2010-2015 --limit 5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", help="Topic or phrase to search for")
    parser.add_argument("--year", type=int, help="Single year to search")
    parser.add_argument("--range", dest="year_range", type=str,
                        help="Year range to search (e.g., 2000-2005)")
    parser.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT,
                        help=f"Max results (default: {DEFAULT_RESULT_LIMIT})")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Output as JSON (for tool integration)")
    args = parser.parse_args()

    if not args.year and not args.year_range:
        parser.error("Must provide --year or --range")

    year_start = year_end = None
    if args.year:
        year_start = year_end = args.year
    elif args.year_range:
        year_start, year_end = parse_range(args.year_range)

    try:
        results = await query_past_self(
            query=args.query,
            year_start=year_start,
            year_end=year_end,
            limit=args.limit,
        )
    except httpx.HTTPError as exc:
        print(f"Error: Cannot reach embedding server at {EMBED_URL}: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: Database connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json_output:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(format_narrative(results, args.query, year_start, year_end))


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
