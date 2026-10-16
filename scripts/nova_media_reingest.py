#!/usr/bin/env python3
"""nova_media_reingest.py — re-transcribe Nova's TV/video library with LOCAL MLX Whisper, most-watched first.

Little Mister approved 2026-10-15. Until that day two filters threw real speech away: nova_tv_ingest.is_trash_chunk
rejected a whole 400-word chunk on any short repetition ("no, no, no"), and nova_memory_quality_filter rejected
whole memories the same way (fixed in 49760d5: junk must now cover >= 35% of a chunk). Anchorman lost 19 of 27
chunks. Every episode/video ingested before the fix went through those filters and the transcripts were not kept,
so recovery means transcribing again. This job does it slowly, overnight, one Whisper at a time, on the Studio GPU
(mlx-community/whisper-large-v3-turbo, HF_HOME=~/mlxb_hf). Zero cloud cost; audio and text never leave the LAN.

Queue    media_reingest_queue (nova_ops): one row per video file still on disk, built by --build-queue from
         media_ingest_state rows with status ingested / trash / no_transcript under VIDEO_ROOT. Excluded: Ripped
         Movies (handled by the 2026-10-15 movie job), other/, files < 10 MB, silent-era films (year <= 1928 in the
         title), files already reingested, files that no longer exist (the media gardener pruned ~23k).
Priority "most-watched first", strongest real signal first (read-only):
           1. Plex per-file viewCount (library /all, every section)          x1000
           2. Plex views summed over the file's series (TVShows/, youtube/)   x100
           3. memory recall: sum(access_count) of the show's memories   10*ln(1+n)
           4. episodes of the show in the queue                              ln(1+n)
         Plex history itself is near-empty (14 rows since 2026-06), so viewCount + recall carry the order.
Worker   read the file locally, or (TCC blocks python from /Volumes/external) stream it with
         `ssh nova-core cat /external/videos/...` into a temp dir on the local SSD -> ffmpeg 16 kHz mono wav ->
         mlx_whisper CLI (condition_on_previous_text False) -> temp files deleted -> nova_tv_ingest.chunk_text
         (Skipping Needle loop collapse + the fixed junk-share filter) -> one synchronous /remember per chunk with
         the original ingest's show/title/type/source plus metadata.pipeline='reingest-2026-10' and the chunk index
         -> ONLY THEN the file's old transcript memories are superseded (superseded_by = first new id,
         valid_to = now()), exactly as nova_skipping_needle.repair_one does. Nothing is ever deleted.
         media_ingest_state (words/chunks/status) and the queue row are updated last.
Coexist  --scheduled holds the shared PG advisory lock GPU_WHISPER_LOCK for the whole batch and exits at once if
         another whisper user holds it or the 2026-10-15 movie job (movie-ingest/run.py) is running; it stops at
         06:00 or after --hours, whichever is first. A 'running' row whose worker died reverts to pending after 2 h.
Safety   service_config media_reingest/enabled (default true); nova_autonomy_safety.kill_switch_engaged.
Report   --report: queue by status and signal, words recovered, measured throughput, ETA. The scheduled run writes
         service_config ('media_reingest','latest') = {day, line, ...} for the 18:00 summary.

CLI      --build-queue | --scheduled [--hours H] [--limit N] | --dry-run [--limit N | --path FILE ...] [--out DIR]
         | --report | --selftest
Schedule Studio config/scheduler.yaml: cron 0 23 * * *, timeout 25200, overlap skip, gpu_heavy.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "media_reingest"
PIPELINE = "reingest-2026-10"
VIDEO_ROOT = "/Volumes/external/videos/"
REMOTE_HOST = "nova-core"
REMOTE_ROOT = "/external/videos/"
PLEX_ROOT = "/external3/videos/"
EXCLUDED_TOP = ("Ripped Movies", "other", "Other", "@eaDir")
SHOW_DIRS = ("TVShows", "youtube")            # Plex 'show' sections: a folder there is a series, elsewhere just a bin
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv", ".webm"}
SOURCE_STATUSES = ("ingested", "trash", "no_transcript")
MIN_BYTES = 10_000_000
SILENT_MAX_YEAR = 1928
STALE_RUNNING_H = 2
MAX_ATTEMPTS = 3
DEFAULT_HOURS = 7.0
STOP_AT = "06:00"
MIN_FREE_GB = 20
GPU_WHISPER_LOCK = "nova_gpu_whisper"          # shared: any other whisper batch should take the same lock
MOVIE_JOB = "movie-ingest/run.py"
WHISPER_BIN = "/opt/homebrew/bin/mlx_whisper"
FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
HF_HOME = str(Path.home() / "mlxb_hf")
WAV_BYTES_PER_SEC = 32000                      # 16 kHz * 16-bit * mono
STATEMENT_TIMEOUT_MS = 120000
TRANSCRIPT_TYPES = ("tv_transcript", "video_transcript", "youtube_transcript")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_reingest_queue (
  file_path text PRIMARY KEY,
  show text,
  title text,
  priority numeric NOT NULL DEFAULT 0,
  signal text,
  status text NOT NULL DEFAULT 'pending',
  prior_status text,
  size_bytes bigint,
  duration_s real,
  old_chunks integer,
  old_words integer,
  legacy_chunks integer,
  new_chunks integer,
  words integer,
  superseded integer,
  audio_s real,
  gpu_s real,
  wall_s real,
  attempts integer NOT NULL DEFAULT 0,
  error text,
  started_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS media_reingest_queue_next ON media_reingest_queue (status, priority DESC);
"""


