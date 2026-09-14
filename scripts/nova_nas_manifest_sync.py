#!/usr/bin/env python3
"""nova_nas_manifest_sync.py — FAST Synology -> UNAS backup replication by
manifest diff (kills rsync's 3+ hour whole-tree scan).

Design #656 §7 (LOCKED). Instead of letting rsync stat every file on both 3M+
file volumes before moving a byte, we:

  1. SSH into EACH box and run `find` LOCALLY (Synology source + UNAS dest, in
     parallel) building a path+size manifest per side. No CIFS/SMB tree-walk.
  2. Stream each manifest into Postgres (COPY, not row-by-row) and keep the
     CURRENT manifest per (box,share) in telemetry.backup_manifest.
  3. Diff in SQL: source rows missing/size-differ on dest => COPY;
     dest rows absent from source => ORPHAN (quarantine, never rm).
  4. Transport ONLY the delta via `rsync --files-from` (no tree walk), pulled
     from the Synology over ssh into the local UNAS mount on .6.
  5. Quarantine orphans on the UNAS via ssh-local `mv` into a dated .nova-trash
     dir (14-day retention), guarded by hard delete-threshold + source-sanity
     gates so a dropped Synology mount can NEVER wipe the 3.26M-file UNAS.

State lives in Postgres nova_ops (NOT sqlite / flat files). Every run records a
row to telemetry.backup_runs so the dead-man's-switch / Grafana see it.

TOPOLOGY (verified live 2026-08-04 — see AS-BUILT note in agent_docs #656):
  * The REAL Synology source trees are /volume1/nas and /volume1/external.
    NOTE: /volume1/docker/{nas,external} on the Synology are CIFS RE-MOUNTS of
    the UNAS (//192.168.1.69/...), i.e. the DEST, not the source — using them as
    the source would diff the UNAS against itself (a no-op / self-wipe risk).
  * UNAS dest content lives under <share>/.data/ beneath the unifi-drive root,
    resolved dynamically via the glob /volume/*/.srv/.unifi-drive (the UUID
    changes on volume rebuild — never hardcode it). External is capital-E.
  * The UNAS share is mounted on .6 at /Volumes/{nas,external}-1; its root maps
    to the UNAS <share>/.data/ so relative paths line up with the Synology
    source. rsync is orchestrated on .6 (Synology-over-ssh source -> local mount
    dest) so no new box-to-box trust is needed.

Written by Jordan Koch (via Claude Opus 4.8).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── connection / host constants ──────────────────────────────────────────────
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SYNO_HOST = "kochj@192.168.1.11"          # source of truth
UNAS_HOST = "root@192.168.1.69"           # backup target (local find + quarantine)
UNAS_GLOB = "/volume/*/.srv/.unifi-drive"  # resolves to the unifi-drive root (UUID varies)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new"]

MANIFEST_TABLE = "telemetry.backup_manifest"  # overridable in tests
RUNS_TABLE = "telemetry.backup_runs"

# name, Synology local source, UNAS dest sub-path under the unifi-drive root,
# local mount on .6 used as the rsync dest.
SHARES = [
    {"name": "nas",      "src": "/volume1/nas",      "dest_sub": "nas/.data",      "mount": "/Volumes/nas-1",      "syno_dest": "/volume1/docker/nas"},
    {"name": "external", "src": "/volume1/external", "dest_sub": "External/.data", "mount": "/Volumes/external-1", "syno_dest": "/volume1/docker/external"},
]

# Metadata that must never be copied, diffed, orphaned, or quarantined.
EXCLUDE_SEGMENTS = {
    "@eaDir", "#recycle", "#snapshot", ".Spotlight-V100", ".Trashes",
    "@tmp", ".nova-trash", ".DS_Store",
}

# Safety defaults (all overridable on the CLI).
DEFAULT_MAX_DELETE_PCT = 5.0     # abort delete if orphans > 5% of dest count …
DEFAULT_MAX_DELETE_ABS = 500     # … or > 500 files (whichever is larger)
DEFAULT_MIN_SRC_RATIO = 0.5      # abort if source < 50% of previous source count
TRASH_RETENTION_DAYS = 14


def log(msg: str) -> None:
    print(f"[manifest_sync {datetime.now():%H:%M:%S}] {msg}", flush=True)


def alert(subject: str, body: str, critical: bool = False) -> None:
    """Post to #nova-warning (or #nova-critical) — never fail silently."""
    try:
        import nova_config
        chan = nova_config.SLACK_BB if critical else nova_config.SLACK_NOTIFY
        nova_config.post_both(f"*{subject}*\n{body}", slack_channel=chan)
    except Exception as e:  # noqa: BLE001 — alerting must never crash the run
        log(f"alert post failed: {e}")
    try:
        from nova_notify import notify
        notify(subject, body=body, level="critical" if critical else "warning",
               category="backup", dedup_key=f"manifest-sync-{subject[:40]}")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers (unit-tested, no I/O)
