#!/usr/bin/env python3
"""
nova_homepage_server.py — Serve digitalnoise.net homepage via Cloudflare Tunnel.

Simple static file server for the homepage at /Volumes/Data/xcode/digitalnoise-homepage.
Port: 37491

Written by Jordan Koch.
"""

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

SITE_DIR = Path("/Volumes/Data/xcode/digitalnoise-homepage")
PORT = 37491

app = FastAPI(title="digitalnoise.net")


@app.get("/")
async def index():
    return FileResponse(SITE_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
