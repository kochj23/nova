#!/usr/bin/env python3
"""nova_browser_service.py — a small, read-only headless-browser service for Nova (2026-10-01).

Why: SearXNG gives Nova headlines; it cannot read a JavaScript-rendered page. OpenClaw 2.0's
"live browser automation" is the one capability she lacked that would have made tonight's
TV-show scrape a single call. This is the bounded version: GET a page in headless Chromium
on the Studio (Playwright + the chromium build already cached here), return readable text,
title, final URL and the first links. Nothing else.

Hard limits (by construction, not by prompt):
  * http/https only; private/loopback/link-local addresses refused unless the host is on the
    service_config allowlist (service=browser, key=allow_hosts);
  * fresh incognito context per request, no cookies kept, no downloads, images/media/fonts
    blocked, JavaScript dialogs auto-dismissed, no clicks/forms/typing — read only;
  * 30s navigation timeout, 2 MB text cap, 3 concurrent pages, one Chromium per process;
  * every fetch logged; output passes through nova_untrusted before leaving.

HTTP (binds 0.0.0.0:37482 — fleet services are never localhost-only):
  GET  /health
  GET  /fetch?url=<...>&max_chars=20000&links=30   -> {"ok":true,"url","title","text","links":[{"text","href"}],"verdict"}
  POST /fetch  {"url": ..., "max_chars": ..., "links": ...}
Registered in service_registry as 'browser' so the gateway reaches it via resolve_url("browser").
launchd: net.digitalnoise.nova-browser. Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import nova_untrusted
except Exception:  # pragma: no cover
    nova_untrusted = None

PORT = int(os.environ.get("NOVA_BROWSER_PORT", "37482"))
NAV_TIMEOUT_MS = 30000
MAX_CHARS_CAP = 2_000_000
DEFAULT_CHARS = 20000
MAX_CONCURRENCY = 3
import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [browser] %(message)s")
log = logging.getLogger("nova_browser")


# ─────────────────────────── pure helpers (selftested) ───────────────────────────
def is_private_host(host: str) -> bool:
    """True for loopback/private/link-local/multicast/reserved targets (incl. names that resolve there)."""
    h = (host or "").strip("[]").lower()
    if not h or h in ("localhost",) or h.endswith(".local") or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return not ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return True            # unresolvable → refuse (fail closed)
    for fam, _, _, _, sockaddr in infos:
        try:
            if not ipaddress.ip_address(sockaddr[0]).is_global:
                return True
        except ValueError:
            return True
    return False


def validate_url(url: str, allow_hosts: set[str] | frozenset = frozenset()) -> tuple[bool, str]:
    try:
        u = urllib.parse.urlsplit(url or "")
    except Exception:
        return False, "unparseable url"
    if u.scheme not in ("http", "https"):
        return False, "only http/https"
    if not u.hostname:
        return False, "no host"
    if u.username or u.password:
        return False, "credentials in url refused"
    if u.hostname.lower() in {h.lower() for h in allow_hosts}:
        return True, "allowlisted"
    if is_private_host(u.hostname):
        return False, "private/loopback/link-local target refused"
    return True, "ok"


def clamp(n, lo, hi, default):
    try:
        n = int(n)
    except Exception:
        return default
    return max(lo, min(hi, n))


def tidy_text(text: str, max_chars: int) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out)[:max_chars]


# ─────────────────────────── browser worker (sync Playwright in a thread) ───────────────────────────
class Browser:
    def __init__(self):
        self._pw = None; self._browser = None
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="pw")
        self._lock = asyncio.Semaphore(MAX_CONCURRENCY)
        self._start_lock = None

    def _ensure(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True, args=["--disable-gpu", "--no-first-run"])
            log.info("chromium launched")

    def _fetch_sync(self, url: str, max_chars: int, links_n: int) -> dict:
        self._ensure()
        ctx = self._browser.new_context(accept_downloads=False, java_script_enabled=True,
                                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36 NovaBrowser/1.0",
                                        viewport={"width": 1280, "height": 900})
        try:
            ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED_RESOURCE_TYPES else route.continue_())
            page = ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.on("dialog", lambda d: d.dismiss())
            resp = page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            title = page.title()
            final = page.url
            text = ""
            for sel in ("main", "article", "[role=main]", "body"):
                try:
                    loc = page.locator(sel).first
                    if loc.count():
                        text = loc.inner_text(timeout=5000)
                        if len(text) > 400 or sel == "body":
                            break
                except Exception:
                    continue
            links = []
            try:
                for a in page.eval_on_selector_all("a[href]", "els => els.slice(0,400).map(a => ({text: (a.innerText||'').trim().slice(0,80), href: a.href}))"):
                    if a.get("href", "").startswith(("http://", "https://")) and a.get("text"):
                        links.append(a)
                    if len(links) >= links_n:
                        break
            except Exception:
                pass
            return {"ok": True, "url": final, "status": resp.status if resp else None, "title": title,
                    "text": tidy_text(text, max_chars), "links": links}
        finally:
            ctx.close()

    async def fetch(self, url: str, max_chars: int, links_n: int) -> dict:
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, self._fetch_sync, url, max_chars, links_n)

    def close(self):
        try:
            if self._browser: self._browser.close()
            if self._pw: self._pw.stop()
        except Exception:
            pass


# ─────────────────────────── HTTP ───────────────────────────
def load_allow_hosts() -> set[str]:
    try:
        import psycopg2
        c = psycopg2.connect(OPS_DSN, connect_timeout=4); cur = c.cursor()
        cur.execute("SELECT value FROM service_config WHERE service='browser' AND key='allow_hosts'")
        r = cur.fetchone(); c.close()
        v = r[0] if r else None
        v = json.loads(v) if isinstance(v, str) else v
        return set(v) if isinstance(v, list) else set()
    except Exception:
        return set()


async def serve():
    from aiohttp import web
    browser = Browser()
    state = {"allow": load_allow_hosts(), "allow_ts": time.time(), "fetches": 0, "refused": 0, "errors": 0, "started": time.time()}

    def allow():
        if time.time() - state["allow_ts"] > 300:
            state["allow"] = load_allow_hosts(); state["allow_ts"] = time.time()
        return state["allow"]

    async def do_fetch(url, max_chars, links_n):
        ok, why = validate_url(url, allow())
        if not ok:
            state["refused"] += 1
            log.info(f"REFUSED {url!r}: {why}")
            return web.json_response({"ok": False, "error": why}, status=400)
        t0 = time.time()
        try:
            res = await asyncio.wait_for(browser.fetch(url, max_chars, links_n), timeout=NAV_TIMEOUT_MS / 1000 + 20)
        except Exception as e:
            state["errors"] += 1
            log.info(f"ERROR {url!r}: {e}")
            return web.json_response({"ok": False, "error": str(e)[:200]}, status=502)
        verdict = "clean"
        if nova_untrusted is not None:
            fenced, verdict = nova_untrusted.gate(res["text"], "web page")
            res["text"] = fenced if fenced is not None else ""
            if verdict == "hostile":
                res["error"] = "page content dropped: prompt-injection pattern"
        res["verdict"] = verdict; res["elapsed_s"] = round(time.time() - t0, 1)
        state["fetches"] += 1
        log.info(f"FETCH {url!r} -> {res.get('status')} {len(res['text'])}ch {verdict} {res['elapsed_s']}s")
        return web.json_response(res)

    async def fetch_get(request):
        q = request.query
        return await do_fetch(q.get("url", ""), clamp(q.get("max_chars"), 500, MAX_CHARS_CAP, DEFAULT_CHARS), clamp(q.get("links"), 0, 200, 30))

    async def fetch_post(request):
        try:
            b = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        return await do_fetch(b.get("url", ""), clamp(b.get("max_chars"), 500, MAX_CHARS_CAP, DEFAULT_CHARS), clamp(b.get("links"), 0, 200, 30))

    async def health(request):
        return web.json_response({"ok": True, "service": "nova-browser", "uptime_s": int(time.time() - state["started"]),
                                  "fetches": state["fetches"], "refused": state["refused"], "errors": state["errors"],
                                  "chromium": browser._browser is not None})

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/fetch", fetch_get)
    app.router.add_post("/fetch", fetch_post)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"nova-browser listening on 0.0.0.0:{PORT}")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        browser.close()


def selftest() -> int:
    assert validate_url("https://www.gutenberg.org/ebooks/1")[0]
    assert not validate_url("http://127.0.0.1:18792/health")[0]
    assert not validate_url("http://192.168.1.2:3000/")[0]
    assert validate_url("http://192.168.1.2:3000/", {"192.168.1.2"})[0]
    assert not validate_url("ftp://example.com/x")[0]
    assert not validate_url("https://user:pw@example.com/")[0]
    assert not validate_url("http://localhost/")[0]
    assert not validate_url("http://nova.local/")[0]
    assert not validate_url("http://169.254.169.254/latest/meta-data")[0]
    assert clamp("abc", 1, 10, 5) == 5 and clamp("99", 1, 10, 5) == 10
    assert tidy_text("a\n\n\n\nb  \n", 100) == "a\n\nb"
    assert tidy_text("x" * 50, 10) == "x" * 10
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--once" in sys.argv:          # quick manual check: python3 nova_browser_service.py --once <url>
        b = Browser()
        print(json.dumps(b._fetch_sync(sys.argv[sys.argv.index("--once") + 1], 3000, 10), indent=1)[:3000]); b.close(); sys.exit(0)
    asyncio.run(serve())
