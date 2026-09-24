#!/usr/bin/env python3
"""nova_openrouter_watch.py — daily OpenRouter credit-balance watchdog.

2026-09-14: the OpenRouter account silently hit $2160.02/$2160.00 and image gen
failed for a day with a bare HTTP 402 in the logs (queue #2620). This checks
GET /api/v1/credits once a day and raises a WARNING via nova_notify when the
remaining balance (total_credits - total_usage) drops below THRESHOLD_USD.

Uses the same key as every other OpenRouter caller (nova_config.openrouter_api_key,
Keychain 'nova-openrouter-api-key'). Dedup: one alert per 24h while low.

    nova_openrouter_watch.py            # check + alert if low
    nova_openrouter_watch.py --dry-run  # print balance, never alert
"""
import json
import sys
import urllib.error
import urllib.request

import nova_config

CREDITS_URL = "https://openrouter.ai/api/v1/credits"
THRESHOLD_USD = 5.0
SOURCE = "nova-openrouter-watch"


def log(msg):
    print(f"[openrouter-watch] {msg}", flush=True)


def fetch_balance():
    key = nova_config.openrouter_api_key()
    if not key:
        raise RuntimeError("no OpenRouter key available")
    req = urllib.request.Request(CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read()).get("data") or {}
    credits = float(d.get("total_credits") or 0)
    usage = float(d.get("total_usage") or 0)
    return credits, usage, credits - usage


def main():
    dry = "--dry-run" in sys.argv
    try:
        credits, usage, remaining = fetch_balance()
    except urllib.error.HTTPError as e:
        log(f"credits endpoint HTTP {e.code}")
        return 1
    except Exception as e:
        log(f"credits check failed: {e}")
        return 1
    log(f"total_credits=${credits:.2f} total_usage=${usage:.2f} remaining=${remaining:.2f}")
    if dry or remaining >= THRESHOLD_USD:
        return 0
    from nova_notify import notify
    notify(
        f"OpenRouter credit low: ${remaining:.2f} remaining",
        f"OpenRouter balance is ${remaining:.2f} (credits ${credits:.2f}, usage ${usage:.2f}) — "
        f"below the ${THRESHOLD_USD:.0f} floor. Image gen and cloud LLM calls will start failing "
        f"with HTTP 402. Top up at https://openrouter.ai/settings/credits",
        level="warning", category="finance", source=SOURCE,
        dedup_key="openrouter-low-credit", meta={"dedup_window_s": 86400},
    )
    log("low-balance alert sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
