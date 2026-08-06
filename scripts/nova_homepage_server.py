#!/usr/bin/env python3
"""
nova_homepage_server.py — Serve digitalnoise.net homepage via Cloudflare Tunnel.

Simple static file server for the homepage at ~/.openclaw/digitalnoise-homepage.
Port: 37491

NOTE (2026-08-06): moved OFF /Volumes/Data/xcode — that path is on the flaky external
enclosure behind the macOS FDA/TCC wall, so whenever the drive glitched the page 404'd
("Operation not permitted" reading index.html). Serve from the main SSD only. See
memory 'fda-volumes-data-route-around': never serve Nova-managed content from /Volumes/Data.

Written by Jordan Koch.
"""

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

SITE_DIR = Path.home() / ".openclaw" / "digitalnoise-homepage"  # main SSD; NOT /Volumes/Data (FDA wall)
PORT = 37491

app = FastAPI(title="digitalnoise.net")


@app.get("/")
async def index():
    return FileResponse(SITE_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
