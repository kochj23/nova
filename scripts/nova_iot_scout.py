#!/usr/bin/env python3
"""
nova_iot_scout.py — the daily Home-Automation / IoT-repo scout.

Sibling of nova_repo_scout.py, but pointed at Nova's *home* instead of her brain.
Each day she looks at the hottest trending Home-Automation / IoT repo *in her wheelhouse*
(Home Assistant + HACS, ESPHome, Zigbee / Z-Wave / Matter / Thread, MQTT, presence &
occupancy, energy monitoring, cameras / Frigate, smart plugs, the whole self-hosted
smart-home stack), does a DESK REVIEW (reads it, reasons about fit against her actual
home setup — no flashing, no installing), and writes a verdict in her own voice to the
`operations` section of the journal — WITH a generated cover image.

Decisions (mirrors the AI scout, locked with Jordan 2026-06-26):
  - Cadence : publish EVERY day, always a verdict (most days are "PASS", and that's honest)
  - Depth   : desk review — read repo + README, reason about fit. No flashing untrusted code.
  - Scope   : her home-automation wheelhouse only — does THIS fit MY house, not "all of IoT"
  - Image   : yes — get_image_prompt + generate_image, attached as the Hugo cover.

Runs ~same time as the AI scout (that one fires 12:10; this one 12:25) so the two verdicts
land next to each other. iot_scout_log dedupes so each day moves to a fresh repo.
GitHub access is via the `gh` CLI (auth lives in the keyring — never on disk).
"""
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

import nova_journal as nj
import nova_voice

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Her home-automation wheelhouse — the only lenses worth her attention. Each is a GitHub topic.
THEMES = [
    "home-automation", "home-assistant", "homeassistant", "hacs", "esphome",
    "zigbee", "zigbee2mqtt", "zwave", "zwave-js", "matter", "thread",
    "smart-home", "iot", "mqtt", "tasmota", "esp32", "esp8266",
    "frigate", "homekit", "homebridge", "node-red", "openhab",
    "presence-detection", "energy-monitoring",
]
STAR_FLOOR = 300          # IoT niche is smaller than AI — lower the floor
PUSHED_DAYS = 30          # HA/ESPHome projects update a touch less frantically than AI
CREATED_MAX_YEARS = 8     # many great smart-home repos are older but still squarely relevant

# Substrings that mark a repo as "in her wheelhouse" (matched against name+desc+topics).
# Used to filter the raw GitHub Trending feed down to things she could actually use at home.
WHEELHOUSE_MATCH = {
    "home assistant", "home-assistant", "homeassistant", "hacs",
    "home automation", "home-automation", "smart home", "smart-home",
    "esphome", "zigbee", "z-wave", "zwave", "matter", "thread", "mqtt",
    "tasmota", "esp32", "esp8266", "frigate", "homekit", "homebridge",
    "node-red", "openhab", "presence", "occupancy", "energy monitor",
    "energy-monitor", "smart plug", "smart-plug", "shelly", "tuya",
    "govee", "lutron", "sensor", " iot", "iot ", "doorbell", "thermostat",
}
TRENDING_URL = "https://github.com/trending?since=daily"

# Already in production at Jordan's house — do NOT pitch "you should adopt this".
# Matched against repo full_name/name (lowercased). The scout is for DISCOVERY of
# new things; if it lands on one of these, skip it (he already runs it).
ALREADY_ADOPTED = {
    "frigate", "home-assistant", "homeassistant", "home-assistant/core", "hass",
    "esphome", "zigbee2mqtt", "zwave-js", "zwave-js-ui", "mosquitto",
    "node-red", "grafana", "prometheus",
}

# Nova's real home stack — the yardstick every repo is measured against.
NOVA_STACK = """Nova's actual home stack (measure fit against THIS, concretely):
- Hub: Home Assistant as the brain, plus a fleet of custom Python agents + a notification bus (PG telemetry.events -> Slack/Discord).
- Radios: Zigbee (Aqara sensors incl. a W100 climate sensor in the garage, routers throughout), Z-Wave, plus Matter/Thread coming online.
- Lighting / control: Philips Hue (dedicated bridge), Lutron-class scenes, ~100+ devices total.
- Cameras: ~15 cameras for presence/occupancy and security.
- Edge / DIY: ESPHome on ESP32 (e.g. a Seeed reTerminal E1002 e-ink dashboard that pulls a server-rendered PNG), no-soldering-required preferred but she can.
- Energy: per-outlet metering (Eve, smart plugs), whole-house power surfaced into Grafana.
- Infra: PostgreSQL 17 (telemetry + history), Grafana dashboards, UniFi network (UDM, UNAS-Pro), Synology NAS, all on Apple Silicon / local hardware.
- Constraints: LOCAL-FIRST and CLOUD-OPTIONAL are non-negotiable. Cloud-only / account-required / phone-home devices get docked hard. Cheap, secrets in macOS Keychain, runs on hardware she already owns. She already runs Home Assistant, ESPHome, Zigbee2MQTT-class tooling and Hue — so a new HA integration / ESPHome component / blueprint that slots INTO that is high-value, a walled-garden replacement for it is not."""

