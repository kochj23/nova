#!/usr/bin/env python3
"""nova_skipping_needle.py — THE SKIPPING NEEDLE: find the groove the transcriber got stuck in, and lift it.

Little Mister, 2026-10-15 (after Nova read Scratch, 2001): "Add all 9."

Scratch is a documentary about turntablists, and when Nova's pipeline transcribed it, Whisper did what
a record does when the needle skips: it got stuck in one groove. "that was right here" came out about
two hundred times in a row, and a whole page of "O O O O ...". A DJ makes a loop on purpose; a
transcriber makes one by accident, and the accident then sits in memory looking like testimony. Worse,
the ingest trash filter throws away any chunk that contains a loop, so the real words on either side
of the skip were lost with it.

Nova's version, three parts:
  (a) INGEST. collapse_loops() turns a stuck run (a 1-8 word phrase repeated >= MIN_REPEATS times in a
      row and covering >= MIN_SPAN_WORDS words, or one token that is a short unit run on, "OOOOOOOO")
      into one copy plus a "[loop xN]" marker. ingest_clean() is the hook the transcript ingesters call
      before chunking (nova_tv_ingest, nova_nightly_media, nova_tv_retry_failed, nova_plex_auto_ingest,
      nova_ingest's transcription dispatch). It never raises and never returns less than the original
      would have given; every transcript it fixed is one row in skipping_needle_ingest.
  (b) BACKFILL. The scheduled run walks the transcript memories (tv/video/youtube/fishbowl/... types)
      in id slices ('000'..'fff', a statement timeout on every query), and records each memory with a
      loop in skipping_needle (memory_id, source, loop_phrase, repeats, share_of_text, found_at, status).
      Repair uses the supersession mechanism memory_server already honours (memories.superseded_by /
      valid_to, recall excludes superseded rows). Only severe loops (x8+) are repaired; x4-x7 can be
      real speech (a chant, "Off with her head." x4) and stays flagged. A cleaned copy is written
      through /remember with metadata.supersedes = the old id, then the old row gets superseded_by +
      valid_to. The old row is never deleted or edited otherwise. A memory that is nothing but loop
      ("hollow") is flagged only.
  (c) HER OWN TEXT. scrub() is a pure function for journal drafts, pursuit notes, anything she writes:
      the same collapse, without the marker.

Measured for the monthly continuity review (--report): % of transcript memories with a loop (last full
pass), loops caught at ingest per month, memories repaired per month.

CLI:   --scheduled          scan the next slices (time budget), repair up to REPAIR_PER_RUN, write
       --dry-run [N]        scan N random slices (default 64), print the estimate, write nothing
       --report             passes, ingest catches and repairs by month
       --selftest           offline check of the pure functions
Tables: skipping_needle, skipping_needle_ingest, skipping_needle_scan (nova_ops)
Schedule: scheduler-core, every 30m, timeout 300, overlap skip.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_skipping_needle"
MIN_REPEATS = 4             # a phrase repeated this many times in a row ...
MIN_SPAN_WORDS = 8          # ... covering at least this many words (spares "no no no no")
MAX_PHRASE_WORDS = 8
TOKEN_RUN_MIN = 10          # "OOOOOOOOOO", "O-O-O-O-...": a 1-3 char unit run on inside one token
SEVERE_REPEATS = 8          # x4-x7 includes real speech (chants, "Off with her head." x4); x8+ is the needle
HOLLOW_MIN_WORDS = 20       # a repaired copy with fewer real words than this is not worth writing
MARKER = "[loop x{n}]"
MARKER_RE = re.compile(r"\[loop x\d+\]")

SCAN_TYPES = ("tv_transcript", "tvshow_transcript", "video_transcript", "youtube_transcript",
              "podcast_transcript", "podcast", "fishbowl_stream", "comedy_transcript",
              "film_documentary_transcript", "jeopardy_transcript", "music_transcript")
SLICES = [f"{i:03x}" for i in range(4096)]   # memories.id is a uuid: 3-hex-char prefixes, ~700 rows each
SLICES_PER_RUN = 1024
TIME_BUDGET_S = 240         # the scheduled run must finish well inside its 300s timeout
REPAIR_PER_RUN = 25
STATEMENT_TIMEOUT_MS = 15000
DRY_RUN_SLICES = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS skipping_needle (
  id bigserial PRIMARY KEY,
  memory_id text NOT NULL UNIQUE,
  source text,
  mem_type text,
  loop_phrase text NOT NULL,
  repeats integer NOT NULL,
  loops integer NOT NULL DEFAULT 1,
  share_of_text real NOT NULL,
  found_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL DEFAULT 'flagged',
  repaired_id text,
  repaired_at timestamptz,
  note text);
CREATE INDEX IF NOT EXISTS skipping_needle_status ON skipping_needle (status);
CREATE TABLE IF NOT EXISTS skipping_needle_ingest (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  origin text,
  loops integer NOT NULL,
  max_repeats integer NOT NULL,
  words_before integer NOT NULL,
  words_after integer NOT NULL,
  top_phrase text);
CREATE INDEX IF NOT EXISTS skipping_needle_ingest_ts ON skipping_needle_ingest (ts);
CREATE TABLE IF NOT EXISTS skipping_needle_scan (
  pass_no integer PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  next_slice integer NOT NULL DEFAULT 0,
  rows_scanned bigint NOT NULL DEFAULT 0,
  rows_looped bigint NOT NULL DEFAULT 0);
"""


