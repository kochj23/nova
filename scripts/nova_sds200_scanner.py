#!/usr/bin/env python3
"""nova_sds200_scanner.py — turn the spare Uniden SDS200 into a Nova memory feed.

The SDS200 already trunk-tracks P25 and resolves talkgroups against its database,
so unlike the SDRplay sweep this source arrives pre-labeled: system / department /
talkgroup name + TGID / frequency. This service marries that live metadata to the
scanner's audio and files each transmission into Nova's memory, tagged.

Two threads:
  * METADATA — pysds200.Scanner.stream(): polls GSI over UDP:50536, keeps the
    "current call context" (only while the squelch is open).
  * AUDIO — ffmpeg reads the scanner audio (RTSP by default, or a line-out USB
    ADC via ALSA), a simple RMS gate segments per-transmission, faster-whisper
    transcribes, and the segment is stored with the metadata that was current
    when it began.

Consumes the public `pysds200` library (github.com/kochj23/pysds200), the same
way nrsp_sweep.py consumes pynrsp. Whisper/numpy are imported lazily so the
module loads (and `--selftest` runs) on a box without them.

Resilient by design: if the scanner is offline or ffmpeg dies, it logs and backs
off — it never crashes — so it can sit running before the garage unit is wired.

    nova_sds200_scanner.py --selftest     # no hardware, no whisper: validate wiring
    nova_sds200_scanner.py --probe        # one GSI against the scanner (needs it live)
    nova_sds200_scanner.py                # run the service
"""
import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pysds200 import Scanner, ScannerInfo  # noqa: E402

MEM = "http://memory-server.digitalnoise.net:18790/remember"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# --- audio segmentation knobs (16 kHz mono s16le) ---------------------------
SR = 16000
FRAME_MS = 20
FRAME_BYTES = int(SR * FRAME_MS / 1000) * 2      # 640 bytes / 20 ms
RMS_OPEN = 500          # linear s16 RMS to consider "voice present"
HANG_MS = 700           # trailing silence that closes a segment
MIN_SEG_MS = 700        # ignore blips shorter than this
MAX_SEG_MS = 30000      # hard cap per transmission

# service_type / department -> the memory `source` Nova's airwaves reports bucket by
_SOURCE_RULES = [
    ("fire", "fire"), ("ems", "fire"), ("medical", "fire"),
    ("rail", "rail"), ("railroad", "rail"), ("metrolink", "rail"), ("union pacific", "rail"),
    ("chp", "chp"), ("highway patrol", "chp"),
    ("law", "scanner"), ("police", "scanner"), ("sheriff", "scanner"), ("dispatch", "scanner"),
]


def log(m):
    print(f"[sds200 {datetime.now():%H:%M:%S}] {m}", flush=True)


def cfg(key, default=None):
    """Read a value from service_config(service='nova'), env override, or default."""
    env = os.environ.get("NOVA_" + key.upper())
    if env:
        return env
    try:
        import psycopg2
        c = psycopg2.connect(DSN)
        cur = c.cursor()
        cur.execute("SELECT value FROM service_config WHERE service='nova' AND key=%s", (key,))
        row = cur.fetchone()
        c.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return default


def source_for(tag: dict) -> str:
    """Route a call into the memory `source` bucket Nova's airwaves reports use."""
    hay = " ".join(str(tag.get(k) or "") for k in ("service_type", "department", "system")).lower()
    for needle, src in _SOURCE_RULES:
        if needle in hay:
            return src
    return "scanner"


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


# ---------------------------------------------------------------------------
class Store:
    """Best-effort sinks: Nova vector memory + a structured PG table."""

    def __init__(self):
        self._ensure_table()

    def _ensure_table(self):
        try:
            import psycopg2
            c = psycopg2.connect(DSN)
            c.cursor().execute("""
                CREATE TABLE IF NOT EXISTS telemetry.sds200_calls (
                    id            bigserial PRIMARY KEY,
                    ts            timestamptz NOT NULL DEFAULT now(),
                    system        text, department text, site text, channel text,
                    tgid          integer, frequency_mhz numeric, service_type text,
                    p25_status    text, source text, transcript text,
                    metadata      jsonb )""")
            c.commit()
            c.close()
        except Exception as e:
            log(f"PG table ensure skipped: {e}")

    def save(self, text: str, tag: dict, source: str):
        label = " / ".join(x for x in (tag.get("department"), tag.get("channel")) if x) or "scanner"
        # 1) vector memory (feeds airwaves/local reports) — async, fire-and-forget
        try:
            body = json.dumps({
                "text": f"[{label}] {text}",
                "source": source,
                "metadata": {"kind": "sds200", "location": "Burbank, CA",
                             "receiver": "Uniden SDS200", **tag},
            }).encode()
            urllib.request.urlopen(urllib.request.Request(
                MEM + "?async=1", data=body, headers={"Content-Type": "application/json"}), timeout=15)
        except Exception as e:
            log(f"memory post failed: {e}")
        # 2) structured PG row
        try:
            import psycopg2
            c = psycopg2.connect(DSN)
            c.cursor().execute(
                "INSERT INTO telemetry.sds200_calls (system,department,site,channel,tgid,"
                "frequency_mhz,service_type,p25_status,source,transcript,metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tag.get("system"), tag.get("department"), tag.get("site"), tag.get("channel"),
                 tag.get("tgid"), tag.get("frequency_mhz"), tag.get("service_type"),
                 tag.get("p25_status"), source, text, json.dumps(tag)))
            c.commit()
            c.close()
        except Exception as e:
            log(f"PG insert failed: {e}")


