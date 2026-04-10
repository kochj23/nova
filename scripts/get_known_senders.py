#!/usr/bin/env python3
"""Print comma-separated known sender emails from herd_config.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".openclaw"))
try:
    from herd_config import HERD
    print(",".join(m["email"] for m in HERD))
except ImportError:
    print("")