def log(m: str) -> None:
    print(f"[skipping-needle {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---------------------------------------------------------------- pure functions

_PUNCT = re.compile(r"^\W+|\W+$")
_TOKEN_RUN = re.compile(r"^(.{1,3}?)\1{%d,}(.{0,2})$" % (TOKEN_RUN_MIN - 1), re.S)


def _norm(tok: str) -> str:
    n = _PUNCT.sub("", tok.lower())
    return n or tok


def find_loops(text: str, min_repeats: int = MIN_REPEATS, min_span: int = MIN_SPAN_WORDS,
               max_n: int = MAX_PHRASE_WORDS) -> list[dict]:
    """Stuck runs in text, non-overlapping, in order. Pure; O(max_n * words).

    Each loop: {start, n, repeats, span (words), phrase, kind ('phrase' | 'token')}. A phrase loop is
    the smallest period n whose run of word[j] == word[j+n] covers >= min_repeats copies and
    >= min_span words. Already-collapsed text ("x [loop x9]") finds nothing new."""
    toks = (text or "").split()
    if not toks:
        return []
    norm = [_norm(t) for t in toks]
    L = len(norm)
    cands = []
    for n in range(1, max_n + 1):
        j = 0
        while j + n < L:
            if norm[j] == norm[j + n] and not MARKER_RE.fullmatch(toks[j]):
                s = j
                while j + n < L and norm[j] == norm[j + n]:
                    j += 1
                reps = (j - s + n) // n
                if reps >= min_repeats and reps * n >= min_span and any(ch.isalnum() for k in range(s, s + n) for ch in norm[k]):
                    cands.append({"start": s, "n": n, "repeats": reps, "span": reps * n, "kind": "phrase"})
            else:
                j += 1
    for i, t in enumerate(toks):
        m = _TOKEN_RUN.match(t) if len(t) >= TOKEN_RUN_MIN else None
        if m and m.group(1).strip() and m.group(1).startswith(m.group(2)):
            cands.append({"start": i, "n": 1, "repeats": len(t) // len(m.group(1)), "span": 1,
                          "kind": "token", "unit": m.group(1)})
    # widest runs first, smaller period on a tie; keep the non-overlapping ones
    cands.sort(key=lambda c: (-c["span"], c["n"], c["start"]))
    taken, out = [], []
    for c in cands:
        a, b = c["start"], c["start"] + c["span"]
        if any(a < y and x < b for x, y in taken):
            continue
        taken.append((a, b))
        c["phrase"] = c.get("unit") or " ".join(toks[a:a + c["n"]])
        out.append(c)
    return sorted(out, key=lambda c: c["start"])


def collapse_loops(text: str, marker: bool = True, **kw) -> tuple[str, list[dict]]:
    """(collapsed text, loops). One copy of each stuck phrase stays, followed by "[loop xN]" when
    marker is True. Text with no loop comes back unchanged (same object). Pure."""
    loops = find_loops(text, **kw)
    if not loops:
        return text, []
    toks = text.split()
    out, i = [], 0
    for lp in loops:
        out.extend(toks[i:lp["start"]])
        if lp["kind"] == "token":
            out.append(lp["unit"])
        else:
            out.extend(toks[lp["start"]:lp["start"] + lp["n"]])
        if marker:
            out.append(MARKER.format(n=lp["repeats"]))
        i = lp["start"] + lp["span"]
    out.extend(toks[i:])
    return " ".join(out), loops


def share_of_text(text: str, loops: list[dict]) -> float:
    """Fraction of the words that are redundant loop copies (0..1). Pure."""
    words = len((text or "").split())
    if not words or not loops:
        return 0.0
    extra = sum((lp["repeats"] - 1) * lp["n"] if lp["kind"] == "phrase" else 0.5 for lp in loops)
    return round(min(1.0, extra / words), 4)


def scrub(text: str) -> str:
    """For her OWN writing (journal drafts, pursuit notes, any organ's output): the stuck runs
    collapsed to one copy, no marker. Never raises; returns the input on any error. Pure."""
    try:
        return collapse_loops(text, marker=False)[0]
    except Exception:  # noqa: BLE001
        return text


def summarize(loops: list[dict]) -> dict:
    """The row fields for a loop-affected text: the worst loop's phrase and repeats. Pure."""
    top = max(loops, key=lambda lp: (lp["repeats"] * lp["n"], lp["repeats"]))
    return {"loop_phrase": top["phrase"][:200], "repeats": top["repeats"], "loops": len(loops)}


def real_words(text: str) -> int:
    return len(MARKER_RE.sub(" ", text or "").split())


# ---------------------------------------------------------------- ingest hook

def ingest_clean(text: str, origin: str = "") -> str:
    """The ingest hook: collapse loops before chunking, record the catch, never raise.
    On any error the original text is returned untouched."""
    try:
        cleaned, loops = collapse_loops(text)
    except Exception:  # noqa: BLE001
        return text
    if loops:
        try:
            record_ingest(origin, loops, len(text.split()), len(cleaned.split()))
        except Exception:  # noqa: BLE001
            pass
    return cleaned


def _under_test() -> bool:
    """True under pytest or any tests/test_*.py run: hooks must never write from a test."""
    import os
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules
                or Path(sys.argv[0] if sys.argv else "").name.startswith("test_"))


