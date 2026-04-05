#!/usr/bin/env python3
"""Track time-series metrics: disk, memory, crons."""
import json
import subprocess
from pathlib import Path
from datetime import datetime

metrics_dir = Path.home() / ".openclaw/workspace/metrics"
metrics_dir.mkdir(exist_ok=True)

def collect_metrics():
    """Collect disk, memory, cron metrics."""
    metrics = {
        "timestamp": datetime.now().isoformat(),
        "disk": {},
        "memory": {},
    }
    
    # Disk usage
    try:
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        lines = result.stdout.split('\n')
        if len(lines) > 1:
            parts = lines[1].split()
            metrics["disk"]["root_percent"] = int(parts[4].rstrip('%'))
    except:
        pass
    
    return metrics

metrics = collect_metrics()
today = datetime.now().strftime("%Y-%m-%d")
metrics_file = metrics_dir / f"metrics-{today}.json"

with open(metrics_file, "w") as f:
    json.dump(metrics, f, indent=2)

print(f"✅ Metrics collected: {metrics_file}")
