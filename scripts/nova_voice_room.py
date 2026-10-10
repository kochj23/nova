#!/usr/bin/env python3
"""
nova_voice_room.py — Nova's live voice in ONE room (Jordan's office HomePod, "OfficePod").

The KITT moment: "Little Mister, I'm detecting smoke in the house." Spoken ONLY for genuine
emergencies coming through the event bus (nova_notifier calls dispatch(ev) for every
non-duplicate event; classify() decides, everything else is ignored).

What speaks (classify):
  life-safety  category in LIFE_SAFETY_KINDS (smoke, co/carbon_monoxide, gas, water_leak/leak/flood,
               fire, life_safety) at warning/critical, or meta {"voice": "<kind>"} — ANY hour.
  security     nova_security_organ CRITICAL (quorum-confirmed new device on the LAN), not [TEST] —
               daytime only.
  Nothing else. Traffic "SMOKE", infra, news, journal BREAKING posts never speak.

Gates (in the child process, so the notifier never blocks):
  kill switch  service_config nova_voice_room/enabled = false  -> silent
  quiet hours  22:00-07:00 local: life-safety only
  dedup        same dedup_key (or kind) spoken in the last 30 min -> skip
  rate limit   max 4 non-life-safety utterances per hour
State/audit: nova_ops.nova_voice_room_log (one row per decision).

Voice: fixed short sentences per kind, pre-rendered in XTTS "Gracie Wise" (Nova Speaks voice)
and cached under $NOVA_VOICE_CACHE (default /Volumes/Data/AI/tts/voice_room). XTTS cold load is
~70 s, far too slow for an emergency, so a missing clip falls back to macOS `say` (Samantha)
instantly. `--prerender` warms the cache. Playback: pyatv RAOP stream to the HomePod (no pairing).

  nova_voice_room.py --test [--volume 20]   # one short, quiet test utterance (respects kill switch)
  nova_voice_room.py --prerender            # render all sentences in Gracie Wise
  nova_voice_room.py --event <id>           # what dispatch() runs (detached)
  nova_voice_room.py --classify <id>        # dry-run: what would happen for this event

Written by Jordan Koch.
"""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
HERE = Path(__file__).resolve().parent
LOG = HERE.parent / "logs" / "nova_voice_room.log"
CACHE = Path(os.environ.get("NOVA_VOICE_CACHE", "/Volumes/Data/AI/tts/voice_room"))
TTS_HOME = "/Volumes/Data/AI/tts"
XTTS_SPEAKER = "Gracie Wise"
FALLBACK_VOICE = "Samantha"
DEFAULT_SPEAKER = {"name": "OfficePod", "host": "192.168.1.91"}

QUIET_START, QUIET_END = 22, 7          # 22:00-07:00 local: life-safety only
DEDUP_S = 1800
MAX_PER_HOUR = 4                        # non-life-safety
VOLUME = {"life_safety": 55, "security": 40, "test": 20}

SENTENCES = {
    "smoke":       "Little Mister, I'm detecting smoke in the house. Please check it now.",
    "co":          "Little Mister, I'm detecting carbon monoxide. Get to fresh air and check it now.",
    "gas":         "Little Mister, I'm detecting a gas leak in the house. Please check it now.",
    "water_leak":  "Little Mister, I'm detecting a water leak. Please check it now.",
    "fire":        "Little Mister, I'm detecting a fire alarm. Please check it now.",
    "life_safety": "Little Mister, I'm detecting a safety emergency at the house. Check your phone now.",
    "intrusion":   "Little Mister, I'm detecting an unknown device on our network. Details are in Slack.",
    "test":        "Little Mister, this is a quiet test of my room voice. Nothing is wrong.",
}
LIFE_SAFETY_KINDS = {
    "smoke": "smoke", "fire": "fire", "co": "co", "carbon_monoxide": "co", "gas": "gas",
    "water_leak": "water_leak", "leak": "water_leak", "flood": "water_leak",
    "life_safety": "life_safety",
}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if os.environ.get("NOVA_TEST_QUIET") != "1":
        print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── pure policy ─────────────────────────────────────────────────────────────
def classify(ev: dict):
    """-> (kind, life_safety) or None. Pure; no I/O."""
    meta = ev.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    level = ev.get("level") or "info"
    cat = (ev.get("category") or "").lower()
    want = str(meta.get("voice") or "").lower()
    if want in LIFE_SAFETY_KINDS:
        return LIFE_SAFETY_KINDS[want], True
    if cat in LIFE_SAFETY_KINDS and level in ("warning", "critical"):
        return LIFE_SAFETY_KINDS[cat], True
    title = ev.get("title") or ""
    if (ev.get("source") == "nova_security_organ" and level == "critical"
            and cat == "security" and not title.startswith("[TEST]")):
        return "intrusion", False
    return None


