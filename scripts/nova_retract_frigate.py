#!/usr/bin/env python3
import os
import subprocess
ROOT = os.path.expanduser("~/nova-journal")
SLUG = "2026-06-30-frigate-nvr-the-camera-brain-you-already-need-but-haven-t-ad"
def run(args):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    print(">", " ".join(args), "->", r.returncode)
    if r.stdout.strip(): print("  out:", r.stdout.strip()[-300:])
    if r.stderr.strip(): print("  err:", r.stderr.strip()[-300:])
run(["git", "rm", "-f", f"content/operations/{SLUG}.md"])
run(["git", "rm", "-f", f"static/images/operations/{SLUG}.webp"])
run(["git", "commit", "-m", "Retract Frigate scout article - already in production (#635); scout false positive, now guarded."])
# Rebase onto origin BEFORE pushing so a diverged clone can't silently strand commits.
pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
if pull.returncode != 0:
    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True)
    print(f"push ABORTED — pull --rebase failed (diverged/conflict): {pull.stderr.strip()[-200:]}")
else:
    push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
    print(f"push rc={push.returncode} {push.stderr.strip()[-200:]}")
print("RETRACT DONE")
