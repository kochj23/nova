#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_channel_router.py — keep the *and Friends* channel lists in step with Jordan's YouTube subscriptions.

Jordan 2026-11-02: "I am subbing and unsubbing to channels all the damn time. You are going to need to tune that
more than once a month." (The subscription list had sat frozen at Oct 7 — a logged-out cookie jar — so ten new
cooking channels never reached Food and Friends.)

Every 4 h: refresh the subscription list (nova_yt_subs_audio.subscriptions -> service_config
yt_subs_audio/subscriptions), then for each channel subscribed SINCE THE BASELINE that no hand-written list
(nova_friends *_CHANNELS, nova_channel_pools) claims, read its six latest video titles and let the local model
put it in ONE section (or none). Routes live in service_config friends/auto_channels; nova_friends.main adds them
to the subject's channels at run time. Unsubscribed channels drop out of the auto routes. The first run only
records a baseline: channels Jordan already subscribed to and deliberately left out (Bacon 28 ...) stay out.

Usage: nova_channel_router.py [--dry-run]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

YTDLP = "/opt/homebrew/bin/yt-dlp"
MAX_PER_RUN = 25


def log(m):
    print(f"[channel-router {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def claimed() -> set[str]:
    """Normalised names every hand-written list already owns (plus the explicit exclusions)."""
    import nova_friends as nf
    import nova_channel_pools as cp
    names = set()
    for mod in (nf, cp):
        for k, v in vars(mod).items():
            if (k.endswith("_CHANNELS") or k.endswith("_EXCLUDED")) and isinstance(v, tuple):
                names |= {nf.norm(x) for x in v}
    return names


def titles(cid: str, n=6) -> list[str]:
    r = subprocess.run([YTDLP, "--no-warnings", "--flat-playlist", "--playlist-end", str(n), "--print", "%(title)s",
                        f"https://www.youtube.com/channel/{cid}/videos"], capture_output=True, text=True, timeout=120)
    return [t for t in r.stdout.splitlines() if t.strip()][:n]


def route(name: str, handle: str, recent: list[str]) -> str:
    """Section slug or 'none' from the channel name and its latest titles (local qwen3)."""
    import nova_friends as nf
    import nova_liked_music as lm            # reuses its local JSON LLM call
    menu = "\n".join(f"- {k}: {v['name']} — {v.get('beats', '')[:160]}" for k, v in nf.SUBJECTS.items())
    v = lm.llm_json(f"""Which ONE section of a personal magazine should a YouTube channel feed? Choose from:
{menu}
- none: it fits none of these clearly (vlogs, kids, gaming, news, comedy, etc.)
CHANNEL: {name} ({handle})
LATEST VIDEO TITLES:
""" + "\n".join(f"- {t}" for t in recent) + """
Answer JSON {"section": "<slug or none>", "why": "<one short reason>"}. Pick a section only if most titles clearly
belong to it.""", max_tokens=200, temperature=0.1)
    s = (v.get("section") or "none").strip().lower()
    return s if s in nf.SUBJECTS else "none"


def main(argv) -> int:
    import nova_friends as nf
    import nova_yt_subs_audio as ys
    import psycopg2
    dry = "--dry-run" in argv
    subs = ys.subscriptions()                        # also refreshes the stored list
    if not subs:
        log("no subscriptions readable — nothing to do"); return 1
    c = psycopg2.connect(ys.DSN); c.autocommit = True; cur = c.cursor()
    cur.execute("SELECT value FROM service_config WHERE service='friends' AND key='auto_channels'")
    row = cur.fetchone()
    state = row[0] if row else {}
    routes, seen = state.get("routes", {}), set(state.get("seen", []))
    now_ids = {s[0] for s in subs}
    if not seen:                                     # first run: baseline only
        state = {"routes": {}, "seen": sorted(now_ids), "baseline_at": datetime.now().isoformat(timespec="seconds")}
        log(f"baseline: {len(now_ids)} subscriptions recorded; only channels subscribed from now on are routed")
    else:
        gone = [cid for cid in routes if cid not in now_ids]
        for cid in gone:
            log(f"unsubscribed: {routes[cid]['name']} (was {routes[cid]['section']}) — dropped"); routes.pop(cid)
        own = claimed()
        fresh = [s for s in subs if s[0] not in seen]
        done = 0
        for cid, handle, name in fresh:
            seen.add(cid)
            if nf.norm(name) in own:
                continue
            if done >= MAX_PER_RUN:
                seen.discard(cid); continue          # next run
            sec = route(name, handle, titles(cid))
            done += 1
            log(f"new subscription {name!r} -> {sec}")
            if sec != "none":
                routes[cid] = {"name": name, "section": sec, "at": datetime.now().isoformat(timespec="seconds")}
        state = {**state, "routes": routes, "seen": sorted(seen & now_ids)}   # a re-subscribe is routed afresh
    if dry:
        print(json.dumps({k: v for k, v in state.items() if k != "seen"}, indent=1)); return 0
    cur.execute("INSERT INTO service_config (service, key, value) VALUES ('friends','auto_channels',%s) "
                "ON CONFLICT (service, key) DO UPDATE SET value=excluded.value, updated_at=now()", (json.dumps(state),))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
