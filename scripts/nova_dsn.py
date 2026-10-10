"""nova_dsn — the ops database address, in one place (added 2026-10-09).

The host is a DNS name, not an IP. It comes from NOVA_PG_HOST so a move of the primary changes one setting.
Kept separate from nova_config so that tests which stub nova_config never lose this helper.
"""
import os
import time

DEFAULT_HOST = "pg-primary.digitalnoise.net"


def pg_dsn(dbname: str = "nova_ops", extra: str = "") -> str:
    """libpq DSN for the ops database. extra: optional options such as "connect_timeout=5"."""
    host = os.environ.get("NOVA_PG_HOST", DEFAULT_HOST)
    return f"host={host} dbname={dbname} user=kochj" + (f" {extra}" if extra else "")


def pg_connect(dbname: str = "nova_ops", attempts: int = 3, _sleep=None):
    """psycopg2 connection, retried with backoff on transient failures."""
    import psycopg2
    for i in range(attempts):
        try:
            return psycopg2.connect(pg_dsn(dbname))
        except psycopg2.OperationalError:
            if i == attempts - 1:
                raise
            (_sleep or time.sleep)(2 * (i + 1))


def pg_url(dbname: str = "nova_ops") -> str:
    """postgresql:// URL for drivers that take a URL (asyncpg, SQLAlchemy)."""
    host = os.environ.get("NOVA_PG_HOST", DEFAULT_HOST)
    return f"postgresql://kochj@{host}:5432/{dbname}"


def pg_host_dbname(dbname: str = "nova_ops") -> str:
    """'host=… dbname=…' only, for callers that set their own user (e.g. the secrets service account)."""
    host = os.environ.get("NOVA_PG_HOST", DEFAULT_HOST)
    return f"host={host} dbname={dbname}"