def log(m: str) -> None:
    print(f"[media-reingest {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---------------------------------------------------------------- pure functions

_YEAR = re.compile(r"\((1[89]\d\d)\)")


def is_silent_era(title: str) -> bool:
    """A '(1925)'-style year <= SILENT_MAX_YEAR in the title: a silent film, nothing for Whisper. Pure."""
    return any(int(y) <= SILENT_MAX_YEAR for y in _YEAR.findall(title or ""))


def top_dir(path: str) -> str:
    return path[len(VIDEO_ROOT):].split("/", 1)[0] if path.startswith(VIDEO_ROOT) else ""


def eligible(path: str, size: int | None, title: str = "") -> str | None:
    """None when the file belongs in the queue, else the reason it does not. Pure."""
    if not path.startswith(VIDEO_ROOT):
        return "outside VIDEO_ROOT"
    if top_dir(path) in EXCLUDED_TOP:
        return "excluded dir"
    if Path(path).suffix.lower() not in VIDEO_EXTS:
        return "not video"
    if size is None:
        return "missing"
    if size < MIN_BYTES:
        return "small"
    if is_silent_era(title or Path(path).stem):
        return "silent era"
    return None


def to_remote(path: str) -> str:
    """/Volumes/external/videos/X -> /external/videos/X (nova-core's mount). Pure."""
    if not path.startswith(VIDEO_ROOT):
        raise ValueError(f"not under {VIDEO_ROOT}: {path}")
    return REMOTE_ROOT + path[len(VIDEO_ROOT):]


def plex_to_local(path: str) -> str | None:
    return VIDEO_ROOT + path[len(PLEX_ROOT):] if path and path.startswith(PLEX_ROOT) else None


# Files whose speech the old filters actually threw away come before files that were only duplicated:
# the dry run showed ~0% net loss on 'ingested' rows and real recovery on 'trash'/'no_transcript' ones.
RECOVERY_FIRST = ("trash", "no_transcript")
RECOVERY_BOOST = 1e15


def priority(file_views: int, show_views: int, show_access: int, show_episodes: int) -> tuple[float, str]:
    """(priority, strongest signal present). Pure; see the module docstring for the weights."""
    p = 1000 * file_views + 100 * show_views + 10 * math.log1p(show_access) + math.log1p(show_episodes)
    sig = ("plex_file" if file_views else "plex_show" if show_views else
           "memory_recall" if show_access else "episode_count")
    return round(p, 3), sig


def batch_deadline(now: datetime, hours: float, stop_at: str = STOP_AT) -> datetime:
    """min(now + hours, the next stop_at wall-clock time). Pure."""
    hh, mm = (int(x) for x in stop_at.split(":"))
    stop = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= now:
        stop += timedelta(days=1)
    return min(now + timedelta(hours=hours), stop)


def plan_supersede(old_ids: list[str], new_ids: list[str]) -> tuple[str | None, list[str]]:
    """(target id, old ids to supersede). A new chunk identical to an old one dedups onto the OLD id (text_hash);
    that row is the new memory too and must stay live. Pure."""
    if not new_ids:
        return None, []
    keep = set(new_ids)
    return new_ids[0], [i for i in old_ids if i not in keep]


def majority(sources: list[str]) -> str | None:
    """Most common source, ignoring quarantine:* re-labels. Pure."""
    counts: dict[str, int] = {}
    for x in sources:
        if x and not x.startswith("quarantine:"):
            counts[x] = counts.get(x, 0) + 1
    return max(sorted(counts), key=counts.get) if counts else None


def choose_source(explicit: str | None, current: list[str], state_vector: str | None, classified: str) -> str:
    """The vector the original ingest would use, in nova_tv_ingest's own order of authority: the explicit channel
    map, then the source the current pipeline gave this file, then media_ingest_state, then the keyword
    classifier. Legacy May-2026 rows were re-labelled in bulk (Engine Masters as sci_fi), so they never vote. Pure."""
    return explicit or majority(current) or state_vector or classified


def wav_seconds(nbytes: int) -> float:
    return max(0, nbytes - 44) / WAV_BYTES_PER_SEC


def eta_days(remaining_audio_s: float, audio_per_wall: float, hours_per_night: float) -> float | None:
    """Nights to finish at the measured audio-seconds-per-wall-second rate. Pure."""
    if audio_per_wall <= 0 or hours_per_night <= 0:
        return None
    return remaining_audio_s / audio_per_wall / (hours_per_night * 3600)


def report_line(c: dict) -> str:
    """One line for the 18:00 summary. Pure."""
    eta = f", ~{c['eta_days']:.0f} nights left" if c.get("eta_days") else ""
    return (f"re-transcription: {c.get('done', 0):,}/{c.get('total', 0):,} files done "
            f"({c.get('today', 0)} today), {c.get('new_chunks', 0):,} chunks vs {c.get('old_chunks', 0):,} before "
            f"(+{c.get('recovered_words', 0):,} words){eta}")


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
    cur.execute("ALTER TABLE media_reingest_queue ADD COLUMN IF NOT EXISTS node text")


def cfg(cur, key: str, default=None):
    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s", (SERVICE, key))
    r = cur.fetchone()
    if not r or r[0] is None:
        return default
    v = r[0]
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def set_cfg(cur, key: str, value) -> None:
    cur.execute("INSERT INTO service_config (service, key, value, updated_at, updated_by) "
                "VALUES (%s, %s, %s::jsonb, now(), %s) ON CONFLICT (service, key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                (SERVICE, key, json.dumps(value), "nova_media_reingest"))


def enabled(cur) -> bool:
    v = cfg(cur, "enabled", True)
    return str(v).strip().strip('"').lower() not in ("false", "0", "off", "no")


def kill_switch(cur) -> bool:
    try:
        import nova_autonomy_safety
        return bool(nova_autonomy_safety.kill_switch_engaged(cur))
    except Exception as e:  # noqa: BLE001 — cannot read the switch: stand down
        log(f"kill switch unreadable ({e}); standing down")
        return True


def try_gpu_lock(cur) -> bool:
    """Session-level advisory lock shared by every whisper batch; released when the connection closes."""
    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (GPU_WHISPER_LOCK,))
    return bool(cur.fetchone()[0])


def revert_stale(cur) -> int:
    cur.execute("UPDATE media_reingest_queue SET status = 'pending', error = 'worker died; reverted', "
                "updated_at = now() WHERE status = 'running' AND updated_at < now() - make_interval(hours => %s)",
                (STALE_RUNNING_H,))
    return cur.rowcount or 0


def claim_next(cur) -> tuple | None:
    cur.execute("UPDATE media_reingest_queue SET status = 'running', started_at = now(), updated_at = now() "
                "WHERE file_path = (SELECT file_path FROM media_reingest_queue WHERE status = 'pending' "
                "ORDER BY priority DESC, file_path LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING file_path, show, title, prior_status")
    return cur.fetchone()


def finish(cur, path: str, status: str, **kw) -> None:
    cols = "".join(f", {k} = %s" for k in kw)
    cur.execute(f"UPDATE media_reingest_queue SET status = %s, updated_at = now(){cols} WHERE file_path = %s",
                (status, *kw.values(), path))


# ---------------------------------------------------------------- environment

def movie_job_running(_run=subprocess.run) -> bool:
    """The 2026-10-15 one-off movie job: matches 'movie-ingest/run.py' in argv, or a run.py whose cwd is
    .../movie-ingest (it is started as `python -I run.py` from its own directory)."""
    try:
        ps = _run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return False
    for line in ps.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if MOVIE_JOB in cmd:
            return True
        if re.search(r"(^|[\s/])run\.py(\s|$)", cmd) and "python" in cmd.lower():
            try:
                cwd = _run(["/usr/sbin/lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=10).stdout
            except Exception:  # noqa: BLE001
                continue
            if re.search(r"^n.*/movie-ingest$", cwd, re.M):
                return True
    return False


def local_readable(path: str = VIDEO_ROOT) -> bool:
    """False when TCC (or a missing mount) keeps this process out of /Volumes/external."""
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def list_files(_run=subprocess.run) -> dict[str, int]:
    """{local path: size} of every video file under VIDEO_ROOT; walks locally, or over ssh when TCC blocks it."""
    out: dict[str, int] = {}
    if local_readable():
        for root, dirs, files in os.walk(VIDEO_ROOT):
            if root.rstrip("/") == VIDEO_ROOT.rstrip("/"):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_TOP]
            for f in files:
                p = os.path.join(root, f)
                try:
                    out[p] = os.path.getsize(p)
                except OSError:
                    pass
        return out
    cmd = f"find {REMOTE_ROOT.rstrip('/')} -path '*/@eaDir' -prune -o -type f -printf '%s\\t%p\\n'"
    r = _run(["ssh", "-o", "BatchMode=yes", REMOTE_HOST, cmd], capture_output=True, text=True, timeout=900)
    for line in r.stdout.splitlines():
        size, _, p = line.partition("\t")
        if p.startswith(REMOTE_ROOT) and size.isdigit():
            out[VIDEO_ROOT + p[len(REMOTE_ROOT):]] = int(size)
    return out


def plex_index() -> dict[str, tuple[int, float | None]]:
    """{local path: (viewCount, duration seconds)} from every Plex section. Read-only; {} if Plex is down."""
    try:
        import nova_plex
        out = {}
        secs = nova_plex.plex_get_json("/library/sections")["MediaContainer"].get("Directory", [])
        for s in secs:
            typ = 4 if s.get("type") == "show" else 1
            items = nova_plex.plex_get_json(f"/library/sections/{s['key']}/all", {"type": typ},
                                            timeout=120)["MediaContainer"].get("Metadata", [])
            for it in items:
                for media in it.get("Media", []):
                    for part in media.get("Part", []):
                        lp = plex_to_local(part.get("file", ""))
                        if lp:
                            dur = it.get("duration") or part.get("duration")
                            out[lp] = (int(it.get("viewCount", 0) or 0), dur / 1000 if dur else None)
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Plex unavailable ({e}); priority falls back to memory recall")
        return {}


def show_access(mcur) -> dict[str, int]:
    """Memory-recall signal: sum(access_count) per metadata.show over live memories."""
    mcur.execute("SELECT metadata->>'show', sum(access_count) FROM memories "
                 "WHERE metadata->>'show' IS NOT NULL AND superseded_by IS NULL GROUP BY 1")
    return {s: int(n or 0) for s, n in mcur.fetchall()}


# ---------------------------------------------------------------- queue build

def build_queue(oc, mc, files: dict | None = None, plex: dict | None = None) -> dict:
    """Insert new eligible files, refresh pending priorities. Never touches done/running/error rows."""
    ensure_schema(oc)
    files = list_files() if files is None else files
    plex = plex_index() if plex is None else plex
    access = show_access(mc)
    oc.execute("SELECT file_path, show, title, status, error_message FROM media_ingest_state "
               "WHERE status = ANY(%s) AND file_path LIKE %s", (list(SOURCE_STATUSES), VIDEO_ROOT + "%"))
    rows, skipped = [], {}
    for path, show, title, status, err in oc.fetchall():
        why = ("already reingested" if err and (PIPELINE in err or "hand-ingested" in err)
               else eligible(path, files.get(path), title or ""))
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        rows.append((path, show or "", title or Path(path).stem, status))
    episodes: dict[str, int] = {}
    show_views: dict[str, int] = {}
    for path, show, _t, _s in rows:
        episodes[show] = episodes.get(show, 0) + 1
        if top_dir(path) in SHOW_DIRS:
            show_views[show] = show_views.get(show, 0) + plex.get(path, (0, None))[0]
    added = 0
    for path, show, title, status in rows:
        views, dur = plex.get(path, (0, None))
        pri, sig = priority(views, show_views.get(show, 0), access.get(show, 0), episodes.get(show, 0))
        if status in RECOVERY_FIRST:      # lost speech first, most-watched within it (Claude, 2026-10-15)
            pri += RECOVERY_BOOST
        oc.execute("INSERT INTO media_reingest_queue (file_path, show, title, priority, signal, prior_status, "
                   "size_bytes, duration_s) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                   "ON CONFLICT (file_path) DO UPDATE SET priority = EXCLUDED.priority, signal = EXCLUDED.signal, "
                   "size_bytes = EXCLUDED.size_bytes, duration_s = coalesce(EXCLUDED.duration_s, "
                   "media_reingest_queue.duration_s), updated_at = now() "
                   "WHERE media_reingest_queue.status = 'pending' RETURNING (xmax = 0)",
                   (path, show, title, pri, sig, status, files.get(path), dur))
        r = oc.fetchone()
        added += bool(r and r[0])
    log(f"queue: {len(rows):,} eligible, {added:,} new; skipped {skipped}")
    return {"eligible": len(rows), "added": added, "skipped": skipped}


# ---------------------------------------------------------------- one file

def _ffmpeg(src: str, wav: Path, _run=subprocess.run) -> None:
    _run([FFMPEG_BIN, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
          "-acodec", "pcm_s16le", str(wav)], check=True, timeout=3600, capture_output=True)


_DENIED = ("Operation not permitted", "Permission denied")


def extract_audio(path: str, tmp: Path, _run=subprocess.run) -> Path:
    """16 kHz mono wav in tmp. ffmpeg reads the file in place; when TCC (or the mount) keeps it out, the file is
    copied from nova-core (`ssh nova-core cat`, path shell-quoted) into tmp first and deleted after ffmpeg."""
    wav = tmp / "audio.wav"
    if local_readable(os.path.dirname(path)):
        try:
            _ffmpeg(path, wav, _run)
            return wav
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode(errors="ignore") if isinstance(e.stderr, bytes) else str(e.stderr or "")
            if not any(d in err for d in _DENIED):
                raise
    src = tmp / ("video" + Path(path).suffix.lower())
    try:
        with open(src, "wb") as fh:
            _run(["ssh", "-o", "BatchMode=yes", REMOTE_HOST, "cat " + shlex.quote(to_remote(path))],
                 stdout=fh, check=True, timeout=3600)
        _ffmpeg(str(src), wav, _run)
    finally:
        src.unlink(missing_ok=True)
    return wav


def transcribe_local(wav: Path, tmp: Path, timeout: float, _run=subprocess.run) -> str:
    """mlx_whisper CLI (a subprocess, so a time-boxed batch can kill it). Local only."""
    env = dict(os.environ, HF_HOME=os.environ.get("HF_HOME", HF_HOME))
    _run([WHISPER_BIN, str(wav), "--model", WHISPER_MODEL, "--language", "en",
          "--condition-on-previous-text", "False", "--output-format", "txt",
          "--output-dir", str(tmp), "--output-name", "transcript", "--verbose", "False"],
         check=True, timeout=timeout, capture_output=True, env=env)
    txt = tmp / "transcript.txt"
    return txt.read_text(encoding="utf-8", errors="ignore").strip() if txt.exists() else ""


def transcribe_remote(host: str, wav: Path, timeout: float, _run=subprocess.run) -> str:
    """The same MLX Whisper on a minion Mac (2026-10-16): copy the WAV over, run nova_minion_whisper.py in its
    ~/.nova-minion/whisper-venv, read the text from stdout, delete the WAV. host = the minion's ssh name."""
    name = f"reingest-{uuid.uuid4().hex[:12]}.wav"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]
    _run(["scp", "-q", "-o", "BatchMode=yes", str(wav), f"{host}:.nova-minion/{name}"], check=True, timeout=600)
    try:
        r = _run(ssh + [f"cd .nova-minion && whisper-venv/bin/python nova_minion_whisper.py {name}"],
                 check=True, timeout=timeout, capture_output=True)
        out = r.stdout
        return (out.decode("utf-8", "ignore") if isinstance(out, bytes) else str(out or "")).strip()
    finally:
        _run(ssh + [f"cd .nova-minion && /bin/rm -f -- {name}"], timeout=60, capture_output=True)


def old_memories(mcur, show: str, path: str) -> list[tuple]:
    """Live transcript memories of this file from earlier ingests: (id, original source, type, words, current).
    The source is the one the ingest wrote (metadata.reclass_from when a later re-router moved the row).
    Two shapes: nova_tv_ingest's (metadata.source_file) and the May-2026 'tv_transcript-transcription' rows
    (metadata.file = basename, 'part n/m' titles); both are transcripts of this file and both are superseded."""
    mcur.execute("SELECT id, coalesce(metadata->>'reclass_from', source), metadata->>'type', "
                 "array_length(regexp_split_to_array(text, '\\s+'), 1), "
                 "metadata->>'source_file' = %s "
                 "FROM memories WHERE metadata->>'show' = %s AND superseded_by IS NULL "
                 "AND coalesce(metadata->>'pipeline', '') <> %s "
                 "AND (metadata->>'source_file' = %s OR (metadata->>'file' = %s "
                 "AND metadata->>'origin' LIKE '%%transcription')) "
                 "ORDER BY (metadata->>'chunk')::int NULLS LAST, id",
                 (path, show, PIPELINE, path, os.path.basename(path)))
    return mcur.fetchall()


def store(chunks: list[str], show: str, title: str, path: str, source: str, mtype: str, post=None) -> list[str] | None:
    """One synchronous /remember per chunk (ids needed for supersession). None if any chunk failed to store."""
    import nova_skipping_needle
    post = post or nova_skipping_needle.post_remember
    today = datetime.now().strftime("%Y-%m-%d")
    ids = []
    for i, ch in enumerate(chunks, 1):
        res = post(f"[{show}] {ch}", source,
                   {"type": mtype, "show": show, "title": title, "chunk": i, "total_chunks": len(chunks),
                    "ingested_date": today, "source_file": path, "pipeline": PIPELINE, "privacy": "local-only"})
        if not res:
            return None
        if res.get("id") and res.get("status") != "rejected":
            ids.append(res["id"])
    return ids


def supersede(mcur, target: str, old_ids: list[str]) -> int:
    """Same convention as nova_skipping_needle.repair_one. Never deletes."""
    if not old_ids:
        return 0
    mcur.execute("UPDATE memories SET superseded_by = %s, valid_to = now() WHERE id = ANY(%s) "
                 "AND superseded_by IS NULL", (target, list(old_ids)))
    return mcur.rowcount or 0


def process(oc, mc, item: tuple, timeout: float, dry_out: Path | None = None, post=None, transcribe=None) -> dict:
    """Transcribe one file and (unless dry_out) store + supersede. Returns the measured result."""
    import nova_tv_ingest as tv
    path, show, title, prior = item
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="nova-reingest-"))
    try:
        wav = extract_audio(path, tmp)
        audio_s = wav_seconds(wav.stat().st_size)
        g0 = time.monotonic()
        text = transcribe(wav, timeout) if transcribe else transcribe_local(wav, tmp, timeout)
        gpu_s = time.monotonic() - g0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raw_words = len(text.split())
    chunks = tv.chunk_text(text)
    old = old_memories(mc, show, path)
    res = {"file_path": path, "show": show, "title": title, "prior_status": prior, "audio_s": round(audio_s, 1),
           "gpu_s": round(gpu_s, 1), "raw_words": raw_words,
           # old_* = what nova_tv_ingest kept of this file; legacy = May-2026 duplicate transcripts (also retired)
           "old_chunks": sum(1 for o in old if o[4]), "old_words": sum(o[3] or 0 for o in old if o[4]),
           "legacy_chunks": sum(1 for o in old if not o[4]), "new_chunks": len(chunks),
           "new_words": sum(len(c.split()) for c in chunks)}
    oc.execute("SELECT source_vector FROM media_ingest_state WHERE file_path = %s", (path,))
    r = oc.fetchone()
    source = choose_source(tv.explicit_vector(show), [o[1] for o in old if o[4]], r[0] if r else None,
                           tv.classify_source(show, title, text[:500]))
    mtype = next((o[2] for o in old if o[2] in TRANSCRIPT_TYPES), "tv_transcript")
    res.update(source=source, type=mtype)
    if dry_out is not None:
        res["wall_s"] = round(time.monotonic() - t0, 1)
        (dry_out / (re.sub(r"[^\w.-]+", "_", title)[:80] + ".json")).write_text(
            json.dumps({**res, "chunks": chunks}, indent=1))
        return res
    if not chunks:
        res.update(status="empty", superseded=0, wall_s=round(time.monotonic() - t0, 1))
        return res                       # nothing usable: the old memories stay as they are
    ids = store(chunks, show, title, path, source, mtype, post=post)
    if ids is None:
        raise RuntimeError("memory server did not store every chunk; old memories left untouched")
    target, to_sup = plan_supersede([o[0] for o in old], ids)
    res["superseded"] = supersede(mc, target, to_sup) if target else 0
    res["stored"] = len(ids)
    oc.execute("UPDATE media_ingest_state SET status = 'ingested', chunks = %s, words = %s, source_vector = %s, "
               "processed_at = now(), error_message = %s WHERE file_path = %s",
               (len(ids), raw_words, source, f"{PIPELINE}: {'minion' if transcribe else 'local'} MLX whisper; "
                                             f"{res['superseded']} old superseded",
                path))
    try:
        import nova_media_registry
        nova_media_registry.mark_ingested(path, len(ids), source)
    except Exception:  # noqa: BLE001 — the registry is a mirror; never fail the item for it
        pass
    res.update(status="done", wall_s=round(time.monotonic() - t0, 1))
    return res


