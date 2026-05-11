#!/usr/bin/env python3
"""
nova_media_registry.py — Shared media-file registry backed by nova_media PostgreSQL DB.

All ingest scripts import this module to register, update, and query the
processing status of media files. Provides a single source of truth across
the entire ingest pipeline.

DB: nova_media (local Unix socket, PostgreSQL 17)
Table: media_files — see schema comment below.

Written by Jordan Koch.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
# psycopg2.errors imported lazily inside register_file() to avoid import-order
# issues when pytest uses importlib mode with scripts/ on sys.path.

# ── Connection ────────────────────────────────────────────────────────────────
# Connects via the default Unix socket. If PostgreSQL is on a non-standard
# socket dir, set PGHOST to the socket directory path.

DSN = "dbname=nova_media"


@contextlib.contextmanager
def _conn():
    """Open a short-lived connection, commit on success, rollback + close always."""
    con = psycopg2.connect(DSN)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── schema (media_files)
# id SERIAL PRIMARY KEY
# path TEXT UNIQUE NOT NULL
# file_size BIGINT
# inode BIGINT
# status TEXT NOT NULL DEFAULT 'pending'
#   -- pending/ingested/trash/audio_failed/no_transcript/skipped/error/downloaded
# source_label TEXT
# show_name TEXT
# title TEXT
# memory_chunks INTEGER DEFAULT 0
# ingest_script TEXT
# ingested_at TIMESTAMPTZ
# error_msg TEXT
# notes TEXT
# created_at TIMESTAMPTZ DEFAULT NOW()
# updated_at TIMESTAMPTZ DEFAULT NOW()

# Statuses that mean "already fully processed — do not retry"
_DONE_STATUSES = frozenset(
    {"ingested", "trash", "audio_failed", "no_transcript", "skipped"}
)


# ── Public API ────────────────────────────────────────────────────────────────


def register_file(
    path: str,
    show_name: Optional[str] = None,
    title: Optional[str] = None,
    source_label: Optional[str] = None,
    ingest_script: Optional[str] = None,
) -> dict:
    """
    Upsert a media_files row with status='pending'.

    If the path already exists (any status), the existing row is returned
    unchanged — register_file never overwrites a row that has already been
    processed.

    Returns the current row as a dict.
    """
    p = Path(path)
    try:
        st = p.stat()
        file_size = int(st.st_size)
        inode     = int(st.st_ino)
    except Exception:
        file_size = None
        inode     = None

    sql_insert = """
        INSERT INTO media_files
            (path, file_size, inode, status, show_name, title, source_label, ingest_script)
        VALUES
            (%(path)s, %(file_size)s, %(inode)s, 'pending',
             %(show_name)s, %(title)s, %(source_label)s, %(ingest_script)s)
        ON CONFLICT (path) DO NOTHING
    """
    sql_select = "SELECT * FROM media_files WHERE path = %s"

    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql_insert, {
                "path":         path,
                "file_size":    file_size,
                "inode":        inode,
                "show_name":    show_name,
                "title":        title,
                "source_label": source_label,
                "ingest_script": ingest_script,
            })
        except psycopg2.IntegrityError:
            con.rollback()
        cur.execute(sql_select, (path,))
        row = cur.fetchone()
    return dict(row) if row else {}


def mark_ingested(
    path: str,
    chunks: int,
    source_label: Optional[str] = None,
) -> None:
    """
    Mark a file as successfully ingested.

    Sets status='ingested', memory_chunks=chunks, ingested_at=NOW(),
    and optionally updates source_label.
    """
    sql = """
        UPDATE media_files
        SET status        = 'ingested',
            memory_chunks = %(chunks)s,
            ingested_at   = NOW(),
            updated_at    = NOW()
            {source_clause}
        WHERE path = %(path)s
    """
    source_clause = ", source_label = %(source_label)s" if source_label is not None else ""
    sql = sql.format(source_clause=source_clause)

    with _conn() as con:
        cur = con.cursor()
        cur.execute(sql, {"path": path, "chunks": chunks, "source_label": source_label})


def mark_status(
    path: str,
    status: str,
    error_msg: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Generic status setter — use for trash/audio_failed/no_transcript/skipped/error/downloaded.

    Always updates updated_at. error_msg and notes are optional and only
    written when provided (non-None).
    """
    # Build SET clause dynamically based on what's provided
    set_parts = ["status = %(status)s", "updated_at = NOW()"]
    params: dict = {"path": path, "status": status}

    if error_msg is not None:
        set_parts.append("error_msg = %(error_msg)s")
        params["error_msg"] = error_msg

    if notes is not None:
        set_parts.append("notes = %(notes)s")
        params["notes"] = notes

    sql = f"UPDATE media_files SET {', '.join(set_parts)} WHERE path = %(path)s"

    with _conn() as con:
        cur = con.cursor()
        cur.execute(sql, params)


def is_done(path: str) -> bool:
    """
    Return True if the path has already been fully processed.

    A file is "done" if its status is one of:
      ingested, trash, audio_failed, no_transcript, skipped

    Files with status 'pending', 'error', or 'downloaded' are NOT done
    and will be retried by the ingest pipeline.
    """
    status = get_status(path)
    return status in _DONE_STATUSES


def get_status(path: str) -> Optional[str]:
    """Return the current status string for a path, or None if not registered."""
    sql = "SELECT status FROM media_files WHERE path = %s"
    with _conn() as con:
        cur = con.cursor()
        cur.execute(sql, (path,))
        row = cur.fetchone()
    return row[0] if row else None


def pending_files(
    source_label: Optional[str] = None,
    show_name: Optional[str] = None,
) -> list[str]:
    """
    Return a list of file paths with status='pending'.

    Optionally filter by source_label and/or show_name.
    Results are ordered by created_at ascending (oldest first).
    """
    conditions = ["status = 'pending'"]
    params: list = []

    if source_label is not None:
        conditions.append("source_label = %s")
        params.append(source_label)

    if show_name is not None:
        conditions.append("show_name = %s")
        params.append(show_name)

    where = " AND ".join(conditions)
    sql   = f"SELECT path FROM media_files WHERE {where} ORDER BY created_at ASC"

    with _conn() as con:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [r[0] for r in rows]


def coverage_report() -> dict:
    """
    Return a summary dict with counts broken down by status and source_label.

    Structure:
    {
        "by_status": {"pending": N, "ingested": N, ...},
        "by_source": {"military_history": {"ingested": N, ...}, ...},
        "total": N,
    }
    """
    sql_status = """
        SELECT status, COUNT(*) AS cnt
        FROM media_files
        GROUP BY status
        ORDER BY status
    """
    sql_source = """
        SELECT COALESCE(source_label, 'unknown') AS source_label, status, COUNT(*) AS cnt
        FROM media_files
        GROUP BY source_label, status
        ORDER BY source_label, status
    """

    with _conn() as con:
        cur = con.cursor()

        cur.execute(sql_status)
        by_status: dict[str, int] = {}
        for row in cur.fetchall():
            by_status[row[0]] = row[1]

        cur.execute(sql_source)
        by_source: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            src, status, cnt = row
            by_source.setdefault(src, {})[status] = cnt

    total = sum(by_status.values())
    return {"by_status": by_status, "by_source": by_source, "total": total}
