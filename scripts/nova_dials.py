#!/usr/bin/env python3
"""
nova_dials.py — see and set Nova's TARS-style dials (humor, snark, proactivity, bluntness,
profanity, verbosity). Values live in nova_ops.service_config (service='nova_dials', one row
per dial); nova_voice renders them into every system prompt (cached 60s).

  nova_dials.py show
  nova_dials.py set humor 60
  nova_dials.py set profanity off
  nova_dials.py reset [dial]      # drop the row(s) -> back to the default

Written by Jordan Koch.
"""
import json
import sys

import nova_voice

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SERVICE = "nova_dials"


def _connect():
    import psycopg2
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    conn.autocommit = True
    return conn


def show(vals=None) -> str:
    vals = vals if vals is not None else nova_voice.dials(refresh=True)
    lines = []
    for k, d in nova_voice.DIAL_DEFAULTS.items():
        v = vals[k]
        shown = ("on" if v else "off") if isinstance(d, bool) else f"{v:>3}/100"
        mark = "" if v == d else f"   (default {('on' if d else 'off') if isinstance(d, bool) else d})"
        lines.append(f"{k:<12} {shown}{mark}")
    return "\n".join(lines)


def parse_set(name, raw):
    """-> validated value or raises ValueError."""
    if name not in nova_voice.DIAL_DEFAULTS:
        raise ValueError(f"unknown dial {name!r}; dials: {', '.join(nova_voice.DIAL_DEFAULTS)}")
    if isinstance(nova_voice.DIAL_DEFAULTS[name], bool):
        r = str(raw).strip().lower()
        if r not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            raise ValueError(f"{name} is on/off")
        return r in ("on", "true", "1", "yes")
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{name} takes an integer 0-100") from None
    if not 0 <= v <= 100:
        raise ValueError(f"{name} takes an integer 0-100")
    return v


def set_dial(name, value, by="nova_dials.py"):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO service_config (service, key, value, updated_at, updated_by) "
                    "VALUES (%s, %s, %s::jsonb, now(), %s) ON CONFLICT (service, key) DO UPDATE "
                    "SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                    (SERVICE, name, json.dumps(value), by))


def reset(name=None):
    with _connect() as conn, conn.cursor() as cur:
        if name:
            cur.execute("DELETE FROM service_config WHERE service = %s AND key = %s", (SERVICE, name))
        else:
            cur.execute("DELETE FROM service_config WHERE service = %s", (SERVICE,))


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] == "show":
        print(show()); return 0
    if a[0] == "set" and len(a) == 3:
        try:
            v = parse_set(a[1], a[2])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr); return 2
        set_dial(a[1], v)
        print(show()); return 0
    if a[0] == "reset" and len(a) <= 2:
        if len(a) == 2 and a[1] not in nova_voice.DIAL_DEFAULTS:
            print(f"error: unknown dial {a[1]!r}", file=sys.stderr); return 2
        reset(a[1] if len(a) == 2 else None)
        print(show()); return 0
    if a[0] == "--selftest":
        assert parse_set("humor", "60") == 60 and parse_set("profanity", "off") is False
        for bad in (("humor", "101"), ("nope", "1"), ("profanity", "maybe")):
            try:
                parse_set(*bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        assert "humor" in show(dict(nova_voice.DIAL_DEFAULTS))
        print("selftest ok"); return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