# ---------------------------------------------------------------- batch

def guards(oc, check_movie: bool = True) -> str | None:
    """Reason to stand down, or None."""
    if not enabled(oc):
        return "service_config media_reingest/enabled is false"
    if kill_switch(oc):
        return "autonomy kill switch engaged"
    if check_movie and movie_job_running():
        return f"{MOVIE_JOB} is running"
    if shutil.disk_usage(tempfile.gettempdir()).free < MIN_FREE_GB * 1e9:
        return f"less than {MIN_FREE_GB} GB free for temp files"
    return None


def do_item(oc, mc, item: tuple, left: float, stats: dict, post=None, transcribe=None, tag: str = "",
            node: str = "mac-studio") -> bool:
    """Process one claimed file and book the result. False = the window closed mid-file (stop the loop)."""
    log(f"▶{tag} {item[1]} — {item[2][:70]}")
    try:
        r = process(oc, mc, item, timeout=left, post=post, transcribe=transcribe)
    except subprocess.TimeoutExpired:
        finish(oc, item[0], "pending", error="batch window closed mid-file")
        log(f" {tag} window closed mid-file; back to pending")
        return False
    except Exception as e:  # noqa: BLE001 — one bad file never stops the night
        oc.execute("SELECT attempts FROM media_reingest_queue WHERE file_path = %s", (item[0],))
        att = (oc.fetchone() or [0])[0] + 1
        finish(oc, item[0], "error" if att >= MAX_ATTEMPTS else "pending", attempts=att, error=str(e)[:300])
        stats["error"] += 1
        log(f" {tag} ✗ {e}")
        return True
    finish(oc, item[0], r["status"], old_chunks=r["old_chunks"], old_words=r["old_words"],
           legacy_chunks=r["legacy_chunks"],
           new_chunks=r.get("stored", 0), words=r["new_words"] if r["status"] == "done" else 0,
           superseded=r["superseded"], audio_s=r["audio_s"], gpu_s=r["gpu_s"], wall_s=r["wall_s"], error=None,
           node=node)                    # which machine's Whisper did it (minion displays count it)
    stats[r["status"]] += 1
    stats["audio_s"] += r["audio_s"]
    stats["gpu_s"] += r["gpu_s"]
    log(f" {tag} ✓ {r['status']}: {r['old_chunks']} (+{r['legacy_chunks']} legacy) -> {r.get('stored', 0)} chunks, "
        f"{r['old_words']:,} -> {r['new_words']:,} words, "
        f"{r['audio_s'] / 60:.0f} min audio in {r['gpu_s']:.0f}s GPU")
    return True


