#!/usr/bin/env python3
"""
nova_analytics_collector.py — Privacy-first web analytics collector.

Accepts page view and event data from:
  - JS beacons on static sites (POST /collect)
  - Server-side middleware on Python apps (POST /collect/internal)
  - Fallback pixel (GET /pixel)

Security:
  - No PII stored — IPs hashed with daily-rotating salt
  - Rate limiting per visitor hash (60 events/min)
  - CORS locked to known domains
  - DNT respected at beacon level (client-side)
  - Redis stream capped at 10k entries

Port: 37490
"""

import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

REDIS_URL = "redis://192.168.1.6:6379"
PORT = 37490
STREAM_KEY = "analytics:events"
STREAM_MAXLEN = 10000
RATE_LIMIT_MAX = 60
RATE_LIMIT_WINDOW = 60

ALLOWED_SITES = {
    "nova.digitalnoise.net",
    "digitalnoise.net",
    "chat.digitalnoise.net",
    "gauges.digitalnoise.net",
}

ALLOWED_ORIGINS = [
    "https://nova.digitalnoise.net",
    "https://digitalnoise.net",
    "https://chat.digitalnoise.net",
    "https://gauges.digitalnoise.net",
]

PIXEL_GIF = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
    b"\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00"
    b"\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
    b"\x44\x01\x00\x3b"
)

_redis: aioredis.Redis = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield
    await _redis.close()


app = FastAPI(title="Nova Analytics Collector", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
    max_age=86400,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def get_daily_salt() -> str:
    today = time.strftime("%Y-%m-%d")
    key = f"analytics:salt:{today}"
    salt = await _redis.get(key)
    if not salt:
        salt = secrets.token_hex(32)
        await _redis.set(key, salt, ex=172800)
    return salt


async def hash_ip(ip: str) -> str:
    salt = await get_daily_salt()
    raw = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()
    return raw[:16]


def bucket_ua(raw_ua: str) -> str:
    ua = raw_ua.lower()
    if any(k in ua for k in ("bot", "spider", "crawl", "curl", "wget", "python")):
        return "bot"
    platform = "desktop"
    if any(k in ua for k in ("mobile", "android", "iphone")):
        platform = "mobile"
    elif any(k in ua for k in ("tablet", "ipad")):
        platform = "tablet"
    browser = "other"
    if "chrome" in ua and "edg" not in ua and "opr" not in ua:
        browser = "chrome"
    elif "firefox" in ua:
        browser = "firefox"
    elif "safari" in ua and "chrome" not in ua:
        browser = "safari"
    elif "edg" in ua:
        browser = "edge"
    return f"{platform}-{browser}"


def extract_domain(referrer: str) -> str:
    if not referrer:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(referrer if "://" in referrer else f"https://{referrer}")
        return parsed.hostname or ""
    except Exception:
        return ""


async def check_rate_limit(visitor_hash: str) -> bool:
    key = f"analytics:ratelimit:{visitor_hash}"
    count = await _redis.incr(key)
    if count == 1:
        await _redis.expire(key, RATE_LIMIT_WINDOW)
    return count <= RATE_LIMIT_MAX


async def push_event(event: dict):
    await _redis.xadd(STREAM_KEY, event, maxlen=STREAM_MAXLEN, approximate=True)


# ── Models ───────────────────────────────────────────────────────────────────

class PageViewEvent(BaseModel):
    site: str
    path: str
    referrer: str = ""
    screen: str = ""

    @field_validator("site")
    @classmethod
    def validate_site(cls, v):
        if v not in ALLOWED_SITES:
            raise ValueError("invalid site")
        return v

    @field_validator("path")
    @classmethod
    def validate_path(cls, v):
        if len(v) > 500:
            raise ValueError("path too long")
        return v[:500]


class CustomEvent(BaseModel):
    site: str
    path: str
    event_type: str
    event_data: dict = {}

    @field_validator("site")
    @classmethod
    def validate_site(cls, v):
        if v not in ALLOWED_SITES:
            raise ValueError("invalid site")
        return v

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        allowed = {"scroll", "engagement", "outbound_click", "download", "custom"}
        if v not in allowed:
            raise ValueError("invalid event type")
        return v


class InternalPageView(BaseModel):
    site: str
    path: str
    ip: str
    user_agent: str = ""
    referrer: str = ""
    country: str = ""
    response_ms: int = 0


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/collect")
async def collect_beacon(event: PageViewEvent, request: Request):
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host
    country = request.headers.get("cf-ipcountry", "")
    ua = request.headers.get("user-agent", "")

    visitor_hash = await hash_ip(ip)
    if not await check_rate_limit(visitor_hash):
        return JSONResponse({"ok": False}, status_code=429)

    await push_event({
        "type": "pageview",
        "site": event.site,
        "path": event.path,
        "referrer_domain": extract_domain(event.referrer),
        "country": country,
        "ua_bucket": event.screen or bucket_ua(ua),
        "visitor_hash": visitor_hash,
        "ts": str(int(time.time())),
        "response_ms": "0",
    })
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/collect/event")
async def collect_event(event: CustomEvent, request: Request):
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host
    country = request.headers.get("cf-ipcountry", "")

    visitor_hash = await hash_ip(ip)
    if not await check_rate_limit(visitor_hash):
        return JSONResponse({"ok": False}, status_code=429)

    await push_event({
        "type": "event",
        "site": event.site,
        "path": event.path,
        "event_type": event.event_type,
        "event_data": json.dumps(event.event_data),
        "visitor_hash": visitor_hash,
        "country": country,
        "ts": str(int(time.time())),
    })
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/collect/internal")
async def collect_internal(event: InternalPageView, request: Request):
    client_ip = request.client.host if request.client else ""
    if not (client_ip.startswith("192.168.1.") or client_ip.startswith("127.") or client_ip.startswith("10.0.")):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    visitor_hash = await hash_ip(event.ip)

    await push_event({
        "type": "pageview",
        "site": event.site,
        "path": event.path,
        "referrer_domain": extract_domain(event.referrer),
        "country": event.country,
        "ua_bucket": bucket_ua(event.user_agent),
        "visitor_hash": visitor_hash,
        "ts": str(int(time.time())),
        "response_ms": str(event.response_ms),
    })
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/pixel")
async def tracking_pixel(request: Request):
    ip = request.headers.get("cf-connecting-ip") or request.client.host
    country = request.headers.get("cf-ipcountry", "")
    ua = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    site = request.query_params.get("s", "")
    path = request.query_params.get("p", "/")

    if site in ALLOWED_SITES:
        visitor_hash = await hash_ip(ip)
        if await check_rate_limit(visitor_hash):
            await push_event({
                "type": "pageview",
                "site": site,
                "path": path[:500],
                "referrer_domain": extract_domain(referrer),
                "country": country,
                "ua_bucket": bucket_ua(ua),
                "visitor_hash": visitor_hash,
                "ts": str(int(time.time())),
                "response_ms": "0",
            })

    return Response(
        content=PIXEL_GIF,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/beacon.js")
async def serve_beacon():
    """Serve the analytics beacon JS (cached aggressively)."""
    beacon_path = Path.home() / ".openclaw/apps/nova-control-web/static/analytics-beacon.js"
    if not beacon_path.exists():
        return Response(content="", media_type="application/javascript")
    return Response(
        content=beacon_path.read_text(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/health")
async def health(request: Request):
    client_ip = request.client.host if request.client else ""
    if client_ip.startswith("192.168.1.") or client_ip.startswith("127."):
        stream_len = await _redis.xlen(STREAM_KEY)
        return {"ok": True, "stream_length": stream_len}
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
