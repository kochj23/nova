#!/usr/bin/env python3
"""nova_face_retention.py — purpose limitation for cameras and faces (Clancy guardrail 7b).

Cameras, the scanner and ADS-B are for HOME SAFETY ONLY (Proteus rule P3, nova_privacy_guards).
Safety does not need a lasting record of who else walked past the house. So, daily:

  KEPT (household = service_config face_retention/household; default Jordan Koch, Amy):
    * everything about household members;
    * "Unknown person detected at <camera>" memories — a safety event with no identity in it;
    * face_people / face_encodings of people Jordan ENROLLED. That enrollment is a deliberate grant
      of authority by Jordan, reconfirmed quarterly by Commander's Intent (grant
      'face.enrolled_non_household'). If he declines it: --purge-person NAME --by jordan.

  DELETED once older than TTL (service_config face_retention/ttl_hours, default 72 h):
    * face_unknown_candidates rows — unresolved ones, and ones resolved as anything non-household —
      and their crop files;
    * crop files in workspace/faces/unknown/;
    * top-level sighting crops workspace/faces/known/known_<name>_<camera>_latest_<t>_<l>.jpg of
      non-household names (subdirectories there are Jordan's enrollment photos: never touched);
    * face_presence rows (where-and-when sightings) of non-household people;
    * vector memories source='face_recognition' whose metadata.person is non-household — deleted
      through the memory server (DELETE /forget?id=), 3 attempts with backoff; failures are counted,
      logged to face_retention_log and make the run exit non-zero (never silent).

Every deletion count goes to nova_ops.face_retention_log. The Mr. Harrigan / Cell rules and the
P3 camera purpose guard already exist (nova_privacy_guards); this organ only enforces retention.

Usage: nova_face_retention.py [--dry-run] | --purge-person NAME --by jordan [--dry-run] | --selftest
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
MEM_DSN = os.environ.get("NOVA_MEM_DSN", "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
FACES = Path.home() / ".openclaw" / "workspace" / "faces"
DEFAULT_HOUSEHOLD = {"jordan koch": "jordan", "amy mccaine": "amy", "amy": "amy"}
DEFAULT_TTL_H = 72
# camera frame stems written by nova_face_recognition (EXTERIOR_CAMERAS); a sighting crop is
# known_<name with _>_<stem>_<top>_<left>.jpg
CAMERA_STEMS = ("front_door_patio_latest", "front_door_latest", "front_yard_alt_latest", "front_yard_latest",
                "carport_latest", "alley_north_latest", "alley_south_latest", "exterior_garbage_latest",
                "garage_latest", "abundio_boundary_latest")
SIGHTING_RX = re.compile(r"^known_(?P<name>.+)_(?P<cam>" + "|".join(CAMERA_STEMS) + r")_\d+_\d+\.jpg$")
GENERIC_RX = re.compile(r"^known_(?P<name>.+?)_(?P<cam>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*?_latest)_\d+_\d+\.jpg$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS face_retention_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL,
  n int NOT NULL,
  dry_run boolean NOT NULL DEFAULT false,
  detail jsonb NOT NULL DEFAULT '{}');
"""


