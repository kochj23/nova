#!/usr/bin/env python3
"""nova_fm_capture.py — dedicated continuous FM-voice capture on one or two RSPduo
tuners, VAD-gated into tagged WAV chunks for nova_rf_discovery_whisper.py-style
transcription. Reuses nova_rsp_bridge.py's proven FM-discriminator math, but writes
dwell WAVs (matching nrsp2_<ts>__<freq>__<demod>__<label>.wav) instead of piping to
dsd-fme -- these are analog voice targets, not P25 digital.

Single channel (Master mode, antenna-switchable):
  nova_fm_capture.py <serial> <out_dir> <label>:<freq_hz>:<antenna>

Dual channel (Dual Tuner mode, each channel hardwired to its own port):
  nova_fm_capture.py <serial> <out_dir> <label1>:<freq_hz1>:1 <label2>:<freq_hz2>:2
  (antenna arg is the tuner NUMBER in dual mode, "1" or "2" -- fixed per channel)

Written 2026-07-23 for the six-tuner SIGINT mission assignment.
"""
import json, os, sys, time, wave
import numpy as np
import SoapySDR
from scipy import signal as sps
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

SERIAL = sys.argv[1]
OUT_DIR = sys.argv[2]
TARGETS = [t.split(":") for t in sys.argv[3:]]   # [(label, freq_hz, antenna[, demod]), ...]
# 2026-10-12: optional 4th field "AM" -> envelope demod (airband/CB); default stays FM/NFM.
os.makedirs(OUT_DIR, exist_ok=True)

IQ_RATE_REQUEST = 96000  # what we ask for -- the RSPduo driver actually snaps this to its own
                         # minimum supported rate (measured: 2,000,000 Hz), NOT what's requested.
                         # We query the real rate after opening the device and decimate from that.
DECIM_STAGES = (5, 8)    # two-stage decimate (scipy recommends factors <=13 per call);
                         # 2,000,000 / (5*8) = 50,000 Hz final audio rate -- set for the
                         # measured 2 MHz case, recomputed at runtime if the real rate differs
CHUNK_S = 30             # fixed dwell length -- matches nova_rf_discovery.py's "hit -> 40s dwell"
                         # pattern; simpler and more robust than real-time VAD gating, and
                         # correct for always-on channels (NOAA WX) as well as intermittent ones
MIN_RMS_TO_KEEP = 80     # skip writing/transcribing a chunk that's just silence/noise floor
# 2026-10-15 (plan #6): per-channel heartbeat for nova_sdr_actuator -- IQ samples, audio level and live SNR every
# STATS_EVERY_S, written EVEN WHEN readStream returns nothing (a wedged SDRplay API keeps this process alive
# with zero samples, which unit health could not see).
STATS_DIR = os.environ.get("NOVA_SDR_STATS_DIR", os.path.join(os.path.expanduser("~"), ".cache", "nova-sdr"))
STATS_EVERY_S = 30
SNR_EVERY = 16           # FFT every Nth buffer (~6/s at 2 MHz) -- cheap enough on every host
CH_HALF_BW_HZ = 6250     # channel = +-6.25 kHz around the tuned frequency (centre of the IQ)
DC_SKIP_HZ = 600         # zero-IF DC/LO-leak skirt excluded from the channel
NOISE_FROM_HZ, NOISE_TO_HZ = 20_000, 90_000   # noise-floor annulus around the channel


