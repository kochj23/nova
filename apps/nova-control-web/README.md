# Nova Control

Real-time monitoring dashboard for the [Nova](https://github.com/kochj23) AI assistant infrastructure. Provides a live visualization of all subsystems, data flow, and health metrics across the entire Nova stack.

Written by Jordan Koch.

![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green?logo=fastapi)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Overview

Nova Control is a single-page web dashboard that monitors Nova's infrastructure in real time. It connects to all running services, databases, and schedulers, then pushes live state updates to the browser via WebSocket every 2.5 seconds.

### Features

- **Animated Node Graph** — Canvas-based topology visualization with channels on the left (Slack, Discord, Signal, iMessage, Email), the gateway hub in the center, and backends on the right (Ollama, OpenRouter, MLX Chat, TinyChat, OpenWebUI). Support services (Redis, PostgreSQL, Memory Server, Scheduler) line the bottom.
- **Traffic-Driven Particle System** — Animated particles flow along edges between nodes, with density, speed, and brightness driven by **actual traffic volume**: gateway log analysis for channel activity, scheduler task deltas, Redis ingest queue changes, and service response latency.
- **System Resources** — CPU, RAM, swap, disk usage across all volumes with color-coded progress bars and live network TX/RX rates.
- **Ollama Model Stats** — Loaded models with VRAM usage, parameter counts, quantization levels, and context lengths.
- **PostgreSQL Stats** — Database size, total row counts, per-table breakdown, and index count for Nova's 14 GB vector memory brain.
- **Service Latency Sparklines** — Real-time response time tracking for all 7 services with trend history and min/avg/max stats.
- **Task Throughput Chart** — 24-hour stacked bar chart showing task completions per hour (succeeded/failed/timed_out).
- **Scheduler Job Table** — Full sortable table of all 36+ scheduled jobs with run counts, durations, failure tracking, and time-to-next-run. Collapsible to save screen space.
- **Agent Cards** — Status, model, uptime, and task completion counts for all 5 Nova sub-agents (Analyst, Sentinel, Coder, Lookout, Librarian).
- **Gateway, Redis, Memory System Cards** — Health status, connection state, queue depths.
- **Historical Trends** — SQLite time-series database records snapshots every 30s. Click any card to see trends over 1h/6h/24h/7d with interactive line charts for CPU, RAM, disk, latency, memory growth, and cost tracking.
- **Alerting Panel** — Persistent banner surfaces anomalies: disk below 10GB, services down, task failure streaks, queue backups, high CPU/memory. Color-coded by severity (warning/critical).
- **Conversation Activity** — Live view of active Nova sessions from OpenClaw: who's talking, which channel, token counts, session labels.
- **UniFi Network Card** — Device count, client count, WAN uptime from the UDM Pro API. Full device list in detail view.
- **OpenRouter Cost Tracking** — Daily cost aggregation with running monthly projection. Cost trend charts in detail modals.
- **Memory Growth Visualization** — Total memory count over time with per-source growth trends.
- **Dark/Light Theme Toggle** — CSS variable swap with localStorage persistence. Light theme for outdoor/mobile use.
- **Mobile-Optimized Layout** — Responsive breakpoints at 768px and 480px. Cards go single-column, modals go full-screen, graph shrinks gracefully.
- **Keyboard Shortcuts** — `R` reconnect, `1-9` jump to cards, `/` search tasks, `?` show help, `Esc` close modals.
- **Click-to-Detail Modals** — Click any card or graph node for deep stats. PostgreSQL shows daily ingestion charts, Redis shows cache hit rates, Ollama shows all installed models, channels show gateway log analysis.
- **Dark Cyberpunk Theme** — Monospace typography, cyan/green/magenta accent palette, glowing node halos, subtle grid background.
- **LAN Accessible** — Binds to `0.0.0.0` so any device on the local network can view the dashboard.
- **Responsive** — Works on desktop, tablet, and mobile layouts.

## Architecture

```
Browser (Canvas + WebSocket)
    ↕ WebSocket push every 2.5s
FastAPI Server (port 37450)
    ├── Scheduler API (port 37460) — job status, run counts, failures
    ├── Gateway Health (port 18789) — WebSocket reachability, channel status
    ├── Redis (port 6379) — agent status, ingest queue depth
    ├── Ollama API (port 11434) — loaded models, VRAM usage
    ├── PostgreSQL (nova_memories) — DB size, row counts, table stats
    ├── SQLite (tasks/runs.sqlite) — task history, throughput bucketing
    ├── SQLite (flows/registry.sqlite) — flow run status
    ├── SQLite (history.db) — time-series snapshots every 30s (local)
    ├── UniFi API (192.168.1.1) — network devices, clients, WAN health
    ├── OpenClaw sessions.json — conversation activity, model usage, costs
    ├── Service HTTP checks — latency timing for 7 services
    ├── Gateway log tail — per-channel message activity parsing
    └── psutil — CPU, RAM, disk, network counters
```

All data collection runs concurrently via `asyncio.gather()`. Each collector is independently fault-tolerant — if a service goes down, its card shows the error state while everything else continues updating.

## Data Sources

| Source | What It Provides | Update Method |
|--------|-----------------|---------------|
| Scheduler API (`37460`) | 36 job statuses, run counts, durations, failures | HTTP GET |
| Gateway Health (`18789`) | Live/down status, WebSocket reachability | HTTP GET + TCP probe |
| Redis (`6379`) | Agent status/meta, ingest queue depth | Redis commands |
| Ollama (`11434`) | Loaded models, VRAM, context lengths | HTTP GET `/api/ps` |
| PostgreSQL | DB size, 1.3M+ memory rows, table stats | `psql` subprocess |
| Task SQLite | Success/fail/timeout counts, hourly throughput | `aiosqlite` read-only |
| Flow SQLite | Workflow orchestration status | `aiosqlite` read-only |
| Gateway Log | Per-channel message activity (Slack/Discord/Signal) | File tail + regex |
| `psutil` | CPU %, RAM, swap, disk volumes, network I/O | Python library |
| Service HTTP probes | Response latency for 7 services with trend history | `aiohttp` timed requests |

## Traffic Flow Visualization

The particle system is **not cosmetic** — it represents real data flow:

- **Channel → Gateway edges**: Particle density maps to actual message counts parsed from the gateway log since last poll. A burst of Slack messages creates a visible stream of cyan particles.
- **Gateway → Backend edges**: Lights up when scheduler tasks are running (indicating LLM inference) or when service response latency spikes.
- **Support service edges**: Driven by Redis ingest queue depth changes — when memories are being written, these edges pulse.
- **Scheduler edge**: Fires on task completion deltas.
- **Zero activity = near-zero particles**: Just a barely visible ambient trickle. Active traffic is dramatically visible.
- **Edge brightness and width** also scale with flow rate.

## Installation

```bash
# Clone the repo
git clone git@github.com:kochj23/nova-control.git
cd nova-control

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run the dashboard
python server.py
```

The dashboard starts on `http://0.0.0.0:37450` — accessible from any device on the local network.

## Requirements

- Python 3.12+
- FastAPI, uvicorn, aiohttp, aiosqlite, redis, psutil
- Running Nova/OpenClaw infrastructure (gateway, scheduler, Redis, Ollama, PostgreSQL, etc.)

## Configuration

All service endpoints are configured as constants at the top of `server.py`:

```python
SCHEDULER_BASE = "http://127.0.0.1:37460"
GATEWAY_HEALTH = "http://127.0.0.1:18789/health"
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"
REDIS_URL = "redis://127.0.0.1:6379"
POLL_INTERVAL = 2.5  # seconds
```

Adjust ports if your Nova infrastructure uses different bindings.

## File Structure

```
nova-control/
├── server.py              # FastAPI app, 15 data collectors, WebSocket broadcast,
│                          # history DB, alert evaluator, 20+ REST API endpoints
├── requirements.txt       # Python dependencies
├── history.db             # SQLite time-series DB (auto-created, 30-day retention)
├── static/
│   ├── index.html         # Dashboard page with card layout + alert banner
│   ├── css/
│   │   └── dashboard.css  # Dark/light themes, responsive grid, modals, mobile
│   └── js/
│       ├── charts.js      # Reusable Canvas line/area chart library
│       ├── graph.js       # Canvas node graph + traffic-driven particle system
│       └── main.js        # WebSocket client, card renderers, modals, search,
│                          # keyboard shortcuts, theme toggle, trend charts
├── LICENSE                # MIT License
└── README.md
```

## Screenshots

The dashboard features:
- A full-screen animated node graph at the top showing live data flow between all Nova subsystems
- Color-coded stat cards below for gateway health, scheduler stats, system resources, Ollama models, PostgreSQL, Redis, service latencies, task throughput, and agent status
- A collapsible sortable table of all 36+ scheduler jobs

## Related Projects

- [OpenClaw](https://github.com/kochj23) — The AI assistant framework Nova runs on
- [MLXCode](https://github.com/kochj23/MLXCode) — Apple Silicon ML code editor

## License

MIT License — see [LICENSE](LICENSE) for details.