def run_batch(oc, mc, deadline: datetime, limit: int | None, post=None) -> dict:
    stats = {"done": 0, "empty": 0, "error": 0, "audio_s": 0.0, "gpu_s": 0.0}
    n = 0
    while limit is None or n < limit:
        left = (deadline - datetime.now()).total_seconds()
        if left < 300:
            break
        item = claim_next(oc)
        if not item:
            break
        n += 1
        if not do_item(oc, mc, item, left, stats, post=post):
            break
    return stats


# ---------------------------------------------------------------- minions (2026-10-16)
# Idle minion Macs with MLX Whisper (agent config "whisper": true -> minion_nodes.info.whisper) transcribe while
# idle, any hour: the Studio claims + extracts audio (CPU), the minion runs Whisper on its GPU, the Studio stores.
# No GPU lock: the Studio's GPU is not used. Each minion shows "transcribing: <episode>" on its screensaver.
MINION_FRESH_S = 90


def whisper_minions(oc) -> list[str]:
    oc.execute("SELECT node FROM minion_nodes WHERE state IN ('idle','dedicated') AND (info->>'whisper')::boolean "
               "AND last_seen > now() - %s * interval '1 second' ORDER BY node", (MINION_FRESH_S,))
    return [r[0] for r in oc.fetchall()]