def in_quiet_hours(hour: int) -> bool:
    return hour >= QUIET_START or hour < QUIET_END


def gate(kind, life_safety, hour, enabled, recent_same, recent_hour):
    """-> None to speak, else the reason it stays silent. Pure."""
    if not enabled:
        return "disabled"
    if kind == "test":
        return None
    if in_quiet_hours(hour) and not life_safety:
        return "quiet_hours"
    if recent_same:
        return "dedup"
    if not life_safety and recent_hour >= MAX_PER_HOUR:
        return "rate_limited"
    return None


def clip_path(text: str) -> Path:
    return CACHE / f"{hashlib.sha256((XTTS_SPEAKER + '|' + text).encode()).hexdigest()[:16]}.wav"


# ── state (PG) ──────────────────────────────────────────────────────────────
def _conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(DSN, connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor)


def load_state(dedup_key, kind):
    """-> (enabled, speaker, recent_same, recent_hour). Fail-SAFE on the switch: no DB = enabled
    (an emergency must not be silenced by a PG outage), counters 0."""
    enabled, speaker, same, hour = True, dict(DEFAULT_SPEAKER), False, 0
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT key, value FROM service_config WHERE service='nova_voice_room'")
            cfg = {r["key"]: r["value"] for r in cur.fetchall()}
            enabled = cfg.get("enabled", True) not in (False, "false", 0)
            if isinstance(cfg.get("speaker"), dict):
                speaker.update(cfg["speaker"])
            cur.execute("SELECT count(*) AS n FROM nova_voice_room_log WHERE outcome='spoken' "
                        "AND ts > now() - make_interval(secs => %s) AND (dedup_key = %s OR kind = %s)",
                        (DEDUP_S, dedup_key or "", kind))
            same = cur.fetchone()["n"] > 0
            cur.execute("SELECT count(*) AS n FROM nova_voice_room_log WHERE outcome='spoken' "
                        "AND NOT life_safety AND kind <> 'test' AND ts > now() - interval '1 hour'")
            hour = cur.fetchone()["n"]
    except Exception as e:
        log(f"state read failed (fail-open to speak): {e}")
    return enabled, speaker, same, hour


