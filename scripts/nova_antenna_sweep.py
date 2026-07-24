#!/usr/bin/env python3
"""nova_antenna_sweep.py — direct-SoapySDR SNR sweep across an RSPduo's two 50-ohm
antenna ports, at a set of reference frequencies. Written 2026-07-23 because
nrsp_sweep.py assumed both RSPduo units shared one SDRconnect instance (device
index 1 vs 2) -- that stopped being true once the units moved to separate hosts,
and the RSPduo units aren't even on SDRconnect in production anymore (that's
now dedicated to the garage RSP-ST). This talks to SoapySDR directly, matching
how nova-lapd/nova-rsp-rotate actually access the hardware.

Usage: nova_antenna_sweep.py <serial>
Prints ranked SNR per antenna port per reference frequency, and writes results
to antenna_baseline_snapshots (nova_ops) under label 'post-move-2026-07-23'.
"""
import sys
import time
import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16

SERIAL = sys.argv[1]
ANTENNAS = ["Tuner 1 50 ohm", "Tuner 2 50 ohm"]
# label, freq_hz, samp_rate -- 506.9375 MHz is the established UHF/P25 (police/fire)
# reference used in the pre-move baseline; 106.7 the established VHF/FM commercial
# reference (KROQ, always-on strong carrier, good general propagation check);
# 146.520 MHz is the US national 2m FM simplex calling frequency -- not guaranteed
# active, but it's the standard reference point for 2m antenna work absent a known
# local repeater, and still gives a valid noise-floor/gain comparison between ports
# even with no one transmitting.
BANDS = [
    ("UHF/police-fire (P25 506.9375)", 506_937_500, 2_000_000),
    ("VHF/FM broadcast (KROQ 106.7)", 106_700_000, 2_000_000),
    ("2m ham (146.520 calling freq)", 146_520_000, 2_000_000),
]
SETTLE_S = 1.5
CAPTURE_N = 65536


def snr_db(iq: np.ndarray) -> tuple[float, float]:
    """Return (snr_db, signal_power_db) via FFT: peak bin vs. median noise floor."""
    window = np.hanning(len(iq))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(iq * window))) ** 2
    spectrum_db = 10 * np.log10(spectrum + 1e-12)
    peak = np.max(spectrum_db)
    noise_floor = np.median(spectrum_db)
    return round(peak - noise_floor, 2), round(peak, 2)


def sweep_antenna(sdr, antenna: str, freq_label: str, freq_hz: float, samp_rate: float) -> dict:
    sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, samp_rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, freq_hz)
    time.sleep(SETTLE_S)

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(stream)
    buf = np.empty(CAPTURE_N, dtype=np.complex64)
    raw = np.empty(2 * CAPTURE_N, dtype=np.int16)
    readings = []
    for _ in range(3):  # 3 captures per antenna/freq, report the median
        sr = sdr.readStream(stream, [raw], CAPTURE_N, timeoutUs=int(2e6))
        if sr.ret > 0:
            n = sr.ret
            iq = raw[:2 * n].astype(np.float32).view(np.complex64) / 32768.0
            readings.append(snr_db(iq[:n]))
        time.sleep(0.2)
    sdr.deactivateStream(stream)
    sdr.closeStream(stream)

    if not readings:
        return {"antenna": antenna, "band": freq_label, "snr_db": None, "signal_db": None}
    snrs = sorted(r[0] for r in readings)
    sigs = sorted(r[1] for r in readings)
    mid = len(snrs) // 2
    return {"antenna": antenna, "band": freq_label, "snr_db": snrs[mid], "signal_db": sigs[mid]}


def main():
    # NOTE: passing a plain dict to SoapySDR.Device() fails ("no match") on this
    # binding version -- must construct from an actual enumerate() result object.
    candidates = SoapySDR.Device.enumerate(f"driver=sdrplay,serial={SERIAL}")
    match = next((c for c in candidates if dict(c).get("mode") == "MA"), None)
    if match is None:
        sys.exit(f"no Master-mode RSPduo found for serial {SERIAL}")
    sdr = SoapySDR.Device(match)
    results = []
    for freq_label, freq_hz, samp_rate in BANDS:
        print(f"\n=== {freq_label} ===")
        band_results = []
        for antenna in ANTENNAS:
            r = sweep_antenna(sdr, antenna, freq_label, freq_hz, samp_rate)
            band_results.append(r)
            print(f"  {antenna:16} SNR={r['snr_db']} dB  signal={r['signal_db']} dB")
        ranked = sorted([r for r in band_results if r["snr_db"] is not None],
                        key=lambda r: r["snr_db"], reverse=True)
        if ranked:
            print(f"  BEST: {ranked[0]['antenna']}")
        results.extend(band_results)
    del sdr

    import json
    print("\n" + json.dumps({"serial": SERIAL, "results": results}))


if __name__ == "__main__":
    main()
