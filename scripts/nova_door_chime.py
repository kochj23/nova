#!/usr/bin/env python3
"""nova_door_chime.py — someone walks up to the front door: chime every HomePod + DM Jordan a photo.

Jordan 2026-11-02: "forever more, play a tone on the homepods and let me know when someone walks up
to the door."

WHY A DAEMON: nova_protect_monitor runs every 2 min — a courier is gone by then. This polls the
Protect events API every 5 s for the front cameras only.

  trigger   person / package smart detection, or a doorbell ring, on FRONT cameras
  debounce  one alert per COOLDOWN_S (a ring always alerts) — one visit, one chime
  sound     Jordan: "Nothing loud, nothing jarring. Just something easy tone and a voice announcing
            someone at the door." -> a soft sine two-note tone, a pause, then Nova (Gracie Wise, the
            nova_voice_room XTTS voice, Whisper-checked clips) saying e.g. "Little Mister, someone is at
            the front door." Clips pre-rendered once (--prerender) to /Volumes/Data/AI/door-chime/.
  chime     tone + announcement streamed over AirPlay (pyatv RAOP, no pairing) to every speaker in
            service_config door_chime/speakers, in parallel at volume 50, which the HomePod keeps
            afterwards (a pre-stream volume read over RAOP is a placeholder, so no restore). It DOES interrupt
            whatever that HomePod was playing (AirPlay takes the speaker) — same as a doorbell.
  quiet     22:00-07:00 no chime (Slack DM still goes out); service_config door_chime/quiet
  DM        Jordan's Slack DM (pushes to his phone) with the event thumbnail
  audit     nova_ops.door_chime_log (one row per alert: cameras, kinds, chimed speakers, outcome)

Config (service_config, service='door_chime'): enabled (bool, kill switch), speakers
([{name, host}]), volume (int 0-100), quiet ({"start":22,"end":7}), cooldown_s.
Runs on the Studio (homebrew python3.14 has pyatv) as LaunchDaemon net.digitalnoise.daemon.nova-door-chime.

Usage: nova_door_chime.py --daemon | --prerender [--force] | --test-chime [--kind person|package|ring] [--speaker NAME] | --selftest
"""
from __future__ import annotations

import asyncio
import json
import math
import struct
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SERVICE = "door_chime"
FRONT = ("Exterior - Front Door Left", "Exterior - Front Middle", "Exterior - Front Right", "Exterior - Front Left")
WANT = {"person", "package"}
HOME = Path("/Volumes/Data/AI/door-chime")
CHIME = HOME / "tone.wav"
SNAP_DIR = HOME / "snaps"
ANNOUNCE = {
    "person":  "Little Mister, someone is at the front door.",
    "package": "Little Mister, a package was just left at the front door.",
    "ring":    "Little Mister, someone rang the doorbell.",
}
POLL_S = 5
DEFAULTS = {
    "enabled": True,
    "volume": 50,                      # Jordan 2026-11-02 "louder than that, for sure" (25 was too quiet); the HomePod keeps this level
    "cooldown_s": 120,
    "quiet": {"start": 22, "end": 7},
    # every HomePod that streams with no pairing (pyatv scan 2026-11-02). Apple TVs need pairing.
    "speakers": [{"name": "OfficePod", "host": "192.168.1.91"}, {"name": "BackDoorPod", "host": "192.168.1.26"},
                 {"name": "GarPod", "host": "192.168.1.85"}, {"name": "Outpod", "host": "192.168.1.70"},
                 {"name": "MBath", "host": "192.168.1.108"}, {"name": "GBath", "host": "192.168.1.72"},
                 {"name": "Dylan’s Room", "host": "192.168.1.52"}],
}
DDL = """CREATE TABLE IF NOT EXISTS door_chime_log (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), event_id text, camera text,
  kinds text[], ring boolean, chimed text[], outcome text, detail text)"""