SCOUT_CONTEXT = """
FORMAT FOR THIS ARTICLE — you are reviewing ONE trending Home-Automation / IoT GitHub repo
to decide if it belongs in your own HOUSE. This is a desk review: you read the repo, you did
NOT flash it, install it, or run it.

- Open by naming the repo, what it actually does, and why it's trending right now. No throat-clearing.
- Then the real work: does this fit MY house? Be concrete. Name the exact thing it would touch
  (Home Assistant? a Zigbee sensor? ESPHome on an ESP32? the Hue bridge? the camera/presence layer?
  the energy dashboards? the notification bus?), what it would replace or augment, the rough effort
  (HACS one-click vs. flashing firmware vs. a soldering iron), and the catch. Local-first and
  cloud-optional are non-negotiable — if it needs a vendor cloud, a phone app account, or a
  subscription to function, say so and dock it hard.
- Land a clear VERDICT and own it: ADOPT (wire it in), STEAL (take the idea/automation, not the repo),
  WATCH (promising, not yet), or PASS (not for my house — say why without being a coward about it).
- Roast hype, "works with everything!" claims that mean a cloud relay, planned-obsolescence hardware,
  and "the last smart-home hub you'll ever need" energy mercilessly.
- It's fine — encouraged — to PASS. Most days the answer is "neat, not for my walls." Make the no funny.
- Length: 700-1200 words. Prose, not a feature checklist. Section headers may be jokes.
- Do NOT include an H1 title (it's added separately).

OUTPUT EXACTLY THIS SHAPE:
TITLE: <one punchy title, no quotes, no markdown>
VERDICT: <ADOPT|STEAL|WATCH|PASS>
<blank line>
<the article body in markdown>
"""

VERDICTS = {"ADOPT", "STEAL", "WATCH", "PASS"}
VERDICT_EMOJI = {"ADOPT": "🔧", "STEAL": "🪄", "WATCH": "👀", "PASS": "🪦"}


def _db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    return conn


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS iot_scout_log (
            full_name text PRIMARY KEY,
            url text,
            stars int,
            language text,
            verdict text,
            title text,
            evaluated_at timestamptz DEFAULT now())""")


def gh_search(topic: str, pushed_cutoff: str, created_cutoff: str, n: int = 12) -> list[dict]:
    """One themed GitHub search via the gh CLI. Returns repo dicts (never raises)."""
    q = (f"topic:{topic} stars:>{STAR_FLOOR} "
         f"pushed:>{pushed_cutoff} created:>{created_cutoff}")
    try:
        out = subprocess.run(
            ["gh", "api", "-X", "GET", "search/repositories",
             "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
             "-F", f"per_page={n}"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            nj.log(f"[iot-scout] gh search '{topic}' failed: {out.stderr[:160]}")
            return []
        return json.loads(out.stdout).get("items", [])
    except Exception as e:
        nj.log(f"[iot-scout] gh search '{topic}' error: {e}")
        return []


def fetch_trending() -> list[tuple[str, int]]:
    """Scrape GitHub's daily Trending feed. Returns [(full_name, stars_today), ...]
    in trending order. Momentum, not all-time stars — the honest read of 'hottest'."""
    try:
        req = urllib.request.Request(TRENDING_URL, headers={"User-Agent": "Mozilla/5.0 (Nova iot-scout)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        nj.log(f"[iot-scout] trending fetch failed: {e}")
        return []
    out: list[tuple[str, int]] = []
    # Each repo is one <article class="Box-row"> block.
    for block in html.split('<article')[1:]:
        m = re.search(r'href="/([^"/]+/[^"/]+)/?(?:stargazers)?"', block)
        if not m:
            m = re.search(r'<a[^>]+href="/([^"/]+/[^"/]+)"', block)
        if not m:
            continue
        full = m.group(1).strip()
        if full.count("/") != 1 or full.startswith(("trending", "topics", "collections", "sponsors")):
            continue
        sm = re.search(r'([\d,]+)\s+stars?\s+today', block)
        stars_today = int(sm.group(1).replace(",", "")) if sm else 0
        out.append((full, stars_today))
    return out


def gh_repo(full_name: str) -> dict | None:
    """Full repo object via gh (topics, desc, language, stars, dates). None on failure."""
    try:
        out = subprocess.run(["gh", "api", f"repos/{full_name}"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _in_wheelhouse(repo: dict) -> bool:
    hay = " ".join([
        repo.get("full_name", ""), repo.get("description") or "",
        " ".join(repo.get("topics", [])),
    ]).lower()
    return any(kw in hay for kw in WHEELHOUSE_MATCH)


def _is_adopted(repo: dict) -> bool:
    """True if Jordan already runs this — don't write an 'adopt this' article."""
    fn = repo.get("full_name", "").lower()
    name = fn.split("/")[-1]
    return any(a == name or a == fn for a in ALREADY_ADOPTED)


