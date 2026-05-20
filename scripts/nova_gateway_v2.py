#!/usr/bin/env python3
"""
nova_gateway_v2.py — Thin wrapper for nova_gateway package.

All functionality has been refactored into the nova_gateway/ package.
This file remains for backward compatibility with launchd and start scripts.

Written by Jordan Koch.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from nova_gateway.main import main

if __name__ == "__main__":
    asyncio.run(main())