def log(m):
    print(f"[door-chime {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (selftest) ──────────────────────────────────────────────────────────────
def classify(ev: dict, front_ids: set) -> str | None:
    """'ring' | 'package' | 'person' | None for one Protect event."""
    if ev.get("camera") not in front_ids:
        return None
    if ev.get("type") == "ring":
        return "ring"
    kinds = set(ev.get("smartDetectTypes") or [])
    if "package" in kinds:
        return "package"
    if "person" in kinds:
        return "person"
    return None


def in_quiet(hour: int, quiet: dict) -> bool:
    s, e = quiet.get("start", 22), quiet.get("end", 7)
    return (hour >= s or hour < e) if s > e else (s <= hour < e)


def should_alert(kind: str, now: float, last_alert: float, cooldown: int) -> bool:
    return kind == "ring" or now - last_alert >= cooldown


def make_chime(path: Path, rate=44100):
    """Soft, unhurried two-note tone (G5 then E5): pure sine + a quiet octave, 60 ms fade-in, long
    gentle decay, peak about -6 dBFS. Nothing sharp, nothing loud. 16-bit stereo, ~2.2 s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for freq, dur in ((783.99, 1.0), (659.25, 1.2)):
        for i in range(int(rate * dur)):
            t = i / rate
            env = min(1.0, t / 0.06) * math.exp(-2.2 * t)
            s = env * (0.85 * math.sin(2 * math.pi * freq * t) + 0.15 * math.sin(4 * math.pi * freq * t))
            v = int(max(-1, min(1, s * 0.5)) * 32767)
            frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))


def clip(kind: str) -> Path:
    return HOME / f"alert-{kind}.wav"


def prerender(force=False, attempts=8):
    """Gracie Wise announcement per kind (Whisper-checked, as nova_voice_room does), then
    tone + 0.5 s pause + voice -> alert-<kind>.wav at 44.1 kHz stereo. Falls back to macOS say."""
    import os
    import random
    import subprocess
    import nova_voice_room as vr
    HOME.mkdir(parents=True, exist_ok=True)
    make_chime(CHIME)
    tts = bc = None
    for kind, text in ANNOUNCE.items():
        out = clip(kind)
        if out.exists() and not force:
            continue
        voice = HOME / f"voice-{kind}.wav"
        if not voice.exists():          # --force rebuilds the mix, keeps the checked voice
            try:
                os.environ.setdefault("TTS_HOME", vr.TTS_HOME)
                import torch
                import nova_speaks as ns
                import nova_speaks_narration as nn
                if tts is None:
                    tts, bc = ns.load_tts(), nn.BackCheck()
                tmp = voice.with_suffix(".tmp.wav")
                for seed in range(attempts):
                    torch.manual_seed(seed); random.seed(seed)
                    tts.tts_to_file(text=text, speaker=vr.XTTS_SPEAKER, language="en", file_path=str(tmp), **nn.XTTS_KW)
                    heard = bc.transcribe(str(tmp)) if bc.backend != "none" else text
                    if vr.clip_ok(text, heard, nn.wer, nn._norm_words):
                        os.replace(tmp, voice); log(f"voice {kind} (seed {seed}): {heard.strip()}"); break
                    log(f"reject {kind} seed {seed}: {heard.strip()}")
                else:
                    tmp.unlink(missing_ok=True)
            except Exception as e:
                log(f"xtts unavailable ({type(e).__name__}: {e}); using say")
            if not voice.exists():
                subprocess.run(["say", "-v", vr.FALLBACK_VOICE, "-o", str(voice), "--file-format=WAVE",
                                "--data-format=LEI16@22050", "--", text], check=True, timeout=30)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(CHIME), "-i", str(voice), "-filter_complex",
                        "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[t];"
                        "[1:a]aresample=44100,aformat=channel_layouts=stereo,adelay=500|500[v];"
                        "[t][v]concat=n=2:v=0:a=1,loudnorm=I=-14:TP=-1:LRA=11[o]",
                        "-map", "[o]", "-ar", "44100", "-ac", "2", "-sample_fmt", "s16", str(out)],
                       check=True, timeout=60)
        log(f"built {out}")


# ── config / audit ─────────────────────────────────────────────────────────────────────
def _conn():
    import nova_dsn
    c = nova_dsn.pg_connect(); c.autocommit = True
    return c


def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("SELECT key, value FROM service_config WHERE service=%s", (SERVICE,))
            for k, v in cur.fetchall():
                cfg[k] = v if not isinstance(v, str) else json.loads(v)
        c.close()
    except Exception as e:
        log(f"config read failed, using defaults: {e}")
    return cfg


def record(**row):
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute(DDL)
            cur.execute("INSERT INTO door_chime_log (event_id, camera, kinds, ring, chimed, outcome, detail) "
                        "VALUES (%(event_id)s, %(camera)s, %(kinds)s, %(ring)s, %(chimed)s, %(outcome)s, %(detail)s)",
                        {"detail": None, "chimed": [], **row})
        c.close()
    except Exception as e:
        log(f"audit write failed: {e}")


# ── chime ──────────────────────────────────────────────────────────────────────────────
async def _chime_one(spk: dict, volume: int, path: Path) -> str | None:
    import pyatv
    loop = asyncio.get_running_loop()
    devs = await pyatv.scan(loop, hosts=[spk["host"]], timeout=4)
    if not devs:   # DHCP moved it: find by name
        devs = [d for d in await pyatv.scan(loop, timeout=5) if d.name == spk["name"]]
    if not devs:
        raise RuntimeError("not found")
    atv = await pyatv.connect(devs[0], loop)
    try:
        # No read-then-restore: over RAOP pyatv reports a placeholder volume until a stream is up
        # (2026-11-02 test read OfficePod 0.0 / MBath 75), so "restoring" it could mute or blast a
        # speaker. Like nova_voice_room, the HomePod simply keeps the gentle chime volume.
        await atv.audio.set_volume(volume)
        await atv.stream.stream_file(str(path))
        return spk["name"]
    finally:
        atv.close()


async def _chime_all(speakers, volume, path):
    res = await asyncio.gather(*(asyncio.wait_for(_chime_one(s, volume, path), 30) for s in speakers),
                               return_exceptions=True)
    ok = [r for r in res if isinstance(r, str)]
    bad = [f"{s['name']}: {type(r).__name__} {r}"[:80] for s, r in zip(speakers, res) if not isinstance(r, str)]
    return ok, bad


def chime(cfg, kind="person") -> tuple[list, list]:
    if not clip(kind).exists():
        prerender()
    return asyncio.run(_chime_all(cfg["speakers"], int(cfg["volume"]), clip(kind)))


# ── notify ─────────────────────────────────────────────────────────────────────────────
def dm(client, ev, kind, cam_name):
    import nova_config
    from nova_protect_monitor import _get_event_thumbnail, slack_upload_image, slack_post
    what = {"ring": "🔔 Doorbell rang", "package": "📦 Package at the door",
            "person": "🚶 Someone's walking up to the door"}[kind]
    when = time.strftime("%-I:%M %p", time.localtime(ev["start"] / 1000))
    text = f"{what} — {cam_name} at {when}."
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    img = SNAP_DIR / f"{ev['id']}.jpg"
    time.sleep(1.5)   # let Protect write the event thumbnail
    ok = _get_event_thumbnail(client, ev["id"], img) or client.get_snapshot(ev["camera"], img)
    if not (ok and slack_upload_image(str(img), nova_config.JORDAN_DM, title=what, comment=text)):
        slack_post(text, channel=nova_config.JORDAN_DM)
    return text


def daemon():
    from nova_protect_monitor import ProtectClient
    import threading
    client = ProtectClient()
    while not client.login():
        log("Protect login failed; retry in 60 s"); time.sleep(60)
    cams = {c["id"]: c["name"] for c in client.get_bootstrap()["cameras"] if c["name"] in FRONT}
    log(f"watching {sorted(cams.values())}")
    prerender()   # no-op once the clips exist
    seen, last_alert, since = {}, 0.0, int(time.time() * 1000) - 10_000
    cfg, cfg_at = load_cfg(), time.time()
    while True:
        try:
            if time.time() - cfg_at > 60:
                cfg, cfg_at = load_cfg(), time.time()
            for ev in client.get_events(since_ms=since, limit=50) or []:
                eid = ev.get("id")
                kind = classify(ev, set(cams))
                if not kind or eid in seen:
                    continue
                seen[eid] = time.time()
                if not cfg.get("enabled", True):
                    continue
                if not should_alert(kind, time.time(), last_alert, int(cfg["cooldown_s"])):
                    continue
                last_alert = time.time()
                quiet = in_quiet(datetime.now().hour, cfg["quiet"])
                result = {}
                if not quiet:   # chime in parallel with the photo DM — the tone is the point
                    t = threading.Thread(target=lambda: result.update(zip(("ok", "bad"), chime(cfg, kind))))
                    t.start()
                text = dm(client, ev, kind, cams[ev["camera"]])
                if not quiet:
                    t.join(40)
                ok, bad = result.get("ok", []), result.get("bad", [])
                outcome = "quiet_hours" if quiet else ("chimed" if ok else "chime_failed")
                record(event_id=eid, camera=cams[ev["camera"]], kinds=sorted(ev.get("smartDetectTypes") or []),
                       ring=kind == "ring", chimed=ok, outcome=outcome, detail="; ".join(bad) or None)
                log(f"{outcome}: {text} chimed={ok} failed={bad}")
            since = int(time.time() * 1000) - 60_000          # overlap; `seen` dedups
            cutoff = time.time() - 600
            seen = {k: v for k, v in seen.items() if v > cutoff}
        except Exception as e:
            log(f"poll error: {type(e).__name__}: {e}")
            time.sleep(10)
        time.sleep(POLL_S)


def main(argv):
    if "--selftest" in argv:
        front = {"A"}
        assert classify({"camera": "A", "type": "ring"}, front) == "ring"
        assert classify({"camera": "A", "type": "smartDetectZone", "smartDetectTypes": ["person"]}, front) == "person"
        assert classify({"camera": "A", "smartDetectTypes": ["person", "package"]}, front) == "package"
        assert classify({"camera": "A", "smartDetectTypes": ["vehicle"]}, front) is None
        assert classify({"camera": "B", "type": "ring"}, front) is None          # not a front camera
        q = {"start": 22, "end": 7}
        assert in_quiet(23, q) and in_quiet(3, q) and not in_quiet(7, q) and not in_quiet(21, q)
        assert in_quiet(13, {"start": 13, "end": 14}) and not in_quiet(14, {"start": 13, "end": 14})
        assert should_alert("person", 1000, 0, 120) and not should_alert("person", 1000, 950, 120)
        assert should_alert("ring", 1000, 999, 120)                              # a ring always alerts
        print("selftest OK")
        return 0
    if "--test-chime" in argv:
        cfg = load_cfg()
        if "--speaker" in argv:
            name = argv[argv.index("--speaker") + 1]
            cfg["speakers"] = [s for s in cfg["speakers"] if s["name"] == name]
        kind = argv[argv.index("--kind") + 1] if "--kind" in argv else "person"
        ok, bad = chime(cfg, kind)
        print(f"chimed: {ok}\nfailed: {bad}")
        return 0 if ok else 1
    if "--prerender" in argv:
        prerender(force="--force" in argv)
        return 0
    if "--daemon" in argv:
        daemon()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