def minion_free(oc, node: str) -> bool:
    return node in whisper_minions(oc)


def minion_worker(node: str, deadline: datetime, stats: dict, lock: threading.Lock):
    oc, mc = connect("nova_ops").cursor(), connect("nova_memories").cursor()
    mine = {"done": 0, "empty": 0, "error": 0, "audio_s": 0.0, "gpu_s": 0.0}
    while (deadline - datetime.now()).total_seconds() > 300 and minion_free(oc, node) and not guards(oc, check_movie=False):
        item = claim_next(oc)
        if not item:
            break
        key = "media:" + item[0][:290]
        oc.execute("INSERT INTO minion_activity (node, work_key, title, kind, phase) VALUES (%s,%s,%s,'tv','transcribing') "
                   "ON CONFLICT (node, work_key) DO UPDATE SET started_at=now()", (node, key, f"{item[1]} — {item[2]}"[:300]))
        try:
            ok = do_item(oc, mc, item, (deadline - datetime.now()).total_seconds(), mine,
                         transcribe=lambda wav, t: transcribe_remote(node, wav, t), tag=f" [{node}]", node=node)
        finally:
            oc.execute("DELETE FROM minion_activity WHERE node=%s AND work_key=%s", (node, key))
        if not ok:
            break
    with lock:
        for k, v in mine.items():
            stats[k] += v
    log(f"[{node}] stopped: {mine['done']} done, {mine['error']} error, {mine['audio_s'] / 60:.0f} min audio")


