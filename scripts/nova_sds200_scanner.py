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
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")

# --- audio segmentation knobs (16 kHz mono s16le) ---------------------------
SR = 16000
FRAME_MS = 20
FRAME_BYTES = int(SR * FRAME_MS / 1000) * 2      # 640 bytes / 20 ms
RMS_OPEN = 500          # linear s16 RMS to consider "voice present"
HANG_MS = 700           # trailing silence that closes a segment
MIN_SEG_MS = 700        # ignore blips shorter than this
MAX_SEG_MS = 30000      # hard cap per transmission

# --- reachability / backoff (2026-10-08) -------------------------------------
# telemetry.sds200_calls stayed EMPTY because the scanner at sds200_host has been off the LAN
# (ARP incomplete, "Host is down") almost continuously since 09-16; the one time RTSP answered it
# said 400. The old loop restarted ffmpeg + GSI every 10-20 s forever (9.7 MB of log, 38k
# "Host is down") and never said so anywhere Nova looks. Now: probe before connecting, back off
# exponentially, and publish up/down to health_checks so the outage is visible.
BACKOFF_BASE_S = 10
BACKOFF_MAX_S = 300
PROBE_TIMEOUT_S = 3
HEALTH_EVERY_S = 300
CHECKED_BY = "nova_sds200_scanner"

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


def backoff_s(fails: int) -> int:
    """Exponential backoff for consecutive failures: 10, 20, 40 ... capped at BACKOFF_MAX_S."""
    return int(min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, min(fails, 10) - 1)))) if fails > 0 else 0


def probe_tcp(host: str, port: int = 554, timeout: float = PROBE_TIMEOUT_S) -> str:
    """'up' (port open), 'refused' (host on the LAN, service not listening), 'down' (unreachable)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "up"
    except ConnectionRefusedError:
        return "refused"
    except Exception:  # noqa: BLE001 — timeout / host down / no route
        return "down"


def _retry(fn, attempts=3, base=0.5):
    """Call fn() up to `attempts` times with exponential backoff; re-raise the last error."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(base * (2 ** i))
    raise last


class Health:
    """Publish scanner reachability to health_checks: on every state change, else every HEALTH_EVERY_S."""

    def __init__(self, host):
        self.host = host
        self.state = None
        self.since = time.time()
        self._last_write = 0.0

    def report(self, component: str, status: str, error: str = ""):
        now = time.time()
        changed = status != self.state
        if changed:
            log(f"{component}: {self.state or 'start'} -> {status}" + (f" ({error})" if error else ""))
            self.state, self.since = status, now
        if not changed and now - self._last_write < HEALTH_EVERY_S:
            return
        self._last_write = now
        try:
            import psycopg2

            def once():
                c = psycopg2.connect(DSN, connect_timeout=5)
                try:
                    c.cursor().execute(
                        "INSERT INTO health_checks (service_name, node_name, checked_by, status, latency_ms, "
                        "error_message, checked_at) VALUES (%s,%s,%s,%s,NULL,%s,now())",
                        ("sds200", self.host, CHECKED_BY, "up" if status == "up" else "down",
                         (f"{component}: {error}" if error else component)[:500]))
                    c.commit()
                finally:
                    c.close()
            _retry(once)
        except Exception as e:  # noqa: BLE001
            log(f"health write failed after retries: {e}")


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
            _retry(lambda: urllib.request.urlopen(urllib.request.Request(
                MEM + "?async=1", data=body, headers={"Content-Type": "application/json"}), timeout=15).close())
        except Exception as e:
            log(f"memory post failed after retries: {e}")
        # 2) structured PG row
        try:
            import psycopg2

            def once():
                c = psycopg2.connect(DSN, connect_timeout=5)
                try:
                    c.cursor().execute(
                        "INSERT INTO telemetry.sds200_calls (system,department,site,channel,tgid,"
                        "frequency_mhz,service_type,p25_status,source,transcript,metadata) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (tag.get("system"), tag.get("department"), tag.get("site"), tag.get("channel"),
                         tag.get("tgid"), tag.get("frequency_mhz"), tag.get("service_type"),
                         tag.get("p25_status"), source, text, json.dumps(tag, default=str)))
                    c.commit()
                finally:
                    c.close()
            _retry(once)
        except Exception as e:
            log(f"PG insert failed after retries: {e}")