# ══════════════════════════════════════════════════════════════════════════════

def is_excluded(rel: str) -> bool:
    """True if any path segment is Synology/UNAS metadata that must be ignored."""
    return any(seg in EXCLUDE_SEGMENTS for seg in rel.split("/"))


def parse_manifest_line(line: str):
    """Parse one `find -printf '%P\\t%s\\n'` line -> (path, size:int) or None.

    Splits on the LAST tab so paths containing tabs are still bounded; a line
    whose trailing field isn't an int is treated as unparseable (skipped)."""
    line = line.rstrip("\n")
    if not line:
        return None
    rel, tab, size = line.rpartition("\t")
    if not tab or not rel:
        return None
    try:
        return rel, int(size)
    except ValueError:
        return None


def load_manifest(lines) -> dict:
    """Build {relpath: size} from find output, dropping excluded/unparseable."""
    out: dict[str, int] = {}
    for line in lines:
        parsed = parse_manifest_line(line)
        if parsed is None:
            continue
        rel, size = parsed
        if is_excluded(rel):
            continue
        out[rel] = size
    return out


def diff_manifests(src: dict, dst: dict):
    """Set-based diff (O(n), never O(n^2)).

    to_copy = source path missing on dest OR size differs -> [(path, size)]
    orphans = dest path absent from source           -> [(path, size)]"""
    to_copy = [(p, s) for p, s in src.items() if dst.get(p) != s]
    orphans = [(p, dst[p]) for p in dst if p not in src]
    return to_copy, orphans


def parse_uuid_root(text: str):
    """Pick the unifi-drive root from `for d in /volume/*/...; echo $d` output.
    Returns None if the glob didn't expand (still literal '*') or nothing valid."""
    for line in text.splitlines():
        d = line.strip()
        if d and "*" not in d and d.startswith("/volume/") and d.endswith("/.srv/.unifi-drive"):
            return d
    return None


def safe_relpath(rel: str) -> bool:
    """Quarantine/transport guard: reject absolute paths, `..` traversal, NUL."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return False
    return ".." not in rel.split("/")


def evaluate_guards(nsrc: int, ndst: int, n_orphans: int, prev_src,
                    max_pct: float, max_abs: int, min_ratio: float):
    """Decide what is safe to do this run. Returns (allow_copy, allow_delete, reasons)."""
    reasons: list[str] = []
    allow_copy = True
    allow_delete = True

    # Source-sanity: an empty or implausibly-small source means the Synology
    # mount likely dropped — copying nothing is fine, but DELETING would wipe the
    # UNAS. Abort the whole share.
    if nsrc == 0:
        reasons.append("source manifest EMPTY — aborting share (mount dropped?)")
        return False, False, reasons
    if prev_src and nsrc < int(prev_src * min_ratio):
        reasons.append(
            f"source shrank implausibly: {nsrc:,} < {min_ratio:.0%} of previous {prev_src:,}"
            " — aborting share (possible dropped mount)")
        return False, False, reasons

    # Delete-threshold: too many orphans => refuse deletion (copy still safe).
    threshold = max(max_abs, int(ndst * max_pct / 100.0))
    if n_orphans > threshold:
        allow_delete = False
        reasons.append(
            f"orphan set {n_orphans:,} exceeds delete guard "
            f"(> max({max_abs}, {max_pct:.0f}% of {ndst:,} = {threshold:,})) — skipping quarantine")

    return allow_copy, allow_delete, reasons


def quarantine_dest(trash_root: str, share: str, day: str, rel: str) -> str:
    """Compute the quarantine destination for an orphan, kept inside the trash dir."""
    if not safe_relpath(rel):
        raise ValueError(f"unsafe relpath refused: {rel!r}")
    base = f"{trash_root}/{share}/{day}"
    dest = os.path.normpath(f"{base}/{rel}")
    if not (dest == base or dest.startswith(base + "/")):
        raise ValueError(f"quarantine path escapes trash dir: {rel!r}")
    return dest


# ══════════════════════════════════════════════════════════════════════════════
# I/O helpers (ssh / rsync / pg) — retryable
# ══════════════════════════════════════════════════════════════════════════════

def run_with_retry(argv, timeout, retries=3, backoff=5, ok_rc=(0,)):
    """Run argv; retry on nonzero/exception with linear backoff. Returns
    CompletedProcess on success, raises RuntimeError after the last attempt."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if r.returncode in ok_rc:
                return r
            last = f"rc={r.returncode} {r.stderr.strip()[:160]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        if attempt < retries:
            log(f"  attempt {attempt}/{retries} failed ({last}); retrying in {backoff * attempt}s")
            time.sleep(backoff * attempt)
    raise RuntimeError(f"command failed after {retries} attempts: {last}")


