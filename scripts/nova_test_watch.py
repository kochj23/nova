#!/usr/bin/env python3
"""
nova_test_watch.py — run Nova's test suite and emit each file's result LIVE so you
can watch the tests fire, script by script, in #nova-info.

Each test file is run on its own; as it finishes, a notification goes out via the
central bus (nova_notify -> #nova-info): a green tick + pass count, or a red X +
the failures. A final summary closes it out. Because it rides the notification bus,
"watching the tests fire" is just watching #nova-info.

  nova_test_watch.py                 # run all tests under tests/ + scripts/tests/, emit per file
  nova_test_watch.py <path...>       # only these files/dirs
  nova_test_watch.py --quiet         # only emit failures + the final summary
  nova_test_watch.py --no-slack      # terminal only (no bus emissions)
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TEST_ROOTS = [Path.home() / ".openclaw/tests", Path.home() / ".openclaw/scripts/tests"]


def _emit(title, body, level, no_slack):
    if no_slack:
        return
    try:
        from nova_notify import notify
        notify(title, body=body, level=level, category="tests",
               source="nova_test_watch", dedup_key=None)
    except Exception:
        pass


def discover(paths):
    files = []
    roots = [Path(p) for p in paths] if paths else TEST_ROOTS
    for r in roots:
        if r.is_file() and r.name.startswith("test_"):
            files.append(r)
        elif r.is_dir():
            files.extend(sorted(r.rglob("test_*.py")))
    # de-dup, stable order
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f); out.append(f)
    return out


def run_one(test_file):
    """Run pytest on a single file; return (passed, failed, skipped, dur_s, tail)."""
    t0 = time.time()
    r = subprocess.run(
        ["python3", "-m", "pytest", str(test_file), "-q", "--no-header",
         "-p", "no:cacheprovider", "--tb=line"],
        capture_output=True, text=True, timeout=600)
    dur = time.time() - t0
    out = (r.stdout or "") + (r.stderr or "")
    # parse the pytest summary line (e.g. "3 passed, 1 failed in 0.4s")
    import re
    passed = failed = skipped = 0
    m = re.search(r"(\d+) passed", out);  passed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) failed", out);  failed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) error", out);   failed += int(m.group(1)) if m else 0
    m = re.search(r"(\d+) skipped", out); skipped = int(m.group(1)) if m else 0
    if r.returncode != 0 and passed == 0 and failed == 0:
        failed = 1  # collection error / no tests ran
    fail_tail = "\n".join(l for l in out.splitlines()
                          if "FAILED" in l or "Error" in l or "assert" in l)[:600]
    return passed, failed, skipped, dur, fail_tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--quiet", action="store_true", help="emit only failures + summary")
    ap.add_argument("--no-slack", action="store_true")
    a = ap.parse_args()

    files = discover(a.paths)
    if not files:
        print("No test files found.")
        return 0
    _emit(f"Test run starting — {len(files)} files", None, "info", a.no_slack)
    print(f"Running {len(files)} test files...\n")

    tot_p = tot_f = tot_s = 0
    failed_files = []
    for i, f in enumerate(files, 1):
        p, fl, sk, dur, tail = run_one(f)
        tot_p += p; tot_f += fl; tot_s += sk
        ok = fl == 0
        mark = "✅" if ok else "❌"
        line = f"[{i}/{len(files)}] {mark} {f.name}: {p} passed" + (f", {fl} failed" if fl else "") + (f", {sk} skipped" if sk else "") + f" ({dur:.1f}s)"
        print(line)
        if not ok:
            failed_files.append(f.name)
            _emit(f"❌ {f.name} — {fl} failed", tail or None, "warning", a.no_slack)
        elif not a.quiet:
            _emit(f"✅ {f.name} — {p} passed", None, "info", a.no_slack)

    verdict = "PASS" if tot_f == 0 else "FAIL"
    summary = (f"{len(files)} files · {tot_p} passed · {tot_f} failed · {tot_s} skipped"
               + (f"\nFailing: {', '.join(failed_files[:20])}" if failed_files else ""))
    print(f"\n=== {verdict} — {summary} ===")
    _emit(f"Test run complete — {verdict}", summary, "info" if tot_f == 0 else "warning", a.no_slack)
    return 1 if tot_f else 0


if __name__ == "__main__":
    sys.exit(main())