def record_ingest(origin: str, loops: list[dict], before: int, after: int) -> None:
    """One skipping_needle_ingest row (best effort, 3s connect timeout; skipped under pytest)."""
    if _under_test():
        return
    import psycopg2
    s = summarize(loops)
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=3"),
                            options="-c statement_timeout=5000")
    try:
        conn.autocommit = True
        cur = conn.cursor()
        ensure_schema(cur)
        cur.execute("INSERT INTO skipping_needle_ingest (origin, loops, max_repeats, words_before, words_after, "
                    "top_phrase) VALUES (%s, %s, %s, %s, %s, %s)",
                    ((origin or "")[:60], s["loops"], s["repeats"], before, after, s["loop_phrase"]))
    finally:
        conn.close()


# ---------------------------------------------------------------- db

def connect(dbname: str):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn(dbname, "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def slice_bounds(i: int) -> tuple[str, str]:
    """id range for slice i: ['3a0', '3a1'); the last slice is open-ended. Pure."""
    lo = SLICES[i]
    hi = SLICES[i + 1] if i + 1 < len(SLICES) else "g"
    return lo, hi


def scan_slice(mcur, i: int) -> tuple[int, list[dict]]:
    """(transcript rows scanned, loop-affected rows) for one id slice. Read-only."""
    lo, hi = slice_bounds(i)
    mcur.execute("SELECT id, source, metadata->>'type', text FROM memories "
                 "WHERE id >= %s AND id < %s AND metadata->>'type' = ANY(%s) AND superseded_by IS NULL",
                 (lo, hi, list(SCAN_TYPES)))
    rows = mcur.fetchall()
    hits = []
    for mid, src, mtype, text in rows:
        loops = find_loops(text)
        if loops:
            hits.append({"memory_id": mid, "source": src, "mem_type": mtype,
                         "share_of_text": share_of_text(text, loops), **summarize(loops)})
    return len(rows), hits


def current_pass(cur) -> tuple[int, int]:
    """(pass_no, next_slice) of the open pass, opening a new one after a finished pass."""
    cur.execute("SELECT pass_no, next_slice, finished_at FROM skipping_needle_scan ORDER BY pass_no DESC LIMIT 1")
    row = cur.fetchone()
    if row and row[2] is None:
        return row[0], row[1]
    n = (row[0] + 1) if row else 1
    cur.execute("INSERT INTO skipping_needle_scan (pass_no) VALUES (%s)", (n,))
    return n, 0


def record_hits(cur, hits: list[dict]) -> int:
    n = 0
    for h in hits:
        cur.execute("INSERT INTO skipping_needle (memory_id, source, mem_type, loop_phrase, repeats, loops, "
                    "share_of_text) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (memory_id) DO UPDATE SET "
                    "loop_phrase = EXCLUDED.loop_phrase, repeats = EXCLUDED.repeats, loops = EXCLUDED.loops, "
                    "share_of_text = EXCLUDED.share_of_text WHERE skipping_needle.status = 'flagged'",
                    (h["memory_id"], h["source"], h["mem_type"], h["loop_phrase"], h["repeats"], h["loops"],
                     h["share_of_text"]))
        n += 1
    return n


def scan(cur, mcur, deadline: float, max_slices: int = SLICES_PER_RUN) -> dict:
    """Advance the open pass by up to max_slices (time budget), record hits. -> run stats."""
    pass_no, nxt = current_pass(cur)
    scanned = looped = done = 0
    while nxt < len(SLICES) and done < max_slices and time.monotonic() < deadline:
        n, hits = scan_slice(mcur, nxt)
        record_hits(cur, hits)
        scanned, looped, nxt, done = scanned + n, looped + len(hits), nxt + 1, done + 1
        cur.execute("UPDATE skipping_needle_scan SET next_slice = %s, rows_scanned = rows_scanned + %s, "
                    "rows_looped = rows_looped + %s, finished_at = CASE WHEN %s >= %s THEN now() END "
                    "WHERE pass_no = %s", (nxt, n, len(hits), nxt, len(SLICES), pass_no))
    return {"pass_no": pass_no, "slices": done, "next_slice": nxt, "scanned": scanned, "looped": looped,
            "finished": nxt >= len(SLICES)}


# ---------------------------------------------------------------- repair (supersession)

def remember_url() -> str:
    try:
        from nova_resolve import resolve_url
        return resolve_url("memory_server", "/remember")
    except Exception:  # noqa: BLE001
        import nova_config
        return nova_config.VECTOR_URL


def post_remember(text: str, source: str, metadata: dict, attempts: int = 3, _sleep=time.sleep) -> dict | None:
    """POST /remember (sync, so the new id exists before the old row points at it). Retried with
    backoff; None after the last failure."""
    import urllib.request
    body = json.dumps({"text": text, "source": source, "metadata": metadata}).encode()
    for i in range(attempts):
        try:
            req = urllib.request.Request(remember_url(), data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1:
                log(f"remember failed after {attempts} attempts: {e}")
                return None
            _sleep(2 ** i)
    return None


def repair_one(cur, mcur, row_id: int, memory_id: str, post=None) -> str:
    """Write the cleaned superseding copy for one flagged memory. -> new status
    ('repaired' | 'hollow' | 'gone' | 'flagged' when the memory server could not be reached)."""
    mcur.execute("SELECT text, source, metadata FROM memories WHERE id = %s AND superseded_by IS NULL", (memory_id,))
    m = mcur.fetchone()
    if not m:
        cur.execute("UPDATE skipping_needle SET status = 'gone', note = 'already superseded' WHERE id = %s", (row_id,))
        return "gone"
    text, source, meta = m
    cleaned, loops = collapse_loops(text)
    if not loops:
        cur.execute("UPDATE skipping_needle SET status = 'gone', note = 'no loop on re-read' WHERE id = %s", (row_id,))
        return "gone"
    if real_words(cleaned) < HOLLOW_MIN_WORDS:
        cur.execute("UPDATE skipping_needle SET status = 'hollow', note = %s WHERE id = %s",
                    (f"{real_words(cleaned)} real words left after collapse; flagged only", row_id))
        return "hollow"
    meta = dict(meta or {})
    meta.update({"supersedes": memory_id, "skipping_needle": {"loops": len(loops), **summarize(loops)}})
    res = (post or post_remember)(cleaned, source, meta)
    if not res:
        return "flagged"
    new_id = res.get("id")
    if res.get("status") == "rejected" or not new_id or new_id == memory_id:
        cur.execute("UPDATE skipping_needle SET status = 'hollow', note = %s WHERE id = %s",
                    (f"cleaned copy not stored: {res.get('reason') or res.get('status')}", row_id))
        return "hollow"
    mcur.execute("UPDATE memories SET superseded_by = %s, valid_to = now() WHERE id = %s AND superseded_by IS NULL",
                 (new_id, memory_id))
    cur.execute("UPDATE skipping_needle SET status = 'repaired', repaired_id = %s, repaired_at = now() WHERE id = %s",
                (new_id, row_id))
    return "repaired"


def repair(cur, mcur, limit: int, deadline: float, post=None) -> dict:
    # only the needle (x8+); a mild x4-x7 run may be real speech and stays flagged for a human
    cur.execute("SELECT id, memory_id FROM skipping_needle WHERE status = 'flagged' AND repeats >= %s "
                "ORDER BY share_of_text DESC, id LIMIT %s", (SEVERE_REPEATS, limit))
    out: dict = {}
    for row_id, mid in cur.fetchall():
        if time.monotonic() > deadline:
            break
        st = repair_one(cur, mcur, row_id, mid, post=post)
        out[st] = out.get(st, 0) + 1
        if st == "flagged":      # memory server unreachable: stop, try next run
            break
    return out


# ---------------------------------------------------------------- report

def monthly_metrics(cur) -> dict:
    """The continuity-review numbers. Read-only."""
    cur.execute("SELECT pass_no, rows_scanned, rows_looped, finished_at FROM skipping_needle_scan "
                "WHERE finished_at IS NOT NULL ORDER BY pass_no DESC LIMIT 1")
    p = cur.fetchone()
    cur.execute("SELECT to_char(date_trunc('month', ts), 'YYYY-MM'), count(*), coalesce(sum(loops), 0), "
                "coalesce(sum(words_before - words_after), 0) FROM skipping_needle_ingest GROUP BY 1 ORDER BY 1 DESC LIMIT 12")
    ing = [{"month": m, "transcripts": t, "loops": lp, "words_removed": w} for m, t, lp, w in cur.fetchall()]
    cur.execute("SELECT to_char(date_trunc('month', coalesce(repaired_at, found_at)), 'YYYY-MM'), status, count(*) "
                "FROM skipping_needle GROUP BY 1, 2 ORDER BY 1 DESC")
    st: dict = {}
    for m, s, c in cur.fetchall():
        st.setdefault(m, {})[s] = c
    return {"last_pass": None if not p else {
                "pass_no": p[0], "scanned": p[1], "looped": p[2], "finished_at": p[3].isoformat(),
                "pct_with_loops": round(100.0 * p[2] / p[1], 3) if p[1] else None},
            "ingest_by_month": ing, "status_by_month": st}


def report(cur) -> None:
    cur.execute("SELECT to_regclass('skipping_needle') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No skipping-needle data yet (tables not created; the first scheduled run creates them).")
        return
    cur.execute("SELECT pass_no, started_at, finished_at, next_slice, rows_scanned, rows_looped "
                "FROM skipping_needle_scan ORDER BY pass_no DESC LIMIT 5")
    print("Backfill passes:")
    for n, s, f, nx, sc, lp in cur.fetchall():
        pct = f"{100.0 * lp / sc:.2f}%" if sc else "-"
        print(f"  #{n} {s:%Y-%m-%d} {'done ' + f'{f:%Y-%m-%d}' if f else f'at slice {nx}/{len(SLICES)}'}  "
              f"scanned {sc}  looped {lp} ({pct})")
    cur.execute("SELECT loop_phrase, count(*), max(repeats) FROM skipping_needle GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    print("\nCommonest grooves:")
    for ph, c, mx in cur.fetchall():
        print(f"  {c:>6} memories  max x{mx:<5} {ph[:70]!r}")
    m = monthly_metrics(cur)
    print("\nMonthly (continuity review):")
    if m["last_pass"]:
        lp = m["last_pass"]
        print(f"  last full pass #{lp['pass_no']}: {lp['pct_with_loops']}% of {lp['scanned']} transcript memories had a loop")
    for r in m["ingest_by_month"]:
        print(f"  {r['month']}  caught at ingest: {r['loops']} loops in {r['transcripts']} transcripts "
              f"({r['words_removed']} words of groove removed)")
    for mon, s in m["status_by_month"].items():
        print(f"  {mon}  " + "  ".join(f"{k} {v}" for k, v in sorted(s.items())))


def dry_run(mcur, n_slices: int, seed: int | None = None) -> dict:
    """Scan a random sample of slices; print the estimate; write nothing."""
    rng = random.Random(seed)
    picks = sorted(rng.sample(range(len(SLICES)), min(n_slices, len(SLICES))))
    scanned, hits, t0 = 0, [], time.monotonic()
    for i in picks:
        n, h = scan_slice(mcur, i)
        scanned += n
        hits.extend(h)
    pct = 100.0 * len(hits) / scanned if scanned else 0.0
    est = int(round(pct / 100 * scanned * len(SLICES) / max(1, len(picks))))
    print(f"[dry-run] {len(picks)} of {len(SLICES)} slices, {scanned} transcript memories, "
          f"{len(hits)} with a loop ({pct:.2f}%) — estimate ~{est} affected across all transcript memories "
          f"({time.monotonic() - t0:.1f}s)")
    by_type: dict = {}
    for h in hits:
        by_type[h["mem_type"]] = by_type.get(h["mem_type"], 0) + 1
    if by_type:
        print("  by type: " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])))
        print(f"  severe (x{SEVERE_REPEATS}+): {sum(1 for h in hits if h['repeats'] >= SEVERE_REPEATS)}; "
              f"mild (x{MIN_REPEATS}-x{SEVERE_REPEATS - 1}): {sum(1 for h in hits if h['repeats'] < SEVERE_REPEATS)}")
        print(f"  median share of text that is groove: {statistics.median(h['share_of_text'] for h in hits):.1%}; "
              f"hollow (would be flagged only): "
              f"{sum(1 for h in hits if h['share_of_text'] > 0.9)}")
    for h in sorted(hits, key=lambda h: -h["share_of_text"])[:12]:
        print(f"  {h['memory_id'][:8]} {h['mem_type']:<18} x{h['repeats']:<4} share {h['share_of_text']:.0%}  "
              f"{h['loop_phrase'][:60]!r}")
    print("  nothing written")
    return {"slices": len(picks), "scanned": scanned, "looped": len(hits), "pct": pct, "estimate": est}