def run_minions(minutes: float) -> dict:
    oc = connect("nova_ops").cursor()
    why = guards(oc, check_movie=False)
    if why:
        log(f"minions: standing down — {why}")
        return {}
    nodes = whisper_minions(oc)
    if not nodes:
        log("minions: no idle whisper minion")
        return {}
    revert_stale(oc)
    deadline = datetime.now() + timedelta(minutes=minutes)
    stats = {"done": 0, "empty": 0, "error": 0, "audio_s": 0.0, "gpu_s": 0.0}
    lock = threading.Lock()
    threads = [threading.Thread(target=minion_worker, args=(n, deadline, stats, lock), daemon=True) for n in nodes]
    log(f"minions: {', '.join(nodes)} for {minutes:.0f} min")
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log(f"minions: {stats['done']} done, {stats['empty']} empty, {stats['error']} error, "
        f"{stats['audio_s'] / 3600:.1f} h of audio")
    return stats


def counts(oc, hours_per_night: float = DEFAULT_HOURS) -> dict:
    oc.execute("SELECT coalesce(sum(legacy_chunks), 0), coalesce(sum(superseded), 0) FROM media_reingest_queue")
    legacy, superseded = oc.fetchone() or (0, 0)
    oc.execute("SELECT status, count(*), coalesce(sum(old_chunks), 0), coalesce(sum(new_chunks), 0), "
               "coalesce(sum(old_words), 0), coalesce(sum(words), 0), coalesce(sum(audio_s), 0), "
               "coalesce(sum(gpu_s), 0), coalesce(sum(wall_s), 0), "
               "count(*) FILTER (WHERE updated_at::date = current_date) FROM media_reingest_queue GROUP BY 1")
    c = {"by_status": {}, "total": 0, "old_chunks": 0, "new_chunks": 0, "old_words": 0, "new_words": 0,
         "audio_s": 0.0, "gpu_s": 0.0, "wall_s": 0.0, "today": 0}
    for st, n, oc_, nc, ow, nw, a, g, w, td in oc.fetchall():
        c["by_status"][st] = n
        c["total"] += n
        c["old_chunks"] += oc_; c["new_chunks"] += nc; c["old_words"] += ow; c["new_words"] += nw
        c["audio_s"] += float(a); c["gpu_s"] += float(g); c["wall_s"] += float(w)
        if st in ("done", "empty"):
            c["today"] += td
    c["done"] = c["by_status"].get("done", 0) + c["by_status"].get("empty", 0)
    c["recovered_words"] = c["new_words"] - c["old_words"]
    c["legacy_chunks"], c["superseded"] = int(legacy or 0), int(superseded or 0)
    oc.execute("SELECT count(*), coalesce(sum(duration_s), 0), count(duration_s) FROM media_reingest_queue "
               "WHERE status IN ('pending', 'running')")
    n_left, dur_known, n_known = oc.fetchone()
    oc.execute("SELECT coalesce(avg(duration_s), 1800) FROM media_reingest_queue WHERE duration_s IS NOT NULL")
    avg = float(oc.fetchone()[0])
    c["remaining_audio_h"] = (float(dur_known) + (n_left - n_known) * avg) / 3600
    c["gpu_min_per_video_h"] = 60 * c["gpu_s"] / c["audio_s"] if c["audio_s"] else None
    c["eta_days"] = eta_days(c["remaining_audio_h"] * 3600, c["audio_s"] / c["wall_s"] if c["wall_s"] else 0,
                             hours_per_night)
    oc.execute("SELECT signal, count(*) FROM media_reingest_queue GROUP BY 1 ORDER BY 2 DESC")
    c["by_signal"] = dict(oc.fetchall())
    return c