# ---------------------------------------------------------------------------
class MetadataFeed:
    """Wraps pysds200.Scanner.stream(); exposes the current call context."""

    def __init__(self, host, health=None):
        self.host = host
        self.health = health
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

    def run_once(self):
        """Connect + stream until error. Raises when the scanner does not answer GSI/MDL —
        UDP connect() always 'succeeds', so an empty model() is the real offline signal."""
        self._scanner = Scanner(self.host).connect()
        model = (self._scanner.model() or "").strip()
        if not model:
            raise ConnectionError("no MDL reply (scanner not answering on UDP 50536)")
        log(f"scanner {model} at {self.host} — metadata stream up")
        self._scanner.on_info = self._on_info
        self._scanner.stream(hz=5, background=False)   # blocks until error

    def run_forever(self, sleep=None):
        sleep = sleep or time.sleep
        fails = 0
        while True:
            try:
                self.run_once()
                fails = 0
            except Exception as e:
                fails += 1
                wait = backoff_s(fails)
                if fails == 1 or fails % 20 == 0:
                    log(f"scanner metadata offline ({e}); fail #{fails}, retrying in {wait}s")
                sleep(wait)


# ---------------------------------------------------------------------------
def audio_command(source: str, host: str, transport: str = "udp"):
    """ffmpeg argv producing s16le/16k/mono on stdout from RTSP or ALSA.
    transport alternates udp/tcp after a failure on a reachable host (the one RTSP answer we ever
    got, 09-16, was '400 Bad Request' to the UDP SETUP)."""
    base = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if source.startswith("alsa:"):
        base += ["-f", "alsa", "-i", source.split(":", 1)[1]]
    else:  # rtsp (default)
        transport = transport if transport in ("udp", "tcp") else "udp"
        base += ["-rtsp_transport", transport, "-i", f"rtsp://{host}/au:scanner.au"]
    return base + ["-ar", str(SR), "-ac", "1", "-f", "s16le", "-"]


# Bias the decoder toward the SDS200's mixed public-safety + RAILROAD vocabulary. The rail
# terms matter: without them Whisper garbles wayside defect-detector readouts (the bulk of rail
# traffic) into nonsense like "total axle two four house" for "…total axles 240, detector out".
_ASR_PROMPT = (
    "Public-safety and railroad radio dispatch. Police/fire unit callsigns and codes "
    "(187 211 415 10-4 code 3 E-11 RA-63). Railroad: wayside defect detector — "
    "'detector milepost 468.2, no defects, total axles 240, train speed 45, ambient "
    "temperature 78 degrees, detector out'; alarm 'you have a defect, stop your train, "
    "hot box axle 12 from the rear'; signal aspects clear/approach/restricting; highball, "
    "milepost, siding, crossover, track warrant, control point, Metrolink, EOT."
)


def transcribe(pcm: bytes, model) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        w = wave.open(f.name, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm); w.close()
        segs, _ = model.transcribe(f.name, language="en", vad_filter=True,
                                   initial_prompt=_ASR_PROMPT)
        return " ".join(s.text.strip() for s in segs).strip()


def run_service(host, source, model_name):
    from faster_whisper import WhisperModel  # lazy: not needed for --selftest
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    log(f"whisper '{model_name}' loaded; audio source = {source}")

    health = Health(host)
    feed = MetadataFeed(host, health)
    threading.Thread(target=feed.run_forever, name="sds200-meta", daemon=True).start()
    store = Store()

    hang_frames = HANG_MS // FRAME_MS
    fails = 0
    transport = "udp"
    while True:  # ffmpeg supervisor loop
        proc = None
        if fails:
            time.sleep(backoff_s(fails))
        if not source.startswith("alsa:"):
            st = probe_tcp(host, 554)
            if st != "up":
                fails += 1
                health.report("rtsp", "down", f"{host}:554 {st}"
                              + (" — scanner off the LAN (power/cable/Wi-Fi)" if st == "down" else
                                 " — scanner up but RTSP not serving (Menu > Settings > Network)"))
                continue
        try:
            proc = subprocess.Popen(audio_command(source, host, transport), stdout=subprocess.PIPE)
            log(f"audio capture up (rtsp_transport={transport})")
            got_audio = False
            seg = bytearray()
            silent = 0
            seg_tag = {}
            while True:
                chunk = proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    raise RuntimeError("audio stream ended")
                if not got_audio:
                    got_audio = True
                    fails = 0
                    health.report("rtsp", "up")
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
            fails += 1
            if not source.startswith("alsa:"):
                transport = "tcp" if transport == "udp" else "udp"
            health.report("rtsp", "down", f"audio error: {e}")
            log(f"audio error ({e}); fail #{fails}, retrying in {backoff_s(fails)}s (next transport {transport})")
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
