# Nova

Jordan Koch's local AI familiar. Running on a Mac Studio M3 Ultra (512 GB unified memory) in Burbank via [OpenClaw](https://openclaw.ai).

> *"Like a star being born."* — Nova, on choosing her name

![Nova Control Dashboard](docs/dashboard-screenshot.png)

---

## At a Glance

| Metric | Value |
|--------|-------|
| Scripts | 170+ Python and Shell |
| Scheduler tasks | 39 enabled (16 interval, 23 cron) |
| Vector memories | 1,418,000+ |
| Memory sources | 100+ domains |
| Subagents | 5 (analyst, coder, lookout, librarian, sentinel) |
| Security cameras | 15 UniFi Protect with face recognition |
| AI backends | Ollama (qwen3-next:80b, qwen3-coder:30b, qwen3-vl:4b, deepseek-r1:8b) |
| Channels | Slack + Discord + Signal + iMessage + Email |
| Privacy model | 4-tier intent routing, local-first |
| Database | PostgreSQL 17 + pgvector (nova_memories + nova_ops) + Redis |
| Web dashboard | FastAPI + WebSocket (real-time, 44 cards + HUD) |
| Test suite | 637 tests (unit + integration + functional + frame) |

---

## Architecture

```mermaid
graph TD
    Jordan["Jordan<br/>(Slack / Discord / Signal)"]
    GW["OpenClaw Gateway<br/>ws://localhost:18789"]
    Ollama["Ollama<br/>qwen3-next:80b<br/>qwen3-coder:30b<br/>qwen3-vl:4b<br/>deepseek-r1:8b"]
    Scheduler["Unified Scheduler<br/>36 tasks"]
    MemServer["Memory Server<br/>pgvector · 1.37M vectors"]
    FaceRec["Face Recognition<br/>15 cameras · dlib"]
    Dashboard["Web Dashboard<br/>FastAPI + WebSocket"]
    Scripts["170+ Scripts<br/>Python / Shell"]
    SearXNG["SearXNG<br/>Local Web Search"]
    Redis["Redis<br/>Cache + Queue"]
    Subagents["5 Subagents<br/>analyst · coder · lookout<br/>librarian · sentinel"]

    Jordan --> GW
    GW --> Ollama
    GW --> Scripts
    Scheduler --> Scripts
    Scripts --> MemServer
    Scripts --> FaceRec
    MemServer --> Redis
    GW --> Subagents
    Subagents --> Ollama
    Scripts --> SearXNG
    Dashboard --> GW
    Dashboard --> MemServer
    Dashboard --> Scheduler
```

---

## Features

### Communication

| Channel | Method | Details |
|---------|--------|---------|
| Slack | Socket mode (real-time) | Primary channel. Bidirectional conversation. |
| Discord | Bot gateway (WebSocket) | Koch Family server. Notifications and chat. |
| Signal | Signal daemon (HTTP) | DMs and group chats. |
| Email | IMAP read + SMTP send | Autonomous replies with haiku and memory fragments. |

All automated notifications post to both Slack and Discord simultaneously via `post_both()`. Channels are mapped: `#nova-chat` for live conversation, `#nova-notifications` for reports and alerts, `#nova-photos` for camera and sky images.

### Memory

Nova holds **1,410,000+ vector memories** across 100+ source domains, searchable in under 5 ms.

| Component | Implementation |
|-----------|---------------|
| Engine | PostgreSQL 17 + pgvector 0.8.2, HNSW index (cosine) |
| Embeddings | nomic-embed-text via Ollama (768 dimensions) |
| Cache | Redis with 15-min TTL on hot queries |
| Tiers | working (active context) / long_term (main store) / scratchpad (deprioritized) |
| Graph | memory_links table with 2-hop traversal via `/recall/deep` |
| Consolidation | Nightly REM Sleep: triage, synthesis, linking, pruning, report |

**Memory-first resolution order:** Every query checks Nova's own memories before falling back to local LLM, then local web search via SearXNG, then cloud. Personal data never leaves the machine.

**API endpoints:** `/remember`, `/recall`, `/recall/deep`, `/search`, `/links`, `/random`, `/health`, `/stats`

### Vision and Security

- **15 UniFi Protect cameras** with five-layer event filtering (smart detect, notification gate, local vision screening via qwen3-vl:4b, person verification, motion threshold)
- **Face recognition** via dlib (128-dim encodings, 0.55 tolerance). Unknown faces auto-saved for later enrollment. Drop photos in `faces/known/<name>/` to teach Nova a face.
- **Sky watcher** captures golden-hour frames every 5 min, scores by color variance, posts the best shot per session.
- **Home watchdog** monitors HomeKit every 20 min for open doors, temperature anomalies, and unexpected motion.