def ssh_find(host: str, path: str, outfile: str, retries=3, timeout=2400) -> int:
    """Stream `find <path> -type f -printf '%s\\0%P\\0'` on <host> to outfile.

    NUL-DELIMITED on purpose: NUL is the one byte a filename can NOT contain, so
    records survive filenames with embedded tabs/newlines/quotes (a recurring
    fleet bug — the old %P\\t%s\\n format split such names into bogus rows and
    collided on the manifest PK). Each record is `<size>\\0<path>\\0`.

    find exits nonzero on benign permission warnings even when it lists
    everything, so success is gauged by OUTPUT SIZE, not rc. Retries on empty
    output. Returns bytes written. The remote command quotes <path>; the outer
    ssh call is an argv list (no shell) so no injection is possible."""
    remote = f"cd {shquote(path)} && find . -type f -printf '%s\\0%P\\0'"
    last = ""
    for attempt in range(1, retries + 1):
        with open(outfile, "wb") as f:
            try:
                subprocess.run(["ssh", *SSH_OPTS, host, remote],
                               stdout=f, stderr=subprocess.DEVNULL, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                last = str(e)
        size = os.path.getsize(outfile)
        if size > 0:
            return size
        last = last or "empty find output"
        if attempt < retries:
            log(f"  {host}:{path} find empty (attempt {attempt}/{retries}); retrying")
            time.sleep(5 * attempt)
    raise RuntimeError(f"find produced no output for {host}:{path} — {last}")


def shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def clean_to_file(raw_file: str, cleaned_file: str) -> dict:
    """Parse NUL-delimited `<size>\\0<path>\\0` find output, drop excluded/
    unparseable, write a CSV `(path,size)` for COPY, AND return {path: size}.

    CSV (not tab-delimited) is used for the COPY wire format so a path containing
    a tab/newline/comma/quote is safely quoted instead of corrupting the load —
    the same weird-filename class the NUL find format guards on the read side."""
    import csv
    manifest: dict[str, int] = {}
    with open(raw_file, "rb") as src:
        fields = src.read().split(b"\x00")
    with open(cleaned_file, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        # records are (size, path) pairs; a trailing empty field follows the last NUL
        for i in range(0, len(fields) - 1, 2):
            size_b, path_b = fields[i], fields[i + 1]
            if not path_b:
                continue
            try:
                size = int(size_b)
            except ValueError:
                continue
            rel = path_b.decode("utf-8", "replace")
            if is_excluded(rel):
                continue
            w.writerow([rel, size])
            manifest[rel] = size          # last-wins on a dup path (matches DISTINCT ON intent)
    return manifest


def pg_replace_manifest(conn, box: str, share: str, cleaned_file: str) -> int:
    """COPY cleaned manifest into a temp staging table, then replace the current
    (box,share) rows in MANIFEST_TABLE. Returns previous source-row count for
    box='syno' (the sanity baseline) — 0 for others. Caller controls commit."""
    import zlib
    prev = 0
    with conn.cursor() as cur:
        # Serialize concurrent loads of the SAME (box,share) so two runs can't race
        # the DELETE+INSERT into a PK violation. crc32 gives a STABLE key across
        # processes (Python's hash() is randomized per-process and would NOT match).
        # Held until the caller's commit (xact lock).
        lock_key = zlib.crc32(f"{box}:{share}".encode()) - 2**31
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        cur.execute(f"SELECT count(*) FROM {MANIFEST_TABLE} WHERE box=%s AND share=%s",
                    (box, share))
        prev = cur.fetchone()[0]
        cur.execute("CREATE TEMP TABLE _stg (path text, size bigint) ON COMMIT DROP")
        with open(cleaned_file, encoding="utf-8", newline="") as f:
            cur.copy_expert("COPY _stg (path, size) FROM STDIN WITH (FORMAT csv)", f)
        cur.execute(f"DELETE FROM {MANIFEST_TABLE} WHERE box=%s AND share=%s", (box, share))
        # Belt-and-suspenders: DISTINCT ON collapses any duplicate path (prefer the
        # larger size so an ambiguous dup errs toward "copy", never a missed change),
        # and ON CONFLICT makes the insert idempotent even under an unexpected dup.
        cur.execute(
            f"INSERT INTO {MANIFEST_TABLE} (box, share, path, size, run_ts) "
            f"SELECT %s, %s, path, size, now() FROM "
            f"  (SELECT DISTINCT ON (path) path, size FROM _stg ORDER BY path, size DESC) t "
            f"ON CONFLICT (box, share, path) DO UPDATE SET size = EXCLUDED.size, run_ts = now()",
            (box, share))
        cur.execute("DROP TABLE _stg")
    return prev


def sql_diff(conn, share: str):
    """Diff the persisted manifests in SQL (§7 requirement).
    Returns (to_copy[(path,size)], orphans[(path,size)])."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT s.path, s.size FROM {MANIFEST_TABLE} s "
            f"LEFT JOIN {MANIFEST_TABLE} d "
            f"  ON d.box='unas' AND d.share=%s AND d.path=s.path "
            f"WHERE s.box='syno' AND s.share=%s "
            f"  AND (d.path IS NULL OR d.size <> s.size)", (share, share))
        to_copy = cur.fetchall()
        cur.execute(
            f"SELECT d.path, d.size FROM {MANIFEST_TABLE} d "
            f"LEFT JOIN {MANIFEST_TABLE} s "
            f"  ON s.box='syno' AND s.share=%s AND s.path=d.path "
            f"WHERE d.box='unas' AND d.share=%s AND s.path IS NULL", (share, share))
        orphans = cur.fetchall()
    return to_copy, orphans


def record_run(share: str, rc: int, elapsed_s: int, files: int,
               nbytes: int, errors: int, ok: bool) -> None:
    """Write the telemetry row on a FRESH connection. A multi-hour copy lets the
    main connection time out server-side (idle), so depending on it here would
    silently DROP the record of a real, successful run — hiding it from the
    dead-man's-switch. Best-effort: a telemetry failure is logged, never raised."""
    import psycopg2
    try:
        c = psycopg2.connect(DSN)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO {RUNS_TABLE} (ts, job, rc, elapsed_s, files, bytes, errors, ok) "
                f"VALUES (now(), %s, %s, %s, %s, %s, %s, %s)",
                (f"nova-backup:{share}:manifest-sync", rc, elapsed_s, files, nbytes, errors, ok))
        c.close()
    except Exception as e:  # noqa: BLE001
        log(f"{share}: telemetry record_run failed (run itself unaffected): {e}")


def record_delta(share: str, copied, quarantined) -> None:
    """Persist WHAT changed this run (path + size, per action) so a backup is
    AUDITABLE — 'what did last night copy/quarantine?' becomes an instant SQL query
    (SELECT ... FROM telemetry.backup_delta WHERE ...) instead of re-scanning
    millions of files. Fresh connection (a long copy times the main one out); CSV
    COPY so a weird filename (tab/newline) can't corrupt the load; all rows in one
    run share ts=now() for clean per-run grouping. Best-effort — never fails the run."""
    if not copied and not quarantined:
        return
    import csv
    import io
    import psycopg2
    job = f"nova-backup:{share}:manifest-sync"
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    for p, s in copied:
        w.writerow([job, "copy", p, s])
    for p, s in quarantined:
        w.writerow([job, "quarantine", p, s])
    buf.seek(0)
    try:
        c = psycopg2.connect(DSN)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS telemetry.backup_delta (
                ts timestamptz NOT NULL DEFAULT now(), job text NOT NULL,
                action text NOT NULL, path text NOT NULL, size bigint)""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_backup_delta_ts_job "
                        "ON telemetry.backup_delta (ts, job)")
            cur.copy_expert("COPY telemetry.backup_delta (job, action, path, size) "
                            "FROM STDIN WITH (FORMAT csv)", buf)
        c.close()
        log(f"{share}: delta logged ({len(copied)} copy + {len(quarantined)} quarantine rows)")
    except Exception as e:  # noqa: BLE001 — auditability logging must never fail the run
        log(f"{share}: delta logging failed (non-fatal): {e}")


