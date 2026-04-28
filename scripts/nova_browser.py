#!/usr/bin/env python3
"""
nova_browser.py — Full Playwright browser automation for Nova.

Gives Nova real eyes on the web beyond basic HTTP fetches:
  - JavaScript-rendered page content (SPAs, dynamic sites)
  - Full page screenshots
  - Element-targeted screenshots
  - Form interaction (fill, click, submit)
  - Content extraction with CSS/XPath selectors
  - PDF generation from web pages
  - Cookie/session management for authenticated browsing
  - Multi-tab support
  - Network request interception
  - Page performance metrics

All browsing is headless by default. Screenshots and PDFs are saved
to workspace for Slack posting or analysis.

PRIVACY: Browser state (cookies, local storage) is ephemeral by default.
Persistent sessions can be saved to a profile directory for sites that
need authentication.

Usage:
  # Fetch rendered page content (JS executed)
  python3 nova_browser.py --fetch "https://example.com"

  # Full page screenshot
  python3 nova_browser.py --screenshot "https://example.com"

  # Screenshot a specific element
  python3 nova_browser.py --screenshot "https://example.com" --selector ".main-content"

  # Extract text from specific elements
  python3 nova_browser.py --extract "https://example.com" --selector "h1, h2, p"

  # Fill a form and submit
  python3 nova_browser.py --interact "https://example.com/login" --fill '{"#email":"test@test.com","#pass":"xxx"}' --click "#submit"

  # Generate PDF
  python3 nova_browser.py --pdf "https://example.com"

  # Monitor a page for changes
  python3 nova_browser.py --monitor "https://example.com" --selector ".price" --interval 300

  # Get page performance metrics
  python3 nova_browser.py --perf "https://example.com"

Written by Jordan Koch.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

NOW = datetime.now()
TODAY = date.today().isoformat()
WORKSPACE = Path.home() / ".openclaw/workspace"
BROWSER_DIR = WORKSPACE / "browser"
SCREENSHOTS_DIR = BROWSER_DIR / "screenshots"
PDFS_DIR = BROWSER_DIR / "pdfs"
PROFILES_DIR = BROWSER_DIR / "profiles"
MONITOR_STATE = BROWSER_DIR / "monitor_state.json"

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_NOTIFY
VECTOR_URL = nova_config.VECTOR_URL

# Default browser config
DEFAULT_TIMEOUT = 30000  # 30 seconds
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def log(msg):
    print(f"[nova_browser {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_dirs():
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    PDFS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def slack_upload(filepath, comment="", channel=None):
    import subprocess
    try:
        cmd = [
            "curl", "-s", "-F", f"file=@{filepath}",
            "-F", f"channels={channel or SLACK_CHAN}",
            "-F", f"initial_comment={comment}",
            "-H", f"Authorization: Bearer {SLACK_TOKEN}",
            "https://slack.com/api/files.upload"
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception as e:
        log(f"Slack upload error: {e}")


def vector_remember(text, metadata=None):
    import urllib.request
    try:
        payload = json.dumps({
            "text": text, "source": "browser", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Core browser operations ──────────────────────────────────────────────────

async def create_browser(headless=True, profile=None):
    """Create a Playwright browser instance."""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()

    launch_args = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }

    if profile:
        # Persistent context with saved cookies/storage
        profile_dir = PROFILES_DIR / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            **launch_args,
            viewport=DEFAULT_VIEWPORT,
            user_agent=USER_AGENT,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        return pw, context, page

    browser = await pw.chromium.launch(**launch_args)
    context = await browser.new_context(
        viewport=DEFAULT_VIEWPORT,
        user_agent=USER_AGENT,
    )
    page = await context.new_page()
    return pw, context, page


async def close_browser(pw, context):
    """Clean shutdown."""
    try:
        await context.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass


# ── Fetch (rendered content) ─────────────────────────────────────────────────

async def fetch_rendered(url, wait_for=None, timeout=DEFAULT_TIMEOUT):
    """Fetch a URL with full JS rendering. Returns page text content."""
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        if wait_for:
            await page.wait_for_selector(wait_for, timeout=10000)

        # Get the full rendered text
        content = await page.content()
        text = await page.inner_text("body")
        title = await page.title()

        return {
            "url": url,
            "title": title,
            "text": text[:10000],
            "html_length": len(content),
            "text_length": len(text),
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Screenshot ───────────────────────────────────────────────────────────────

async def take_screenshot(url, selector=None, full_page=True,
                          output=None, timeout=DEFAULT_TIMEOUT):
    """Take a screenshot of a URL or specific element."""
    ensure_dirs()
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")

        if output:
            path = Path(output)
        else:
            slug = hashlib.md5(url.encode()).hexdigest()[:8]
            ts = NOW.strftime("%Y%m%d_%H%M%S")
            path = SCREENSHOTS_DIR / f"shot_{slug}_{ts}.png"

        if selector:
            element = await page.query_selector(selector)
            if element:
                await element.screenshot(path=str(path))
            else:
                return {"error": f"Selector '{selector}' not found"}
        else:
            await page.screenshot(path=str(path), full_page=full_page)

        title = await page.title()
        size = path.stat().st_size // 1024

        return {
            "url": url,
            "title": title,
            "path": str(path),
            "size_kb": size,
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Content extraction ───────────────────────────────────────────────────────

async def extract_content(url, selector, attribute=None, timeout=DEFAULT_TIMEOUT):
    """Extract content from specific elements on a page."""
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        elements = await page.query_selector_all(selector)

        results = []
        for el in elements:
            if attribute:
                value = await el.get_attribute(attribute)
            else:
                value = await el.inner_text()
            if value and value.strip():
                results.append(value.strip()[:500])

        return {
            "url": url,
            "selector": selector,
            "count": len(results),
            "results": results[:50],
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Form interaction ─────────────────────────────────────────────────────────

async def interact(url, fill_map=None, click_selector=None, wait_after=2000,
                   screenshot_after=True, timeout=DEFAULT_TIMEOUT):
    """Fill forms and click buttons on a page."""
    ensure_dirs()
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")

        if fill_map:
            for selector, value in fill_map.items():
                await page.fill(selector, value)

        if click_selector:
            await page.click(click_selector)
            await page.wait_for_timeout(wait_after)

        result = {
            "url": page.url,
            "title": await page.title(),
            "filled": list(fill_map.keys()) if fill_map else [],
            "clicked": click_selector,
        }

        if screenshot_after:
            slug = hashlib.md5(url.encode()).hexdigest()[:8]
            ts = NOW.strftime("%Y%m%d_%H%M%S")
            shot_path = SCREENSHOTS_DIR / f"interact_{slug}_{ts}.png"
            await page.screenshot(path=str(shot_path))
            result["screenshot"] = str(shot_path)

        return result
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── PDF generation ───────────────────────────────────────────────────────────

async def generate_pdf(url, output=None, timeout=DEFAULT_TIMEOUT):
    """Generate a PDF from a web page."""
    ensure_dirs()
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")

        if output:
            path = Path(output)
        else:
            slug = hashlib.md5(url.encode()).hexdigest()[:8]
            ts = NOW.strftime("%Y%m%d_%H%M%S")
            path = PDFS_DIR / f"page_{slug}_{ts}.pdf"

        await page.pdf(path=str(path), format="A4", print_background=True)
        title = await page.title()

        return {
            "url": url,
            "title": title,
            "path": str(path),
            "size_kb": path.stat().st_size // 1024,
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Page monitoring ──────────────────────────────────────────────────────────

async def monitor_page(url, selector, label=None):
    """Check a page element for changes since last check."""
    pw, ctx, page = await create_browser()
    try:
        await page.goto(url, timeout=DEFAULT_TIMEOUT, wait_until="networkidle")
        elements = await page.query_selector_all(selector)
        current = []
        for el in elements:
            text = await el.inner_text()
            if text and text.strip():
                current.append(text.strip()[:200])

        current_hash = hashlib.md5(json.dumps(current).encode()).hexdigest()

        # Load previous state
        state = {}
        if MONITOR_STATE.exists():
            try:
                state = json.loads(MONITOR_STATE.read_text())
            except Exception:
                pass

        key = f"{url}|{selector}"
        prev = state.get(key, {})
        prev_hash = prev.get("hash", "")
        changed = prev_hash != "" and prev_hash != current_hash

        state[key] = {
            "hash": current_hash,
            "values": current[:10],
            "checked_at": NOW.isoformat(),
            "label": label or url[:60],
        }
        MONITOR_STATE.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_STATE.write_text(json.dumps(state, indent=2))

        return {
            "url": url,
            "selector": selector,
            "changed": changed,
            "values": current[:10],
            "previous": prev.get("values", [])[:5] if changed else [],
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Performance metrics ──────────────────────────────────────────────────────

async def page_performance(url, timeout=DEFAULT_TIMEOUT):
    """Get page load performance metrics."""
    pw, ctx, page = await create_browser()
    try:
        start = time.time()
        response = await page.goto(url, timeout=timeout, wait_until="networkidle")
        load_time = time.time() - start

        # Get performance timing from the browser
        timing = await page.evaluate("""() => {
            const t = performance.timing;
            return {
                dns: t.domainLookupEnd - t.domainLookupStart,
                tcp: t.connectEnd - t.connectStart,
                ttfb: t.responseStart - t.requestStart,
                download: t.responseEnd - t.responseStart,
                dom_interactive: t.domInteractive - t.navigationStart,
                dom_complete: t.domComplete - t.navigationStart,
                load: t.loadEventEnd - t.navigationStart,
            };
        }""")

        # Count resources
        resources = await page.evaluate("""() => {
            const entries = performance.getEntriesByType('resource');
            const types = {};
            entries.forEach(e => {
                const t = e.initiatorType || 'other';
                types[t] = (types[t] || 0) + 1;
            });
            return {count: entries.length, types: types, totalSize: entries.reduce((s, e) => s + (e.transferSize || 0), 0)};
        }""")

        return {
            "url": url,
            "status": response.status if response else None,
            "load_time_s": round(load_time, 2),
            "timing_ms": timing,
            "resources": resources,
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
    finally:
        await close_browser(pw, ctx)


# ── Multi-page scrape ────────────────────────────────────────────────────────

async def scrape_links(url, link_selector="a", max_depth=1, max_pages=20):
    """Scrape a page and optionally follow links."""
    pw, ctx, page = await create_browser()
    pages_scraped = []

    try:
        await page.goto(url, timeout=DEFAULT_TIMEOUT, wait_until="networkidle")
        title = await page.title()
        text = await page.inner_text("body")
        pages_scraped.append({"url": url, "title": title, "text": text[:2000]})

        if max_depth > 0:
            links = await page.query_selector_all(link_selector)
            urls_found = set()
            for link in links[:max_pages]:
                href = await link.get_attribute("href")
                if href and href.startswith("http") and href not in urls_found:
                    urls_found.add(href)

            for link_url in list(urls_found)[:max_pages - 1]:
                try:
                    await page.goto(link_url, timeout=15000, wait_until="domcontentloaded")
                    t = await page.title()
                    txt = await page.inner_text("body")
                    pages_scraped.append({"url": link_url, "title": t, "text": txt[:2000]})
                except Exception:
                    continue

        return {"pages": pages_scraped, "count": len(pages_scraped)}
    except Exception as e:
        return {"error": str(e), "pages": pages_scraped}
    finally:
        await close_browser(pw, ctx)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine."""
    return asyncio.run(coro)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Browser Automation")
    parser.add_argument("--fetch", type=str, help="Fetch rendered page content")
    parser.add_argument("--screenshot", type=str, help="Take screenshot of URL")
    parser.add_argument("--extract", type=str, help="Extract content from URL")
    parser.add_argument("--interact", type=str, help="Interact with a page")
    parser.add_argument("--pdf", type=str, help="Generate PDF from URL")
    parser.add_argument("--monitor", type=str, help="Monitor page element for changes")
    parser.add_argument("--perf", type=str, help="Page performance metrics")
    parser.add_argument("--scrape", type=str, help="Scrape page and follow links")

    parser.add_argument("--selector", type=str, help="CSS selector for extract/screenshot/monitor")
    parser.add_argument("--fill", type=str, help="JSON map of selectors to values for --interact")
    parser.add_argument("--click", type=str, help="CSS selector to click for --interact")
    parser.add_argument("--output", type=str, help="Output file path")
    parser.add_argument("--full-page", action="store_true", default=True, help="Full page screenshot")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--post-slack", action="store_true", help="Post result to Slack")
    parser.add_argument("--timeout", type=int, default=30, help="Page load timeout in seconds (default: 30)")
    args = parser.parse_args()

    result = None
    timeout = args.timeout

    if args.fetch:
        result = run_async(fetch_rendered(args.fetch, wait_for=args.selector, timeout=timeout))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"Title: {result['title']}")
                print(f"Text length: {result['text_length']} chars")
                print(f"---\n{result['text'][:3000]}")

    elif args.screenshot:
        result = run_async(take_screenshot(args.screenshot, selector=args.selector,
                                           output=args.output, timeout=timeout, full_page=args.full_page))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"Screenshot: {result['path']} ({result['size_kb']}KB)")
                if args.post_slack:
                    slack_upload(result['path'], f"Screenshot: {result['title']}")

    elif args.extract:
        if not args.selector:
            print("Error: --extract requires --selector")
            sys.exit(1)
        result = run_async(extract_content(args.extract, args.selector, timeout=timeout))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"Found {result['count']} elements:")
                for i, r in enumerate(result['results'][:20]):
                    print(f"  [{i}] {r[:100]}")

    elif args.interact:
        fill_map = json.loads(args.fill) if args.fill else None
        result = run_async(interact(args.interact, fill_map=fill_map,
                                    click_selector=args.click, timeout=timeout))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"Page: {result['url']}")
                print(f"Title: {result['title']}")
                if result.get("screenshot"):
                    print(f"Screenshot: {result['screenshot']}")

    elif args.pdf:
        result = run_async(generate_pdf(args.pdf, output=args.output, timeout=timeout))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"PDF: {result['path']} ({result['size_kb']}KB)")

    elif args.monitor:
        if not args.selector:
            print("Error: --monitor requires --selector")
            sys.exit(1)
        result = run_async(monitor_page(args.monitor, args.selector))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            elif result['changed']:
                print(f"CHANGED!")
                print(f"Current: {result['values'][:3]}")
                print(f"Previous: {result['previous'][:3]}")
            else:
                print(f"No change. Values: {result['values'][:3]}")

    elif args.perf:
        result = run_async(page_performance(args.perf, timeout=timeout))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                t = result['timing_ms']
                r = result['resources']
                print(f"Performance: {result['url']}")
                print(f"  Status:        {result['status']}")
                print(f"  Total load:    {result['load_time_s']}s")
                print(f"  TTFB:          {t.get('ttfb', 0)}ms")
                print(f"  DOM ready:     {t.get('dom_interactive', 0)}ms")
                print(f"  DOM complete:  {t.get('dom_complete', 0)}ms")
                print(f"  Resources:     {r.get('count', 0)} ({r.get('totalSize', 0) // 1024}KB)")
                print(f"  Types:         {r.get('types', {})}")

    elif args.scrape:
        result = run_async(scrape_links(args.scrape, link_selector=args.selector or "a"))
        if not args.json:
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                for p in result.get("pages", []):
                    print(f"\n--- {p['title']} ---")
                    print(f"URL: {p['url']}")
                    print(f"{p['text'][:500]}")

    else:
        parser.print_help()
        sys.exit(0)

    if args.json and result:
        print(json.dumps(result, indent=2))