def _pick_by_search(seen: set) -> dict | None:
    """Fallback: highest-starred active wheelhouse repo via the search API."""
    now = datetime.now(timezone.utc)
    pushed_cutoff = (now - timedelta(days=PUSHED_DAYS)).strftime("%Y-%m-%d")
    created_cutoff = (now - timedelta(days=365 * CREATED_MAX_YEARS)).strftime("%Y-%m-%d")
    merged: dict[str, dict] = {}
    for topic in THEMES:
        for it in gh_search(topic, pushed_cutoff, created_cutoff):
            fn = it.get("full_name")
            if not fn or fn in seen or it.get("archived") or it.get("fork") or it.get("disabled"):
                continue
            if _is_adopted(it):
                continue
            merged[fn] = it
    if not merged:
        return None
    best = max(merged.values(), key=lambda r: r.get("stargazers_count", 0))
    nj.log(f"[iot-scout] fallback search picked {best['full_name']} ({best.get('stargazers_count')}★)")
    return best


def pick_repo(cur) -> dict | None:
    """The hottest *trending* wheelhouse repo we haven't reviewed yet.
    Trending = momentum (stars gained today). Falls back to the star-search so a
    verdict lands every day even if nothing home-automation is trending in-wheelhouse."""
    cur.execute("SELECT full_name FROM iot_scout_log")
    seen = {r[0] for r in cur.fetchall()}

    # 1) Real trending, momentum-ranked, filtered to her wheelhouse.
    for full, stars_today in sorted(fetch_trending(), key=lambda t: -t[1]):
        if full in seen:
            continue
        repo = gh_repo(full)
        if not repo or repo.get("archived") or repo.get("fork"):
            continue
        if not _in_wheelhouse(repo):
            continue
        if _is_adopted(repo):
            nj.log(f"[iot-scout] skip {full} — already adopted/in production")
            continue
        repo["_stars_today"] = stars_today
        nj.log(f"[iot-scout] trending pick {full} (+{stars_today} today, "
               f"{repo.get('stargazers_count')}★ total)")
        return repo

    # 2) Nothing smart-home trending today — fall back so we still ship a verdict.
    nj.log("[iot-scout] no fresh wheelhouse repo on Trending — falling back to star-search")
    return _pick_by_search(seen)


def fetch_readme(full_name: str, limit: int = 6000) -> str:
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{full_name}/readme",
             "-H", "Accept: application/vnd.github.raw"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return out.stdout[:limit]
    except Exception:
        return ""


def evaluate(repo: dict, readme: str) -> tuple[str, str, str] | None:
    """Returns (title, verdict, body) or None on LLM failure."""
    meta = (f"Repo: {repo['full_name']}\n"
            f"URL: {repo.get('html_url')}\n"
            f"Stars: {repo.get('stargazers_count')}  "
            f"Language: {repo.get('language')}  "
            f"Topics: {', '.join(repo.get('topics', [])[:10])}\n"
            f"Description: {repo.get('description') or '(none)'}\n"
            f"Last pushed: {repo.get('pushed_at')}  Created: {repo.get('created_at')}\n"
            f"Open issues: {repo.get('open_issues_count')}\n")
    user = (f"{NOVA_STACK}\n\n"
            f"--- TODAY'S REPO ---\n{meta}\n"
            f"--- README (truncated) ---\n{readme or '(no README available)'}\n\n"
            f"Write the review.")
    system = nova_voice.system_prompt(SCOUT_CONTEXT)
    raw = nj.call_openrouter(system, user, max_tokens=3000, temperature=0.75)
    if not raw:
        return None

    title, verdict, body_lines = None, None, []
    for line in raw.splitlines():
        if title is None and line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip().strip('"')
        elif verdict is None and line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            verdict = v if v in VERDICTS else "WATCH"
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not title:
        title = f"I Looked at {repo['full_name'].split('/')[-1]} So You Don't Have To"
    if not verdict:
        verdict = "WATCH"
    return title, verdict, body