def report(oc) -> None:
    c = counts(oc)
    print(f"media_reingest_queue: {c['total']:,} files  {c['by_status']}")
    print(f"priority signal: {c['by_signal']}")
    print(f"chunks: {c['old_chunks']:,} old -> {c['new_chunks']:,} new; words {c['old_words']:,} -> "
          f"{c['new_words']:,} ({c['recovered_words']:+,}); {c['legacy_chunks']:,} legacy duplicates, "
          f"{c['superseded']:,} memories superseded")
    if c["gpu_min_per_video_h"]:
        print(f"throughput: {c['gpu_min_per_video_h']:.1f} GPU min per hour of video; "
              f"{c['audio_s'] / max(c['wall_s'], 1):.1f}x realtime end to end")
    print(f"remaining: ~{c['remaining_audio_h']:,.0f} h of video"
          + (f", ~{c['eta_days']:.0f} nights at {DEFAULT_HOURS:.0f} h/night" if c["eta_days"] else ""))
    print(report_line(c))


def scheduled(oc, mc, hours: float, limit: int | None, _sleep=time.sleep, _now=datetime.now,
              _movie_running=None) -> int:
    ensure_schema(oc)
    # Wait (inside the window) for the one-off movie job instead of giving up the whole night (2026-10-15).
    window_end = batch_deadline(_now(), hours)
    _movie_running = _movie_running or movie_job_running
    while _movie_running() and _now() < window_end - timedelta(minutes=30):
        log(f"waiting: {MOVIE_JOB} is running; re-checking in 5 min (window ends {window_end:%H:%M})")
        _sleep(300)
    why = guards(oc)
    if why:
        log(f"standing down: {why}")
        return 0
    if not try_gpu_lock(oc):
        log(f"standing down: advisory lock {GPU_WHISPER_LOCK} is held by another whisper job")
        return 0
    n = revert_stale(oc)
    if n:
        log(f"reverted {n} stale running row(s) to pending")
    deadline = batch_deadline(datetime.now(), hours)
    log(f"batch until {deadline:%H:%M}" + (f", limit {limit}" if limit else ""))
    st = run_batch(oc, mc, deadline, limit)
    c = counts(oc, hours)
    set_cfg(oc, "latest", {"day": datetime.now().strftime("%Y-%m-%d"), "line": report_line(c),
                           "batch": st, "by_status": c["by_status"], "eta_days": c["eta_days"],
                           "gpu_min_per_video_h": c["gpu_min_per_video_h"]})
    log(f"batch: {st}; {report_line(c)}")
    return 0


