#!/usr/bin/env python3
"""
nova_recent_memories.py — Query Nova's PostgreSQL memory store for recent additions.

Shows recently added memories grouped by source, with optional detail and JSON output.
Usable as a CLI tool or importable as a module.

Written by Jordan Koch.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

DB_NAME = "nova_memories"
TABLE = "memories"
DEFAULT_HOURS = 24
DETAIL_PREVIEW_COUNT = 5
SNIPPET_LENGTH = 80


def connect():
    """Open a read-only connection to the nova_memories database."""
    conn = psycopg2.connect(f"dbname={DB_NAME}")
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _cutoff(hours: int) -> datetime:
    """Return a timezone-aware UTC datetime N hours in the past."""
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def get_recent_summary(hours: int = DEFAULT_HOURS, source: str | None = None):
    """
    Return a summary of memories added in the last *hours* hours.

    Returns a dict:
        {
            "hours": int,
            "cutoff": str (ISO),
            "total": int,
            "by_source": [
                {"source": str, "count": int, "labels": [str, ...]},
                ...
            ]
        }
    """
    cutoff = _cutoff(hours)

    conn = connect()
    cur = conn.cursor()

    # Total count
    if source:
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE created_at >= %s AND source = %s",
            (cutoff, source),
        )
    else:
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE created_at >= %s",
            (cutoff,),
        )
    total = cur.fetchone()[0]

    # Per-source breakdown with representative labels from metadata
    if source:
        cur.execute(
            f"""
            SELECT source, COUNT(*) AS cnt,
                   array_agg(DISTINCT COALESCE(metadata->>'show', metadata->>'title', ''))
                     FILTER (WHERE COALESCE(metadata->>'show', metadata->>'title', '') != '')
                     AS labels
            FROM {TABLE}
            WHERE created_at >= %s AND source = %s
            GROUP BY source
            ORDER BY cnt DESC
            """,
            (cutoff, source),
        )
    else:
        cur.execute(
            f"""
            SELECT source, COUNT(*) AS cnt,
                   array_agg(DISTINCT COALESCE(metadata->>'show', metadata->>'title', ''))
                     FILTER (WHERE COALESCE(metadata->>'show', metadata->>'title', '') != '')
                     AS labels
            FROM {TABLE}
            WHERE created_at >= %s
            GROUP BY source
            ORDER BY cnt DESC
            """,
            (cutoff,),
        )

    rows = cur.fetchall()
    by_source = []
    for row in rows:
        labels = row[2] if row[2] else []
        by_source.append({
            "source": row[0],
            "count": row[1],
            "labels": sorted(labels),
        })

    cur.close()
    conn.close()

    return {
        "hours": hours,
        "cutoff": cutoff.isoformat(),
        "total": total,
        "by_source": by_source,
    }


def get_recent_detail(hours: int = DEFAULT_HOURS, source: str | None = None,
                      preview_count: int = DETAIL_PREVIEW_COUNT):
    """
    Return per-source detail including sample memory snippets.

    Returns a dict:
        {
            "hours": int,
            "cutoff": str (ISO),
            "total": int,
            "sources": [
                {
                    "source": str,
                    "count": int,
                    "labels": [str, ...],
                    "samples": [
                        {"text": str, "label": str, "created_at": str},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    cutoff = _cutoff(hours)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Get sources list
    if source:
        cur.execute(
            f"""
            SELECT source, COUNT(*) AS cnt,
                   array_agg(DISTINCT COALESCE(metadata->>'show', metadata->>'title', ''))
                     FILTER (WHERE COALESCE(metadata->>'show', metadata->>'title', '') != '')
                     AS labels
            FROM {TABLE}
            WHERE created_at >= %s AND source = %s
            GROUP BY source
            ORDER BY cnt DESC
            """,
            (cutoff, source),
        )
    else:
        cur.execute(
            f"""
            SELECT source, COUNT(*) AS cnt,
                   array_agg(DISTINCT COALESCE(metadata->>'show', metadata->>'title', ''))
                     FILTER (WHERE COALESCE(metadata->>'show', metadata->>'title', '') != '')
                     AS labels
            FROM {TABLE}
            WHERE created_at >= %s
            GROUP BY source
            ORDER BY cnt DESC
            """,
            (cutoff,),
        )

    source_rows = cur.fetchall()

    total = sum(r["cnt"] for r in source_rows)
    sources = []

    for row in source_rows:
        src = row["source"]
        cnt = row["cnt"]
        labels = sorted(row["labels"]) if row["labels"] else []

        # Fetch a few sample memories for this source
        cur.execute(
            f"""
            SELECT text,
                   COALESCE(metadata->>'show', metadata->>'title', '') AS label,
                   created_at
            FROM {TABLE}
            WHERE created_at >= %s AND source = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (cutoff, src, preview_count),
        )
        samples = []
        for s in cur.fetchall():
            samples.append({
                "text": s["text"],
                "label": s["label"],
                "created_at": s["created_at"].isoformat(),
            })

        sources.append({
            "source": src,
            "count": cnt,
            "labels": labels,
            "samples": samples,
        })

    cur.close()
    conn.close()

    return {
        "hours": hours,
        "cutoff": cutoff.isoformat(),
        "total": total,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_count(n: int) -> str:
    """Format an integer with thousands separators."""
    return f"{n:,}"


def _truncate(text: str, length: int = SNIPPET_LENGTH) -> str:
    """Truncate text to *length* characters, adding ellipsis if needed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "..."


def _label_tag(label: str) -> str:
    """Format a metadata label as a bracketed tag, or empty string."""
    if label:
        return f"[{label}] "
    return ""


def format_summary(data: dict) -> str:
    """Render the summary dict as human-readable text."""
    lines = []
    hours = data["hours"]
    total = data["total"]

    period = f"last {hours} hour{'s' if hours != 1 else ''}"
    lines.append(f"Memories added in the {period}: {_fmt_count(total)}")
    lines.append("")

    if not data["by_source"]:
        lines.append("  (none)")
        return "\n".join(lines)

    lines.append("By source:")

    # Calculate column width for alignment
    max_src_len = max(len(r["source"]) for r in data["by_source"])
    max_cnt_len = max(len(_fmt_count(r["count"])) for r in data["by_source"])

    for row in data["by_source"]:
        src = row["source"].ljust(max_src_len)
        cnt = _fmt_count(row["count"]).rjust(max_cnt_len)
        label_str = ""
        if row["labels"]:
            label_str = f"  ({', '.join(row['labels'][:3])})"
            if len(row["labels"]) > 3:
                label_str = label_str[:-1] + f", +{len(row['labels']) - 3} more)"
        lines.append(f"  {src}  {cnt}{label_str}")

    return "\n".join(lines)


def format_detail(data: dict) -> str:
    """Render the detail dict as human-readable text."""
    lines = []
    hours = data["hours"]
    total = data["total"]

    period = f"last {hours} hour{'s' if hours != 1 else ''}"
    lines.append(f"Memories added in the {period}: {_fmt_count(total)}")
    lines.append("")

    if not data["sources"]:
        lines.append("  (none)")
        return "\n".join(lines)

    for src_data in data["sources"]:
        source = src_data["source"]
        count = src_data["count"]
        lines.append(f"{source} ({_fmt_count(count)} new):")

        for sample in src_data["samples"]:
            tag = _label_tag(sample["label"])
            snippet = _truncate(sample["text"])
            lines.append(f"  - {tag}{snippet}")

        shown = len(src_data["samples"])
        if count > shown:
            lines.append(f"  ...showing {shown} of {_fmt_count(count)}")

        lines.append("")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Show recently added memories in Nova's vector store.",
    )
    parser.add_argument(
        "--hours", type=int, default=DEFAULT_HOURS,
        help=f"Look back N hours (default: {DEFAULT_HOURS})",
    )
    parser.add_argument(
        "--source", type=str, default=None,
        help="Filter to a specific source (e.g. television, email, local_knowledge)",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="Show individual memory snippets per source",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output machine-readable JSON",
    )
    args = parser.parse_args()

    try:
        if args.detail:
            data = get_recent_detail(hours=args.hours, source=args.source)
            if args.json_output:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(format_detail(data))
        else:
            data = get_recent_summary(hours=args.hours, source=args.source)
            if args.json_output:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(format_summary(data))
    except psycopg2.OperationalError as exc:
        print(f"Error: Cannot connect to database '{DB_NAME}': {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