def log(m: str) -> None:
    print(f"[face-retention {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure ────────────────────────────────────────────────────────────────────

def is_household(name: str | None, household: dict) -> bool:
    n = " ".join((name or "").replace("_", " ").split()).lower()
    return bool(n) and n in household


def parse_ts(s) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def sighting_name(filename: str, known_names=()) -> str | None:
    """Name in a sighting crop's filename. Enrolled names (longest first) win over the pattern."""
    if filename.startswith("known_"):
        rest = filename[len("known_"):].lower()
        for n in sorted(known_names or (), key=len, reverse=True):
            if rest.startswith(n.lower().replace(" ", "_") + "_"):
                return n
    m = SIGHTING_RX.match(filename) or GENERIC_RX.match(filename)
    return m.group("name").replace("_", " ") if m else None


def candidates_to_delete(rows: list, household: dict, cutoff: datetime) -> list:
    """rows: [(id, image_path, face_crop_path, detected_at, resolved, resolved_as)] -> rows to delete."""
    out = []
    for r in rows:
        ts = parse_ts(r[3])
        if ts is None or ts >= cutoff:
            continue
        if not r[4] or not is_household(r[5], household):
            out.append(r)
    return out


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def forget_url() -> str:
    try:
        import nova_config
        base = nova_config.VECTOR_URL
    except Exception:  # noqa: BLE001
        base = "http://memory-server.digitalnoise.net:18790/remember"
    return re.sub(r"/remember/?$", "", base) + "/forget"


def forget(mem_id: str, attempts: int = 3, base: float = 1.0, _open=None, _sleep=time.sleep) -> bool:
    """DELETE /forget?id= with retry+backoff. 404 = already gone = success."""
    _open = _open or urllib.request.urlopen
    hdr = {}
    if os.environ.get("NOVA_MEMORY_TOKEN"):
        hdr["Authorization"] = "Bearer " + os.environ["NOVA_MEMORY_TOKEN"]
    url = f"{forget_url()}?id={urllib.request.quote(str(mem_id))}"
    for i in range(attempts):
        try:
            with _open(urllib.request.Request(url, method="DELETE", headers=hdr), timeout=10):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            if e.code in (401, 403):
                log(f"forget {mem_id}: unauthorized ({e.code}) — set NOVA_MEMORY_TOKEN")
                return False
            err = e
        except Exception as e:  # noqa: BLE001
            err = e
        if i < attempts - 1:
            _sleep(base * (2 ** i))
    log(f"forget {mem_id} failed after {attempts} attempts: {err}")
    return False


# ── run ─────────────────────────────────────────────────────────────────────

def _connect(dsn):
    import nova_watch_common as W
    return W.connect(dsn)


def settings(cur) -> tuple[dict, float]:
    try:
        import nova_watch_common as W
        hh = W.get_config(cur, "face_retention", "household", None)
        ttl = W.get_config(cur, "face_retention", "ttl_hours", None)
    except Exception:  # noqa: BLE001
        hh, ttl = None, None
    hh = {k.lower(): v for k, v in (hh or DEFAULT_HOUSEHOLD).items()}
    return hh, float(ttl or DEFAULT_TTL_H)


def _unlink(p: Path, dry: bool) -> bool:
    if dry:
        return True
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log(f"could not delete {p.name}: {e}")
        return False


def run(dry: bool = False, now: datetime | None = None, faces: Path = FACES) -> dict:
    now = now or datetime.now(timezone.utc)
    conn = _connect(DSN)
    cur = conn.cursor()
    if not dry:
        cur.execute(SCHEMA)
    hh, ttl = settings(cur)
    cutoff = now - timedelta(hours=ttl)
    res = {"ttl_hours": ttl, "household": sorted(set(hh.values())), "dry_run": dry}

    # 1. unknown candidates + their crops
    cur.execute("SELECT id, image_path, face_crop_path, detected_at, resolved, resolved_as FROM face_unknown_candidates")
    dels = candidates_to_delete(cur.fetchall(), hh, cutoff)
    crops = 0
    for r in dels:
        if r[2] and inside(Path(r[2]), faces) and Path(r[2]).exists() and _unlink(Path(r[2]), dry):
            crops += 1
    if dels and not dry:
        cur.execute("DELETE FROM face_unknown_candidates WHERE id = ANY(%s)", ([r[0] for r in dels],))
    res["unknown_candidates"], res["candidate_crops"] = len(dels), crops

    # 2. stray files in faces/unknown
    n = 0
    udir = faces / "unknown"
    for p in (udir.iterdir() if udir.is_dir() else []):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png") and \
                datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff and _unlink(p, dry):
            n += 1
    res["unknown_files"] = n

    # 3. top-level non-household sighting crops in faces/known (never subdirectories)
    n, unparsed = 0, 0
    kdir = faces / "known"
    cur.execute("SELECT name FROM face_people")
    enrolled = [r[0] for r in cur.fetchall()]
    for p in (kdir.iterdir() if kdir.is_dir() else []):
        if not p.is_file() or not p.name.startswith("known_"):
            continue
        name = sighting_name(p.name, enrolled)
        if name is None:
            unparsed += 1
            continue
        if not is_household(name, hh) and datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff \
                and _unlink(p, dry):
            n += 1
    res["sighting_crops"], res["sighting_crops_unparsed_kept"] = n, unparsed

    # 4. face_presence sightings of non-household people
    cur.execute("SELECT id, person_name FROM face_presence WHERE last_seen < %s", (cutoff,))
    fp = [i for i, name in cur.fetchall() if not is_household(name, hh)]
    if fp and not dry:
        cur.execute("DELETE FROM face_presence WHERE id = ANY(%s)", (fp,))
    res["face_presence"] = len(fp)

    # 5. vector memories naming non-household people
    mconn = _connect(MEM_DSN)
    try:
        mc = mconn.cursor()
        mc.execute("SELECT id::text, metadata->>'person' FROM memories WHERE source='face_recognition' "
                   "AND metadata->>'person' IS NOT NULL AND created_at < %s", (cutoff,))
        mems = [(i, p) for i, p in mc.fetchall() if not is_household(p, hh)]
    finally:
        mconn.close()
    ok = failed = 0
    for mid, _p in mems:
        if dry or forget(mid):
            ok += 1
        else:
            failed += 1
    res["memories"], res["memory_failures"] = ok, failed
    res["memories_people"] = len({p for _i, p in mems})       # count only; names are not logged

    if not dry:
        for k in ("unknown_candidates", "candidate_crops", "unknown_files", "sighting_crops", "face_presence",
                  "memories", "memory_failures"):
            if res[k]:
                cur.execute("INSERT INTO face_retention_log (kind, n, dry_run, detail) VALUES (%s,%s,%s,%s::jsonb)",
                            (k, res[k], dry, json.dumps({"ttl_hours": ttl})))
    log(json.dumps(res))
    conn.close()
    return res


def purge_person(name: str, by: str, dry: bool = False) -> int:
    """Jordan declined the grant for one enrolled person: remove their identity (face_people,
    face_encodings, face_presence). Enrollment photo folders are Jordan's files — listed, not deleted."""
    if not (by or "").lower().startswith("jordan"):
        print("refusing: only Jordan can purge an enrolled identity (--by jordan)")
        return 2
    conn = _connect(DSN)
    cur = conn.cursor()
    hh, _ttl = settings(cur)
    if is_household(name, hh):
        print("refusing: that is a household member")
        return 2
    cur.execute("SELECT id FROM face_people WHERE lower(name)=lower(%s)", (name,))
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        print("no such enrolled person")
        return 1
    cur.execute("SELECT count(*) FROM face_encodings WHERE person_id = ANY(%s)", (ids,))
    n_enc = cur.fetchone()[0]
    if not dry:
        cur.execute(SCHEMA)
        cur.execute("DELETE FROM face_presence WHERE person_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM face_encodings WHERE person_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM face_people WHERE id = ANY(%s)", (ids,))
        cur.execute("INSERT INTO face_retention_log (kind, n, dry_run, detail) VALUES ('purge_person', %s, false, %s::jsonb)",
                    (n_enc, json.dumps({"by": by[:40], "people": len(ids)})))
    folders = [p for p in (FACES / "known").glob("*") if p.is_dir() and p.name.lower().replace("_", " ") == name.lower()]
    print(f"{'would purge' if dry else 'purged'} {len(ids)} identity row(s), {n_enc} encoding(s). "
          f"Enrollment photo folder(s) left for Jordan: {[str(p) for p in folders]}")
    return 0


def selftest() -> int:
    hh = dict(DEFAULT_HOUSEHOLD)
    assert is_household("Jordan Koch", hh) and is_household("jordan_koch", hh) and not is_household("Dave Bloom", hh)
    assert sighting_name("known_Dave_Bloom_front_yard_latest_120_340.jpg") == "Dave Bloom"
    assert sighting_name("README.md") is None
    assert sighting_name("known_Mary-Ann_Riordan_carport_latest_1_2.jpg", ["Mary-Ann Riordan"]) == "Mary-Ann Riordan"
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    rows = [("a", "", "", (now - timedelta(days=5)).isoformat(), 0, None),
            ("b", "", "", (now - timedelta(hours=1)).isoformat(), 0, None),
            ("c", "", "", (now - timedelta(days=5)).isoformat(), 1, "Jordan Koch"),
            ("d", "", "", (now - timedelta(days=5)).isoformat(), 1, "vehicle — false positive")]
    assert [r[0] for r in candidates_to_delete(rows, hh, now - timedelta(hours=72))] == ["a", "d"]
    assert forget_url().endswith("/forget")
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-person")
    ap.add_argument("--by", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.purge_person:
        return purge_person(a.purge_person, a.by, a.dry_run)
    res = run(a.dry_run)
    return 1 if res.get("memory_failures") else 0


if __name__ == "__main__":
    sys.exit(main())
