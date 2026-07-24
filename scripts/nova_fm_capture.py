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
import os, sys, time, wave
import numpy as np
import SoapySDR
from scipy import signal as sps
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

SERIAL = sys.argv[1]
OUT_DIR = sys.argv[2]
TARGETS = [t.split(":") for t in sys.argv[3:]]   # [(label, freq_hz, antenna), ...]
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


class ChannelState:
    def __init__(self, label, freq_hz, chan_idx, audio_rate):
        self.label, self.freq_hz, self.chan_idx = label, float(freq_hz), chan_idx
        self.audio_rate = audio_rate
        self.prev = np.complex64(0)
        self.buf = []
        self.chunk_start = time.time()

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
            if rms >= MIN_RMS_TO_KEEP:
                fname = f"nrsp_{int(time.time()*1000)}__{self.freq_hz/1e6:.4f}__NFM__{self.label.replace(' ', '_')}.wav"
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
    actual_iq_rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    total_decim = 1
    for s in DECIM_STAGES:
        total_decim *= s
    audio_rate = actual_iq_rate / total_decim
    print(f"[fm-capture] requested {IQ_RATE_REQUEST} Hz, driver gave {actual_iq_rate} Hz "
          f"-> decimating by {total_decim} -> {audio_rate:.0f} Hz audio", flush=True)

    channels = []
    for i, (label, freq_hz, ant) in enumerate(TARGETS):
        chan = i if dual else 0
        if not dual:
            sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
        sdr.setFrequency(SOAPY_SDR_RX, chan, float(freq_hz))
        channels.append(ChannelState(label, freq_hz, chan, audio_rate))

    n_chans = 2 if dual else 1
    # ponytail: this driver rejects a single combined multi-channel setupStream([0,1]) call
    # ("invalid channel selection") -- one stream object per RX channel instead.
    streams = [sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [i]) for i in range(n_chans)]
    for s in streams:
        sdr.activateStream(s)
    print(f"[fm-capture] {SERIAL} {'DT' if dual else 'MA'} mode: "
          f"{', '.join(f'{c.label}@{c.freq_hz/1e6:.4f}MHz' for c in channels)}", flush=True)

    bufs = [np.empty(19200, np.complex64) for _ in range(n_chans)]
    try:
        while True:
            for c in channels:
                sr = sdr.readStream(streams[c.chan_idx], [bufs[c.chan_idx]], len(bufs[c.chan_idx]),
                                    timeoutUs=int(2e6))
                if sr.ret <= 0:
                    continue
                audio, c.prev = fm_demod_chunk(bufs[c.chan_idx][:sr.ret], c.prev, DECIM_STAGES)
                c.feed(audio)
    finally:
        for c in channels:
            c._flush()
        for s in streams:
            sdr.deactivateStream(s)
            sdr.closeStream(s)


if __name__ == "__main__":
    main()
