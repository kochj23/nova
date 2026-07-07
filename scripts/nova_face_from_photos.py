#!/usr/bin/env python3
"""Extract labeled face crops from the macOS Photos 'People & Pets' library and enroll them into
the Nova face DB (PostgreSQL, via sam_faces). Originals are iCloud-optimized (not on disk), so we
crop from the local ~1024px derivatives that Photos keeps regardless.

Usage:
  nova_face_from_photos.py "Brady Riordan"             # extract top faces + enroll into PG
  nova_face_from_photos.py "Bailey" --no-enroll -n 6   # extract crops only (pets can't enroll —
                                                       #   sam_faces is a human-face model)
"""
import sqlite3, os, glob, sys, argparse
from PIL import Image

LIB = "/Volumes/Data/Pictures/Photos Library.photoslibrary"
OUT = os.path.expanduser("~/.openclaw/workspace/faces/photos_crops")


def derivative(uuid: str):
    d = f"{LIB}/resources/derivatives/{uuid[0].lower()}"
    m = glob.glob(f"{d}/{uuid}_*_105_c.jpeg") or glob.glob(f"{d}/{uuid}*.jpeg")
    return max(m, key=os.path.getsize) if m else None


def crop_face(img_path, cx, cy, size, margin=2.2):
    """Crop a generous box around the Photos face bbox so sam_faces can re-detect the single face.
    Photos coords are normalized with a bottom-left origin -> flip Y. margin is the calibration
    knob: >1 pads the box; generous padding absorbs coordinate quirks across library versions."""
    im = Image.open(img_path).convert("RGB")
    W, H = im.size
    px, py = cx * W, (1 - cy) * H
    half = size * max(W, H) * margin / 2
    box = (max(0, px - half), max(0, py - half), min(W, px + half), min(H, py + half))
    return im.crop(box)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("-n", type=int, default=8, help="target crops to keep")
    ap.add_argument("--no-enroll", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{LIB}/database/Photos.sqlite?immutable=1", uri=True)
    rows = con.execute("""
        SELECT a.ZUUID, f.ZCENTERX, f.ZCENTERY, f.ZSIZE
        FROM ZDETECTEDFACE f
        JOIN ZASSET a  ON a.Z_PK = f.ZASSETFORFACE
        JOIN ZPERSON p ON p.Z_PK = f.ZPERSONFORFACE
        WHERE p.ZDISPLAYNAME = ? AND f.ZQUALITYMEASURE IS NOT NULL
              AND f.ZSIZE > 0.08 AND f.ZCENTERX > 0
        ORDER BY f.ZQUALITYMEASURE DESC
        LIMIT ?""", (a.name, a.n * 4)).fetchall()

    outdir = os.path.join(OUT, a.name.replace(" ", "_"))
    os.makedirs(outdir, exist_ok=True)
    crops = []
    for uuid, cx, cy, size in rows:
        d = derivative(uuid)
        if not d:
            continue
        try:
            p = os.path.join(outdir, f"{uuid}.jpg")
            crop_face(d, cx, cy, size).save(p, quality=92)
            crops.append(p)
        except Exception as e:
            print(f"  crop fail {uuid[:8]}: {str(e)[:40]}")
        if len(crops) >= a.n:
            break
    print(f"Extracted {len(crops)} crops for '{a.name}' -> {outdir}")

    if a.no_enroll or not crops:
        return
    sys.path.insert(0, "/Volumes/nas/nova/Nova/skills/sam-faces")
    from sam_faces.enroll import enroll
    ok = 0
    for p in crops:
        try:
            enroll(a.name, p, note="photos-people")
            ok += 1
        except Exception as e:
            print(f"  enroll skip {os.path.basename(p)[:8]}: {str(e)[:55]}")
    print(f"Enrolled {ok}/{len(crops)} into PG for '{a.name}'")


if __name__ == "__main__":
    main()