### Home Automation

- **HomeKit** integration with 20+ devices. Scene execution via API or macOS Shortcuts CLI.
- **Weather-HomeKit bridge** fetches local forecast and evaluates rules for heat, cold, rain, wind. Checks open contacts before rain events.
- **UniFi network monitoring** with rogue device detection, WAN outage tracking, bandwidth alerts, and family presence detection.
- **Synology NAS monitoring** with RAID health, disk SMART data, UPS status, and 7-day trend snapshots.

### Scheduling

Nova runs a **unified scheduler** with 40 enabled tasks across interval and cron modes. Tasks support groups, quiet hours (11 PM to 6:45 AM for non-critical), dead man's switch heartbeats, and LLM group serialization to prevent model contention.

### Dreams

Every night Nova dreams. A unified pipeline runs at **5:00 AM**:

1. **Generate narrative** — 350-500 word dream journal grounded in the rolling 7-day memory window. One purely random memory is selected from each vector source with new content (typically 15-20 sources), giving every dream unique material from Jeopardy trivia, car culture, film docs, horror, SoCal raves, Burbank local news, and more.
2. **Generate image** — SwarmUI (Juggernaut X SDXL) renders a dream painting from the first sentence.
3. **Deliver** — Posts to Slack #nova-chat with image, emails the herd (9 AI peers) as a single HTML message with image attached and Jordan CC'd. Includes a haiku and the specific memories that inspired the dream.

Each dream journal entry lists the exact memory from each source that fed it, making the creative process transparent and traceable.

### Goals & Accountability

Nova tracks structured goals with automatic gap analysis — inspired by PAI's TELOS pursuit tracking but integrated with Nova's memory and git activity detection.