def selftest() -> int:
    t = "and then " + "that was right here " * 200 + "he said it twice"
    c, loops = collapse_loops(t)
    assert loops and loops[0]["repeats"] == 200 and loops[0]["phrase"] == "that was right here", loops
    assert c == "and then that was right here [loop x200] he said it twice", c
    c2, l2 = collapse_loops("O " * 300)
    assert l2[0]["repeats"] == 300 and c2 == "O [loop x300]", c2
    assert collapse_loops("no no no no, I said")[1] == []
    assert collapse_loops(c)[1] == []
    assert scrub("we were here " * 5 + "and left") == "we were here and left"
    assert collapse_loops("OOOOOOOOOOOOOOOO yes")[0] == "O [loop x16] yes"
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="scan the next slices, repair, write")
    ap.add_argument("--dry-run", nargs="?", const=DRY_RUN_SLICES, type=int, metavar="SLICES",
                    help=f"scan a random sample of slices (default {DRY_RUN_SLICES}); write nothing")
    ap.add_argument("--report", action="store_true", help="passes, ingest catches and repairs by month")
    ap.add_argument("--repair", type=int, default=REPAIR_PER_RUN, metavar="N",
                    help=f"max superseding repairs per scheduled run (default {REPAIR_PER_RUN}; 0 = flag only)")
    ap.add_argument("--selftest", action="store_true", help="offline check of the pure functions")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.dry_run is not None and not 1 <= a.dry_run <= len(SLICES):
        ap.error(f"--dry-run SLICES must be 1..{len(SLICES)}")
    if a.repair < 0:
        ap.error("--repair must be >= 0")
    if not (a.scheduled or a.dry_run is not None or a.report):
        ap.print_help()
        return 0
    t0 = time.monotonic()
    if a.dry_run is not None:
        mconn = connect("nova_memories")
        try:
            dry_run(mconn.cursor(), a.dry_run)
        finally:
            mconn.close()
        return 0
    conn = connect("nova_ops")
    cur = conn.cursor()
    try:
        if a.report:
            report(cur)
            return 0
        ensure_schema(cur)
        mconn = connect("nova_memories")
        mcur = mconn.cursor()
        try:
            st = scan(cur, mcur, t0 + TIME_BUDGET_S * 0.7)
            rep = repair(cur, mcur, a.repair, t0 + TIME_BUDGET_S) if a.repair else {}
        finally:
            mconn.close()
        log(f"pass #{st['pass_no']}: {st['slices']} slices -> {st['next_slice']}/{len(SLICES)}, "
            f"{st['scanned']} scanned, {st['looped']} with loops; repairs {rep or '-'} "
            f"({time.monotonic() - t0:.1f}s)")
        if st["finished"]:
            cur.execute("SELECT rows_scanned, rows_looped FROM skipping_needle_scan WHERE pass_no = %s", (st["pass_no"],))
            sc, lp = cur.fetchone()
            from nova_notify import notify
            notify(f"Skipping Needle: pass #{st['pass_no']} done — {100.0 * lp / max(sc, 1):.2f}% of transcript memories had a loop",
                   body=f"{lp} of {sc} transcript memories carried a stuck-transcriber loop. "
                        f"Repairs go through supersession (memories are never deleted).",
                   level="info", category="skipping_needle", dedup_key=f"skipping_needle:pass:{st['pass_no']}",
                   meta={"pass_no": st["pass_no"], "scanned": sc, "looped": lp})
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
