#!/opt/homebrew/bin/python3
"""nova_build_map.py — builds the end-to-end map of Nova: subsystems, scheduled tasks, launch jobs, ingests,
feeds, publishers, safety gates and tests. Every count comes from the repo, the scheduler config, the launch
agents or the feed scripts at build time; nothing is typed in by hand except the diagram structure.

Writes Nova-Map.html (diagrams rendered in the browser) and Nova-Map.md (the same content as Mermaid fences).

Usage: nova_build_map.py [--out DIR] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import html
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent          # ~/.openclaw
SCRIPTS = ROOT / "scripts"
CONFIG = ROOT / "config" / "scheduler.yaml"
LAUNCH = Path.home() / "Library" / "LaunchAgents"
DAEMONS = Path("/Library/LaunchDaemons")
FEED_SCRIPTS = ["nova_gov_rss_ingest.py", "nova_reddit_rss_ingest.py", "nova_creator_feed.py"]
DEFAULT_OUT = Path.home() / "Desktop" / "Nova-Map"

CATEGORIES = [
    ("Security and network", r"secur|sentinel|rogue|prober|watchtower|cve|purple|canary|wifi|mesh|network|dns|unifi|"
                             r"identity|face|config_drift|cert|firewall|pihole|threat"),
    ("Home and sensors", r"homekit|hue|protect|house|presence|bodach|local_situation|meshtastic|nas|unas|hw_inventory|"
                         r"ha_metrics|energy|light|outlet|reachability|federal_hill|shine|sky|traffic|chp|weather|"
                         r"ble|lora|imessage|derry|buick|thermo|lock|tracker"),
    ("Media and ingest", r"ingest|_tv|tv_|yt|plex|livetv|media|floatplane|patreon|rumble|nebula|speaks|forgotten|"
                         r"screenplay|movie|erowid|mbox|emlx|meeting|oneonone|watch_bill|doorstep|dream_surf"),
    ("Journal, news and articles", r"news|article|journal|rando|digest|mail|local_|column|dashboard_look|"
                                   r"watch_turnover|weekly|trends|copenhagen|art_|brief|herd"),
    ("Organs: cognition and self-model", r"empathy|hold|valdemar|ae35|butlerian|seldon|charles|yellow_eye|busab|"
                                         r"usher|chandra|evitable|ghola|earth_boxes|crain|speedy|rama|bottle|pendulum|"
                                         r"card_rack|threshold|tma1|spectroscope|ivory|jade|peaslee|cardinal|intent|"
                                         r"hotwash|action_audit|relationship|care_|self_repair|pattern|growth|"
                                         r"time_sense|soft_certainty|predict|horology|continuity|quiet|memory_|peace|negative_space|night_watch"),
    ("Operations and reliability", r"backup|pg_|op_sync|drift|cron|health|staleness|reaper|freshness|cadence|doctor|"
                                   r"partition|index|pkg|claude_|token|model|llm|ollama|session|watchdog|prober|"
                                   r"chronic|daemon|live_docs|maintenance|strix|reclassify|cluster|credit|"
                                   r"preload|warm|sync|hw_|advisor|dead_mans|restore|offer|self_audit|reconcil|gardener"),
]


def classify(name: str, script: str) -> str:
    key = f"{name} {script}".lower()
    for label, pat in CATEGORIES:
        if re.search(pat, key):
            return label
    return "Other"


def scheduler_tasks() -> list:
    import yaml
    data = yaml.safe_load(CONFIG.read_text())
    tasks = data.get("tasks", data) if isinstance(data, dict) else {}
    out = []
    for name, v in tasks.items():
        if isinstance(v, dict):
            out.append({"name": name, "script": v.get("script", ""), "schedule": str(v.get("schedule", "")),
                        "category": classify(name, v.get("script", ""))})
    return out


def launch_jobs() -> list:
    names = []
    for d in (LAUNCH, DAEMONS):
        if d.exists():
            names += [p.stem for p in d.glob("*.plist")
                      if re.search(r"digitalnoise|nova|kochj", p.stem, re.I)]
    return sorted(set(names))


def ingest_scripts() -> list:
    return sorted(p.name for p in SCRIPTS.glob("nova_*ingest*.py"))


def publisher_scripts() -> list:
    return sorted(p.name for p in SCRIPTS.glob("*.py") if "publish_hugo" in p.read_text(errors="replace"))


def feed_urls() -> list:
    urls = []
    for f in FEED_SCRIPTS:
        p = SCRIPTS / f
        if p.exists():
            urls += re.findall(r"https?://[^\"' )\]]+", p.read_text(errors="replace"))
    return sorted(set(urls))


def feed_groups(urls: list) -> Counter:
    c = Counter()
    for u in urls:
        host = urlparse(u).netloc.replace("www.", "")
        c[host] += 1
    return c


def test_counts() -> dict:
    files = sorted((SCRIPTS / "tests").glob("test_*.py"))
    seven = sum(1 for f in files if "7cat" in f.name)
    return {"files": len(files), "seven_category": seven}


def build(out_dir: Path, dry_run: bool = False) -> dict:
    tasks = scheduler_tasks()
    launch = launch_jobs()
    ingests = ingest_scripts()
    pubs = publisher_scripts()
    urls = feed_urls()
    groups = feed_groups(urls)
    tests = test_counts()
    cats = Counter(t["category"] for t in tasks)
    stats = {"scripts": len(list(SCRIPTS.glob("*.py"))), "tasks": len(tasks), "launch": len(launch),
             "ingests": len(ingests), "publishers": len(pubs), "feeds": len(urls), "feed_hosts": len(groups),
             "test_files": tests["files"], "seven_category_tests": tests["seven_category"]}
    diagrams = diagrams_for(stats, cats, tasks, ingests, pubs, groups)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "Nova-Map.md").write_text(render_md(stats, diagrams, tasks, launch, ingests, pubs, urls))
        (out_dir / "Nova-Map.html").write_text(render_html(stats, diagrams, tasks, launch, ingests, pubs, urls))
    return {"stats": stats, "categories": dict(cats), "diagrams": len(diagrams)}


def diagrams_for(stats, cats, tasks, ingests, pubs, groups) -> list:
    """(title, mermaid source). Structure is written here; every number is from the build."""
    overview = f"""flowchart LR
    subgraph SENSE["Sensors and sources"]
        HK["HomeKit and Hue ({stats['tasks']} scheduled tasks overall)"]
        FEEDS["RSS and web feeds ({stats['feeds']} URLs, {stats['feed_hosts']} hosts)"]
        LOCAL["Local scanners, radio and cameras"]
        ARCH["Archives: books, manuals, scripture"]
    end
    subgraph CORE["Nova core"]
        SCHED["Scheduler ({stats['tasks']} tasks)"]
        INGEST["Ingest ({stats['ingests']} scripts)"]
        MEM[("Memory server and PostgreSQL")]
        ORGANS["Organs ({stats['tasks']} tasks, grouped)"]
        GATES["Safety gates: CARDINAL, SPINNAKER, two-man, action audit"]
    end
    subgraph OUT["Outputs"]
        ART["Articles ({stats['publishers']} publishers)"]
        JOURNAL["nova.digitalnoise.net (Hugo, GitHub Pages)"]
        ALERT["Alerts: Slack, Discord, Signal, LoRa"]
        HOME["Home control (HomeKit, Hue, Lutron)"]
    end
    HK --> SCHED
    FEEDS --> INGEST
    LOCAL --> SCHED
    ARCH --> INGEST
    SCHED --> ORGANS
    INGEST --> MEM
    ORGANS --> MEM
    MEM --> ORGANS
    ORGANS --> GATES
    GATES --> ART
    GATES --> ALERT
    GATES --> HOME
    ART --> JOURNAL"""
    sched = "flowchart TB\n    NOVA([Nova scheduler])\n"
    for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
        cid = re.sub(r"\W", "_", cat)
        sched += f'    {cid}["{cat}: {n} tasks"]\n    NOVA --> {cid}\n'
    ingest = "flowchart LR\n    SRC[Sources] --> FEEDS[Feed and web ingests]\n    SRC --> BOOKS[Book, manual and scripture ingests]\n"
    ingest += f'    FEEDS --> FEEDN["{len(groups)} hosts, {stats["feeds"]} URLs"]\n'
    ingest += f'    BOOKS --> INGESTN["{stats["ingests"]} ingest scripts"]\n'
    ingest += '    FEEDN --> REMEM[("memory server")]\n    INGESTN --> REMEM\n    REMEM --> RECALL["recall, search, forget"]\n'
    arts = "flowchart LR\n    SRC[Source material] --> WRITERS[" + f'"{stats["publishers"]} article publishers"' + "]\n"
    arts += '    WRITERS --> PUB["nova_journal.publish_hugo"]\n    PUB --> CB["Crystal Ball for news, local, security"]\n'
    arts += '    CB --> GIT["git_push, deploy"]\n    GIT --> SITE["nova.digitalnoise.net"]\n'
    safety = """flowchart LR
    PROP["Proposed action"] --> CORR{"SPINNAKER corroboration rung"}
    CORR -->|CORROBORATED| TP["Turning point ladder"]
    CORR -->|SINGLE_SOURCE or CONTESTED| ASK["Ask a human (max rung)"]
    TP --> TWO{"Two-man rule and MOLINK"}
    TWO -->|approved| DO["Act"]
    TWO -->|refused| HOLD["Hold"]
    DO --> AUDIT["Action audit (no unlogged actions)"]
    HOLD --> AUDIT
    ASK --> AUDIT
    AUDIT --> HOT["Hotwash after-action review"]"""
    tests = f"""flowchart TB
    T["Test suite: {stats['test_files']} test files"]
    T --> SEVEN["{stats['seven_category_tests']} files in the seven-category format"]
    T --> OTHER["{stats['test_files'] - stats['seven_category_tests']} other test files"]
    SEVEN --> CATS["security, performance, retry, unit, integration, functional, frame"]
    CATS --> GATE["Run before every commit and push"]"""
    launch_d = f"""flowchart LR
    LD["launchd: {stats['launch']} Nova jobs"] --> KEEP["KeepAlive daemons"]
    LD --> CALENDAR["Calendar-style jobs"]
    KEEP --> NOVA["Gateway, memory server, mesh agents"]
    CALENDAR --> SCHED2["Scheduler reloads on SIGHUP"]"""
    return [
        ("System overview", overview),
        ("Scheduled tasks, grouped", sched),
        ("Ingest pipeline", ingest),
        ("Article pipeline", arts),
        ("Safety gates", safety),
        ("Test suite", tests),
        ("Launch jobs", launch_d),
    ]


def render_md(stats, diagrams, tasks, launch, ingests, pubs, urls) -> str:
    out = ["# Nova: end-to-end map", "", "Generated from the repo, the scheduler config, the launch agents and the feed scripts.", ""]
    out += ["| Count | Value |", "|---|---|"] + [f"| {k.replace('_', ' ')} | {v} |" for k, v in stats.items()] + [""]
    for title, src in diagrams:
        out += [f"## {title}", "", "```mermaid", src, "```", ""]
    out += ["## Scheduled tasks", "", "| Task | Script | Schedule | Group |", "|---|---|---|---|"]
    out += [f"| {t['name']} | {t['script']} | {t['schedule']} | {t['category']} |" for t in sorted(tasks, key=lambda t: t['name'])]
    out += ["", "## Launch jobs", ""] + [f"- {j}" for j in launch]
    out += ["", "## Ingest scripts", ""] + [f"- {i}" for i in ingests]
    out += ["", "## Article publishers", ""] + [f"- {p}" for p in pubs]
    out += ["", "## Feed URLs", ""] + [f"- {u}" for u in urls]
    return "\n".join(out) + "\n"


def render_html(stats, diagrams, tasks, launch, ingests, pubs, urls) -> str:
    esc = html.escape
    parts = ["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>Nova map</title><style>body{font:14px system-ui;margin:16px;max-width:1200px;color:#111}"
             "h1,h2{font-weight:600}table{border-collapse:collapse;width:100%;font-size:12px}td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}"
             ".mermaid{background:#fafafa;padding:8px;overflow-x:auto}details{margin:8px 0}"
             "@media (prefers-color-scheme: dark){body{background:#111;color:#eee}.mermaid{background:#1b1b1b}td,th{border-color:#444}}"
             "</style>",
             "<script type='module'>import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs';"
             "mermaid.initialize({startOnLoad:true,securityLevel:'loose',theme:'default'});</script></head><body>",
             "<h1>Nova: end-to-end map</h1>",
             "<p>Generated from the repo, the scheduler config, the launch agents and the feed scripts.</p>",
             "<table><tr><th>Count</th><th>Value</th></tr>"]
    parts += [f"<tr><td>{esc(k.replace('_', ' '))}</td><td>{v}</td></tr>" for k, v in stats.items()]
    parts.append("</table>")
    for title, src in diagrams:
        parts.append(f"<h2>{esc(title)}</h2><pre class='mermaid'>{esc(src)}</pre>")
    parts.append("<h2>Scheduled tasks</h2><details open><table><tr><th>Task</th><th>Script</th><th>Schedule</th><th>Group</th></tr>")
    parts += [f"<tr><td>{esc(t['name'])}</td><td>{esc(t['script'])}</td><td>{esc(t['schedule'])}</td><td>{esc(t['category'])}</td></tr>"
              for t in sorted(tasks, key=lambda t: t['name'])]
    parts.append("</table></details>")
    for title, items in [("Launch jobs", launch), ("Ingest scripts", ingests), ("Article publishers", pubs), ("Feed URLs", urls)]:
        parts.append(f"<details><summary>{esc(title)} ({len(items)})</summary><ul>")
        parts += [f"<li>{esc(i)}</li>" for i in items]
        parts.append("</ul></details>")
    parts.append("</body></html>")
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    res = build(Path(a.out), a.dry_run)
    print(res["stats"])
    print("written to", a.out if not a.dry_run else "(dry run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
