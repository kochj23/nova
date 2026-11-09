#!/usr/bin/env python3
"""nova_door_chime.py — someone walks up to the front door: chime every HomePod + DM Jordan a photo.

Jordan 2026-11-02: "forever more, play a tone on the homepods and let me know when someone walks up
to the door."

SOURCE: Frigate on nova-core1 (http://192.168.1.2:8971, no auth — Jordan: "You don't need to login to
the cameras with this"). No Protect login, no Keychain for the cameras. Polls /api/events every 3 s.

  trigger   Frigate "person" on the doorstep camera(s) whose box is >= min_box_h (0.4) of the frame
            tall (walkway/porch) OR overlaps the MAILBOX zone on front_door (Jordan: "focus on the mailbox"). People on the far sidewalk are ~0.23-0.26 tall (measured
            2026-11-02) and do NOT chime. An in-progress event is re-checked every poll, so someone
            who starts small at the gate fires as they come up the walk.
            FRIGATE NAMES ARE NOT THE CAMERA NAMES (frames checked 2026-11-02): front_door = "Exterior -
            Front Right" (porch + walkway), front_door_patio = "Exterior - Front Door Left" (was a black
            frame), front_yard = "Exterior - Dylan" and front_yard_alt = "Exterior - Front Middle" (both
            street views -> passers-by, not used), exterior_front_right = **Interior - Garage** (never use).
  debounce  a visit = close detections with gaps < cooldown_s (600). Only the first of a visit chimes;
            someone lingering on the porch keeps the visit open (replay of 11-01: 9 chimes -> 1).
  sound     Jordan: "Nothing loud, nothing jarring. Just something easy tone and a voice announcing
            someone at the door." -> a soft sine two-note tone, a pause, then Nova (Gracie Wise, the
            nova_voice_room XTTS voice, Whisper-checked clips) saying e.g. "Little Mister, someone is at
            the front door." Clips pre-rendered once (--prerender) to /Volumes/Data/AI/door-chime/.
  chime     tone + announcement streamed over AirPlay (pyatv RAOP, no pairing) to every speaker in
            service_config door_chime/speakers, in parallel at volume 50. Jordan 2026-11-09: only garage,
            outdoor, master bath, guest bath (+ master bedroom) — the dogs; never office/living/dining/
            kitchen. Each speaker's level is read from Home Assistant first and restored after (the RAOP
            read is a placeholder; HA's is real). It DOES interrupt whatever that HomePod was playing.
  quiet     22:00-07:00 no chime (Slack DM still goes out); service_config door_chime/quiet
  DM        Jordan's Slack DM (pushes to his phone) with Frigate's event snapshot
  audit     nova_ops.door_chime_log (one row per alert: cameras, kinds, chimed speakers, outcome)

Config (service_config, service='door_chime'): enabled (bool, kill switch), speakers
([{name, host}]), volume (int 0-100), quiet ({"start":22,"end":7}), cooldown_s, cameras
({frigate_name: label}), min_box_h.
Runs on the Studio (homebrew python3.14 has pyatv) as LaunchAgent net.digitalnoise.nova-door-chime
(gui session: the Slack bot token is in the login Keychain).

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
FRIGATE = "http://192.168.1.2:8971"
HA = "http://127.0.0.1:8123"                 # Home Assistant on the Studio: pre-chime volume read + restore
HOME = Path("/Volumes/Data/AI/door-chime")
CHIME = HOME / "tone.wav"
SNAP_DIR = HOME / "snaps"
ANNOUNCE = {
    "person":  "Little Mister, someone is at the front door.",
    "package": "Little Mister, a package was just left at the front door.",
    "ring":    "Little Mister, someone rang the doorbell.",
}
POLL_S = 3
DEFAULTS = {
    "enabled": True,
    "volume": 50,                      # Jordan 2026-11-02 "louder than that, for sure" (25 was too quiet); the HomePod keeps this level
    "cooldown_s": 600,                 # quiet gap: alert only if nobody was at the door for 10 min
    "min_box_h": 0.4,
    # Jordan 2026-11-02 "focus on the mailbox": the mailbox on the porch post in front_door's view,
    # [x1, y1, x2, y2] as frame fractions. Far-sidewalk walkers' boxes start at y ~0.42, below it.
    "zones": {"front_door": [[0.60, 0.15, 0.82, 0.40]]},
    "cameras": {"front_door": "Exterior - Front Right", "front_door_patio": "Exterior - Front Door Left"},
    "quiet": {"start": 22, "end": 7},
    # Jordan 2026-11-09: the chime set the dogs off -> only these rooms. entity = HA media_player
    # used to restore the volume afterwards. Apple TVs need pairing.
    "speakers": [{"name": "GarPod", "host": "192.168.1.85", "entity": "media_player.garpod"},
                 {"name": "Outpod", "host": "192.168.1.70", "entity": "media_player.outpod"},
                 {"name": "MBath", "host": "192.168.1.108", "entity": "media_player.mbath"},
                 {"name": "GBath", "host": "192.168.1.72", "entity": "media_player.gbath"}],
}
DDL = """CREATE TABLE IF NOT EXISTS door_chime_log (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), event_id text, camera text,
  kinds text[], ring boolean, chimed text[], outcome text, detail text)"""


def log(m):
    print(f"[door-chime {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (selftest) ──────────────────────────────────────────────────────────────
def overlaps(box, zone) -> bool:
    """Frigate box [x, y, w, h] vs zone [x1, y1, x2, y2], all frame fractions."""
    x, y, w, h = box
    return x < zone[2] and x + w > zone[0] and y < zone[3] and y + h > zone[1]


def classify(ev: dict, cams: dict, min_h: float, zones: dict | None = None) -> str | None:
    """'person' when a Frigate person on a doorstep camera is close (box h >= min_h) or at the
    mailbox (box overlaps a zone for that camera), else None."""
    if ev.get("camera") not in cams or ev.get("label") != "person":
        return None
    box = (ev.get("data") or {}).get("box") or []
    if len(box) != 4:
        return None
    at_mailbox = any(overlaps(box, z) for z in (zones or {}).get(ev["camera"], []))
    return "person" if box[3] >= min_h or at_mailbox else None


def in_quiet(hour: int, quiet: dict) -> bool:
    s, e = quiet.get("start", 22), quiet.get("end", 7)
    return (hour >= s or hour < e) if s > e else (s <= hour < e)


def should_alert(kind: str, now: float, last_alert: float, cooldown: int) -> bool:
    return now - last_alert >= cooldown


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
        # Never read the volume here: over RAOP pyatv reports a placeholder until a stream is up
        # (2026-11-02 read OfficePod 0.0 / MBath 75). chime() restores from Home Assistant instead.
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


def _ha(path, body=None):
    import urllib.request
    import nova_ha_metrics
    req = urllib.request.Request(f"{HA}{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {nova_ha_metrics.get_ha_token()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def ha_volumes(speakers) -> dict:
    """{entity: volume_level} before the chime, read from Home Assistant (its apple_tv integration
    reports the HomePod's real level; pyatv over RAOP does not). Unknown -> left out, not restored."""
    vols = {}
    for s in speakers:
        try:
            v = _ha(f"/api/states/{s['entity']}")["attributes"].get("volume_level")
            if v is not None:
                vols[s["entity"]] = v
        except Exception as e:
            log(f"volume read {s.get('entity') or s['name']}: {e}")
    return vols


def restore_volumes(vols: dict):
    for entity, v in vols.items():
        try:
            _ha("/api/services/media_player/volume_set", {"entity_id": entity, "volume_level": v})
        except Exception as e:
            log(f"volume restore {entity} -> {v}: {e}")


def chime(cfg, kind="person") -> tuple[list, list]:
    """Jordan 2026-11-09: put the volume back where it was. Chime at cfg volume, then restore each
    speaker's pre-chime level via Home Assistant."""
    if not clip(kind).exists():
        prerender()
    before = ha_volumes([s for s in cfg["speakers"] if s.get("entity")])
    try:
        return asyncio.run(_chime_all(cfg["speakers"], int(cfg["volume"]), clip(kind)))
    finally:
        restore_volumes(before)


# ── notify ─────────────────────────────────────────────────────────────────────────────
def _get(path, timeout=10):
    import urllib.request
    with urllib.request.urlopen(f"{FRIGATE}{path}", timeout=timeout) as r:
        return r.read()


def dm(ev, kind, cam_label):
    import nova_config
    from nova_protect_monitor import slack_upload_image, slack_post
    what = "🚶 Someone's walking up to the door"
    when = time.strftime("%-I:%M %p", time.localtime(ev["start_time"]))
    text = f"{what} — {cam_label} at {when}."
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    img = SNAP_DIR / f"{ev['id']}.jpg"
    ok = False
    for path in (f"/api/events/{ev['id']}/snapshot.jpg?bbox=1", f"/api/{ev['camera']}/latest.jpg"):
        try:
            img.write_bytes(_get(path)); ok = img.stat().st_size > 1000
        except Exception as e:
            log(f"snapshot {path}: {e}")
        if ok:
            break
    if not (ok and slack_upload_image(str(img), nova_config.JORDAN_DM, title=what, comment=text)):
        slack_post(text, channel=nova_config.JORDAN_DM)
    return text


def daemon():
    import threading
    import urllib.parse
    prerender()   # no-op once the clips exist
    cfg, cfg_at = load_cfg(), time.time()
    log(f"watching Frigate {sorted(cfg['cameras'])} (person, box h >= {cfg['min_box_h']})")
    done, last_alert = {}, 0.0
    while True:
        try:
            if time.time() - cfg_at > 60:
                cfg, cfg_at = load_cfg(), time.time()
            q = urllib.parse.urlencode({"cameras": ",".join(cfg["cameras"]), "labels": "person",
                                        "after": int(time.time()) - 180, "limit": 50})
            for ev in json.loads(_get(f"/api/events?{q}")):
                eid = ev["id"]
                if eid in done or not classify(ev, cfg["cameras"], float(cfg["min_box_h"]), cfg.get("zones")):
                    continue           # not close yet: re-checked next poll while the event runs
                done[eid] = time.time()
                fresh = should_alert("person", time.time(), last_alert, int(cfg["cooldown_s"]))
                last_alert = time.time()          # every close sighting extends the current visit
                if not cfg.get("enabled", True) or not fresh:
                    continue
                label = cfg["cameras"][ev["camera"]]
                quiet = in_quiet(datetime.now().hour, cfg["quiet"])
                result = {}
                if not quiet:   # chime in parallel with the photo DM — the sound is the point
                    t = threading.Thread(target=lambda: result.update(zip(("ok", "bad"), chime(cfg, "person"))))
                    t.start()
                text = dm(ev, "person", label)
                if not quiet:
                    t.join(40)
                ok, bad = result.get("ok", []), result.get("bad", [])
                outcome = "quiet_hours" if quiet else ("chimed" if ok else "chime_failed")
                record(event_id=eid, camera=label, kinds=["person"], ring=False, chimed=ok, outcome=outcome,
                       detail="; ".join(bad) or None)
                log(f"{outcome}: {text} box={ev['data'].get('box')} chimed={ok} failed={bad}")
            done = {k: v for k, v in done.items() if v > time.time() - 900}
        except Exception as e:
            log(f"poll error: {type(e).__name__}: {e}")
            time.sleep(10)
        time.sleep(POLL_S)


def main(argv):
    if "--selftest" in argv:
        cams = {"front_door": "Exterior - Front Right"}
        ev = lambda h, cam="front_door", label="person": {"camera": cam, "label": label, "data": {"box": [0.5, 0.3, 0.1, h]}}
        assert classify(ev(0.6), cams, 0.4) == "person"                     # on the walkway / porch
        assert classify(ev(0.25), cams, 0.4) is None                        # passer-by on the far sidewalk
        assert classify(ev(0.6, cam="exterior_front_right"), cams, 0.4) is None   # (= Interior - Garage)
        assert classify(ev(0.9, label="dog"), cams, 0.4) is None
        assert classify({"camera": "front_door", "label": "person", "data": {}}, cams, 0.4) is None
        z = {"front_door": [[0.60, 0.15, 0.82, 0.40]]}
        assert classify(ev(0.3) | {"data": {"box": [0.7, 0.2, 0.08, 0.3]}}, cams, 0.4, z) == "person"   # at the mailbox
        assert classify({"camera": "front_door", "label": "person", "data": {"box": [0.59, 0.42, 0.06, 0.26]}}, cams, 0.4, z) is None  # far sidewalk
        q = {"start": 22, "end": 7}
        assert in_quiet(23, q) and in_quiet(3, q) and not in_quiet(7, q) and not in_quiet(21, q)
        assert in_quiet(13, {"start": 13, "end": 14}) and not in_quiet(14, {"start": 13, "end": 14})
        assert should_alert("person", 1000, 0, 120) and not should_alert("person", 1000, 950, 120)
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