def dry_run(oc, mc, limit: int, out: Path, paths: list[str] | None = None) -> int:
    """Transcribe the top `limit` pending files (or the queued `paths`) for real; store nothing; write each
    result, chunks included, to `out`."""
    ensure_schema(oc)
    out.mkdir(parents=True, exist_ok=True)
    why = guards(oc, check_movie=False)
    if why:
        log(f"standing down: {why}")
        return 0
    if movie_job_running():
        log(f"note: {MOVIE_JOB} is running; GPU timings will be pessimistic")
    if paths:
        oc.execute("SELECT file_path, show, title, prior_status FROM media_reingest_queue WHERE file_path = ANY(%s)",
                   (paths,))
    else:
        oc.execute("SELECT file_path, show, title, prior_status FROM media_reingest_queue WHERE status = 'pending' "
                   "ORDER BY priority DESC, file_path LIMIT %s", (limit,))
    for item in oc.fetchall():
        r = process(oc, mc, item, timeout=4 * 3600, dry_out=out)
        log(f"DRY {r['show']} — {r['title'][:60]} [{r['prior_status']}]: old {r['old_chunks']} chunks "
            f"(+{r['legacy_chunks']} legacy) / "
            f"{r['old_words']:,} words -> new {r['new_chunks']} chunks / {r['new_words']:,} words; "
            f"{r['audio_s'] / 60:.1f} min audio, {r['gpu_s']:.0f}s GPU "
            f"({60 * r['gpu_s'] / max(r['audio_s'], 1):.2f} GPU min per video hour), {r['wall_s']:.0f}s wall")
    log(f"dry-run output in {out}")
    return 0


def selftest() -> int:
    assert is_silent_era("The Phantom of the Opera (1925)") and not is_silent_era("Freaks (1932)")
    assert eligible(VIDEO_ROOT + "Ripped Movies/x.mp4", 10**9) == "excluded dir"
    assert eligible(VIDEO_ROOT + "TVShows/A/Season 01/a.mkv", 5_000_000) == "small"
    assert eligible(VIDEO_ROOT + "TVShows/A/Season 01/a.mkv", None) == "missing"
    assert eligible(VIDEO_ROOT + "TVShows/A/Season 01/a.mkv", 10**8) is None
    assert to_remote(VIDEO_ROOT + "TVShows/a b.mp4") == "/external/videos/TVShows/a b.mp4"
    assert plex_to_local(PLEX_ROOT + "yt/x.mp4") == VIDEO_ROOT + "yt/x.mp4"
    assert priority(1, 0, 0, 0)[0] > priority(0, 0, 10**6, 10**4)[0]
    assert priority(0, 0, 5, 1)[1] == "memory_recall"
    assert plan_supersede(["a", "b", "c"], ["b", "n1"]) == ("b", ["a", "c"])
    assert plan_supersede(["a"], []) == (None, [])
    assert majority(["quarantine:documentary", "automotive", "automotive", "sci_fi"]) == "automotive"
    assert choose_source(None, ["quarantine:sci_fi"], None, "automotive") == "automotive"
    assert choose_source("crime_drama", ["documentary"], "x", "y") == "crime_drama"
    assert batch_deadline(datetime(2026, 10, 15, 23, 0), 7).hour == 6
    assert batch_deadline(datetime(2026, 10, 15, 23, 0), 2).hour == 1
    assert abs(eta_days(7 * 3600 * 10, 1.0, 7) - 10) < 1e-9 and eta_days(1, 0, 7) is None
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build-queue", action="store_true", help="add eligible files, refresh pending priorities")
    ap.add_argument("--scheduled", action="store_true", help="time-boxed batch (until 06:00 or --hours)")
    ap.add_argument("--hours", type=float, default=DEFAULT_HOURS, help=f"batch length cap (default {DEFAULT_HOURS})")
    ap.add_argument("--limit", type=int, default=None, metavar="N", help="at most N files this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="transcribe the top --limit (default 2) files; store nothing")
    ap.add_argument("--path", action="append", metavar="FILE", help="dry-run these queued files instead of the top N")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "nova-reingest-dryrun",
                    help="dry-run output directory")
    ap.add_argument("--report", action="store_true", help="queue counts, words recovered, throughput, ETA")
    ap.add_argument("--selftest", action="store_true", help="offline check of the pure functions")
    ap.add_argument("--minions", action="store_true", help="transcribe on idle whisper minions (any hour)")
    ap.add_argument("--minutes", type=float, default=25, help="--minions run length (default 25)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.minions:
        run_minions(a.minutes)
        return 0
    if a.limit is not None and a.limit < 1:
        ap.error("--limit must be >= 1")
    if not 0 < a.hours <= 12:
        ap.error("--hours must be in (0, 12]")
    if not (a.build_queue or a.scheduled or a.dry_run or a.report):
        ap.print_help()
        return 0
    oc = connect("nova_ops")
    mc = connect("nova_memories")
    try:
        ocur, mcur = oc.cursor(), mc.cursor()
        if a.build_queue:
            build_queue(ocur, mcur)
        if a.dry_run:
            dry_run(ocur, mcur, a.limit or 2, a.out, a.path)
        if a.scheduled:
            scheduled(ocur, mcur, a.hours, a.limit)
        if a.report:
            report(ocur)
        return 0
    finally:
        mc.close()
        oc.close()


if __name__ == "__main__":
    sys.exit(main())