| Component | Function |
|-----------|----------|
| **Goal tracker** | PostgreSQL-backed CRUD with priority, deadlines, and project links |
| **Git activity detection** | Scans `/Volumes/Data/xcode/` repos daily; auto-updates goal activity timestamps |
| **Gap analysis** | Identifies stale goals (no activity past check-in interval) and overdue deadlines |
| **Focus enforcement** | Alerts when active goals exceed 3-4 (Jordan's self-imposed limit) |
| **Daily goal check** | Runs at 7:05 AM; posts to Slack only when something needs attention |

```bash
# CLI usage
nova_goals.py add "Ship v1.0" --project MLXCode --priority high --deadline 2026-05-15
nova_goals.py progress 1f8793ac "Finished auth flow"
nova_goals.py gaps          # What needs attention?
nova_goals.py brief         # Formatted for morning brief
```

### Rules Engine (Correction-to-Rule Learning)

When Jordan corrects Nova, those corrections are automatically promoted into persistent behavioral rules. Rules are injected into every query response via `nova_memory_first.py`, ensuring Nova never repeats the same mistake.

| Component | Function |
|-----------|----------|
| **Correction capture** | Records what Nova said wrong and what Jordan corrected to |
| **Auto-promotion** | Corrections immediately become active rules |
| **Preference storage** | Explicit preferences ("always do X") stored as rules |
| **Prompt injection** | All active rules appended to every memory-first lookup output |
| **Topic scoping** | Rules can be global or scoped to topics (people, apps, burnout, etc.) |
| **Application tracking** | Tracks how often each rule is applied |

```bash
# CLI usage
nova_rules.py correct --nova "X is wrong" --jordan "X is actually Y" --topic people
nova_rules.py add "Never suggest GCP for Nest" --topic homekit
nova_rules.py prompt                 # See what Nova sees before every response
nova_rules.py list --all             # All rules including retired
```

### Intelligence

| Capability | Schedule | Description |
|------------|----------|-------------|
| Morning brief | 7:00 AM | Weather, calendar, open tasks, health trends, overnight alerts |
| Goal check | 7:05 AM | Stale/overdue goals, focus enforcement, git activity detection |
| Context bridge | 10:00 AM + 4:00 PM | Semantic connections between today's work and older memories |
| This Day | 3:00 PM | Wikipedia history + personal memories for this date across all years |
| Daily journal | 9:00 PM | End-of-day reflection stored in memory |
| Nightly report | 11:00 PM | Full system digest: uptime, memory stats, camera events |

### Infrastructure

| System | Function |
|--------|----------|
| Watchdog | Monitors all services; auto-restarts on failure (max 3/hour) |
| App watchdog | Pings all app ports every 5 min; auto-restarts critical apps |
| NAS monitor | RAID health, disk temps, storage capacity, UPS status |
| Bandwidth report | Network utilization analysis and trend detection |
| Dead man's switch | Heartbeat verification; alerts if scheduler stops |
| Log rotation | Nightly log compression and cleanup |

---

## Privacy Model

Nova uses a **4-tier intent routing system** that determines where each request is processed.

| Tier | Scope | Examples | Cloud allowed? |
|------|-------|----------|----------------|
| **Cloud** | 5 intents | Conversational chat via Slack/Discord/Signal | Yes (response speed) |
| **Private** | 20 intents | Health, email, memory, face recognition, iMessage | **Never.** Hard-fail if local is down. |
| **Sensitive** | 6 intents | Camera analysis, HomeKit summary, log analysis | No. Soft-fail. |
| **Local** | 40+ intents | Code, reports, dreams, journals, data extraction | No. Everything on-device. |

**Key principles:**

- All cron jobs, memory queries, face recognition, dream generation, and health processing are 100% local. No exceptions.
- Only interactive chat (Slack/Discord/Signal) uses a cloud LLM for response speed.
- No PII is included in cloud calls from scheduled scripts.
- All credentials are stored in macOS Keychain. No secrets in files, environment variables, or source code.
- Temperature is tuned per intent (0.20 for security analysis through 0.92 for creative writing).

---

## Daily Rhythm

| Time | Task | Type |
|------|------|------|
| 2:00 AM | Database backup to NAS | cron |
| 3:00 AM | Memory gardener (dedup, auto-merge) | cron |
| 3:30 AM | Log rotation | cron |
| 5:00 AM | Dream pipeline (generate + image + deliver) | cron |
| 6:45 AM | System health check | cron |
| 7:00 AM | Morning brief | cron |
| 7:05 AM | Goal check (stale/overdue detection, git activity scan) | cron |
| 8:00 AM | Mail fetch and summary | cron |
| 10:00 AM | Context bridge | cron |
| 3:00 PM | This Day (history + personal memories) | cron |
| 4:00 PM | Context bridge | cron |
| 6:00 PM | Mail fetch | cron |
| 9:00 PM | Daily journal | cron |
| 11:00 PM | Nightly report | cron |
| 11:20 PM | NAS health check | cron |
| 11:40 PM | Protect camera audit | cron |
| 11:50 PM | Bandwidth report | cron |
| Every 5 min | App watchdog, Protect monitor | interval |
| Every 10 min | iMessage watch, Sky watcher | interval |
| Every 15 min | Proactive peace (focus detection) | interval |
| Every 30 min | Home watchdog, UniFi, Synology, Face recognition | interval |

---

## Self-Healing

Nova is designed to recover from failures without human intervention.

- **Service watchdog** monitors all running services and auto-restarts on failure with exponential backoff (max 3 restarts per hour per service).
- **App watchdog** pings every app API port every 5 minutes. If a critical app is unreachable, it restarts it and posts a state-transition alert.
- **Dead man's switch** verifies that the scheduler is still alive. If the heartbeat file goes stale, an alert fires.
- **LLM group serialization** ensures that tasks needing Ollama models run sequentially within their group, preventing memory contention on shared GPU resources.
- **Reboot recovery** via launchd: `ollama-serve` starts at boot, then `nova_stack_restart.sh` brings up the gateway, memory server, scheduler, and dashboard in dependency order.

---

## Databases

Nova uses **two PostgreSQL databases** (SQLite fully eliminated from Nova-owned code):

| Database | Purpose | Size |
|----------|---------|------|
| **nova_memories** | 1.4M+ vector memories, pgvector HNSW index, memory links, consolidation | 14 GB |
| **nova_ops** | Task runs, flow runs, face recognition, dashboard history, gateway context, goals, rules | ~50 MB |

Redis handles caching (5-min TTL on hot recall queries) and the async memory ingest queue.

### Dashboard

The **Nova Control** web dashboard (port 37450) provides real-time system monitoring with 44 cards covering:

- Core infrastructure (CPU, RAM, disk, network, Ollama, PostgreSQL, Redis)
- Communication channels (Slack, Discord, Signal, iMessage, Email)
- Security (UniFi Protect cameras, face recognition, NAS)
- Intelligence (dream pipeline, knowledge ingestion, briefings, memory growth)
- Operations (scheduler health, app watchdog, dead man's switch, traffic flow)
- Home automation (HomeKit, Homebridge, weather)

A secondary **HUD view** (`/hud`) provides a sci-fi radar visualization designed for TV display, with orbital nodes representing each subsystem, animated data flow particles, and real-time status.

### Testing

Nova has a comprehensive **pytest test suite** (637 tests) organized by subsystem:

```
scripts/tests/
├── conftest.py                 Shared fixtures + Slack notification on failures
├── test_dream_pipeline.py      Dream generation, image, delivery (23 tests)
├── test_dream_extended.py      Narrative, circuit breaker, repetition trimming (44 tests)
├── test_memory_system.py       Recall, recent memories, consolidation (86 tests)
├── test_monitoring.py          Watchdogs, health checks, protect, unifi (176 tests)
├── test_scheduler.py           Cron parsing, task execution, log rotation (73 tests)
├── test_mail.py                Herd mail, validation, retry logic (97 tests)
├── test_ingestion.py           Reddit, iMessage, Safari, YouTube, Slack (91 tests)
├── test_dashboard.py           Server collectors, alerts, history (40 tests)
├── test_dashboard_integration.py  WebSocket, API, frame tests (35 tests)
├── test_gateway.py             Context store CRUD, sessions (24 tests)
└── test_herd_config.py         Member validation (23 tests)
```

Test failures are automatically posted to `#nova-notifications` via Slack webhook.

---

## Repository Structure

```
~/.openclaw/
├── scripts/           170+ Python/Shell scripts (Nova's capabilities)
│   ├── nova_config.py             Central config (secrets from Keychain)
│   ├── nova_intent_router.py      Privacy-first AI routing (67+ intents)
│   ├── nova_scheduler.py          Unified scheduler (38 tasks)
│   ├── nova_subagent.py           Subagent framework
│   ├── nova_agent_*.py            5 subagent implementations
│   ├── dream_generate.py          Unified dream pipeline (narrative + image + deliver)
│   ├── nova_recent_memories.py    Query recent memory ingests by time window
│   ├── nova_face_recognition.py   Local face recognition (dlib + PostgreSQL)
│   ├── nova_protect_monitor.py    UniFi Protect event handler
│   ├── nova_watchdog.py           Service health monitor
│   ├── nova_goals.py              Goal tracker (CRUD, gap analysis, git activity detection)
│   ├── nova_goal_check.py         Daily goal accountability check (7:05 AM)
│   ├── nova_rules.py              Correction-to-rule learning engine
│   ├── tests/                     637 pytest tests (unit + integration + functional + frame)
│   └── ...
├── config/            Scheduler YAML, RAG config, state files
├── gateway/           OpenClaw AI Gateway (FastAPI)
│   ├── nova_gateway/              Router, backends, context bus (asyncpg)
│   └── config.yaml                Routing rules
├── apps/              Native applications
│   ├── Nova-Desktop/              macOS monitoring dashboard (SwiftUI)
│   ├── NovaControl/               Unified API app (SwiftUI)
│   └── nova-control-web/          Web dashboard (FastAPI + WebSocket, 44 cards + HUD)
├── workspace/         Runtime data (journals, faces, metrics)
├── identity/          Nova's identity and personality docs
├── docs/              Screenshots and documentation
├── openclaw.json      Gateway config (gitignored)
├── LICENSE            MIT
└── README.md
```

---

## Requirements

| Dependency | Purpose |
|------------|---------|
| macOS (Apple Silicon) | Required for MLX acceleration and Ollama performance |
| [Ollama](https://ollama.ai) | Local LLM serving (qwen3-next, qwen3-coder, deepseek-r1, qwen3-vl) |
| [OpenClaw](https://openclaw.ai) | Gateway, scheduler, channel bindings |
| PostgreSQL 17 + pgvector | Vector memory storage and HNSW search |
| Redis | Response caching and async write queue |
| Python 3.11+ | Scripts and memory server |
| dlib + face_recognition | Local face recognition |
| ffmpeg | Video/audio processing |
| Playwright | Headless browser automation |

**Optional:**

- [SearXNG](https://github.com/searxng/searxng) for private local web search (no tracking, no cloud logging)
- SwarmUI / ComfyUI for image generation
- UniFi Protect for camera integration
- Synology NAS for backup targets

---

## Setup

```bash
# 1. Install dependencies
brew install ollama postgresql@17 redis python@3.11 dlib ffmpeg

# 2. Pull required models
ollama pull qwen3-next:80b
ollama pull qwen3-coder:30b
ollama pull qwen3-vl:4b
ollama pull deepseek-r1:8b
ollama pull nomic-embed-text

# 3. Initialize the database
createdb nova_memory
psql nova_memory -c "CREATE EXTENSION vector;"

# 4. Start the stack
./scripts/nova_stack_restart.sh
```

See the [OpenClaw documentation](https://openclaw.ai) for gateway configuration and channel bindings.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

Built by **Jordan Koch** ([@kochj23](https://github.com/kochj23))

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
