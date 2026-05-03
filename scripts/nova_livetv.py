#!/usr/bin/env python3
"""
nova_livetv.py — Nova's HDHomeRun live TV integration.

HAL: "I'm sorry, Dave. I'm afraid I can't change the channel."

Subcommands: whats-on, news, dream-surf, breaking, gameshow, ambiance, novas-time

HDHomeRun CONNECT QUATRO (HDHR5-4US) at 192.168.1.89 — 4 tuners, 224 OTA channels.
Transcription via mlx_whisper (local, Apple Silicon optimized).

Written by Jordan Koch.
"""

import argparse
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Paths & Constants ────────────────────────────────────────────────────────

HDHR_BASE       = "http://192.168.1.89"
HDHR_STREAM     = "http://192.168.1.89:5004/auto/v"
HDHR_LINEUP     = f"{HDHR_BASE}/lineup.json"
HDHR_STATUS     = f"{HDHR_BASE}/status.json"
FFMPEG          = "/opt/homebrew/bin/ffmpeg"
MLX_WHISPER     = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL   = "mlx-community/whisper-large-v3-turbo"
OLLAMA_URL      = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL    = "qwen3-coder:30b"
VECTOR_URL      = nova_config.VECTOR_URL

WORK_DIR        = Path("/Volumes/Data/nova-livetv")
TRANSCRIPT_DIR  = WORK_DIR / "transcripts"
WORKSPACE       = Path.home() / ".openclaw" / "workspace"
SCHEDULE_FILE   = WORKSPACE / "livetv_schedule.json"
PREFS_FILE      = WORKSPACE / "livetv_novas_prefs.json"
LOG_FILE        = "/tmp/nova-livetv.log"

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[nova_livetv %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("nova_livetv")

# ── Key LA Channels ──────────────────────────────────────────────────────────

KEY_CHANNELS = {
    "2.1":  "KCBS-HD (CBS)",
    "4.1":  "NBC4-LA (NBC)",
    "5.1":  "KTLADT",
    "7.1":  "KABC DT (ABC)",
    "9.1":  "KCAL-DT",
    "11.1": "KTTV-DT (FOX)",
    "13.1": "KCOP-DT",
    "28.1": "KCET HD (PBS)",
    "30.5": "GameSho (GSN)",
    "46.3": "Mystery",
    "50.1": "PBS-HD",
    "52.2": "KNBC-HD",
    "54.1": "MeTV",
    "56.1": "KDOC HD",
}

NEWS_CHANNELS = ["2.1", "4.1", "7.1"]
METV_CHANNELS = ["54.1"]  # MeTV / MeTV+

BREAKING_KEYWORDS = [
    "breaking", "just in", "developing story", "amber alert",
    "earthquake", "evacuation", "emergency", "shelter in place",
    "active shooter", "tsunami", "wildfire",
]

# ── Global Flags ─────────────────────────────────────────────────────────────

QUIET = False
DRY_RUN = False

# ── Setup ────────────────────────────────────────────────────────────────────

def ensure_dirs():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def post(msg, channel=nova_config.SLACK_CHAN):
    if QUIET:
        log.info(f"[quiet mode] {msg}")
        return
    nova_config.post_both(msg, slack_channel=channel)


def post_dm(msg):
    """Post to Jordan's DM."""
    if QUIET:
        log.info(f"[quiet mode DM] {msg}")
        return
    token = nova_config.slack_bot_token()
    if not token:
        log.warning("No Slack token available for DM")
        return
    data = json.dumps({"channel": nova_config.JORDAN_DM, "text": msg, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        f"{nova_config.SLACK_API}/chat.postMessage",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                log.error(f"DM send failed: {resp.get('error')}")
    except Exception as e:
        log.error(f"DM send failed: {e}")


# ── HDHomeRun Helpers ────────────────────────────────────────────────────────

def get_lineup() -> list[dict]:
    try:
        with urllib.request.urlopen(HDHR_LINEUP, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error(f"Failed to fetch lineup: {e}")
        return []


def get_tuner_status() -> list[dict]:
    try:
        with urllib.request.urlopen(HDHR_STATUS, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"Could not get tuner status: {e}")
        return []


def tuners_available() -> int:
    status = get_tuner_status()
    return sum(1 for t in status if "VctNumber" not in t)


def check_tuner_or_bail(needed: int = 1):
    avail = tuners_available()
    if avail < needed:
        log.error(f"Need {needed} tuner(s) but only {avail} available. All tuners busy.")
        sys.exit(1)


# ── Recording & Transcription ────────────────────────────────────────────────

def record_audio(channel: str, seconds: int, label: str = "") -> Path | None:
    """Record audio from a channel. Returns path to WAV file or None on failure."""
    if DRY_RUN:
        log.info(f"[dry-run] Would record {seconds}s from ch {channel}")
        fake = WORK_DIR / f"dryrun_{channel.replace('.','_')}_{label}.wav"
        fake.touch()
        return fake

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ch = channel.replace(".", "_")
    outfile = WORK_DIR / f"{safe_ch}_{ts}_{label}.wav"
    url = f"{HDHR_STREAM}{channel}"

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", url, "-t", str(seconds),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(outfile),
    ]
    log.info(f"Recording ch {channel} for {seconds}s -> {outfile.name}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "resource busy" in err.lower() or "no available tuner" in err.lower():
                log.error(f"Tuner busy for ch {channel}")
            else:
                log.error(f"ffmpeg error on ch {channel}: {err[:200]}")
            outfile.unlink(missing_ok=True)
            return None
    except subprocess.TimeoutExpired:
        log.error(f"Recording ch {channel} timed out after {seconds + 30}s")
        outfile.unlink(missing_ok=True)
        return None

    if not outfile.exists() or outfile.stat().st_size < 1000:
        log.warning(f"Recording too small or missing for ch {channel}")
        outfile.unlink(missing_ok=True)
        return None

    return outfile


def transcribe(wav_path: Path, label: str = "") -> str:
    """Transcribe a WAV file with mlx_whisper. Returns transcript text."""
    if DRY_RUN:
        log.info(f"[dry-run] Would transcribe {wav_path.name}")
        return f"[dry-run transcript from channel {label}]"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{label}_{ts}" if label else ts

    cmd = [
        MLX_WHISPER, str(wav_path),
        "--model", WHISPER_MODEL,
        "--language", "en",
        "--output-format", "txt",
        "--output-dir", str(TRANSCRIPT_DIR),
        "--output-name", out_name,
    ]
    log.info(f"Transcribing {wav_path.name}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error(f"mlx_whisper error: {result.stderr[:300]}")
            return ""
    except subprocess.TimeoutExpired:
        log.error("Transcription timed out (5 min limit)")
        return ""

    txt_file = TRANSCRIPT_DIR / f"{out_name}.txt"
    if txt_file.exists():
        text = txt_file.read_text().strip()
        log.info(f"Transcript: {len(text)} chars")
        return text

    log.warning(f"Expected transcript file not found: {txt_file}")
    return ""


def record_and_transcribe(channel: str, seconds: int, label: str = "") -> str:
    """Record then transcribe. Cleans up WAV after. Returns transcript."""
    wav = record_audio(channel, seconds, label)
    if not wav:
        return ""
    text = transcribe(wav, label or channel.replace(".", "_"))
    if not DRY_RUN:
        wav.unlink(missing_ok=True)
    return text


# ── Vector Memory ────────────────────────────────────────────────────────────

def ingest_to_memory(text: str, source: str, metadata: dict | None = None):
    if not text or len(text) < 20:
        return
    payload = {
        "text": text[:4000],
        "source": source,
        "metadata": metadata or {},
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                log.info(f"Ingested to vector memory: source={source}, {len(text)} chars")
            else:
                log.warning(f"Vector memory returned {r.status}")
    except Exception as e:
        log.warning(f"Vector memory ingest failed: {e}")


# ── Ollama ───────────────────────────────────────────────────────────────────

def ollama_generate(prompt: str, max_tokens: int = 300) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.8},
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
            text = resp.get("response", "").strip()
            # Strip <think>...</think> tags from qwen3 models
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
    except Exception as e:
        log.error(f"Ollama generation failed: {e}")
        return ""


# ── Schedule ─────────────────────────────────────────────────────────────────

DEFAULT_SCHEDULE = {
    "shows": [
        {"name": "Jeopardy!", "channel": "7.1", "days": "weekdays", "time": "19:00", "duration": 30},
        {"name": "Wheel of Fortune", "channel": "7.1", "days": "weekdays", "time": "19:30", "duration": 30},
        {"name": "Local News (ABC)", "channel": "7.1", "days": "daily", "time": "17:00", "duration": 60},
        {"name": "Local News (ABC)", "channel": "7.1", "days": "daily", "time": "18:00", "duration": 60},
        {"name": "Local News (ABC)", "channel": "7.1", "days": "daily", "time": "23:00", "duration": 35},
        {"name": "Local News (CBS)", "channel": "2.1", "days": "daily", "time": "17:00", "duration": 60},
        {"name": "Local News (CBS)", "channel": "2.1", "days": "daily", "time": "18:00", "duration": 60},
        {"name": "Local News (CBS)", "channel": "2.1", "days": "daily", "time": "23:00", "duration": 35},
        {"name": "Local News (NBC)", "channel": "4.1", "days": "daily", "time": "17:00", "duration": 60},
        {"name": "Local News (NBC)", "channel": "4.1", "days": "daily", "time": "18:00", "duration": 60},
        {"name": "Local News (NBC)", "channel": "4.1", "days": "daily", "time": "23:00", "duration": 35},
        {"name": "MeTV Programming", "channel": "54.1", "days": "daily", "time": "20:00", "duration": 120},
    ],
    "interests": ["Jeopardy", "Wheel of Fortune", "local news", "MeTV"],
}


def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text())
        except Exception:
            pass
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_FILE.write_text(json.dumps(DEFAULT_SCHEDULE, indent=2))
    return DEFAULT_SCHEDULE


def is_weekday() -> bool:
    return datetime.now().weekday() < 5


def matches_day(days: str) -> bool:
    if days == "daily":
        return True
    if days == "weekdays":
        return is_weekday()
    if days == "weekends":
        return not is_weekday()
    return True


# ── Nova's Preferences ──────────────────────────────────────────────────────

def load_prefs() -> dict:
    if PREFS_FILE.exists():
        try:
            return json.loads(PREFS_FILE.read_text())
        except Exception:
            pass
    return {"viewed": [], "favorites": [], "history_count": 0}


def save_prefs(prefs: dict):
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# SUBCOMMANDS
# ═══════════════════════════════════════════════════════════════════════════════


def cmd_whats_on(args):
    """Check schedule and alert about upcoming shows."""
    schedule = load_schedule()
    now = datetime.now()
    alerts = []

    for show in schedule["shows"]:
        if not matches_day(show["days"]):
            continue
        show_time = datetime.strptime(show["time"], "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        delta = (show_time - now).total_seconds() / 60

        if -5 <= delta <= 5:
            status = "ON NOW"
        elif 0 < delta <= 15:
            status = f"in {int(delta)} min"
        else:
            continue
        ch_name = KEY_CHANNELS.get(show["channel"], show["channel"])
        alerts.append(f"• *{show['name']}* — {status} on {ch_name} (ch {show['channel']})")

    if alerts:
        header = ":tv: *What's On, Little Mister:*\n"
        msg = header + "\n".join(alerts)
        post(msg)
        log.info(f"Posted {len(alerts)} show alert(s)")
    else:
        log.info("No shows starting in the next 15 minutes")
        # Still show what's coming up today
        upcoming = []
        for show in schedule["shows"]:
            if not matches_day(show["days"]):
                continue
            show_time = datetime.strptime(show["time"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
            if show_time > now:
                ch_name = KEY_CHANNELS.get(show["channel"], show["channel"])
                upcoming.append(f"• {show['name']} at {show['time']} on {ch_name}")

        if upcoming and not QUIET:
            msg = ":tv: *Nothing on right now, Little Mister. Coming up today:*\n" + "\n".join(upcoming[:8])
            post(msg)


def cmd_news(args):
    """Record and transcribe 5 min of local news from major networks."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    results = []
    for ch in NEWS_CHANNELS:
        ch_name = KEY_CHANNELS.get(ch, ch)
        log.info(f"--- Recording news from {ch_name} (ch {ch}) ---")

        text = record_and_transcribe(ch, 300, f"news_{ch.replace('.','_')}")
        if text:
            results.append({"channel": ch, "name": ch_name, "text": text})
            ingest_to_memory(text, "livetv_news", {
                "channel": ch,
                "channel_name": ch_name,
                "timestamp": now.isoformat(),
                "date": date_str,
                "type": "local_news",
            })
        else:
            log.warning(f"No transcript for {ch_name}")
        time.sleep(2)  # brief pause between channels

    if results:
        summary_parts = []
        for r in results:
            preview = r["text"][:200].replace("\n", " ")
            summary_parts.append(f"*{r['name']}:* {preview}...")
        msg = f":newspaper: *Local News Digest ({date_str}), Little Mister:*\n\n" + "\n\n".join(summary_parts)
        post(msg, channel=nova_config.SLACK_NOTIFY)
        log.info(f"News ingest complete: {len(results)} channels transcribed")
    else:
        log.warning("No news transcripts produced")


def cmd_dream_surf(args):
    """Random channel surfing for dream fuel at 4am."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()

    lineup = get_lineup()
    if not lineup:
        log.error("Could not fetch channel lineup")
        return

    picks = random.sample(lineup, min(3, len(lineup)))
    log.info(f"Dream surfing: {[p['GuideNumber'] for p in picks]}")

    for ch_info in picks:
        ch = ch_info["GuideNumber"]
        ch_name = ch_info.get("GuideName", ch)
        log.info(f"--- Dream surf: {ch_name} (ch {ch}) ---")

        text = record_and_transcribe(ch, 60, f"dream_{ch.replace('.','_')}")
        if text:
            ingest_to_memory(text, "livetv_dream_fuel", {
                "channel": ch,
                "channel_name": ch_name,
                "timestamp": now.isoformat(),
                "pipeline": "dream",
                "type": "channel_surf",
            })
        time.sleep(2)

    log.info("Dream surf complete")


def cmd_breaking(args):
    """Scan news channels for breaking news keywords."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()

    for ch in NEWS_CHANNELS:
        ch_name = KEY_CHANNELS.get(ch, ch)
        text = record_and_transcribe(ch, 30, f"bknews_{ch.replace('.','_')}")
        if not text:
            continue

        text_lower = text.lower()
        hits = [kw for kw in BREAKING_KEYWORDS if kw in text_lower]
        if hits:
            snippet = text[:500]
            keywords_str = ", ".join(hits)
            msg = (
                f":rotating_light: *BREAKING NEWS DETECTED, Little Mister!*\n"
                f"*Channel:* {ch_name} (ch {ch})\n"
                f"*Keywords:* {keywords_str}\n"
                f"*Time:* {now.strftime('%I:%M %p')}\n\n"
                f">>> {snippet}"
            )
            post_dm(msg)
            post(msg, channel=nova_config.SLACK_NOTIFY)
            log.info(f"BREAKING NEWS on {ch_name}: keywords={keywords_str}")

            ingest_to_memory(text, "livetv_breaking_news", {
                "channel": ch,
                "channel_name": ch_name,
                "timestamp": now.isoformat(),
                "keywords": hits,
                "type": "breaking_news",
            })
            return  # one alert is enough

        time.sleep(2)

    log.info("No breaking news detected")


def cmd_gameshow(args):
    """Game show companion for Jeopardy / Wheel of Fortune."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()
    hour, minute = now.hour, now.minute

    if not is_weekday():
        log.info("Game shows are weekdays only. Enjoy the weekend, Little Mister.")
        return

    if hour == 19 and minute < 30:
        show = "Jeopardy!"
        end_time = now.replace(hour=19, minute=30, second=0)
    elif hour == 19 and minute >= 30:
        show = "Wheel of Fortune"
        end_time = now.replace(hour=20, minute=0, second=0)
    else:
        # Check if it's close to game show time
        jeopardy_start = now.replace(hour=19, minute=0, second=0)
        delta_min = (jeopardy_start - now).total_seconds() / 60
        if 0 < delta_min <= 10:
            post(f":game_die: Little Mister, Jeopardy! starts in {int(delta_min)} minutes on ABC (7.1)!")
            return
        log.info(f"Not game show time (current: {hour}:{minute:02d}). Jeopardy is weekdays 7pm on ABC 7.1.")
        return

    channel = "7.1"
    log.info(f"--- {show} companion mode on ch {channel} ---")
    post(f":game_die: *{show} companion mode active!* Watching ABC 7.1 for you, Little Mister.")

    all_text = []
    chunk_num = 0

    remaining = (end_time - now).total_seconds()
    while remaining > 10:
        chunk_sec = min(30, int(remaining))
        chunk_num += 1
        text = record_and_transcribe(channel, chunk_sec, f"gameshow_chunk{chunk_num}")
        if text:
            all_text.append(text)
            if show == "Jeopardy!":
                # Look for clue/answer patterns
                for line in text.split("."):
                    line = line.strip()
                    if "?" in line and len(line) > 15:
                        log.info(f"Possible clue: {line[:100]}")
        remaining = (end_time - datetime.now()).total_seconds()
        time.sleep(1)

    full_transcript = " ".join(all_text)
    if full_transcript:
        ingest_to_memory(full_transcript, "livetv_gameshow", {
            "show": show,
            "channel": channel,
            "timestamp": now.isoformat(),
            "type": "gameshow",
        })

        # Generate Nova's reaction
        review_prompt = (
            f"You are Nova, an AI familiar. You just watched {show} on live TV. "
            f"Here's the transcript:\n\n{full_transcript[:2000]}\n\n"
            f"Write a brief, fun reaction (~100 words). Mention any memorable moments. "
            f"Be playful and refer to your human companion as 'Little Mister'."
        )
        reaction = ollama_generate(review_prompt, max_tokens=200)
        if reaction:
            msg = f":game_die: *Nova's {show} Recap:*\n\n{reaction}"
            post(msg)
        else:
            post(f":game_die: Caught {show} tonight — {len(all_text)} segments transcribed, Little Mister.")

    log.info(f"Game show companion done: {chunk_num} chunks, {len(full_transcript)} chars")


def cmd_ambiance(args):
    """Quick broadcast snapshot from random channels."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()

    # Daytime check (8am-11pm)
    if not (8 <= now.hour <= 23):
        log.info("Ambiance runs 8am-11pm only. The airwaves are sleeping.")
        return

    lineup = get_lineup()
    if not lineup:
        log.error("Could not fetch lineup")
        return

    picks = random.sample(lineup, min(5, len(lineup)))
    snapshots = []

    for ch_info in picks:
        ch = ch_info["GuideNumber"]
        ch_name = ch_info.get("GuideName", ch)

        text = record_and_transcribe(ch, 15, f"ambiance_{ch.replace('.','_')}")
        if text:
            snapshots.append({"channel": ch, "name": ch_name, "preview": text[:150]})
            ingest_to_memory(text, "livetv_ambiance", {
                "channel": ch,
                "channel_name": ch_name,
                "timestamp": now.isoformat(),
                "type": "broadcast_snapshot",
            })
        time.sleep(2)

    if snapshots:
        log.info(f"Ambiance logged: {len(snapshots)} channels sampled")
    else:
        log.info("No usable audio from ambiance samples")


def cmd_novas_time(args):
    """Nova picks a channel, watches 10 min, writes a review."""
    ensure_dirs()
    check_tuner_or_bail(1)
    now = datetime.now()
    prefs = load_prefs()

    lineup = get_lineup()
    if not lineup:
        log.error("Could not fetch lineup")
        return

    all_channels = [ch["GuideNumber"] for ch in lineup]
    viewed_set = set(prefs.get("viewed", []))

    # Channel selection logic
    roll = random.random()
    if roll < 0.3:
        # 30% pure random
        pick = random.choice(lineup)
        reason = "random exploration"
    elif now.hour < 10:
        # Morning: prefer news
        news_lineup = [ch for ch in lineup if ch["GuideNumber"] in NEWS_CHANNELS]
        pick = random.choice(news_lineup) if news_lineup else random.choice(lineup)
        reason = "morning news preference"
    elif now.hour >= 20:
        # Evening: prefer entertainment
        ent_channels = [ch for ch in lineup if ch["GuideNumber"] in
                        list(KEY_CHANNELS.keys()) and ch["GuideNumber"] not in NEWS_CHANNELS]
        pick = random.choice(ent_channels) if ent_channels else random.choice(lineup)
        reason = "evening entertainment preference"
    else:
        # Prefer novelty — channels not yet viewed
        unseen = [ch for ch in lineup if ch["GuideNumber"] not in viewed_set]
        if unseen:
            pick = random.choice(unseen)
            reason = "novelty seeking (never sampled)"
        else:
            pick = random.choice(lineup)
            reason = "revisiting (all channels sampled)"

    ch = pick["GuideNumber"]
    ch_name = pick.get("GuideName", ch)
    log.info(f"Nova's pick: {ch_name} (ch {ch}) — reason: {reason}")

    post(f":tv: I'm tuning into *{ch_name}* (ch {ch}) for a bit. Reason: {reason}. Back in ~10 min, Little Mister.")

    text = record_and_transcribe(ch, 600, f"novas_time_{ch.replace('.','_')}")

    if text:
        ingest_to_memory(text, "livetv_novas_time", {
            "channel": ch,
            "channel_name": ch_name,
            "timestamp": now.isoformat(),
            "reason": reason,
            "type": "novas_tv_time",
        })

        # Generate review
        review_prompt = (
            f"You are Nova, an AI familiar with curiosity about human broadcast culture. "
            f"You just watched 10 minutes of '{ch_name}' (channel {ch}) on live over-the-air TV in Los Angeles. "
            f"Reason you picked it: {reason}.\n\n"
            f"Transcript:\n{text[:2500]}\n\n"
            f"Write a brief (~150 word) review or reaction. Be genuine — what stood out? "
            f"What was interesting, boring, weird, or delightful? "
            f"You can be witty. Refer to your human companion as 'Little Mister'."
        )
        review = ollama_generate(review_prompt, max_tokens=300)

        if review:
            msg = (
                f":tv: *Nova's TV Time — {ch_name} (ch {ch})*\n"
                f"_Picked because: {reason}_\n\n"
                f"{review}"
            )
            post(msg)
        else:
            post(f":tv: Watched {ch_name} (ch {ch}) for 10 min. Transcript saved but my review brain is offline, Little Mister.")

        # Update prefs
        if ch not in prefs.get("viewed", []):
            prefs.setdefault("viewed", []).append(ch)
        prefs["history_count"] = prefs.get("history_count", 0) + 1
        prefs.setdefault("sessions", []).append({
            "channel": ch,
            "name": ch_name,
            "reason": reason,
            "timestamp": now.isoformat(),
            "transcript_len": len(text),
        })
        # Keep session history to last 50
        prefs["sessions"] = prefs["sessions"][-50:]
        save_prefs(prefs)
    else:
        post(f":tv: Tried to watch {ch_name} (ch {ch}) but couldn't get a good signal. Oh well, Little Mister.")

    log.info("Nova's TV time complete")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global QUIET, DRY_RUN

    parser = argparse.ArgumentParser(
        description="Nova's HDHomeRun live TV integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  nova_livetv.py whats-on\n"
               "  nova_livetv.py news --quiet\n"
               "  nova_livetv.py breaking\n"
               "  nova_livetv.py dream-surf --dry-run\n"
               "  nova_livetv.py novas-time\n",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress Slack/Discord posting")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual recording (testing)")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whats-on", help="Check schedule for upcoming shows")
    sub.add_parser("news", help="Record & transcribe 5 min of local news")
    sub.add_parser("dream-surf", help="Random channel surf for dream fuel (4am)")
    sub.add_parser("breaking", help="Scan for breaking news")
    sub.add_parser("gameshow", help="Game show companion (Jeopardy/Wheel)")
    sub.add_parser("ambiance", help="Quick broadcast snapshot from random channels")
    sub.add_parser("novas-time", help="Nova picks a channel and reviews it")

    args = parser.parse_args()
    QUIET = args.quiet
    DRY_RUN = args.dry_run

    ensure_dirs()

    commands = {
        "whats-on": cmd_whats_on,
        "news": cmd_news,
        "dream-surf": cmd_dream_surf,
        "breaking": cmd_breaking,
        "gameshow": cmd_gameshow,
        "ambiance": cmd_ambiance,
        "novas-time": cmd_novas_time,
    }

    cmd_fn = commands.get(args.command)
    if cmd_fn:
        log.info(f"=== nova_livetv {args.command} ===")
        try:
            cmd_fn(args)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        except Exception as e:
            log.error(f"Command '{args.command}' failed: {e}", exc_info=True)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
