import os
import subprocess
from pathlib import Path
ROOT = os.path.expanduser("~/nova-journal")
f = Path(ROOT) / "content/start-here/index.md"
s = f.read_text()
n = s.count("/rando/2026-05-22-")
f.write_text(s.replace("/rando/2026-05-22-", "/operations/2026-05-22-"))
print(f"replaced {n} dead /rando/2026-05-22- links -> /operations/")
def run(a):
    r = subprocess.run(a, cwd=ROOT, capture_output=True, text=True)
    print(">", " ".join(a), "->", r.returncode)
    if r.stderr.strip(): print("  ", r.stderr.strip()[-160:])
run(["git", "add", "content/start-here/index.md"])
run(["git", "commit", "-m", "Fix Start Here: featured reads moved /rando/ -> /operations/ (un-404)"])
# Rebase onto origin BEFORE pushing so a diverged clone can't silently strand commits.
pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
if pull.returncode != 0:
    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True)
    print(f"push ABORTED — pull --rebase failed (diverged/conflict): {pull.stderr.strip()[-200:]}")
else:
    push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
    print(f"push rc={push.returncode} {push.stderr.strip()[-200:]}")
print("DONE")
