#!/usr/bin/env python3
"""nova_make.py — Nova makes physical objects from an idea, autonomously.

Pipeline:  idea (text)
  -> [1] LLM writes build123d code   (local qwen3-coder, escalates to Claude/OpenRouter)
  -> [2] run in venv -> STL + render  (nova_make_part.py, python@3.12 venv)
  -> [3] validate geometry            (watertight, fits bed, sane volume)
  -> [4] slice with OrcaSlicer CLI    -> .gcode.3mf  (X1C / PLA)
  -> [5] enforce filament + time caps (autonomous safety)
  -> [6] print on an idle printer     (nova_bambu_watch.py print)

Autonomous: no human confirmation. Safety is physical-bounds validation, not a
prompt — bed-fit, watertight, filament/time caps, and the idle guard.

Usage:
  nova_make.py "a clever puzzle object that looks like a trefoil knot"
  nova_make.py "a 40mm hex desk tidy" --printer P2 --no-print
  nova_make.py "..." --max-grams 25 --max-hours 2 --dry-run   # stop before slicing
Written by Jordan Koch.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import re
import urllib.request
import zipfile
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
VENV_PY = str(Path.home() / ".openclaw/venvs/nova_make/bin/python3.12")
PART_HELPER = str(Path(__file__).parent / "nova_make_part.py")
WATCH = str(Path(__file__).parent / "nova_bambu_watch.py")
SYS_PY = "/opt/homebrew/bin/python3"
OUT_DIR = Path.home() / ".openclaw/workspace/nova_make"

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
LOCAL_MODEL = "qwen3-coder:30b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CLOUD_MODEL = "anthropic/claude-sonnet-4.5"   # escalation: strong at CAD code

ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
BED_MM = 256.0          # X1C build volume per axis
BED_MARGIN = 6.0        # keep away from the very edge
BUSY = {"RUNNING", "PREPARE", "PAUSE", "SLICING", "RESUMING"}

SYSTEM_PROMPT = r"""You write build123d 0.11 (Python CAD) ALGEBRA-MODE code. Output ONLY one Python code block, no prose.

API you MUST use (algebra mode — primitives are centered on the origin and return a Part):
- import:  from build123d import *
- primitives:  Box(l,w,h)  Cylinder(radius,height)  Sphere(radius)  Cone(r1,r2,h)  Torus(major_r,minor_r)
- booleans (these are the ONLY operators):  union = `+`   difference = `-`   intersection = `&`
  WRONG: `a | b` (no such thing). RIGHT: `a + b`.
- move/rotate:  Pos(x,y,z) * shape   and   Rot(xdeg,ydeg,zdeg) * shape
- rounding:  fillet(shape.edges(), radius=R)   chamfer(shape.edges(), length=L)
  filter edges:  shape.edges().filter_by(Axis.Z)
- sweeps/lofts:  sweep(section, path)   loft([sec1, sec2, ...])   extrude(face, amount)
- There is NO `BuildSolid`, `BuildObject`, or `|` operator. Use the names above only.

Rules:
- Assign the FINAL single solid to a top-level variable named `result`.
- Units are mm. `result` MUST be ONE watertight manifold solid, fitting in 200x200x200mm (aim 40-90mm).
- Printable on FDM: no disconnected pieces, knife-edges, or floating geometry; a flat-ish base helps.
- No file/network/OS access. Only build123d, math, and random (seed it).