def ensure_mount(mount: str) -> bool:
    """Assert the UNAS mount is present + writable; try to reuse nova_nas_rsync's
    ensure_mounted self-heal if available. Returns True if usable."""
    mp = mount.rstrip("/")
    if os.path.ismount(mp) and os.access(mp, os.W_OK):
        return True
    try:
        import nova_nas_rsync
        for s in nova_nas_rsync.SHARES:
            if s.get("dest", "").rstrip("/") == mp:
                nova_nas_rsync.ensure_mounted(s)
                break
    except Exception as e:  # noqa: BLE001
        log(f"  ensure_mount self-heal unavailable: {e}")
    return os.path.ismount(mp) and os.access(mp, os.W_OK)


def write_nul_list(paths, outfile: str) -> int:
    """Write NUL-delimited relative paths (safe for spaces/metachars). Returns count."""
    n = 0
    with open(outfile, "wb") as f:
        for p in paths:
            if not safe_relpath(p):
                log(f"  skipping unsafe path in list: {p!r}")
                continue
            f.write(p.encode("utf-8") + b"\x00")
            n += 1
    return n


def rsync_delta(share: dict, dest_local: str, nul_list: str, timeout=14400):
    """Copy the KNOWN delta files explicitly — no rsync, no compare. The manifest
    diff already decided exactly which files to move, so re-scanning with rsync is
    wasted work. We just stream those files with tar in ONE connection:
      Synology (tar, read as kochj)  ->  .6 pipe  ->  UNAS (untar as ROOT -> .data).

    Why tar, not rsync/scp? (1) DSM rsync *service* is disabled -> rsync-as-sender
    over ssh dies 'service disabled' (code 52). (2) The Synology's CIFS mount of the
    UNAS is writable as SMB-user kochj but CANNOT create dirs (mkdir -> Permission
    denied 13), so rsync-to-mount writes nothing. (3) scp/sftp subsystem on the
    Synology is off. Untarring as ROOT on the UNAS-local .data has full perms and
    needs no per-file comparison; one tar stream carries all files. Returns 0 on ok."""
    remote_list = f"/tmp/nova_copy_{share['name']}.nul"
    with open(nul_list, "rb") as f:  # ship list via stdin (scp/sftp disabled on Syno)
        subprocess.run(["ssh", *SSH_OPTS, SYNO_HOST, f"cat > {shquote(remote_list)}"],
                       stdin=f, timeout=300, check=True)
    src_cmd = (f"cd {shquote(share['src'])} && "
               f"tar --null --files-from={shquote(remote_list)} -cf - ; "
               f"rc=$?; rm -f {shquote(remote_list)}; exit $rc")
    # After extraction, chown anything root-owned back to kochj:unifi-drive (1001:988).
    # tar-as-root creates IMPLICIT parent dirs owned root:root, and the Synology's
    # nightly CIFS rsync (SMB user uid 1001) then gets EPERM setting times on them —
    # every nightly fails rc=23 until someone chowns (bit us 2026-09-08..11 with the
    # backups/postgres/nova_*_20260908 dirs). The find piggybacks on a tree walk this
    # job already pays for elsewhere, and runs only when tar succeeded.
    dst_cmd = (f"mkdir -p {shquote(dest_local)} && cd {shquote(dest_local)} && tar -xf - "
               f"&& find . \\( -uid 0 -o -gid 0 \\) -exec chown -h 1001:988 {{}} +")
    last = ""
    for attempt in range(1, 4):
        p_src = subprocess.Popen(["ssh", *SSH_OPTS, SYNO_HOST, src_cmd],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p_dst = subprocess.Popen(["ssh", *SSH_OPTS, UNAS_HOST, dst_cmd],
                                 stdin=p_src.stdout, stderr=subprocess.PIPE)
        p_src.stdout.close()  # let src get SIGPIPE if dst dies
        try:
            _, dst_err = p_dst.communicate(timeout=timeout)
            _, src_err = p_src.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            p_src.kill(); p_dst.kill(); last = "timeout"
            if attempt < 3:
                time.sleep(10 * attempt)
            continue
        # tar source rc 1 = files changed/vanished mid-read (benign for a live tree)
        if p_src.returncode in (0, 1) and p_dst.returncode == 0:
            if p_src.returncode == 1:
                log(f"{share['name']}: tar-src rc=1 (files changed during read — benign)")
            return 0
        last = (f"src_rc={p_src.returncode} dst_rc={p_dst.returncode} "
                f"{((dst_err or b'') + (src_err or b''))[:180]!r}")
        log(f"  copy attempt {attempt}/3 failed: {last}")
        if attempt < 3:
            time.sleep(10 * attempt)
    log(f"{share['name']}: copy FAILED after 3 attempts: {last}")
    return 1


def quarantine_orphans(share: str, dest_sub: str, trash_root: str, orphans, day: str,
                       timeout=14400) -> int:
    """Move UNAS-only orphans into the dated trash dir via ssh-local `mv`
    (fast, avoids SMB). NUL-delimited list; the remote loop never interpolates a
    filename into shell. UNAS ONLY — the Synology is never touched. Returns count."""
    safe = [p for p in orphans if safe_relpath(p)]
    if not safe:
        return 0
    data_dir = f"{trash_root.rsplit('/.nova-trash', 1)[0]}/{dest_sub}"
    trash_dir = f"{trash_root}/{share}/{day}"
    with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".nul") as tf:
        local_list = tf.name
        for p in safe:
            tf.write(p.encode("utf-8") + b"\x00")
    try:
        remote_list = f"/tmp/nova_orphans_{share}.nul"
        # ship the NUL list to the UNAS (stdin pipe — run_with_retry can't pipe stdin)
        with open(local_list, "rb") as f:
            subprocess.run(["ssh", *SSH_OPTS, UNAS_HOST, f"cat > {shquote(remote_list)}"],
                           stdin=f, timeout=300, check=True)
        remote = (
            f"cd {shquote(data_dir)} && "
            f"while IFS= read -r -d '' rel; do "
            f"  d={shquote(trash_dir)}/\"$(dirname -- \"$rel\")\"; "
            f"  mkdir -p -- \"$d\" && mv -f -- \"$rel\" {shquote(trash_dir)}/\"$rel\"; "
            f"done < {shquote(remote_list)}; echo RC=$?"
        )
        r = run_with_retry(["ssh", *SSH_OPTS, UNAS_HOST, remote],
                           timeout=timeout, retries=2, backoff=10, ok_rc=(0,))
        return len(safe) if "RC=0" in r.stdout else 0
    finally:
        try:
            os.unlink(local_list)
        except OSError:
            pass