def channel_snr_db(iq: np.ndarray, iq_rate: float) -> float | None:
    """Channel power vs the nearby noise floor (median of a 20-90 kHz annulus), live.
    Zero-IF: the tuner's DC offset / LO leakage sits exactly on the channel, so the IQ mean is removed and
    +-DC_SKIP_HZ around 0 is skipped (first cut read 48-53 dB on an idle airband channel, 2026-10-15)."""
    n = len(iq)
    if n < 1024:
        return None
    x = iq - iq.mean()
    p = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(n)))) ** 2
    bin_hz = iq_rate / n
    mid, half, skip = n // 2, max(4, int(CH_HALF_BW_HZ / bin_hz)), max(2, int(DC_SKIP_HZ / bin_hz))
    ch = np.concatenate([p[mid - half:mid - skip], p[mid + skip + 1:mid + half + 1]])
    # noise reference: median of a nearby annulus inside the IF passband (the whole 2 MHz band includes the
    # IF filter's stopband, whose floor made every channel look 45+ dB "above noise")
    lo, hi = int(NOISE_FROM_HZ / bin_hz), min(n // 2 - 1, int(NOISE_TO_HZ / bin_hz))
    noise = float(np.median(np.concatenate([p[mid - hi:mid - lo], p[mid + lo:mid + hi]]))) if hi > lo else float(np.median(p))
    if noise <= 0 or not len(ch):
        return None
    return round(10 * np.log10(float(ch.mean()) / noise), 1)


def write_stats(label: str, stats: dict):
    """Atomic JSON per channel: <STATS_DIR>/<label>.json. Never raises (a full disk must not stop capture)."""
    try:
        os.makedirs(STATS_DIR, exist_ok=True)
        path = os.path.join(STATS_DIR, f"{label}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(stats, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def open_device(dual: bool):
    mode = "DT" if dual else "MA"
    candidates = SoapySDR.Device.enumerate(f"driver=sdrplay,serial={SERIAL}")
    match = next((c for c in candidates if dict(c).get("mode") == mode), None)
    if match is None:
        sys.exit(f"no {mode}-mode RSPduo found for serial {SERIAL}")
    return SoapySDR.Device(match)


def fm_demod_chunk(iq: np.ndarray, prev: np.complex64, decim_stages) -> tuple[np.ndarray, np.complex64]:
    xp = np.empty(len(iq) + 1, np.complex64); xp[0] = prev; xp[1:] = iq
    disc = np.angle(xp[1:] * np.conj(xp[:-1])).astype(np.float32)
    disc -= disc.mean()
    audio = disc
    for stage in decim_stages:
        audio = sps.decimate(audio, stage, ftype="fir").astype(np.float32)
    return np.clip(audio * 12000.0, -32767, 32767).astype("<i2"), iq[-1]


def am_demod_chunk(iq: np.ndarray, prev: np.complex64, decim_stages) -> tuple[np.ndarray, np.complex64]:
    # 2026-10-13: channel-filter the IQ BEFORE the envelope. Taking abs() of the full 2 MHz first
    # mixed every signal in the passband into the audio -- 118.575 transcribed as KROQ and ads.
    ch = iq
    for stage in decim_stages:
        ch = sps.decimate(ch, stage, ftype="fir")
    env = np.abs(ch).astype(np.float32)
    audio = env / (env.mean() + 1e-9) - 1.0          # normalised envelope, DC removed
    return np.clip(audio * 8000.0, -32767, 32767).astype("<i2"), iq[-1]


class ChannelState:
    def __init__(self, label, freq_hz, chan_idx, audio_rate, demod="NFM"):
        self.label, self.freq_hz, self.chan_idx = label, float(freq_hz), chan_idx
        self.demod = demod.upper()
        self.demod_fn = am_demod_chunk if self.demod == "AM" else fm_demod_chunk
        self.audio_rate = audio_rate
        self.prev = np.complex64(0)
        self.buf = []
        self.chunk_start = time.time()
        self.samples, self.n_reads, self.snrs, self.last_rms = 0, 0, [], None
        self.last_sample_ts, self.stats_ts = None, time.time()

    def observe(self, iq: np.ndarray, iq_rate: float):
        self.samples += len(iq)
        self.last_sample_ts = time.time()
        self.n_reads += 1
        if self.n_reads % SNR_EVERY == 0:
            snr = channel_snr_db(iq, iq_rate)
            if snr is not None:
                self.snrs.append(snr)

    def maybe_report(self, now: float | None = None) -> bool:
        now = now or time.time()
        if now - self.stats_ts < STATS_EVERY_S:
            return False
        write_stats(self.label, {"ts": now, "label": self.label, "freq_hz": self.freq_hz, "demod": self.demod,
                                 "window_s": round(now - self.stats_ts, 1), "samples": self.samples,
                                 "last_sample_ts": self.last_sample_ts, "rms": self.last_rms,
                                 "snr_db": max(self.snrs) if self.snrs else None,
                                 "snr_med_db": float(np.median(self.snrs)) if self.snrs else None})
        self.samples, self.snrs, self.stats_ts = 0, [], now
        return True

    def feed(self, audio: np.ndarray):
        # ponytail: wall-clock-based, not sample-count-based -- the RSPduo's actual
        # streamed rate didn't match the requested IQ_RATE closely enough for sample
        # counting to land anywhere near real 30s chunks. Wall-clock is immune to that.
        self.buf.append(audio)
        if time.time() - self.chunk_start >= CHUNK_S:
            self._flush()

    def _flush(self):
        if self.buf:
            pcm = np.concatenate(self.buf)
            rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
            self.last_rms = round(rms, 1)
            if rms >= MIN_RMS_TO_KEEP:
                fname = f"nrsp_{int(time.time()*1000)}__{self.freq_hz/1e6:.4f}__{self.demod}__{self.label.replace(' ', '_')}.wav"
                with wave.open(os.path.join(OUT_DIR, fname), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(round(self.audio_rate)))
                    w.writeframes(pcm.tobytes())
        self.buf = []
        self.chunk_start = time.time()


def main():
    dual = len(TARGETS) == 2 and TARGETS[0][2] in ("1", "2")
    sdr = open_device(dual)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, IQ_RATE_REQUEST)
    if dual:
        sdr.setSampleRate(SOAPY_SDR_RX, 1, IQ_RATE_REQUEST)

    # The RSPduo driver ignores the requested rate and snaps to its own supported
    # rate (measured 2,000,000 Hz) -- query what it actually gave us and derive
    # the real decimation/audio rate from that, rather than trust the request.
    # 2026-10-13: broadcast-FM notch (88-108 MHz) on -- no target lives there, and strong FM stations
    # overloaded the front end into the airband channels.
    try:
        sdr.writeSetting("rfnotch_ctrl", "true")
    except Exception as e:
        print(f"[fm-capture] FM notch not set: {e}", flush=True)
    actual_iq_rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    total_decim = 1
    for s in DECIM_STAGES:
        total_decim *= s
    audio_rate = actual_iq_rate / total_decim
    print(f"[fm-capture] requested {IQ_RATE_REQUEST} Hz, driver gave {actual_iq_rate} Hz "
          f"-> decimating by {total_decim} -> {audio_rate:.0f} Hz audio", flush=True)

    channels = []
    for i, (label, freq_hz, ant, *rest) in enumerate(TARGETS):
        chan = i if dual else 0
        if not dual:
            sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
        sdr.setFrequency(SOAPY_SDR_RX, chan, float(freq_hz))
        channels.append(ChannelState(label, freq_hz, chan, audio_rate, rest[0] if rest else "NFM"))

    n_chans = 2 if dual else 1
    # ponytail: this driver rejects a single combined multi-channel setupStream([0,1]) call
    # ("invalid channel selection") -- one stream object per RX channel instead.
    streams = [sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [i]) for i in range(n_chans)]
    for s in streams:
        sdr.activateStream(s)
    print(f"[fm-capture] {SERIAL} {'DT' if dual else 'MA'} mode: "
          f"{', '.join(f'{c.label}@{c.freq_hz/1e6:.4f}MHz/{c.demod}' for c in channels)}", flush=True)

    bufs = [np.empty(19200, np.complex64) for _ in range(n_chans)]
    try:
        while True:
            for c in channels:
                sr = sdr.readStream(streams[c.chan_idx], [bufs[c.chan_idx]], len(bufs[c.chan_idx]),
                                    timeoutUs=int(2e6))
                c.maybe_report()
                if sr.ret <= 0:
                    continue
                c.observe(bufs[c.chan_idx][:sr.ret], actual_iq_rate)
                audio, c.prev = c.demod_fn(bufs[c.chan_idx][:sr.ret], c.prev, DECIM_STAGES)
                c.feed(audio)
    finally:
        for c in channels:
            c._flush()
        for s in streams:
            sdr.deactivateStream(s)
            sdr.closeStream(s)


if __name__ == "__main__":
    main()
