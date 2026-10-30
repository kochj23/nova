#!/usr/bin/env python3
"""nova_fm_whisper.py — transcribe nova_fm_capture.py's WAV chunks (~/nrsp_calls) into Nova's memory
(source rf_discovery). Runs beside each RSPduo capture: core3 (nova-fm-whisper-ham, as
nova_fm_whisper_new.py) and core7 (nova-fm-whisper). Needs faster-whisper with PyAV <= 18 in
~/scanner-venv (av 19 dropped open(metadata_errors=) -> every file errored, 2026-10-14).
"""
import os, time, glob, json, urllib.request

MEM = 'http://192.168.1.6:18790/remember'
D = os.path.expanduser('~/nrsp_calls')


class _Seg:
    def __init__(self, text):
        self.text = text


class _MlxModel:
    """macOS radio hosts (nova-core4, 2026-10-30): PyAV < 19 has no wheel for their Python, so use mlx-whisper."""
    def __init__(self, repo):
        import mlx_whisper
        self._t, self.repo = mlx_whisper.transcribe, repo

    def transcribe(self, path, language='en', vad_filter=True):
        r = self._t(path, path_or_hf_repo=self.repo, language=language, condition_on_previous_text=False)
        return [_Seg(s['text']) for s in r.get('segments', [])], r


try:
    from faster_whisper import WhisperModel
    m = WhisperModel('base.en', device='cpu', compute_type='int8')
except ImportError:
    m = _MlxModel(os.environ.get('NOVA_FM_WHISPER_MLX', 'mlx-community/whisper-large-v3-turbo'))

def parse_name(path):
    base = os.path.basename(path)[:-4]
    parts = base.split('__')
    if len(parts) >= 4:
        try: freq = float(parts[1])
        except ValueError: freq = None
        return freq, parts[2], parts[3].replace('_', ' ')
    return None, None, 'unknown'

def rem(text, freq, demod, label):
    meta = {'kind': 'rf_discovery', 'channel': label, 'freq_mhz': freq, 'demod': demod,
            'location': 'Burbank, CA', 'receiver': 'RSPduo dedicated'}
    body = json.dumps({'text': f'[{label} {freq}MHz {demod}] {text}',
                       'source': 'rf_discovery', 'metadata': meta}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            MEM + '?async=1', data=body, headers={'Content-Type': 'application/json'}), timeout=15)
    except Exception:
        pass

print('fm whisper (new) up -- watching', D, flush=True)
while True:
    for w in sorted(glob.glob(os.path.join(D, '*.wav'))):
        try:
            if time.time() - os.path.getmtime(w) < 2:
                continue
            if os.path.getsize(w) < 40000:
                os.remove(w); continue
            freq, demod, label = parse_name(w)
            segs, _ = m.transcribe(w, language='en', vad_filter=True)
            t = ' '.join(s.text.strip() for s in segs).strip()
            if t and len(t) > 8:
                rem(t, freq, demod, label)
                print(f"[{time.strftime('%H:%M:%S')}] {label} {freq}MHz :: {t[:100]}", flush=True)
        except Exception as e:
            print('err', e, flush=True)
        finally:
            try: os.remove(w)
            except Exception: pass
    time.sleep(3)
