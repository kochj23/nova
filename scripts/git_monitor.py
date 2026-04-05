#!/usr/bin/env python3
"""Monitor local git repos for changes."""
import subprocess
import json
from pathlib import Path

repos = [
    "/Volumes/Data/AI/MLXCode",
    "/Volumes/Data/AI/NMAPScanner",
    "/Volumes/Data/AI/RsyncGUI",
]

print("✅ Git monitor initialized")
for repo in repos:
    if Path(repo).exists():
        print(f"  ✓ {Path(repo).name}")