def prune_trash(trash_root: str, share: str, keep_days: int = TRASH_RETENTION_DAYS) -> None:
    """Keep only the last <keep_days> dated trash dirs for this share on the UNAS."""
    base = f"{trash_root}/{share}"
    remote = (
        f"cd {shquote(base)} 2>/dev/null && ls -1d [0-9]*-[0-9]*-[0-9]* 2>/dev/null "
        f"| sort -r | tail -n +{keep_days + 1} | while read -r d; do rm -rf -- \"$d\"; done; true"
    )
    try:
        subprocess.run(["ssh", *SSH_OPTS, UNAS_HOST, remote], timeout=600,
                       capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001
        log(f"  trash prune failed for {share}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════

def resolve_trash_root(uroot: str) -> str:
    return f"{uroot}/.nova-trash"


def sync_share(conn, share: dict, uroot: str, args) -> dict:
    name = share["name"]
    t0 = time.time()
    day = datetime.now().strftime("%Y-%m-%d")
    trash_root = resolve_trash_root(uroot)
    dest_local = f"{uroot}/{share['dest_sub']}"
    result = {"name": name, "ok": False, "errors": 0}

    with tempfile.TemporaryDirectory(prefix=f"manifest_{name}_") as td:
        src_raw = f"{td}/src.raw"
        dst_raw = f"{td}/dst.raw"
        src_clean = f"{td}/src.tsv"
        dst_clean = f"{td}/dst.tsv"

        log(f"{name}: finding src ({SYNO_HOST}:{share['src']}) + dest ({UNAS_HOST}:{dest_local}) locally…")
        # Parallel finds — start both, then wait.
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            fut_src = ex.submit(ssh_find, SYNO_HOST, share["src"], src_raw)
            fut_dst = ex.submit(ssh_find, UNAS_HOST, dest_local, dst_raw)
            fut_src.result(); fut_dst.result()

        src_manifest = clean_to_file(src_raw, src_clean)
        dst_manifest = clean_to_file(dst_raw, dst_clean)
        nsrc, ndst = len(src_manifest), len(dst_manifest)
        log(f"{name}: manifests — src={nsrc:,} dst={ndst:,} (found in {int(time.time()-t0)}s)")

        # Persist both manifests to PG (state lives in PG, reloaded each run).
        prev_src = pg_replace_manifest(conn, "syno", name, src_clean)
        pg_replace_manifest(conn, "unas", name, dst_clean)
        conn.commit()
        # Diff set-based in-process from the same manifests (O(n); avoids a
        # nightly 4M-row PG self-join). sql_diff() is the documented SQL
        # equivalent, exercised by the integration test / ad-hoc ops.
        to_copy, orphans = diff_manifests(src_manifest, dst_manifest)

    copy_bytes = sum(s for _, s in to_copy)
    orphan_bytes = sum(s for _, s in orphans)
    allow_copy, allow_delete, reasons = evaluate_guards(
        nsrc, ndst, len(orphans), prev_src,
        args.max_delete_pct, args.max_delete_abs, args.min_src_ratio)

    log(f"{name}: to_copy={len(to_copy):,} ({copy_bytes/1e9:.2f} GB)  "
        f"orphans={len(orphans):,} ({orphan_bytes/1e9:.2f} GB)  prev_src={prev_src:,}")
    for r in reasons:
        log(f"{name}: GUARD — {r}")

    result.update(nsrc=nsrc, ndst=ndst, prev_src=prev_src,
                  to_copy=len(to_copy), copy_bytes=copy_bytes,
                  orphans=len(orphans), orphan_bytes=orphan_bytes,
                  allow_copy=allow_copy, allow_delete=allow_delete, guards=reasons)

    # Guard trip that aborts the share (source sanity) -> alert, force no-op.
    aborted = not allow_copy
    if aborted:
        alert(f"NAS manifest-sync ABORT [{name}]",
              "Source-sanity guard tripped — NO copy, NO delete:\n" +
              "\n".join(f"• {r}" for r in reasons), critical=True)

    if args.dry_run or aborted:
        result["ok"] = not aborted
        result["mode"] = "abort" if aborted else "dry-run"
        return result

    # ── real run ──────────────────────────────────────────────────────────
    if not ensure_mount(share["mount"]):
        alert(f"NAS manifest-sync FAILED [{name}]",
              f"dest mount {share['mount']} not present/writable — aborting.", critical=True)
        result["errors"] += 1
        record_run(name, rc=1, elapsed_s=int(time.time() - t0),
                   files=0, nbytes=0, errors=1, ok=False)
        return result

    rc = 0
    copied = 0
    if to_copy:
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".nul") as tf:
            delta = tf.name
        try:
            copied = write_nul_list((p for p, _ in to_copy), delta)
            log(f"{name}: copying {copied:,} files ({copy_bytes/1e9:.2f} GB) via tar-stream…")
            rc = rsync_delta(share, dest_local, delta)
        finally:
            try:
                os.unlink(delta)
            except OSError:
                pass
    else:
        log(f"{name}: nothing to copy")

    deleted = 0
    if orphans and allow_delete:
        log(f"{name}: quarantining {len(orphans):,} orphans -> {trash_root}/{name}/{day}")
        deleted = quarantine_orphans(name, share["dest_sub"], trash_root,
                                     [p for p, _ in orphans], day)
        prune_trash(trash_root, name)
    elif orphans and not allow_delete:
        alert(f"NAS manifest-sync guard [{name}]",
              f"{len(orphans):,} orphans exceed delete threshold — quarantine SKIPPED:\n" +
              "\n".join(f"• {r}" for r in reasons))
        result["errors"] += 1

    ok = rc in (0, 24) and result["errors"] == 0
    record_run(name, rc=rc, elapsed_s=int(time.time() - t0),
               files=copied, nbytes=copy_bytes, errors=result["errors"], ok=ok)
    # Auditability: persist WHAT actually changed (path+size per action) so a backup
    # can be inspected after the fact without re-scanning millions of files.
    record_delta(name,
                 copied=to_copy if rc in (0, 24) else [],
                 quarantined=orphans if deleted else [])
    result.update(ok=ok, rc=rc, copied=copied, deleted=deleted, mode="live")
    log(f"{name}: {'OK' if ok else 'FAIL'} rc={rc} copied={copied:,} quarantined={deleted:,} "
        f"in {int(time.time()-t0)}s")
    return result


def resolve_uroot() -> str:
    r = run_with_retry(
        ["ssh", *SSH_OPTS, UNAS_HOST, f'for d in {UNAS_GLOB}; do echo "$d"; done'],
        timeout=30, retries=3, backoff=5)
    uroot = parse_uuid_root(r.stdout)
    if not uroot:
        raise RuntimeError(f"could not resolve UNAS unifi-drive root from: {r.stdout!r}")
    return uroot


def run(args) -> int:
    import psycopg2
    log(f"NAS manifest-sync starting ({'DRY-RUN' if args.dry_run else 'LIVE'})")
    uroot = resolve_uroot()
    log(f"UNAS unifi-drive root: {uroot}")
    conn = psycopg2.connect(DSN)
    with conn.cursor() as _c:
        # This job replaces ~2M-row manifests per share; the server's default
        # statement_timeout is too low for the bulk DELETE/INSERT. Raise it and
        # give the writes room. (Session-scoped — does not touch server config.)
        _c.execute("SET statement_timeout = '1200s'")
        _c.execute("SET work_mem = '256MB'")
    conn.commit()
    results = []
    try:
        for share in SHARES:
            if args.share and share["name"] != args.share:
                continue
            try:
                results.append(sync_share(conn, share, uroot, args))
            except Exception as e:  # noqa: BLE001 — never silently succeed
                conn.rollback()
                log(f"{share['name']}: EXCEPTION {e}")
                alert(f"NAS manifest-sync EXCEPTION [{share['name']}]", str(e), critical=True)
                record_run(share["name"], rc=1, elapsed_s=0, files=0,
                           nbytes=0, errors=1, ok=False)
                results.append({"name": share["name"], "ok": False, "error": str(e)})
    finally:
        conn.close()

    # Report
    print("\n──── SUMMARY ────")
    for r in results:
        if "error" in r:
            print(f"  {r['name']}: EXCEPTION — {r['error']}")
            continue
        print(f"  {r['name']}: src={r.get('nsrc', 0):,} dst={r.get('ndst', 0):,} "
              f"to_copy={r.get('to_copy', 0):,} ({r.get('copy_bytes', 0)/1e9:.2f} GB) "
              f"orphans={r.get('orphans', 0):,} ({r.get('orphan_bytes', 0)/1e9:.2f} GB) "
              f"mode={r.get('mode', '?')} ok={r.get('ok')}"
              + (f" GUARDS: {'; '.join(r['guards'])}" if r.get("guards") else ""))
    return 0 if all(r.get("ok") for r in results) else 1


def selftest() -> int:
    """Pure-function smoke test — touches no NAS/PG. Frame-test entrypoint."""
    assert parse_manifest_line("a/b.txt\t123") == ("a/b.txt", 123)
    assert parse_manifest_line("bad") is None
    assert is_excluded("x/@eaDir/y") and not is_excluded("x/y")
    tc, orp = diff_manifests({"a": 1, "b": 2}, {"a": 1, "b": 9, "c": 3})
    assert sorted(tc) == [("b", 2)] and orp == [("c", 3)]
    assert parse_uuid_root("/volume/UUID/.srv/.unifi-drive\n") == "/volume/UUID/.srv/.unifi-drive"
    assert parse_uuid_root("/volume/*/.srv/.unifi-drive") is None
    assert safe_relpath("a/b") and not safe_relpath("../x") and not safe_relpath("/x")
    _, ad, _ = evaluate_guards(0, 100, 0, 200, 5.0, 500, 0.5)
    assert ad is False  # empty source blocks delete
    assert quarantine_dest("/t/.nova-trash", "nas", "2026-08-04", "a/b").endswith("/nas/2026-08-04/a/b")
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fast Synology->UNAS manifest-diff backup sync (#656 §7).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute + report copy/delete plan, touch nothing.")
    p.add_argument("--share", choices=["nas", "external"], help="Only this share.")
    p.add_argument("--max-delete-pct", type=float, default=DEFAULT_MAX_DELETE_PCT,
                   help="Abort delete if orphans exceed this %% of dest (default 5).")
    p.add_argument("--max-delete-abs", type=int, default=DEFAULT_MAX_DELETE_ABS,
                   help="…or this absolute count, whichever is larger (default 500).")
    p.add_argument("--min-src-ratio", type=float, default=DEFAULT_MIN_SRC_RATIO,
                   help="Abort if source < this fraction of previous source count (default 0.5).")
    p.add_argument("--selftest", action="store_true", help="Run pure-function selftest and exit.")
    args = p.parse_args(argv)
    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
