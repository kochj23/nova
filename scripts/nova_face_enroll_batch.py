#!/usr/bin/env python3
"""Batch-enroll reference photos into the sam-faces people DB.

Reads ~/.openclaw/workspace/faces/known/<Name>/*.{jpg,jpeg,png} and enrolls each photo as a
face encoding for <Name>. More photos per person = better, more robust recognition (fewer
"unknown person" misses on people you actually know).

Idempotent: writes a `.enrolled` marker next to each photo so re-runs skip already-enrolled ones.
Name from folder: "amy_mccaine" -> "Amy Mccaine" (merges with an existing person of that name).

Usage:
  nova_face_enroll_batch.py            # DRY RUN — show what would be enrolled
  nova_face_enroll_batch.py --commit   # actually enroll
  # add more reference photos any time: drop them in known/<Name>/ and re-run with --commit
"""
import sys
from pathlib import Path
from collections import Counter

SAM_FACES_DIR = Path("/Volumes/nas/nova/Nova/skills/sam-faces/sam_faces")
sys.path.insert(0, str(SAM_FACES_DIR.parent))
from sam_faces.enroll import enroll  # noqa: E402

KNOWN = Path.home() / ".openclaw" / "workspace" / "faces" / "known"
EXTS = {".jpg", ".jpeg", ".png"}


def norm_name(folder: str) -> str:
    """folder slug -> display name: 'mary-ann_riordan' -> 'Mary Ann Riordan'."""
    s = folder.replace("_", " ").replace("-", " ").strip().strip(".").strip()
    return " ".join(w.capitalize() for w in s.split())


def _marker(photo: Path) -> Path:
    return photo.parent / (photo.name + ".enrolled")


def collect():
    todo = []
    if not KNOWN.exists():
        return todo
    for d in sorted(KNOWN.iterdir()):
        if not d.is_dir():
            continue
        name = norm_name(d.name)
        if not name:
            continue
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in EXTS and not _marker(f).exists():
                todo.append((name, f))
    return todo


def main():
    commit = "--commit" in sys.argv
    todo = collect()
    if not todo:
        print("Nothing new to enroll (all photos already have a .enrolled marker, or none found).")
        return
    print(f"{'ENROLLING' if commit else 'DRY RUN — would enroll'} {len(todo)} photo(s) across "
          f"{len(set(n for n, _ in todo))} people:")
    for name, cnt in sorted(Counter(n for n, _ in todo).items()):
        print(f"  {name:26} {cnt} photo(s)")
    if not commit:
        print("\nRe-run with --commit to actually enroll.")
        return
    ok = fail = 0
    for name, f in todo:
        try:
            enroll(name, str(f))
            _marker(f).write_text("")
            ok += 1
            print(f"  ✓ {name} <- {f.name}")
        except Exception as e:
            fail += 1
            print(f"  ✗ {name} <- {f.name}: {str(e)[:100]}")
    print(f"\nDone. Enrolled {ok}, failed {fail}.")


if __name__ == "__main__":
    main()