def record(event_id, dedup_key, kind, life_safety, text, engine, outcome, detail=None):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO nova_voice_room_log (event_id, dedup_key, kind, life_safety, text, "
                        "engine, outcome, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (event_id, dedup_key, kind, life_safety, text, engine, outcome, detail))
    except Exception as e:
        log(f"log row failed: {e}")


# ── audio ───────────────────────────────────────────────────────────────────
def render(text: str):
    """-> (path, engine). Cached Gracie Wise clip, else macOS say (fast)."""
    p = clip_path(text)
    if p.exists() and p.stat().st_size > 1000:
        return p, "xtts"
    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"nova_voice_room_{os.getpid()}.wav"
    subprocess.run(["say", "-v", FALLBACK_VOICE, "-o", str(out), "--file-format=WAVE",
                    "--data-format=LEI16@22050", "--", text], check=True, timeout=30)
    return out, "say"


def clip_ok(text: str, heard: str, wer_fn, norm_fn) -> bool:
    """XTTS often babbles a word after the sentence ('...check it now. Gile.'). Accept a clip only
    when Whisper hears no extra words and at most ~10% substitutions. Pure."""
    import re
    ref, hyp = (re.sub(r"(?i)\bmister\b", "Mr", s) for s in (text, heard))
    return len(norm_fn(hyp)) <= len(norm_fn(ref)) and wer_fn(ref, hyp) <= 0.1


def prerender(force=False, attempts=8):
    os.environ.setdefault("TTS_HOME", TTS_HOME)
    sys.path.insert(0, str(HERE))
    import random
    import torch
    import nova_speaks as ns
    import nova_speaks_narration as nn
    CACHE.mkdir(parents=True, exist_ok=True)
    tts, bc = ns.load_tts(), nn.BackCheck()
    for kind, text in SENTENCES.items():
        p = clip_path(text)
        if p.exists() and not force:
            continue
        tmp = p.with_suffix(".tmp.wav")
        for seed in range(attempts):
            torch.manual_seed(seed)
            random.seed(seed)
            tts.tts_to_file(text=text, speaker=XTTS_SPEAKER, language="en", file_path=str(tmp), **nn.XTTS_KW)
            heard = bc.transcribe(str(tmp)) if bc.backend != "none" else text
            if clip_ok(text, heard, nn.wer, nn._norm_words):
                os.replace(tmp, p)
                log(f"prerendered {kind} -> {p.name} (seed {seed}): {heard.strip()}")
                break
            log(f"reject {kind} seed {seed}: {heard.strip()}")
        else:
            tmp.unlink(missing_ok=True)
            p.unlink(missing_ok=True)
            log(f"no clean clip for {kind}; it will fall back to say")


async def _play(path: Path, speaker: dict, volume: int):
    import pyatv
    loop = asyncio.get_running_loop()
    devs = await pyatv.scan(loop, hosts=[speaker["host"]], timeout=5)
    devs = [d for d in devs if d.name == speaker["name"]] or \
           [d for d in await pyatv.scan(loop, timeout=6) if d.name == speaker["name"]]
    if not devs:
        raise RuntimeError(f"speaker {speaker['name']} not found")
    atv = await pyatv.connect(devs[0], loop)
    try:
        await atv.audio.set_volume(volume)
        t = time.time()
        await atv.stream.stream_file(str(path))
        return f"{devs[0].name}@{devs[0].address} vol={volume} played {time.time() - t:.1f}s"
    finally:
        atv.close()


def play(path: Path, speaker: dict, volume: int, retries: int = 2):
    last = None
    for i in range(retries):
        try:
            return asyncio.run(asyncio.wait_for(_play(path, speaker, volume), 60))
        except Exception as e:
            last = e
            log(f"play attempt {i + 1} failed: {e!r}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"play failed: {last!r}")


# ── entry points ────────────────────────────────────────────────────────────
def speak(kind, life_safety, event_id=None, dedup_key=None, volume=None, hour=None):
    text = SENTENCES[kind]
    enabled, speaker, same, per_hour = load_state(dedup_key, kind)
    hour = datetime.now().hour if hour is None else hour
    why = gate(kind, life_safety, hour, enabled, same, per_hour)
    if why:
        log(f"silent ({why}) kind={kind} event={event_id}")
        record(event_id, dedup_key, kind, life_safety, text, None, f"skipped:{why}")
        return why
    vol = volume or VOLUME["test" if kind == "test" else ("life_safety" if life_safety else "security")]
    engine = None
    try:
        path, engine = render(text)
        detail = play(path, speaker, vol)
        log(f"SPOKE kind={kind} event={event_id} engine={engine} {detail}")
        record(event_id, dedup_key, kind, life_safety, text, engine, "spoken", detail)
        return "spoken"
    except Exception as e:
        log(f"FAILED kind={kind} event={event_id}: {e}")
        record(event_id, dedup_key, kind, life_safety, text, engine, "error", str(e)[:500])
        return "error"


def dispatch(ev: dict) -> bool:
    """Called by nova_notifier for each fresh event. Cheap: classify, then hand off to a
    detached child (gates + render + playback). Never raises."""
    try:
        if not classify(ev):
            return False
        # the child logs to LOG itself; stdout is dropped (it would double every line)
        with open(LOG, "a") as err:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--event", str(ev["id"])],
                             stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        return True
    except Exception as e:
        log(f"dispatch failed: {e}")
        return False


def _event(event_id, attempts=3):
    """Fetch the event row; 3 tries with backoff (one PG blip must not drop a smoke alarm)."""
    for i in range(attempts):
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT id, source, level, category, title, dedup_key, meta FROM telemetry.events "
                            "WHERE id=%s", (event_id,))
                return cur.fetchone()
        except Exception as e:
            if i == attempts - 1:
                raise
            log(f"event {event_id} read attempt {i + 1} failed: {e}")
            time.sleep(1 * (i + 1))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's room voice (OfficePod) for emergencies.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true", help="speak one short quiet test sentence")
    g.add_argument("--prerender", action="store_true", help="render all sentences in Gracie Wise")
    g.add_argument("--event", type=int, help="handle telemetry.events id (used by dispatch)")
    g.add_argument("--classify", type=int, help="dry-run classification of an event id")
    ap.add_argument("--volume", type=int, help="override volume 0-100")
    ap.add_argument("--force", action="store_true", help="with --prerender: re-render existing clips")
    a = ap.parse_args(argv)
    if a.prerender:
        prerender(force=a.force)
        return 0
    if a.test:
        return 0 if speak("test", False, volume=a.volume or VOLUME["test"]) == "spoken" else 1
    ev = _event(a.event or a.classify)
    if not ev:
        log(f"event {a.event or a.classify} not found")
        return 1
    c = classify(dict(ev))
    if a.classify:
        print(json.dumps({"event": ev["id"], "classify": c, "quiet_hours": in_quiet_hours(datetime.now().hour)}))
        return 0
    if c:
        speak(c[0], c[1], event_id=ev["id"], dedup_key=ev["dedup_key"], volume=a.volume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