Worked example (a filleted coaster with a bore):
```python
from build123d import *
base = Box(60, 60, 12)
base = fillet(base.edges().filter_by(Axis.Z), radius=4)
result = base - Cylinder(8, 20)
```
Return runnable code that defines `result`."""


def log(m):
    print(f"[nova_make {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM calls ─────────────────────────────────────────────────────────────────
def _extract_code(text):
    """Pull the python from a ```python ...``` block, else return the raw text."""
    if "```" in text:
        block = text.split("```", 2)[1]
        if block.startswith("python"):
            block = block[len("python"):]
        return block.strip()
    return text.strip()


def gen_local(prompt):
    payload = json.dumps({"model": LOCAL_MODEL, "prompt": prompt,
                          "system": SYSTEM_PROMPT, "stream": False,
                          "options": {"temperature": 0.4}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    return _extract_code(json.loads(urllib.request.urlopen(req, timeout=300).read())["response"])


def gen_cloud(prompt):
    key = subprocess.check_output(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"]
    ).decode().strip()
    payload = json.dumps({"model": CLOUD_MODEL, "messages": [
        {"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=180).read())
    return _extract_code(r["choices"][0]["message"]["content"])


# ── run generated code in the venv ────────────────────────────────────────────
def run_part(src, stem):
    stl, png = OUT_DIR / f"{stem}.stl", OUT_DIR / f"{stem}.png"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); srcpath = f.name
    try:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # avoid /Volumes/Data FDA wall
        p = subprocess.run([VENV_PY, PART_HELPER, srcpath, str(stl), str(png)],
                           capture_output=True, text=True, timeout=180, env=env)
        line = (p.stdout or "").strip().splitlines()
        meta = json.loads(line[-1]) if line else {"ok": False, "error": p.stderr[-800:]}
    except Exception as e:
        meta = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        os.unlink(srcpath)
    meta["stl"], meta["png"] = str(stl), str(png)
    return meta


def validate(meta):
    """Physical printability gate. Returns (ok, reason)."""
    if not meta.get("ok"):
        return False, meta.get("error", "exec failed")[-400:]
    if not meta.get("watertight"):
        return False, "not watertight (won't slice cleanly)"
    dims = meta.get("dims_mm") or [999, 999, 999]
    if max(dims) > BED_MM - BED_MARGIN:
        return False, f"too big for the bed: {dims} mm (max {BED_MM-BED_MARGIN})"
    if not meta.get("volume_mm3"):
        return False, "zero/None volume"
    if meta.get("faces", 0) < 12:
        return False, "degenerate mesh"
    return True, "ok"


def generate_part(idea):
    """Hybrid generate→run→validate with self-repair. Returns validated meta or raises."""
    base = f"Make this object: {idea}"
    attempts = [("local", gen_local)] * 3 + [("cloud", gen_cloud)] * 2
    feedback = ""
    for i, (who, fn) in enumerate(attempts, 1):
        try:
            log(f"attempt {i}/{len(attempts)} via {who}…")
            src = fn(base + feedback)
            meta = run_part(src, f"part_{int(time.time())}_{i}")
            ok, reason = validate(meta)
            if ok:
                log(f"✓ valid: {meta['dims_mm']} mm, {meta['faces']} faces, "
                    f"{meta['volume_mm3']/1000:.1f} cm³ ({who})")
                meta["source"] = src
                return meta
            log(f"✗ rejected: {reason}")
            feedback = (f"\n\nYour previous attempt failed validation: {reason}\n"
                        f"Here is the code you wrote:\n```python\n{src}\n```\nFix it.")
        except Exception as e:
            log(f"✗ {who} error: {e}")
            feedback = f"\n\nYour previous attempt raised: {e}\nReturn corrected code."
    raise RuntimeError("could not generate a valid, printable solid after all attempts")


# ── slice ─────────────────────────────────────────────────────────────────────
def _find_profile(sub, *needles):
    """Find a BBL profile. When the needles don't ask for a specific nozzle, prefer
    the default 0.4-nozzle variant (its filename omits the ' nozzle' tag) and skip
    template files — otherwise rglob order can grab a 0.2/0.6/0.8-nozzle profile."""
    root = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles")
    want_nozzle = any("nozzle" in n.lower() for n in needles)
    matches = []
    for p in root.rglob(f"{sub}/*.json"):
        name = p.name.lower()
        if "template" in name:
            continue
        if all(n.lower() in name for n in needles):
            matches.append((p, "nozzle" in name))
    # if not asking for a nozzle, prefer the default (no ' nozzle' suffix) profile
    matches.sort(key=lambda m: m[1] and not want_nozzle)
    if matches:
        return str(matches[0][0])
    raise FileNotFoundError(f"no {sub} profile matching {needles}")


def slice_part(stl, stem):
    machine = _find_profile("machine", "X1", "Carbon", "0.4 nozzle")
    process = _find_profile("process", "0.20mm", "Standard", "X1C")
    filament = _find_profile("filament", "PLA Basic", "X1C")
    outdir = OUT_DIR / f"{stem}_slice"
    outdir.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.gcode.3mf"
    cmd = [ORCA, "--slice", "0",
           "--load-settings", f"{machine};{process}",
           "--load-filaments", filament,
           "--orient", "1", "--arrange", "1", "--ensure-on-bed",
           "--allow-newer-file", "--outputdir", str(outdir), "--export-3mf", name, stl]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    out3mf = outdir / name
    if not out3mf.exists():
        raise RuntimeError(f"slice produced no 3mf (exit {p.returncode}): {p.stdout[-500:]}{p.stderr[-300:]}")
    return str(out3mf)


def slice_limits(path3mf, max_grams, max_hours):
    """Read print time + filament use from the sliced 3mf; enforce caps.

    Time = `prediction` (seconds). Weight is often blank in OrcaSlicer's CLI output,
    so fall back: plate `weight` -> sum of filament `used_g` -> estimate from `used_m`
    (~2.98 g per metre of 1.75mm PLA). Unknown values don't block — the bbox gate
    already bounds object size."""
    with zipfile.ZipFile(path3mf) as z:
        info = next((n for n in z.namelist() if n.endswith("slice_info.config")), None)
        text = z.read(info).decode(errors="ignore") if info else ""

    def num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    m = re.search(r'key="prediction"\s+value="([^"]*)"', text)
    secs = num(m.group(1)) if m else None
    hours = secs / 3600.0 if secs else None

    m = re.search(r'key="weight"\s+value="([^"]*)"', text)
    grams = num(m.group(1)) if m else None
    if not grams:
        used_g = [g for g in (num(x) for x in re.findall(r'used_g="([^"]*)"', text)) if g]
        if used_g:
            grams = sum(used_g)
    if not grams:
        used_m = [u for u in (num(x) for x in re.findall(r'used_m="([^"]*)"', text)) if u]
        if used_m:
            grams = sum(used_m) * 2.98  # ~g per metre of 1.75mm PLA (est.)

    if grams and grams > max_grams:
        return False, f"~{grams:.0f} g > cap {max_grams} g", grams, hours
    if hours and hours > max_hours:
        return False, f"{hours:.1f} h > cap {max_hours} h", grams, hours
    return True, "ok", grams, hours


# ── print ─────────────────────────────────────────────────────────────────────
def _watch(args):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(Path(__file__).parent)
    return subprocess.run([SYS_PY, WATCH, *args], capture_output=True, text=True, timeout=90, env=env)


def printer_idle(printer):
    r = _watch(["status", printer])
    line = (r.stdout or "").strip()
    log(f"printer status: {line.splitlines()[-1] if line else '(no status)'}")
    return not any(s in line for s in BUSY) and "idle" in line.lower()


def print_part(path3mf, printer):
    if not printer_idle(printer):
        raise RuntimeError(f"{printer} is not idle — refusing to start a job on top of another")
    # External spool, direct-feed (no AMS — avoids the purge waste). Always --no-ams.
    r = _watch(["print", printer, path3mf, "--no-ams"])
    log(r.stdout.strip() or r.stderr.strip())
    return r.returncode == 0


# ── main ──────────────────────────────────────────────────────────────────────
def selftest():
    # validate() gate
    good = {"ok": True, "watertight": True, "dims_mm": [50, 50, 75], "volume_mm3": 1000, "faces": 44}
    assert validate(good)[0]
    assert not validate({**good, "watertight": False})[0]          # leaky -> reject
    assert not validate({**good, "dims_mm": [300, 50, 50]})[0]      # too big -> reject
    assert not validate({**good, "volume_mm3": None})[0]            # no volume -> reject
    assert not validate({"ok": False, "error": "boom"})[0]          # exec failed -> reject
    # code extraction
    assert _extract_code("```python\nresult=1\n```") == "result=1"
    assert _extract_code("result=2") == "result=2"
    print("selftest OK")


def main():
    if "--selftest" in sys.argv:
        selftest(); return
    ap = argparse.ArgumentParser()
    ap.add_argument("idea")
    ap.add_argument("--printer", default="P1")
    ap.add_argument("--no-print", action="store_true", help="generate + slice, don't print")
    ap.add_argument("--dry-run", action="store_true", help="generate + validate only, no slice")
    ap.add_argument("--max-grams", type=float, default=40.0)
    ap.add_argument("--max-hours", type=float, default=4.0)
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    log(f"idea: {a.idea!r}")
    meta = generate_part(a.idea)
    stem = Path(meta["stl"]).stem
    print(json.dumps({"stl": meta["stl"], "png": meta.get("png"),
                      "dims_mm": meta["dims_mm"], "volume_cm3": round(meta["volume_mm3"]/1000, 1)}))
    if a.dry_run:
        log("dry-run: stopping before slice."); return

    log("slicing…")
    g3mf = slice_part(meta["stl"], stem)
    ok, reason, grams, hours = slice_limits(g3mf, a.max_grams, a.max_hours)
    log(f"sliced: {grams or '?'} g, {f'{hours:.1f}' if hours else '?'} h -> {g3mf}")
    if not ok:
        log(f"ABORT (autonomous safety cap): {reason}"); sys.exit(3)
    if a.no_print:
        log("--no-print: leaving the .3mf for you."); return

    log(f"printing on {a.printer} (autonomous)…")
    if print_part(g3mf, a.printer):
        log("✅ print started.")
    else:
        log("print failed."); sys.exit(4)


if __name__ == "__main__":
    main()
