#!/opt/homebrew/bin/python3
"""Find where a still appears in a Nova Speaks mp4. Frames sampled every STEP s at 64x36 gray, compared with the
image center-cropped to 16:9 at several zooms (Ken Burns 1.0-1.18 + pans) by normalized correlation."""
import subprocess, sys
import numpy as np
from PIL import Image

W, H, STEP = 64, 36, 1.0


def frames(mp4):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp4, "-vf",
                        f"fps=1/{STEP},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=900)
    a = np.frombuffer(r.stdout, np.uint8)
    return a[: a.size // (W * H) * W * H].reshape(-1, H, W).astype(np.float32)


def templates(img):
    im = Image.open(img).convert("L")
    w, h = im.size
    tw, th = (w, int(w * 9 / 16)) if w / h < 16 / 9 else (int(h * 16 / 9), h)
    base = im.crop(((w - tw) // 2, (h - th) // 2, (w - tw) // 2 + tw, (h - th) // 2 + th))
    out = []
    for z in (1.0, 1.08, 1.16):
        cw, ch = int(tw / z), int(th / z)
        for fx, fy in ((.5, .5), (0, .5), (1, .5), (.5, 0), (.5, 1), (0, 0), (1, 1)):
            x, y = int((tw - cw) * fx), int((th - ch) * fy)
            out.append(np.asarray(base.crop((x, y, x + cw, y + ch)).resize((W, H), Image.BILINEAR), np.float32))
            if z == 1.0: break
    return out


def norm(a):
    a = a - a.mean(axis=(-1, -2), keepdims=True)
    return a / (np.sqrt((a ** 2).sum(axis=(-1, -2), keepdims=True)) + 1e-6)


def match(mp4, img, thr=0.80, F=None):
    F = frames(mp4) if F is None else F
    if not len(F): return None, []
    # ignore bottom third (lower-third overlay) for scoring
    Fn = norm(F[:, : H * 2 // 3])
    best = np.zeros(len(F))
    for t in templates(img):
        tn = norm(t[: H * 2 // 3])
        best = np.maximum(best, (Fn * tn).sum(axis=(1, 2)))
    hits = np.where(best >= thr)[0]
    spans = []
    for i in hits:
        s = i * STEP
        if spans and s - spans[-1][1] <= STEP * 1.5: spans[-1][1] = s
        else: spans.append([s, s])
    return float(best.max()), [(round(a, 1), round(b + STEP, 1)) for a, b in spans]


if __name__ == "__main__":
    F = frames(sys.argv[1])
    for img in sys.argv[2:]:
        print(img, *match(sys.argv[1], img, F=F))