# ---------------------------------------------------------------------------
class MetadataFeed:
    """Wraps pysds200.Scanner.stream(); exposes the current call context."""

    def __init__(self, host):
        self.host = host
        self._cur = {}          # last tag() seen while squelch open
        self._lock = threading.Lock()
        self._scanner = None

    @property
    def current(self) -> dict:
        with self._lock:
            return dict(self._cur)

    def _on_info(self, info: ScannerInfo):
        if info.squelch_open:
            with self._lock:
                self._cur = info.tag()

    def run_forever(self):
        while True:
            try:
                self._scanner = Scanner(self.host).connect()
                log(f"scanner {self._scanner.model()} at {self.host} — metadata stream up")
                self._scanner.on_info = self._on_info
                self._scanner.stream(hz=5, background=False)   # blocks until error
            except Exception as e:
                log(f"scanner offline ({e}); retrying in 20s")
                time.sleep(20)


# ---------------------------------------------------------------------------
def audio_command(source: str, host: str):
    """ffmpeg argv producing s16le/16k/mono on stdout from RTSP or ALSA."""
    base = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if source.startswith("alsa:"):
        base += ["-f", "alsa", "-i", source.split(":", 1)[1]]
    else:  # rtsp (default)
        base += ["-rtsp_transport", "udp", "-i", f"rtsp://{host}/au:scanner.au"]
    return base + ["-ar", str(SR), "-ac", "1", "-f", "s16le", "-"]


def transcribe(pcm: bytes, model) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        w = wave.open(f.name, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm); w.close()
        segs, _ = model.transcribe(f.name, language="en", vad_filter=True)
        return " ".join(s.text.strip() for s in segs).strip()


def run_service(host, source, model_name):
    from faster_whisper import WhisperModel  # lazy: not needed for --selftest
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    log(f"whisper '{model_name}' loaded; audio source = {source}")

    feed = MetadataFeed(host)
    threading.Thread(target=feed.run_forever, name="sds200-meta", daemon=True).start()
    store = Store()

    hang_frames = HANG_MS // FRAME_MS
    while True:  # ffmpeg supervisor loop
        proc = None
        try:
            proc = subprocess.Popen(audio_command(source, host), stdout=subprocess.PIPE)
            log("audio capture up")
            seg = bytearray()
            silent = 0
            seg_tag = {}
            while True:
                chunk = proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    raise RuntimeError("audio stream ended")
                loud = rms(chunk) >= RMS_OPEN
                if loud:
                    if not seg:                       # segment opens
                        seg_tag = feed.current        # snapshot metadata at carrier-up
                    seg.extend(chunk)
                    silent = 0
                elif seg:
                    seg.extend(chunk)                 # keep trailing audio during hang
                    silent += 1
                    if silent >= hang_frames or len(seg) > MAX_SEG_MS * SR // 1000 * 2:
                        dur_ms = len(seg) / 2 / SR * 1000
                        if dur_ms >= MIN_SEG_MS:
                            text = transcribe(bytes(seg), model)
                            if text and len(text) > 8:
                                src = source_for(seg_tag)
                                store.save(text, seg_tag, src)
                                lbl = seg_tag.get("department") or seg_tag.get("channel") or "?"
                                log(f"[{src}] {lbl} :: {text[:90]}")
                        seg = bytearray(); silent = 0; seg_tag = {}
        except Exception as e:
            log(f"audio error ({e}); restarting in 10s")
            time.sleep(10)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()


# ---------------------------------------------------------------------------
def selftest(host, source):
    """No hardware, no whisper: prove the wiring and show routing decisions."""
    log("SELFTEST — not touching hardware")
    import pysds200
    log(f"pysds200 {pysds200.__version__}; scanner host = {host or '(unset)'}; audio = {source}")
    log(f"audio cmd: {' '.join(audio_command(source, host or '<host>'))}")
    sample = """<ScannerInfo Mode="Trunk Scan Hold" V_Screen="trunk_scan">
      <System Name="Burbank 911" SystemType="Motorola" />
      <Department Name="Verdugo Fire" />
      <TGID Name="Fire Dispatch" TGID="TGID:2051" SvcType="Fire Dispatch" />
      <SiteFrequency Freq=" 851.0375MHz" />
      <Property Sig="4" Rec="On" P25Status="P25" Rssi="0.71" /></ScannerInfo>"""
    info = ScannerInfo.from_xml(sample)
    tag = info.tag()
    src = source_for(tag)
    log(f"parsed: {info}")
    log(f"squelch_open={info.squelch_open}  ->  source bucket = '{src}'")
    log(f"tag -> {json.dumps(tag)}")
    log(f"would store: [{tag['department']} / {tag['channel']}] <transcript>  (source={src})")
    log("OK — wiring valid. Deploy + wire the scanner when the garage is survivable.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--probe", action="store_true", help="one live GSI (needs the scanner up)")
    args = ap.parse_args()

    host = cfg("sds200_host")            # service_config('nova','sds200_host') or NOVA_SDS200_HOST
    source = cfg("sds200_audio", "rtsp") # 'rtsp' (default) or 'alsa:<device>'
    model_name = cfg("sds200_whisper", "base.en")

    if args.selftest:
        return selftest(host, source)
    if not host:
        log("no sds200_host configured; set service_config('nova','sds200_host') "
            "or NOVA_SDS200_HOST. Running selftest instead.")
        return selftest(host, source)
    if args.probe:
        with Scanner(host) as s:
            log(f"{s.model()} fw {s.version()}")
            log(str(s.get_info()))
        return 0
    run_service(host, source, model_name)


if __name__ == "__main__":
    sys.exit(main())
