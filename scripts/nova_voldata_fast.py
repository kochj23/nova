#!/opt/homebrew/bin/python3
# Fast top-level du of /Volumes/Data via the C `du` (inherits python3's FDA under launchd).
import subprocess as s
out = []
for vol in ("/Volumes/Data", "/Volumes/MoreData"):
    try:
        r = s.run(["/usr/bin/du", "-h", "-d", "1", vol], capture_output=True, text=True, timeout=900)
        out.append(f"### {vol} (rc={r.returncode}) ###\n{r.stdout}")
        if r.stderr:
            out.append("stderr: " + r.stderr[:300])
    except Exception as e:
        out.append(f"### {vol} ERROR: {e}")
out.append("FAST DONE")
open("/tmp/voldata-fast.log", "w").write("\n".join(out) + "\n")