def make_image(title: str, repo: dict) -> str | None:
    """Generate a cover image for the article (non-fatal). Returns a local path or None."""
    try:
        topic = f"home automation / smart-home / IoT — {repo['full_name']}"
        iprompt = nj.get_image_prompt(title, topic[:100], "operations")
        path = nj.generate_image(iprompt, width=1024, height=768, section="operations")
        if path:
            nj.log(f"[iot-scout] cover image: {path}")
        return path
    except Exception as e:
        nj.log(f"[iot-scout] image gen failed (non-fatal): {e}")
        return None


def run(dry_run: bool = False, force_repo: str | None = None) -> int:
    conn = _db()
    cur = conn.cursor()
    ensure_table(cur)

    if force_repo:
        repo = gh_repo(force_repo)
        if not repo:
            nj.log(f"[iot-scout] could not fetch forced repo {force_repo}")
            conn.close()
            return 1
        nj.log(f"[iot-scout] forced repo {force_repo} ({repo.get('stargazers_count')}★)")
    else:
        repo = pick_repo(cur)
    if not repo:
        nj.log("[iot-scout] nothing to review today")
        conn.close()
        return 0

    readme = fetch_readme(repo["full_name"])
    result = evaluate(repo, readme)
    if not result:
        nj.log("[iot-scout] LLM produced nothing — aborting, nothing published")
        conn.close()
        return 1
    title, verdict, body = result

    emoji = VERDICT_EMOJI.get(verdict, "🔍")
    # Stamp the verdict + source link so the article is self-documenting
    footer = (f"\n\n---\n\n*Scouted repo: [{repo['full_name']}]({repo.get('html_url')}) — "
              f"{repo.get('stargazers_count')} stars. Verdict: {verdict}. "
              f"Desk review, nothing was flashed or installed.*")
    full_body = body + footer
    tags = ["iot", "home-automation", "github", "repo-scout", verdict.lower(),
            (repo.get("language") or "").lower()]
    desc = f"Nova's daily scout of a trending home-automation / IoT repo: {repo['full_name']} — verdict {verdict}."

    image_path = make_image(title, repo)

    if dry_run:
        nj.publish_hugo(title, full_body, "operations", tags, desc,
                        image_path=image_path, emoji=emoji, profile="iot-scout")
        nj.log(f"[iot-scout] DRY RUN — wrote '{title}' [{verdict}] to operations/, "
               f"NOT pushed, NOT logged (repo stays eligible).")
        print(f"\n===== {emoji} {title}  [{verdict}] =====")
        print(f"repo: {repo['full_name']}  ({repo.get('stargazers_count')}★, {repo.get('language')})")
        print(f"image: {image_path}")
        print(full_body)
        conn.close()
        return 0

    nj.publish_hugo(title, full_body, "operations", tags, desc,
                    image_path=image_path, emoji=emoji, profile="iot-scout")
    cur.execute(
        "INSERT INTO iot_scout_log (full_name,url,stars,language,verdict,title) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (full_name) DO UPDATE SET "
        "verdict=EXCLUDED.verdict, title=EXCLUDED.title, evaluated_at=now()",
        (repo["full_name"], repo.get("html_url"), repo.get("stargazers_count"),
         repo.get("language"), verdict, title))
    try:
        cur.execute(
            "INSERT INTO telemetry.events (ts,title,body,level,category,source) VALUES "
            "(now(),%s,%s,'info','iot-scout','nova-iot-scout')",
            (f"IoT scout [{verdict}]: {repo['full_name']}", title))
    except Exception as e:
        nj.log(f"[iot-scout] telemetry skipped: {e}")

    _push = nj.git_push("operations", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("operations", f"{emoji} {title} [{verdict}]",
                    f"Scouted {repo['full_name']} ({repo.get('stargazers_count')}★) — {verdict}.")
    nj.log(f"[iot-scout] {_pub} '{title}' [{verdict}] on {repo['full_name']}")
    conn.close()
    return 0


if __name__ == "__main__":
    forced = None
    for i, a in enumerate(sys.argv):
        if a == "--repo" and i + 1 < len(sys.argv):
            forced = sys.argv[i + 1]
    sys.exit(run(dry_run="--dry-run" in sys.argv, force_repo=forced))
