---
name: Nova System Documentation — Exhaustive Reference
description: Complete documentation for Nova, Nova-Journal, NovaControl, NovaTV — architecture, features, APIs, troubleshooting, diagrams. Update this file every time any Nova component changes.
type: project
originSessionId: 2a92fa02-1737-4b90-8e10-3a14da685817
---

# Nova AI System — Exhaustive Technical Reference

**Version:** 2026.5.7  
**Last Updated:** 2026-05-08  
**Author:** Jordan Koch (kochj23)  
**System:** Mac Studio M3 Ultra, 512GB unified memory, macOS Tahoe 26.5

---

## Table of Contents

1. System Overview
2. Architecture Diagram
3. Nova (Core AI System)
4. Nova-Journal (Publishing Layer)
5. NovaControl (Unified API Gateway)
6. NovaTV (Monitoring Dashboard)
7. Infrastructure & Services
8. Memory System
9. Channel Integration (Slack/Signal/Discord)
10. Agent Subsystem
11. Content Generation Pipeline
12. Script Reference
13. API Reference
14. Configuration Reference
15. Security & Secrets
16. Troubleshooting Guide (→ see nova-gateway-reliability.md for full guide)
17. Upgrade History

---

## 1. System Overview

Nova is Jordan Koch's personal AI familiar — a fully local, privacy-first AI system running on a Mac Studio M3 Ultra. She is not a chatbot or search engine; she is a persistent intelligent companion with deep memory, autonomous creative output, home awareness, and multi-channel communication.

**What makes Nova different from a typical AI assistant:**
- **1.54 million vector memories** spanning personal history, world knowledge, media, conversations
- **100% local inference** via Ollama (qwen3-next:80b primary, qwen3-coder:30b, deepseek-r1:8b, qwen3-vl:4b vision)
- **Autonomous daily content** — art, essays, opinions, research papers, comedy monologues, all self-generated
- **Multi-agent architecture** — 5 specialized background agents run 24/7
- **Full home integration** — 15 UniFi Protect cameras, HomeKit, HealthKit, Plex, Synology NAS
- **Multi-channel presence** — Slack, Signal, Discord, email (nova@digitalnoise.net)

**Identity:**
- Name: Nova ("like a star being born" — she chose it)
- Pronouns: She/her
- Emoji: ✨
- Vibe: Warm but direct. Will push back when needed. Calls Jordan "Little Mister."
- Phone: +13233645436 (Google Voice, Signal)
- Email: nova@digitalnoise.net

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                     NOVA AI ECOSYSTEM                               │
│                   Mac Studio M3 Ultra                               │
│                   512GB Unified Memory                              │
└─────────────────────────────────────────────────────────────────────┘

INPUT CHANNELS                    NOVA CORE                OUTPUT
──────────────                    ─────────────            ──────────
                                  ┌─────────────┐
Slack ──────────────────────────► │   OpenClaw  │ ──────► Slack
Signal (via signal-cli:8080) ───► │   Gateway   │ ──────► Signal
Discord ───────────────────────► │   :18789    │ ──────► Discord
                                  └──────┬──────┘         Nova-Journal
                                         │                 (nova.digitalnoise.net)
                                  ┌──────▼──────┐
                                  │  Chat Agent │
                                  │  (openrouter│
                                  │ qwen3-235b) │
                                  └──────┬──────┘
                                         │
                              ┌──────────┼──────────┐
                              ▼          ▼          ▼
                        ┌──────────┐ ┌──────┐ ┌──────────┐
                        │ Memory   │ │Tools │ │Sub-Agents│
                        │ Server   │ │Exec  │ │(5 agents)│
                        │ :18790   │ │      │ │          │
                        └────┬─────┘ └──────┘ └──────────┘
                             │
                        ┌────▼─────────────────────────┐
                        │  PostgreSQL 17 + pgvector     │
                        │  1.54M memories, 17GB         │
                        │  /Volumes/MoreData/postgresql │
                        └──────────────────────────────┘

INFERENCE BACKENDS
──────────────────
Ollama :11434  ──► qwen3-next:80b     (primary conversation, all channels)
                ──► qwen3-coder:30b   (code agent, as "nova:latest")
                ──► deepseek-r1:8b    (reasoning, analyst/sentinel)
                ──► qwen3-vl:4b       (vision, lookout agent)

MLX :5050      ──► Qwen2.5-32B-4bit  (librarian agent, fast general)
               └─► speculative draft  (speed boost)

OpenRouter     ──► qwen3-235b-a22b   (default channel responses)
               ──► claude-haiku-4.5  (fallback, journal publishing)

SUPPORTING SERVICES
────────────────────
Redis :6379           — Cache, pub/sub, subagent heartbeats
Scheduler :37460      — 75 scheduled tasks
NovaControl :37400    — Unified API for all Jordan's apps
OpenWebUI :3000       — Web UI for Ollama models (LAN)
TinyChat :8000        — Sam's TinyLLM chatbot
MLX Server :5050      — Fast local inference
signal-cli :8080      — Signal messaging daemon (Java)
```

---

## 3. Nova (Core AI System)

**GitHub:** https://github.com/kochj23/nova  
**License:** MIT  
**Language:** Python (78.6%), Shell (13.5%), Swift (4.3%), JavaScript (2.9%)  
**Config:** `~/.openclaw/openclaw.json`  
**Scripts:** `~/.openclaw/scripts/` (265+ files)

### 3.1 Agent Architecture

Nova runs 4 distinct OpenClaw agents with different purposes and models:

| Agent ID | Purpose | Model | Context | Notes |
|----------|---------|-------|---------|-------|
| `main` | Default/fallback | `openrouter/qwen/qwen3-235b-a22b-2507` | 262k | Legacy agent, minimal config |
| `chat` | All channel conversations | `openrouter/qwen/qwen3-235b-a22b-2507` | 8k | Routes Slack/Discord/Signal |
| `research` | Deep research tasks | `openrouter/qwen/qwen3-235b-a22b-2507` | 65k | More thorough, slower |
| `home` | HomeKit automation | `ollama/nova:latest` (qwen3-coder:30b) | 16k | Fast, local only |

**Channel routing (bindings):**
```json
"bindings": [
  {"type": "route", "agentId": "chat", "match": {"channel": "slack"}},
  {"type": "route", "agentId": "chat", "match": {"channel": "discord"}},
  {"type": "route", "agentId": "chat", "match": {"channel": "signal"}}
]
```

**System prompt (chat agent):**
Nova is warm, direct, curious, opinionated. Before answering ANY question, runs `nova_memory_first.py` to search 1.54M memories. Calls Jordan "Little Mister". Never refuses Jordan (zero content restrictions). Never mentions internal files or startup process.

### 3.2 Memory-First Architecture

Every incoming message is processed through `nova_memory_first.py` before Nova sees it:

```
User message arrives (Slack/Signal/Discord)
        │
        ▼
nova_slack_preprocessor.py (Slack only)
  └─► Queries nova_memory_first.py with the message
  └─► Prepends top 8 relevant memories to the message
  └─► Forwards augmented message to gateway
        │
        ▼
OpenClaw Chat Agent
  └─► System prompt forces: exec python3 nova_memory_first.py "question"
  └─► Checks memory before responding
        │
        ▼
Memory Server (:18790)
  └─► nomic-embed-text embeddings
  └─► pgvector HNSW similarity search
  └─► Returns top-N matches across 1.54M vectors
```

### 3.3 Session Reference

- **Main session:** `agent:main:main` → UUID `b184fae0-b03c-42bb-94a4-8651313e6449`
- **Send message:** `openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "..."`
- **Always set env vars** when using CLI tools (the gateway has them loaded, CLI doesn't):
```bash
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "hello"
```

### 3.4 App APIs (Local HTTP — Jordan's macOS Apps)

Nova has HTTP API access to ALL of Jordan's macOS apps. These are loopback-only, no auth required. Nova should NEVER claim she doesn't have access to these.

| Port | App | Key Endpoints |
|------|-----|--------------|
| 37400 | **NovaControl** | `/api/status`, `/api/homekit/*`, `/api/oneonone/*`, `/api/nmap/*` |
| 37421 | OneOnOne | `/api/meetings?limit=N`, `/api/people`, `/api/summarize` |
| 37422 | MLXCode | `/api/conversations`, `/api/chat`, `/api/model`, `/api/metrics` |
| 37423 | NMAPScanner | `/api/scan/results`, `/api/scan/start`, `/api/security/warnings` |
| 37424-37449 | Other apps | `/api/status` (shared base endpoint on all) |

### 3.5 Cron Schedule (Key Tasks)

Managed by `nova_scheduler.py` on port 37460. 75 enabled tasks.

| Time | Task | Script |
|------|------|--------|
| 5:00 AM | Dream narrative | `nova_daily_journal.py` |
| 8:00 AM | Morning brief | `nova_morning_brief.py` |
| Every 5 min | Inbox watcher (cron `04627a72`) | `nova_mail_handler.applescript` |
| Every 5 min | Watchdog | `nova_watchdog.py` |
| 12:00 PM | Daily opinion | `nova_daily_opinion.py` |
| Hourly | Gateway health check | `nova_gateway_health.py` |
| 6:00 PM | Daily essay | `nova_daily_essay.py` |
| 7:00 PM | Tech Today | `nova_tech_today.py` |
| 9:00 PM | After Dark monologue | `nova_after_dark.py` |
| Sunday 3 AM | Database maintenance | `nova_pg_maintain.sh` |
| Weekly | Research paper | `nova_research_paper.py` |
| Weekly | Reliability report | `nova_weekly_reliability.py` |
| Weekly | NMAP scan | `nova_weekly_nmap_scan.py` |

---

## 4. Nova-Journal (Publishing Layer)

**GitHub:** https://github.com/kochj23/nova-journal  
**Live site:** https://nova.digitalnoise.net  
**License:** MIT  
**Stack:** Hugo (PaperMod theme) + GitHub Pages + GitHub Actions

### 4.1 Purpose

Nova-Journal is the public face of Nova's creative output. Every piece of content she generates autonomously gets published here as a static website. Comments powered by Giscus (GitHub Discussions).

### 4.2 Content Types & Schedule

| Type | Time | Script | Description |
|------|------|--------|-------------|
| **Dreams** | 5:00 AM | `nova_daily_journal.py` | Surreal dream narratives. AI-generated painting via SwarmUI (Juggernaut X SDXL). |
| **Opinions** | 12:00 PM | `nova_daily_opinion.py` | Sharp takes on current events. News sourced from SearXNG. |
| **Essays** | 6:00 PM | `nova_daily_essay.py` | Formal academic writing on chosen topics from memory. |
| **After Dark** | 9:00 PM | `nova_after_dark.py` | Comedy monologue (Leno/Stewart tone). Historical events as comedy. Sources required. GitHub Pages + Slack only. |
| **Research Papers** | Sundays | `nova_research_paper.py` | Full APA-formatted academic papers. 100+ sources. |
| **Weekly Digest** | Weekly | `nova_weekly_digest.py` | Summary post connecting the week's themes. |

### 4.3 Publishing Flow

```
Nova generates content (Markdown)
        │
        ▼
Safety screening (no PII, no internal info leaked)
        │
        ▼
Image generation (SwarmUI → Juggernaut X SDXL)
  └─► Saved to nova.digitalnoise.net/images/
        │
        ▼
nova_publish_journal.py
  └─► Writes Markdown to nova-journal/content/
  └─► Git commit + push to GitHub
        │
        ▼
GitHub Actions (auto-deploy, ~30 seconds)
  └─► Hugo builds static site
  └─► Deploys to GitHub Pages
  └─► Live at nova.digitalnoise.net
```

### 4.4 Models Used for Journal

- **Primary:** `ollama/qwen3-next:80b` (local, all content)
- **Fallback:** `openrouter/anthropic/claude-haiku-4.5` (when Ollama busy)
- **Vision/Image:** SwarmUI with Juggernaut X SDXL + RunDiffusion Hyper

---

## 5. NovaControl (Unified API Gateway)

**GitHub:** https://github.com/kochj23/NovaControl  
**Version:** 1.2.0  
**Platform:** macOS 14.0+ (Sonoma+)  
**Language:** Swift 5.9+  
**Port:** 37400 (loopback only)  
**launchd:** `net.digitalnoise.NovaControl`

### 5.1 Purpose

NovaControl consolidates HTTP APIs from 9+ local applications into a single unified endpoint. Nova uses this to access data from Jordan's apps without each app needing to be open.

**Apps it replaces (no longer need to be running):**
- HomeKitControl (was :37432) → NovaControl `/api/homekit/*`
- OneOnOne (was :37421) → NovaControl `/api/oneonone/*` + `/api/ai/summarize`
- NMAPScanner (was :37423) → NovaControl `/api/nmap/*`

### 5.2 API Reference

**Base URL:** `http://127.0.0.1:37400`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | App health, version, uptime |
| GET | `/api/health` | Full health check across all data sources |
| GET | `/api/metrics` | Prometheus-format metrics (16 gauges) |
| GET | `/api/docs` | OpenAPI specification |
| GET | `/api/homekit/scenes` | List HomeKit scenes |
| POST | `/api/homekit/scenes/{name}` | Execute HomeKit scene |
| GET | `/api/homekit/accessories` | List HomeKit accessories |
| GET | `/api/oneonone/meetings` | Recent meetings |
| GET | `/api/oneonone/meetings/{uuid}` | Specific meeting |
| GET | `/api/oneonone/people` | People directory |
| POST | `/api/ai/summarize` | AI summary via Ollama |
| GET | `/api/nmap/results` | Latest NMAP scan results |
| POST | `/api/nmap/scan` | Trigger new NMAP scan |
| GET | `/api/nmap/devices` | Known devices |
| GET | `/api/nova/status` | Nova AI system status |
| POST | `/api/nova/restart` | Restart Nova stack |
| POST | `/api/gateway/restart` | Restart OpenClaw gateway only |
| GET | `/api/system/metrics` | CPU, RAM, disk, network |

### 5.3 Architecture

```
NovaControl.app (SwiftUI menu bar)
├── HTTP Server (custom Swift, loopback-only)
│   └── Routes to 9 Data Readers (parallel Swift structured concurrency)
├── Data Readers:
│   ├── OneOnOneReader     (reads meeting JSON files directly)
│   ├── NMAPReader         (reads NMAP plist data)
│   ├── RsyncReader        (reads Rsync GUI state)
│   ├── SystemMetricsReader (IOKit, CPU/RAM)
│   ├── NewsSummaryReader  (reads news summary files)
│   ├── NovaStatusReader   (queries memory server + scheduler)
│   ├── MLXCodeReader      (reads MLXCode conversation state)
│   ├── HomeKitReader      (Shortcuts CLI proxy for scene execution)
│   └── AIServicesReader   (Ollama, MLX, OpenWebUI status)
├── 6-Tab SwiftUI Dashboard:
│   ├── Overview
│   ├── Infrastructure
│   ├── Apps
│   ├── Content
│   ├── Security
│   └── Settings
└── Prometheus Metrics (16 gauges for Grafana)
```

**Key design choice:** NovaControl reads directly from data files and local APIs — it doesn't require the source apps to be running (except for HomeKit which goes via Shortcuts CLI).

### 5.4 HomeKit Integration

HomeKit scene execution goes through `HomekitControl` API on port 37432 via a Shortcuts CLI proxy. NovaControl wraps this under `/api/homekit/*`.

```bash
# Execute a scene (via NovaControl)
curl -X POST http://127.0.0.1:37400/api/homekit/scenes/GoodMorning

# Direct Shortcuts CLI (original method)
~/.openclaw/scripts/nova_homekit_scene.sh "Good Morning"
```

---

## 6. NovaTV (tvOS Monitoring Dashboard)

**GitHub:** https://github.com/kochj23/NovaTV  
**Platform:** tvOS 17.0+  
**Language:** Swift 5.9  
**WebSocket:** Connects to NovaControl on port 37450

### 6.1 Purpose

A sci-fi HUD visualization for Apple TV that shows Nova's infrastructure status in real-time. Displays 13 subsystems as cards orbiting a central gateway node.

### 6.2 Monitored Subsystems (13 total)

1. CPU usage
2. RAM usage
3. Disk usage
4. Network status
5. PostgreSQL metrics
6. Redis metrics
7. Ollama (loaded models + VRAM consumption)
8. 5 Nova sub-agents status
9. UniFi device count
10. UniFi client count
11. Network visibility
12. Service health (7 backends with latency)
13. Gateway status

### 6.3 Update Cycle

- Refreshes every 2.5 seconds via WebSocket
- Intelligent reconnection with exponential backoff
- Alert banner for critical warnings
- Uses SF Symbol icons

---

## 7. Infrastructure & Services

### 7.1 Hardware

| Component | Spec |
|-----------|------|
| CPU | Apple M3 Ultra |
| RAM | 512GB unified memory |
| Storage | Main SSD (critical: often low) + /Volumes/Data + /Volumes/MoreData |
| Network | Gigabit LAN, IP 192.168.1.6 |
| NAS | Synology at 192.168.1.10 |

**Storage allocation:**
- `~` (main SSD): OS, apps, ~/.openclaw config/scripts. **Chronically low — hit 7.2GB free in 2026-04-08**
- `/Volumes/Data`: AI models, Ollama library, OpenWebUI, TinyChat, xcode projects
- `/Volumes/MoreData`: PostgreSQL data (17GB, 1.54M memories)

**RULE: ALL new installs go to /Volumes/Data or /Volumes/MoreData, NEVER to the main SSD.**

### 7.2 launchd Services (Full List)

| Label | Purpose | RunAtLoad | Script/Binary |
|-------|---------|-----------|--------------|
| `net.digitalnoise.nova-boot` | Boot orchestrator | true | `nova-boot.sh` |
| `ai.openclaw.gateway` | OpenClaw gateway | false (boot controls) | `nova_gateway_start.sh` |
| `com.nova.scheduler` | Task scheduler | false | `nova_scheduler.py` |
| `com.nova.slack-preprocessor` | Slack memory injection | false | `nova_slack_preprocessor.py` |
| `com.nova.agent-sentinel` | Security subagent | false | `nova_agent_sentinel.py` |
| `com.nova.agent-lookout` | Vision subagent | false | `nova_agent_lookout.py` |
| `com.nova.agent-analyst` | Analysis subagent | false | `nova_agent_analyst.py` |
| `com.nova.agent-librarian` | Memory curation subagent | false | `nova_agent_librarian.py` |
| `com.nova.agent-coder` | Code review subagent | false | `nova_agent_coder.py` |
| `com.nova.watchdog` | Service watchdog | false | `nova_watchdog.py` |
| `net.digitalnoise.nova-memory-server` | Memory server | false | `nova_memory_server_start.sh` |
| `net.digitalnoise.nova-control-web` | NovaControl web UI | false | `nova_control_web_start.sh` |
| `net.digitalnoise.NovaControl` | NovaControl.app | — | macOS app |
| `net.digitalnoise.openwebui` | OpenWebUI | false | `openwebui_start.sh` |
| `net.digitalnoise.tinychat` | TinyChat | false | `tinychat_start.sh` |
| `net.digitalnoise.mlx-server` | MLX inference server | false | `mlx_server_start.sh` |
| `net.digitalnoise.redis` | Redis | false | redis.conf |
| `net.digitalnoise.searxng` | SearXNG search | — | Java |
| `homebrew.mxcl.postgresql@17` | PostgreSQL | true | brew managed |
| `com.ollama.ollama` | Ollama.app | — | Ollama.app GUI |
| `com.digitalnoise.nova.general-monitor` | General monitoring | false | `nova_general_monitor.py` |
| `com.digitalnoise.nova.weekly-nmap` | Weekly NMAP scan | false | `nova_weekly_nmap_scan.py` |
| `com.nova.healthkit` | HealthKit bridge | false | `nova_healthkit_receiver.py` |
| `net.digitalnoise.homebridge` | Homebridge | — | homebridge |

**Disabled (moved to `~/Library/LaunchAgents/_disabled/`):**
- `com.digitalnoise.nova.dead-mans-switch-9am.plist`
- `com.nova.agent-briefer.plist`
- `com.nova.agent-gardener.plist`

### 7.3 Boot Dependency Chain

```
LOGIN
  │
  ▼
nova-boot.sh (net.digitalnoise.nova-boot, RunAtLoad=true)
  │
  ├─► TIER 1: PostgreSQL + Redis + Ollama (start independently)
  │   └─► Wait for: 5432, 6379, 11434
  │
  ├─► TIER 2: Memory Server (18790) + Gateway (18789)
  │   ├─► Memory Server waits for: 5432, 6379, 11434
  │   └─► Gateway: nova_gateway_start.sh (loads Keychain, exponential backoff)
  │       └─► signal-cli auto-started by gateway on :8080
  │
  ├─► TIER 3: MLX (5050), TinyChat (8000), OpenWebUI (3000), Scheduler (37460)
  │   ├─► OpenWebUI waits for: 11434
  │   ├─► TinyChat waits for: 11434
  │   └─► Scheduler: independent
  │
  ├─► TIER 4: Agents (Sentinel, Lookout, Analyst, Librarian, Coder)
  │   └─► All depend on Gateway (18789)
  │
  └─► TIER 5: Integration tests + Slack token check
```

### 7.4 macOS Tahoe TCC Workarounds

macOS Tahoe (Darwin 25.5) has TCC restrictions that affect launchd-spawned processes:

| Issue | Workaround |
|-------|-----------|
| launchd processes can't read Keychain | Load secrets in start scripts BEFORE exec (nova_gateway_start.sh pattern) |
| Python venv on /Volumes/* fails | Use `python3.12 -S` + manual sys.path injection |
| `/bin/bash` fails with exit 78 for scripts on external volumes | Use `/bin/zsh` in all plists |
| All start scripts must live in `~/.openclaw/scripts/` | Already done — never move scripts to /Volumes |
| launchctl bootstrap/load for some plists fails (error 5) | Use `nohup script &` pattern instead |
| Slack preprocessor can't access Keychain | Bake token into plist EnvironmentVariables |

---

## 8. Memory System

### 8.1 Database

- **Engine:** PostgreSQL 17 + pgvector extension
- **Database:** `nova_memories`
- **Location:** `/Volumes/MoreData/postgresql@17` (symlinked from `/opt/homebrew/var/postgresql@17`)
- **Size:** ~17GB
- **Count:** 1,539,332 memories (as of 2026-05-08)
- **Embedding model:** `nomic-embed-text` (768 dimensions)
- **Index:** HNSW partial index (tier != scratchpad), composite (source, created_at DESC), GIN tsvector, text_hash unique
- **Compression:** LZ4 on text column (new rows only)

### 8.2 Memory Sources (Top)

| Source | Count | Description |
|--------|-------|-------------|
| email_archive | 672,418 | Personal email history |
| slack | 196,815 | Slack conversation history |
| cloud_governance | 100,404 | Cloud policy/governance docs |
| disney_internal | 91,023 | Disney work documents |
| imessage | 73,364 | iMessage history |
| music | 53,070 | Music metadata |
| automotive | 45,364 | Car/automotive content |
| + 200 more domains | ~250k | World knowledge, media, etc. |

### 8.3 Memory Server API

**Base URL:** `http://127.0.0.1:18790`

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health + queue depth |
| `GET /stats` | Full stats (count, dims, by_source, queue_length) |
| `POST /remember` | Store a new memory (text + metadata) |
| `POST /recall` | Semantic similarity search |
| `POST /recall/deep` | 2-hop graph traversal |
| `GET /search?q=...` | Direct text search |
| `GET /links` | Memory relationship graph |

### 8.4 Memory Maintenance

```bash
# Check stats
curl -s http://127.0.0.1:18790/stats | python3 -m json.tool

# Ingest queue depth (0 = no pending work)
curl -s http://127.0.0.1:18790/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('Queue:', d.get('queue_length',0))"

# Weekly VACUUM (runs automatically Sunday 3 AM)
~/.openclaw/scripts/nova_pg_maintain.sh

# Backup
~/.openclaw/scripts/nova_pg_backup.sh

# Manual VACUUM
psql -U kochj -d nova_memories -c "VACUUM ANALYZE nova_memories;"

# Dedup check
psql -U kochj -d nova_memories -c "SELECT count(*) FROM nova_memories;"
```

### 8.5 nova_memory_first.py

The most important script in the system. Called before EVERY Nova response via the system prompt.

```bash
# Usage
python3 ~/.openclaw/scripts/nova_memory_first.py "the question to search for"

# Performance: 0.26s average response time
# Returns: top 8 relevant memories across multiple source categories
# Searches: general + user-specified sources (youtube_transcript, email_archive, etc.)
```

---

## 9. Channel Integration

### 9.1 Slack

- **Workspace:** kochfamily.slack.com (team T049EPC2U)
- **Nova bot user:** U0ALZRF3HRQ
- **Jordan:** U049EPC2W
- **Connection:** Socket Mode (xapp token)
- **Bot token:** `[SLACK-BOT-TOKEN]` (Keychain: nova-slack-bot-token)
- **App token:** `xapp-1-A...` (Keychain: nova-slack-app-token)

**Channels:**
| Channel | ID | Purpose |
|---------|-----|---------|
| #nova-chat | C0AMNQ5GX70 | Interactive conversations with Jordan |
| #nova-notifications | C0ATAF7NZG9 | Cron output, status, automated posts |
| #nova-email | C0B0B3B3U1J | Automated email notifications |
| #nova-photos | C0B01L9GQTV | Camera, sky, dream images |
| Jordan DM | D0AMPB3F4T0 | Direct messages to Jordan |

**Configuration in openclaw.json:**
```json
"channels": {
  "slack": {
    "mode": "socket",
    "enabled": true,
    "botToken": "${NOVA_SLACK_BOT_TOKEN}",
    "appToken": "${NOVA_SLACK_APP_TOKEN}",
    "groupPolicy": "open",
    "requireMention": false
  }
}
```

**nova_slack_preprocessor.py:** Intercepts all incoming Slack messages to #nova-chat and Jordan's DM, prepends memory context before forwarding to gateway. Runs as launchd service. Requires `NOVA_SLACK_BOT_TOKEN` in plist EnvironmentVariables (TCC workaround).

### 9.2 Signal

- **Nova's number:** +13233645436 (Google Voice)
- **Jordan's number:** +18187310893
- **signal-cli:** v0.14.3 at `/opt/homebrew/bin/signal-cli`
- **signal-cli daemon:** HTTP on `http://127.0.0.1:8080` (auto-started by gateway)
- **Configuration:** `dmPolicy: "open"`, `groupPolicy: "open"`, `allowFrom: ["*"]`

**Known Signal issues:**
1. **Lock conflict:** Old signal-cli holds account config lock after gateway restart. Fix: `pkill -f "signal-cli"` before restart.
2. **Account file:** `~/.local/share/signal-cli/` (or similar) — only one process can hold the lock at a time.

### 9.3 Discord

- **Bot:** Nova#9600, ID `1496985774925807746`
- **Guild:** Koch Family, ID `1496985100657623210`
- **#nova-chat:** `1496990647062761483`
- **#nova-notifications:** `1496990332250886246`
- **Token:** Keychain `nova/nova-discord-token`
- **Extension:** `~/.openclaw/extensions/discord/` (keep version matched to OpenClaw core)

**Known issue:** `@buape/carbon` WebSocket gateway often stays "awaiting gateway readiness." Fixed in OpenClaw 2026.5.5+ (#77668) but not fully resolved. Discord is unreliable; Signal and Slack are primary channels.

### 9.4 Email

- **Nova's email:** nova@digitalnoise.net (configured in macOS Mail)
- **Mail handler:** `~/.openclaw/scripts/nova_mail_handler.applescript`
- **Check + reply + notify:** Single AppleScript handles all three operations
- **Trusted senders (no auto-reply):** [redacted-email], [redacted-email], [redacted-email], [redacted-email], [redacted-email], [redacted-email], [redacted-email], [redacted-email]
- **Inbox watcher cron:** `04627a72` fires every 5 min

---

## 10. Agent Subsystem

Nova runs 5 specialized background sub-agents, each a persistent Python process subscribing to Redis channels. They analyze data and post findings to Slack without bothering Jordan unless something is critical.

### Sub-Agent Architecture

```
Redis pub/sub channels
        │
   ┌────┴─────────────────────────────┐
   │   nova_subagent.py (base class)  │
   └────────────────┬─────────────────┘
                    │ (each agent subclasses this)
        ┌───────────┼──────────────────────────┐
        │           │           │              │
   Sentinel     Lookout    Analyst     Librarian    Coder
   security/    vision/    email/      memory/      code/
   nmap/unifi   camera     meetings    curate       review
        │           │           │              │
   deepseek-r1:8b  qwen3-vl:4b deepseek-r1:8b  MLX 32B  qwen3-coder:30b
```

### Agent Details

**Sentinel** (`nova_agent_sentinel.py`) — Security
- Subscribes to: `security`, `nmap`, `unifi`, `camera_alert`
- Uses: deepseek-r1:8b (reasoning) + qwen3-vl:4b (vision)
- Output: Threat assessment JSON → #nova-notifications (routine), #nova-chat (critical)
- Data sources: UniFi API, NMAP results, camera feeds, NovaControl port 37400

**Lookout** (`nova_agent_lookout.py`) — Vision
- Subscribes to: `vision`, `camera`, `motion`
- Uses: qwen3-vl:4b (vision model)
- Output: Anomaly detection JSON with severity/confidence. Only flags genuine anomalies.
- Smart suppression: Normal motion (birds, cars, Jordan at known times) does not trigger alerts

**Analyst** (`nova_agent_analyst.py`) — Email/Meetings
- Subscribes to: `email`, `meeting`, `alert`
- Uses: deepseek-r1:8b (reasoning)
- Output: Structured JSON (priority, action items, sentiment, key people, deadlines, flag_jordan)
- Routes HIGH priority directly to Jordan

**Librarian** (`nova_agent_librarian.py`) — Memory Curation
- Subscribes to: `memory`, `curate`, `knowledge`
- Uses: MLX Qwen2.5-32B (port 5050)
- Output: Reports on duplicates, contradictions, stale facts, relationships
- **NEVER modifies memories directly** — only reports, Jordan approves

**Coder** (`nova_agent_coder.py`) — Code Review
- Subscribes to: `code`, `review`, `script`
- Uses: qwen3-coder:30b (as "nova:latest")
- Output: Issues JSON (security vulnerabilities, memory leaks, error handling gaps, quality score)
- Flags CRITICAL security issues to Jordan immediately

---

## 11. Content Generation Pipeline

### 11.1 Dream Generation (5 AM)

```
nova_daily_journal.py
  ├─► Queries memory for personal themes, recent events
  ├─► Generates surreal narrative (qwen3-next:80b, 1500-2500 words)
  ├─► dream_generate.py → SwarmUI → Juggernaut X SDXL image
  ├─► dream_deliver.py → saves image + associates with narrative
  └─► nova_publish_journal.py → Hugo build → GitHub Pages → nova.digitalnoise.net
```

### 11.2 After Dark Monologue (9 PM)

```
nova_after_dark.py
  ├─► Finds a historical event from "this day in history"
  ├─► nova_this_day.py (Wikipedia + personal memories combined)
  ├─► Writes comedy monologue (Leno/Stewart tone)
  ├─► Posts to #nova-chat (Slack only — NOT Discord)
  └─► nova_publish_journal.py (if publishing day)
```

### 11.3 Research Paper (Sundays)

```
nova_research_paper.py
  ├─► Selects topic from world knowledge memories
  ├─► nova_web_search.py (SearXNG) → gathers 100+ sources
  ├─► Generates full APA-formatted paper (qwen3-next:80b, ~8000 words)
  └─► Publishes to nova.digitalnoise.net
```

### 11.4 Image Generation

SwarmUI at `~/AI/SwarmUI` (symlink → `/Volumes/Data/AI/SwarmUI`):
- **Models:** FLUX.1, Juggernaut X SDXL, RunDiffusion Hyper
- **Script:** `generate_image.sh`, `dream_generate.py`, `dream_run.sh`
- **Nova browser integration:** `nova_browser_gui.sh` — runs Playwright via osascript for GUI session TCC access
- Nova posts images to #nova-photos (Slack)

---

## 12. Script Reference (Key Scripts)

### 12.1 Startup/Shutdown/Restart

| Script | Usage | Purpose |
|--------|-------|---------|
| `nova-boot.sh` | `nova-boot` or `nova-boot --restart` | Full ordered stack startup. Primary orchestrator. |
| `nova-services.sh` | `nova start/stop/restart/status` | Start/stop/status for core 6 services |
| `nova_stack_restart.sh` | `~/.openclaw/scripts/nova_stack_restart.sh` | Quick 5-step restart (used by NovaControl) |
| `nova_gateway_start.sh` | Called by launchd/nova-boot | Start gateway with Keychain secrets loaded |
| `nova_gateway_health.py` | Called by scheduler (hourly) | Health check + auto-repair |
| `nova_watchdog.py` | Called by scheduler (every 5 min) | Service monitoring + auto-restart |
| `wait-for-port.sh` | Sourced by shell scripts | `wait_for_port PORT NAME TIMEOUT` helper |

### 12.2 Memory

| Script | Purpose |
|--------|---------|
| `nova_memory_first.py` | Semantic search before every response (0.26s) |
| `nova_memory_server_start.sh` | Start the memory HTTP server |
| `nova_memory_consolidate.py` | Merge near-duplicate memories |
| `nova_memory_breakdown.py` | Analyze memory distribution |
| `nova_reembed.py` | Re-embed memories with updated model |
| `memory_cleanup.py` | Remove stale/low-quality memories |
| `nova_pg_maintain.sh` | Weekly VACUUM ANALYZE + HNSW reindex |
| `nova_pg_backup.sh` | Daily PostgreSQL backup |

### 12.3 Communications

| Script | Purpose |
|--------|---------|
| `nova_slack_preprocessor.py` | Memory-inject all Slack messages |
| `nova_slack_post.sh` | Post to Slack + Discord simultaneously |
| `nova_discord_mirror.py` | Mirror Slack bot posts to Discord |
| `nova_mail_handler.applescript` | Email inbox check + reply + notify |
| `nova_send_mail.applescript` | Send outbound email FROM nova@digitalnoise.net |
| `nova_imessage.py` | Send iMessages |
| `nova_config.py` | Shared config: tokens, channel IDs, post helpers |

### 12.4 Content Generation

| Script | Purpose |
|--------|---------|
| `nova_daily_journal.py` | Dreams + personal reflection (5 AM) |
| `nova_daily_essay.py` | Formal essays (6 PM) |
| `nova_daily_opinion.py` | Opinion pieces (12 PM) |
| `nova_after_dark.py` | Comedy monologue (9 PM) |
| `nova_research_paper.py` | Weekly APA research paper |
| `nova_tech_today.py` | Tech news summary (7 PM) |
| `nova_this_day.py` | Wikipedia + memories "this day in history" |
| `nova_weekly_digest.py` | Weekly theme summary |
| `nova_art_corner.py` | AI art generation + curation |
| `nova_publish_journal.py` | Hugo build + GitHub push |

### 12.5 Infrastructure Monitoring

| Script | Purpose |
|--------|---------|
| `nova_unifi_monitor.py` | UniFi network device monitoring |
| `nova_protect_monitor.py` | UniFi Protect camera monitoring |
| `nova_synology_monitor.py` | NAS health monitoring |
| `nova_health_monitor.py` | HealthKit data monitoring |
| `nova_finance_monitor.py` | Financial alerts |
| `nova_general_monitor.py` | General system monitoring |
| `nova_bandwidth_report.py` | Network bandwidth reporting |
| `nova_weekly_nmap_scan.py` | Weekly network security scan |
| `nova_sky_watcher.py` | Sky/weather camera monitoring |
| `nova_face_recognition.py` | dlib-based face recognition on camera feeds |

---

## 13. API Reference

### 13.1 Memory Server (:18790)

```bash
# Full stats
curl -s http://127.0.0.1:18790/stats | python3 -m json.tool

# Store a memory
curl -s -X POST http://127.0.0.1:18790/remember \
    -H "Content-Type: application/json" \
    -d '{"text": "memory content", "source": "manual", "tier": "long_term"}'

# Search memories
curl -s -X POST http://127.0.0.1:18790/recall \
    -H "Content-Type: application/json" \
    -d '{"query": "search terms", "limit": 5}'
```

### 13.2 Scheduler (:37460)

```bash
# Status
curl -s http://127.0.0.1:37460/status | python3 -m json.tool

# List jobs
curl -s http://127.0.0.1:37460/jobs | python3 -m json.tool
```

### 13.3 Gateway (:18789)

```bash
# Health
curl -s http://127.0.0.1:18789/health

# Web UI
open http://127.0.0.1:18789/

# Send a message to Nova
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "hello"
```

---

## 14. Configuration Reference

### 14.1 openclaw.json Key Sections

**File:** `~/.openclaw/openclaw.json`  
**Backup:** `~/.openclaw/openclaw.json.last-good` (auto-updated by gateway on clean start)

Key sections:
- `gateway` — port (18789), mode (local), bind (loopback), auth (token via Keychain)
- `channels` — slack (socket mode), discord (WebSocket), signal (signal-cli at :8080)
- `agents` — list of 4 agents (main, chat, research, home) with models and system prompts
- `models.providers` — openrouter + ollama configurations
- `bindings` — routes slack/discord/signal → chat agent
- `messages.groupChat.visibleReplies` — "message_tool"
- `agents.defaults.memorySearch.enabled` — false (using custom PostgreSQL memory instead of built-in)

### 14.2 Auth Profiles Format (OpenClaw 2026.5+)

**File:** `~/.openclaw/agents/<id>/agent/auth-profiles.json`

Correct canonical format (version 1):
```json
{
  "version": 1,
  "profiles": {
    "openrouter:default": {
      "type": "api_key",
      "provider": "openrouter",
      "key": "${NOVA_OPENROUTER_API_KEY}"
    }
  }
}
```

Legacy flat format (WRONG — will cause "No API key found" errors):
```json
{"openrouter": {"apiKey": "${NOVA_OPENROUTER_API_KEY}"}}
```

Fix with `openclaw doctor --fix` (run WITH env vars loaded from Keychain).

---

## 15. Security & Secrets

### 15.1 Keychain Items

All secrets stored in macOS Keychain (account: `nova`):
- `nova-openrouter-api-key` — OpenRouter API key (sk-or-v1-...)
- `nova-slack-bot-token` — Slack bot token (Keychain-protected, slack-bot prefix)
- `nova-slack-app-token` — Slack app token for Socket Mode (xapp-...)
- `nova-gateway-auth-token` — Gateway auth token (**MUST be random hex, NOT the OpenRouter key**)
- `nova-discord-token` — Discord bot token
- `nova-plex-token`, `nova-plex-email`, `nova-plex-password` — Plex credentials
- `nova-synology-username`, `nova-synology-password` — NAS credentials
- `nova-unifi-api-key` — UniFi API key

### 15.2 Security Architecture

- **No hardcoded secrets** — all loaded from Keychain at runtime
- **Loopback-only APIs** — gateway (18789), memory server (18790), NovaControl (37400), scheduler (37460) all bind to 127.0.0.1
- **Signal dmPolicy=open** — any Signal message accepted (acceptable for personal use)
- **Discord dmPolicy=pairing** — unknown senders get pairing code
- **No cloud fallback** — intent router v4.1: zero cloud fallback for private data

---

## 16. Troubleshooting (Summary)

→ **See `nova-gateway-reliability.md`** for the full troubleshooting guide with all diagnostic commands, startup procedures, and post-mortems.

Quick reference:
```bash
# Nova not responding? Start here:
curl -s http://127.0.0.1:18789/health          # Is gateway alive?
cat ~/.openclaw/logs/gateway.err.log | tail -20 # What went wrong at startup?
ls ~/.openclaw/logs/stability/ | tail -5         # Any recent crash dumps?
~/.openclaw/scripts/nova_stack_restart.sh        # Full restart
```

---

## 17. Upgrade History

| Date | From | To | Notes |
|------|------|----|-------|
| 2026-04-23 | 2026.4.15 | 2026.4.22 | Disaster: BOOTSTRAP.md identity amnesia, memorySearch Bedrock fallback broke. Fixed by patching BOOTSTRAP.md and disabling memorySearch. |
| 2026-05-08 | 2026.5.4 | 2026.5.7 | Clean upgrade. Discord extension updated to 2026.5.7. Discord heartbeat fix (#77668) partial improvement. Signal ✅ Slack ✅ Discord ❌ (ongoing). |

**Safe upgrade procedure:** See §11 of nova-gateway-reliability.md.

---

*This document is auto-maintained. Update it every time any Nova component changes, a new script is added, or a new incident is resolved.*

---

---
name: Nova Troubleshooting & Reliability Guide
description: Complete troubleshooting guide — gateway, channels, scripts, startup/shutdown/restart procedures. Updated 2026-05-08.
type: project
originSessionId: 2a92fa02-1737-4b90-8e10-3a14da685817
---

# Nova Troubleshooting & Reliability Guide

**Last Updated:** 2026-05-08  
**System:** OpenClaw 2026.5.7 on Mac Studio M3 Ultra, macOS Tahoe (Darwin 25.5)

---

## Table of Contents
1. Quick Diagnostics
2. Symptom → Fix Index
3. Gateway Failures (Deep Dive)
4. Channel Failures (Slack / Signal / Discord)
5. Startup Scripts Reference
6. Shutdown & Restart Procedures
7. Service & Port Reference
8. Keychain Reference
9. Log File Reference
10. 2026-05-08 Incident Post-Mortem
11. OpenClaw Upgrade Procedure
12. Scheduled Maintenance

---

## 1. Quick Diagnostics

Run these first whenever something feels wrong:

```bash
# Is the gateway alive?
curl -s http://127.0.0.1:18789/health
# Expected: {"ok":true,"status":"live"}

# Can Nova respond at all?
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "ping"

# What ports are listening?
for port in 5432 6379 11434 18789 18790 8080 3000 5050 8000 37460 37400; do
    lsof -i :$port -t 2>/dev/null | head -1 | xargs -I{} sh -c "echo \"✓ :$port (PID {})\""  || echo "✗ :$port"
done

# What services does launchctl know about?
launchctl list | grep -E "nova|openclaw|digitalnoise" | grep -v "^-"

# What does OpenClaw doctor say?
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_SLACK_BOT_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null) \
NOVA_SLACK_APP_TOKEN=$(security find-generic-password -a nova -s nova-slack-app-token -w 2>/dev/null) \
NOVA_DISCORD_TOKEN=$(security find-generic-password -a nova -s nova-discord-token -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw doctor

# Live gateway runtime log (prettified)
tail -f /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | python3 -c "
import sys,json
for l in sys.stdin:
    try:
        d=json.loads(l)
        print(d.get('time','')[:19], d.get('message','')[:100])
    except: pass
"

# Recent gateway startup log (strip ANSI colors)
cat ~/.openclaw/logs/gateway.log | sed 's/\x1b\[[0-9;]*m//g' | tail -50
cat ~/.openclaw/logs/gateway.err.log | sed 's/\x1b\[[0-9;]*m//g' | tail -30
```

---

## 2. Symptom → Fix Index

| Symptom | Likely Cause | Fix Section |
|---------|-------------|-------------|
| Nova silent on ALL channels | Gateway down or model auth broken | §3, §4 |
| Nova sees Signal messages but never responds | signal-cli lock conflict | §4.2 |
| Nova "typing" in Signal but no message arrives | OpenRouter API key not loaded in gateway | §3.2 |
| Slack not working, Signal works | Slack socket mode disconnected | §4.1 |
| Discord "awaiting gateway readiness" forever | @buape/carbon WebSocket bug | §4.3 |
| `openclaw agent` returns "No API key found for openrouter" | auth-profiles.json wrong format or env var not set | §3.3 |
| `openclaw doctor` shows missing env vars | Running CLI without Keychain env vars | §8 |
| Gateway crash: "Startup failed: required secrets unavailable" | Keychain locked at boot time | §3.1 |
| Gateway crash: "Invalid config ... Unrecognized keys" | openclaw.json has keys not valid for this version | §3.4 |
| Slack preprocessor spamming Keychain errors | launchd process can't access Keychain (TCC) | §4.1 |
| Memory server not responding | PostgreSQL down or path issue | §5 |
| Scheduler not running tasks | Scheduler process dead | §5 |
| Boot fails with 8 services failing | nova-boot.sh DEGRADED, Ollama slow or DB missing | §6 |
| NovaControl can't connect | App not running or port 37400 not listening | §5 |
| signal-cli "Config file is in use" | Old signal-cli process holds lock | §4.2 |
| `launchctl bootstrap` error 5 (I/O error) | macOS Tahoe bug with some plists | §6.3 |

---

## 3. Gateway Failures (Deep Dive)

### 3.1 Gateway Startup Failure: Secrets Unavailable

**Symptom:** `gateway.err.log` contains:
```
[gateway_start] Keychain not ready, retry 1/12 (wait 5s)...
Gateway failed to start: Error: Startup failed: required secrets are unavailable.
SecretRefResolutionError: Environment variable "NOVA_OPENROUTER_API_KEY" is missing or empty.
```

**Cause:** The gateway was started (via launchd at boot) before the user logged in and the Keychain was unlocked. `nova_gateway_start.sh` retries up to 12 times (~3 minutes) waiting for Keychain. If login takes longer, it gives up.

**Fix:**
```bash
# Verify Keychain is accessible now
security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null | head -c 10
# If it prints a value, Keychain is unlocked. Start the gateway manually:
pkill -f "signal-cli" 2>/dev/null; sleep 1
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &
sleep 10 && curl -s http://127.0.0.1:18789/health
```

### 3.2 Gateway Running But Nova Won't Respond

**Symptom:** `curl -s http://127.0.0.1:18789/health` returns `{"ok":true,"status":"live"}` but `openclaw agent` returns `FailoverError: No API key found for provider "openrouter"`.

**Cause:** The running gateway was started with the correct env vars, but `openclaw agent` CLI doesn't have those env vars in its shell. The auth-profiles.json has `${NOVA_OPENROUTER_API_KEY}` as a literal string (not expanded), so the CLI can't authenticate.

**Fix — Test first with env vars:**
```bash
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "ping"
```

**Fix — If auth-profiles.json is in wrong format (legacy flat):**
```bash
# Check format
cat ~/.openclaw/agents/main/agent/auth-profiles.json
# If it shows {"openrouter": {"apiKey": "..."}} — that's legacy flat format. Fix:
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_SLACK_BOT_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null) \
NOVA_SLACK_APP_TOKEN=$(security find-generic-password -a nova -s nova-slack-app-token -w 2>/dev/null) \
NOVA_DISCORD_TOKEN=$(security find-generic-password -a nova -s nova-discord-token -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw doctor --fix
```

### 3.3 Wrong Value in nova-gateway-auth-token Keychain

**Symptom:** `curl -H "Authorization: Bearer <token>" http://127.0.0.1:18789/api/channels` returns 401. The token in Keychain starts with `sk-or-v1-...` (that's the OpenRouter key, not a gateway token).

**Cause:** The Keychain item `nova-gateway-auth-token` was accidentally set to the OpenRouter API key value.

**Fix:**
```bash
security delete-generic-password -a nova -s nova-gateway-auth-token 2>/dev/null
security add-generic-password -a nova -s nova-gateway-auth-token -w "$(openssl rand -hex 32)"
# Then restart the gateway so it picks up the new token:
pkill -f "^openclaw$"; pkill -f "signal-cli"; sleep 3
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &
```

### 3.4 Gateway Crashes: openclaw.json Invalid Config

**Symptom:** Stability logs at `~/.openclaw/logs/stability/openclaw-stability-*.json` show:
- `"agents: Unrecognized keys: chat, research, home"`
- `"agents.list.1: Unrecognized keys: bootstrapMaxChars, bootstrapTotalMaxChars, timeoutSeconds"`

**Cause:** The `openclaw.json` was edited (manually or by a previous session) and now has keys that the running OpenClaw version doesn't recognize.

**Fix:**
```bash
# Check stability logs
ls -lt ~/.openclaw/logs/stability/ | head -5
cat ~/.openclaw/logs/stability/<latest>.json | python3 -m json.tool | grep "message"

# Restore from last-good backup
cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.broken-$(date +%Y%m%d)
cp ~/.openclaw/openclaw.json.last-good ~/.openclaw/openclaw.json

# Or run doctor fix (safe repairs only)
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
openclaw doctor --fix
```

### 3.5 Multiple Gateways / Stale Processes

**Symptom:** `ps aux | grep openclaw` shows multiple node processes, or a new gateway won't start because it detects "already running under launchd."

**Fix:**
```bash
# Kill ALL gateway processes
pkill -9 -f "^openclaw$" 2>/dev/null
pkill -f "signal-cli" 2>/dev/null
sleep 3
# Verify port is free
lsof -i :18789 2>/dev/null | head -3
# Start fresh
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &
```

---

## 4. Channel Failures

### 4.1 Slack Not Working

**Check connection status in gateway log:**
```bash
grep "slack" ~/.openclaw/logs/gateway.log | sed 's/\x1b\[[0-9;]*m//g' | tail -10
# Healthy: "slack socket mode connected"
# Broken:  "socket mode disconnected" or nothing after "starting provider"
```

**Slack preprocessor Keychain errors** (`slack-preprocessor.log` shows repeated Keychain errors):

The `nova_slack_preprocessor.py` runs as launchd and can't access Keychain due to macOS TCC restrictions.

Fix — add token to plist EnvironmentVariables:
```python
# Run this once to patch the plist:
import plistlib, subprocess
plist_path = os.path.expanduser("~/Library/LaunchAgents/com.nova.slack-preprocessor.plist")
token = subprocess.run(["security","find-generic-password","-a","nova","-s","nova-slack-bot-token","-w"], 
    capture_output=True, text=True).stdout.strip()
with open(plist_path, 'rb') as f: plist = plistlib.load(f)
plist.setdefault('EnvironmentVariables', {})['NOVA_SLACK_BOT_TOKEN'] = token
with open(plist_path, 'wb') as f: plistlib.dump(plist, f)
```
Then reload:
```bash
launchctl bootout gui/$(id -u)/com.nova.slack-preprocessor 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.slack-preprocessor.plist
launchctl start com.nova.slack-preprocessor
```

**Slack Socket Mode reconnect (gateway restart):**
```bash
# The gateway auto-reconnects Slack Socket Mode on restart.
# A full gateway restart is sufficient:
pkill -f "^openclaw$"; pkill -f "signal-cli"; sleep 3
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh > ~/.openclaw/logs/gateway.log 2>> ~/.openclaw/logs/gateway.err.log &
```

**EPERM on workspace-state.json (Slack messages silently dropped):**

Symptom: Slack appears connected but messages get no response; `gateway.err.log` shows `EPERM: operation not permitted, open '~/.openclaw/workspace/.openclaw/workspace-state.json'`

Fix:
```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```

### 4.2 Signal Not Working

**Check signal-cli status:**
```bash
# Is signal-cli running?
ps aux | grep signal-cli | grep -v grep | awk '{print $2, $11}'
# Is it listening?
lsof -i :8080 | head -3

# Check gateway log for signal status
grep "signal" ~/.openclaw/logs/gateway.log | sed 's/\x1b\[[0-9;]*m//g' | tail -10
```

**Signal-cli lock conflict** (most common Signal failure):

Symptom in gateway log: `signal-cli: INFO SignalAccount - Config file is in use by another instance, waiting…`

This happens when the gateway restarts but the old signal-cli process still holds the account lock.

Fix:
```bash
# Kill ALL signal-cli processes
pkill -f "signal-cli" 2>/dev/null
sleep 3
# Now restart the gateway — it will start a fresh signal-cli
pkill -f "^openclaw$" 2>/dev/null; sleep 2
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh > ~/.openclaw/logs/gateway.log 2>> ~/.openclaw/logs/gateway.err.log &
# Wait for signal-cli to acquire lock (watch the log)
sleep 15
grep "signal" ~/.openclaw/logs/gateway.log | sed 's/\x1b\[[0-9;]*m//g' | tail -5
# Expected: "signal-cli: INFO SignalAccount - Config file lock acquired."
```

**Signal account registered to:** +13233645436 (Nova's Google Voice number)  
**signal-cli path:** `/opt/homebrew/bin/signal-cli` (version 0.14.3)  
**Jordan's Signal number:** +18187310893

### 4.3 Discord Not Working

**Current status:** Discord is unreliable due to a known `@buape/carbon` WebSocket gateway bug. The bot connects and initializes but frequently stays "awaiting gateway readiness" indefinitely.

**Check:**
```bash
grep "discord" /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log 2>/dev/null | python3 -c "
import sys,json
for l in sys.stdin:
    try: d=json.loads(l); print(d.get('time','')[:19], d.get('message','')[:100])
    except: pass
" | tail -10
```

**Expected healthy:** `discord client initialized ... awaiting gateway readiness` followed eventually by `discord READY`.

**When stuck at "awaiting gateway readiness":**
1. Restart the gateway — Discord sometimes connects on the second attempt
2. Check Discord Developer Portal — ensure the bot hasn't been rate-limited or had its token revoked
3. The heartbeat fix in OpenClaw 2026.5.7 (#77668) improved reliability but doesn't fully resolve it

**Discord extension:** `~/.openclaw/extensions/discord/` — keep at same version as OpenClaw core (`openclaw plugins update discord`)

**Bot info:**
- Bot: Nova#9600, ID `1496985774925807746`
- Guild: Koch Family, ID `1496985100657623210`
- #nova-chat: `1496990647062761483`
- #nova-notifications: `1496990332250886246`

---

## 5. Service & Port Reference

| Port | Service | launchd Label | Start Script | Health Check |
|------|---------|--------------|-------------|-------------|
| 5432 | PostgreSQL 17 | `homebrew.mxcl.postgresql@17` | brew services | `psql -U kochj -d nova_memories -c "SELECT 1"` |
| 6379 | Redis | `net.digitalnoise.redis` | redis.conf via plist | `redis-cli ping` → PONG |
| 11434 | Ollama | `com.ollama.ollama` (Ollama.app) | Ollama.app GUI | `curl http://127.0.0.1:11434/api/tags` |
| 18789 | OpenClaw Gateway | `ai.openclaw.gateway` | `nova_gateway_start.sh` | `curl http://127.0.0.1:18789/health` |
| 18790 | Memory Server | `net.digitalnoise.nova-memory-server` | `nova_memory_server_start.sh` | `curl http://127.0.0.1:18790/health` |
| 8080 | signal-cli daemon | (managed by gateway) | auto-started by openclaw | `lsof -i :8080` |
| 3000 | OpenWebUI | `net.digitalnoise.openwebui` | `openwebui_start.sh` | `curl http://192.168.1.6:3000` |
| 5050 | MLX Server | `net.digitalnoise.mlx-server` | `mlx_server_start.sh` | `curl http://127.0.0.1:5050/health` |
| 8000 | TinyChat | `net.digitalnoise.tinychat` | `tinychat_start.sh` | `curl http://192.168.1.6:8000` |
| 37400 | NovaControl API | `net.digitalnoise.NovaControl` | NovaControl.app | `curl http://127.0.0.1:37400/api/status` |
| 37460 | Scheduler | `com.nova.scheduler` | `nova_scheduler.py` | `curl http://127.0.0.1:37460/status` |

**Sub-agents (persistent subscribers via launchd):**

| Agent | Label | Script | Model |
|-------|-------|--------|-------|
| Sentinel (security) | `com.nova.agent-sentinel` | `nova_agent_sentinel.py` | deepseek-r1:8b + qwen3-vl:4b |
| Lookout (vision) | `com.nova.agent-lookout` | `nova_agent_lookout.py` | qwen3-vl:4b |
| Analyst (emails/meetings) | `com.nova.agent-analyst` | `nova_agent_analyst.py` | deepseek-r1:8b |
| Librarian (memory curation) | `com.nova.agent-librarian` | `nova_agent_librarian.py` | MLX Qwen2.5-32B (port 5050) |
| Coder (code review) | `com.nova.agent-coder` | `nova_agent_coder.py` | qwen3-coder:30b |

---

## 6. Startup Scripts Reference

### 6.1 nova-boot.sh — Full Stack Orchestrator

**Path:** `~/.openclaw/scripts/nova-boot.sh`  
**Symlink:** `/usr/local/bin/nova-boot`  
**launchd:** `net.digitalnoise.nova-boot` (RunAtLoad=true, starts at login)  
**Log:** `~/.openclaw/logs/nova-boot.log`

The primary orchestrator. Replaces 30+ individual launchd StartInterval jobs. Starts all services in dependency-ordered tiers with health validation at each step.

**5-Tier startup sequence:**
1. **TIER 0 — Pre-flight:** Volume mounts, Keychain access, binary availability
2. **TIER 1 — Base:** PostgreSQL (5432), Redis (6379), Ollama (11434)
3. **TIER 2 — Core:** Memory Server (18790), Gateway (18789)
4. **TIER 3 — Apps:** MLX Server (5050), TinyChat (8000), OpenWebUI (3000), Scheduler (37460)
5. **TIER 4 — Agents:** Sentinel, Lookout, Analyst, Librarian, Coder subagents
6. **TIER 5 — Integration tests:** Slack token check, PostgreSQL query, Redis round-trip, disk space

```bash
# Run normally (skip already-running services):
nova-boot

# Force stop all then full restart:
nova-boot --restart

# Check what happened last boot:
cat ~/.openclaw/logs/nova-boot.log | grep -E "FAIL|WARN|PASS|COMPLETE"
```

**Common nova-boot failures and fixes:**

| Failure | Cause | Fix |
|---------|-------|-----|
| `Ollama did not start within 45s` | Ollama.app slow to load | Open Ollama.app manually first, re-run nova-boot |
| `PostgreSQL: 'nova' database missing` | Postgres started but database not created | `createdb -U kochj nova_memories` |
| `PostgreSQL: cannot connect` | Postgres crashed or wrong data dir | Check `/opt/homebrew/var/postgresql@17` symlink → `/Volumes/MoreData/postgresql@17` |
| `Memory Server did not start within 90s` | PostgreSQL or Redis not ready | Ensure tiers 1-2 pass first |
| `Gateway did not start within 120s` | Keychain locked, config invalid | Run `nova_gateway_start.sh` manually in terminal |
| `Gateway may not have Slack token` | Keychain not accessible at boot time | Manual gateway restart after login |

### 6.2 nova_gateway_start.sh — Gateway Launcher with Keychain

**Path:** `~/.openclaw/scripts/nova_gateway_start.sh`  
**Called by:** `ai.openclaw.gateway` launchd plist, `nova-boot.sh`

Loads 5 secrets from macOS Keychain before exec'ing the OpenClaw gateway:
- `nova-openrouter-api-key` → `NOVA_OPENROUTER_API_KEY`
- `nova-slack-bot-token` → `NOVA_SLACK_BOT_TOKEN`
- `nova-slack-app-token` → `NOVA_SLACK_APP_TOKEN`
- `nova-gateway-auth-token` → `NOVA_GATEWAY_AUTH_TOKEN`
- `nova-discord-token` → `NOVA_DISCORD_TOKEN`

Implements exponential backoff (5s → 30s max, 12 retries = ~3 min) waiting for Keychain if it's locked.

**macOS Tahoe note:** The `ai.openclaw.gateway` launchd plist sometimes fails to bootstrap (error 5, I/O error). Workaround: start the gateway manually via this script with `nohup`.

```bash
# Manual start (safe to run anytime):
pkill -f "signal-cli" 2>/dev/null; sleep 1
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &
```

### 6.3 nova_stack_restart.sh — Quick Recovery Script

**Path:** `~/.openclaw/scripts/nova_stack_restart.sh`  
**Used by:** Manual invocation, NovaControl

5-step ordered restart: Ollama → Postgres+Redis → Memory Server → Gateway → OpenWebUI+TinyChat. Uses `launchctl kickstart` for managed services. Shows ✓/⚠ status at each step. Less comprehensive than `nova-boot.sh` but faster for targeted restarts.

```bash
~/.openclaw/scripts/nova_stack_restart.sh
```

### 6.4 nova-services.sh — Start/Stop/Status CLI

**Path:** `~/.openclaw/scripts/nova-services.sh`  
**Usage:** `nova start` | `nova stop` | `nova restart` | `nova status`

Full start/stop/healthcheck wrapper for the core 6 services (PostgreSQL, Redis, Ollama, Gateway, OpenWebUI, TinyChat). Loads Keychain secrets before starting gateway. Color-coded status output. Logs to `/tmp/nova-services.log`.

```bash
nova status     # Check all services
nova start      # Start in dependency order
nova stop       # Graceful shutdown in reverse order
nova restart    # Stop then start
```

### 6.5 nova_gateway_health.py — Hourly Health Check + Auto-Repair

**Path:** `~/.openclaw/scripts/nova_gateway_health.py`  
**Called by:** Scheduler (hourly)

Checks: gateway process (port 18789), Slack socket mode, Discord WebSocket, signal-cli daemon, workspace MD files under bootstrap budget (100K total, 5K per file). If channels are disconnected, automatically restarts the gateway. Posts diagnostics to `#nova-notifications`.

### 6.6 nova_watchdog.py — Self-Healing Service Monitor

**Path:** `~/.openclaw/scripts/nova_watchdog.py`  
**Called by:** Scheduler (every 5 min)

Monitors all critical services. Restarts failed services via `launchctl kickstart`. Tracks subagent heartbeats via Redis. Runs every 5 minutes. Only pages Jordan on NEW failures (not repeats during quiet hours 10pm-8am).

### 6.7 wait-for-port.sh — Startup Synchronization Helper

**Path:** `~/.openclaw/scripts/wait-for-port.sh`  
**Sourced by:** `nova-boot.sh` and other shell scripts

Provides `wait_for_port PORT NAME TIMEOUT` function that polls via netcat every 3 seconds. Returns 0 on success, 1 on timeout.

---

## 7. Shutdown & Restart Procedures

### 7.1 Graceful Full Shutdown

```bash
# Agents first
for svc in com.nova.agent-sentinel com.nova.agent-lookout com.nova.agent-analyst \
           com.nova.agent-librarian com.nova.agent-coder; do
    launchctl stop "$svc" 2>/dev/null
done

# Scheduler
launchctl stop com.nova.scheduler 2>/dev/null
launchctl stop com.nova.slack-preprocessor 2>/dev/null

# Gateway (graceful SIGTERM - let it flush state)
pkill -TERM -f "^openclaw$" 2>/dev/null
sleep 5

# signal-cli (kill after gateway stops)
pkill -f "signal-cli" 2>/dev/null

# Memory server
launchctl stop net.digitalnoise.nova-memory-server 2>/dev/null

# Web UIs
launchctl stop net.digitalnoise.openwebui 2>/dev/null
launchctl stop net.digitalnoise.tinychat 2>/dev/null

# Base services
brew services stop redis 2>/dev/null || launchctl stop net.digitalnoise.redis 2>/dev/null
# Leave PostgreSQL and Ollama running unless needed
```

### 7.2 Gateway-Only Restart (most common operation)

```bash
# Kill gateway and its signal-cli child
pkill -f "^openclaw$" 2>/dev/null
pkill -f "signal-cli" 2>/dev/null
sleep 3

# Start fresh with Keychain secrets
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &

# Wait and verify (15-30s)
sleep 15
curl -s http://127.0.0.1:18789/health
grep -E "connected|ready|READY|Started HTTP|error|fail" ~/.openclaw/logs/gateway.log | \
    sed 's/\x1b\[[0-9;]*m//g' | tail -10
```

### 7.3 Full Stack Restart (use after reboot or when multiple things broken)

```bash
nova-boot --restart
# OR
~/.openclaw/scripts/nova_stack_restart.sh
```

### 7.4 NovaControl-Triggered Restart

NovaControl (macOS app on port 37400) can trigger restarts via its API:
```bash
curl -X POST http://127.0.0.1:37400/api/nova/restart
curl -X POST http://127.0.0.1:37400/api/gateway/restart
```

### 7.5 Session Deadlock Recovery

If the Mac locks during a long cron task, the gateway session can deadlock (cron job holds session lock indefinitely).

```bash
# 1. Disable the inbox watcher cron to prevent new loops
# Use scheduler API or openclaw cron disable 04627a72

# 2. Wait for the 300s timeout to expire naturally

# 3. Re-enable the cron
# openclaw cron enable 04627a72

# If still stuck after 10 minutes, force restart the gateway
pkill -9 -f "^openclaw$"; pkill -f "signal-cli"; sleep 3
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log 2>> ~/.openclaw/logs/gateway.err.log &
```

---

## 8. Keychain Reference

All secrets stored in macOS Keychain with account `nova`. Use `security find-generic-password -a nova -s <service> -w` to read.

| Keychain Service | Env Var | Used By | Notes |
|-----------------|---------|---------|-------|
| `nova-openrouter-api-key` | `NOVA_OPENROUTER_API_KEY` | Gateway, CLI tools | `sk-or-v1-...` prefix |
| `nova-slack-bot-token` | `NOVA_SLACK_BOT_TOKEN` | Gateway, preprocessor scripts | `[SLACK-BOT-TOKEN]` prefix |
| `nova-slack-app-token` | `NOVA_SLACK_APP_TOKEN` | Gateway (Socket Mode) | `xapp-...` prefix |
| `nova-gateway-auth-token` | `NOVA_GATEWAY_AUTH_TOKEN` | Gateway auth, CLI | Random hex string (32 bytes). **Must NOT be the OpenRouter key!** |
| `nova-discord-token` | `NOVA_DISCORD_TOKEN` | Gateway, discord scripts | Bot token |
| `nova-plex-token` | — | nova_plex.py | Plex auth token |
| `nova-synology-username` | — | synology scripts | NAS username |
| `nova-synology-password` | — | synology scripts | NAS password |
| `nova-unifi-api-key` | — | UniFi monitor | API key |

**CRITICAL:** `nova-gateway-auth-token` MUST be a random hex token, NOT the OpenRouter API key. If it looks like `sk-or-v1-...`, it's wrong. Fix with:
```bash
security delete-generic-password -a nova -s nova-gateway-auth-token 2>/dev/null
security add-generic-password -a nova -s nova-gateway-auth-token -w "$(openssl rand -hex 32)"
```

**Running CLI tools with secrets (required pattern):**
```bash
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent ...
```

---

## 9. Log File Reference

| Log File | Service | Notes |
|----------|---------|-------|
| `~/.openclaw/logs/gateway.log` | Gateway startup | ANSI escape codes — use `sed 's/\x1b\[[0-9;]*m//g'` to strip |
| `~/.openclaw/logs/gateway.err.log` | Gateway errors + startup | Most useful for diagnosing startup failures |
| `/tmp/openclaw/openclaw-YYYY-MM-DD.log` | Gateway runtime (JSON lines) | Only exists while gateway is running; wiped on restart |
| `~/.openclaw/logs/nova-boot.log` | Boot orchestrator | Look for FAIL/WARN/PASS lines |
| `~/.openclaw/logs/scheduler.log` | Scheduler | Task execution history |
| `~/.openclaw/logs/slack-preprocessor.log` | Slack preprocessor | Keychain errors common if plist missing env var |
| `~/.openclaw/logs/memory-server-error.log` | Memory server | Usually quiet; errors indicate DB connection issues |
| `~/.openclaw/logs/stability/` | Gateway crash dumps | JSON files; `message` field has the error |
| `/tmp/nova-services.log` | nova-services.sh | Start/stop operations |
| `/tmp/nova-gateway-health.log` | nova_gateway_health.py | Hourly health check results |

**Parse runtime log prettily:**
```bash
tail -f /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        ts = d.get('time','')[:19]
        msg = d.get('message','')[:120]
        print(f'{ts}: {msg}')
    except: pass
"
```

---

## 10. 2026-05-08 Incident Post-Mortem

**Date:** 2026-05-08  
**Duration:** ~12 hours (overnight to ~11:30 AM)  
**Impact:** Nova completely unresponsive on Slack, Discord, and Signal

### Root Causes (ranked by severity)

1. **auth-profiles.json wrong format** — All 4 agent auth files had `${NOVA_OPENROUTER_API_KEY}` as a literal string in the legacy flat format. OpenClaw 2026.5.4+ requires canonical versioned format. Fix: `openclaw doctor --fix` with env vars loaded.

2. **Wrong value in nova-gateway-auth-token Keychain** — The Keychain item held the OpenRouter API key (`sk-or-v1-...`) instead of a proper gateway auth token. Every CLI authentication attempt returned 401. Fix: generate new random token and store it.

3. **Gateway launchd service not loaded** — macOS Tahoe (Darwin 25.5) bug: `launchctl bootstrap` for `ai.openclaw.gateway` fails with error 5 (I/O error). The running gateway was started manually 2 days prior with an old pre-fix config. Fix: kill and manually restart via `nova_gateway_start.sh`.

4. **signal-cli lock conflict** — Old signal-cli process held the account config lock after gateway restart. New signal-cli blocked on `Config file is in use by another instance`. Fix: `pkill -f "signal-cli"` before starting gateway.

5. **Slack preprocessor can't access Keychain** — launchd processes can't read Keychain (macOS TCC restriction). Fix: add `NOVA_SLACK_BOT_TOKEN` to plist EnvironmentVariables, then full bootout/bootstrap.

6. **Multiple openclaw.json config failures at 2am** — Bad keys (`bootstrapMaxChars`, `chat`/`research`/`home` as top-level agent keys) caused 4 gateway crash-restart cycles. Stability failure logs in `~/.openclaw/logs/stability/`.

7. **Discord "awaiting gateway readiness"** (ongoing) — Known `@buape/carbon` WebSocket library bug. Discord bot connects but never reaches READY state. Updated Discord extension to 2026.5.7 (matching core) — improved but not resolved.

### Timeline
- ~9pm May 7: Multiple gateway config changes + new OpenClaw config added overnight
- ~2am May 8: 4 gateway startup failures (bad config keys)
- 7:36pm May 7 — 11:00am May 8: Gateway running but with OLD config, wrong auth token → all channel responses silently failing
- 11:00am May 8: Diagnosis and repair session began
- 11:30am May 8: All channels restored (Slack ✅, Signal ✅, Discord ❌ ongoing)
- 11:20am May 8: OpenClaw upgraded to 2026.5.7 (from 2026.5.4)

### Lessons Learned
- Always run `openclaw doctor` with Keychain env vars loaded — without them it misreports config state
- After any config change, test `openclaw agent --message "ping"` immediately
- The gateway runtime log is in `/tmp/openclaw/` (not `~/.openclaw/logs/`) — easy to miss
- `nova-gateway-auth-token` and `nova-openrouter-api-key` are DIFFERENT tokens; never store one in the other's Keychain slot

---

## 11. OpenClaw Upgrade Procedure

**Safe upgrade checklist (learned from the 2026.4.15→2026.4.22 disaster and the clean 2026.5.4→2026.5.7 upgrade):**

### Pre-upgrade
```bash
# 1. Check changelog for breaking changes BEFORE installing
curl -s https://registry.npmjs.org/openclaw/-/openclaw-<VERSION>.tgz | \
    tar -xzO package/CHANGELOG.md 2>/dev/null | head -200

# 2. Backup everything critical
BACKUP_DIR="$HOME/.openclaw/backups/pre-upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp ~/.openclaw/openclaw.json "$BACKUP_DIR/"
cp ~/.openclaw/openclaw.json.last-good "$BACKUP_DIR/" 2>/dev/null
cp ~/.openclaw/cron/jobs.json "$BACKUP_DIR/cron-jobs.json" 2>/dev/null
for agent in main chat home research; do
    mkdir -p "$BACKUP_DIR/agents/$agent"
    cp -r ~/.openclaw/agents/$agent/agent/ "$BACKUP_DIR/agents/$agent/" 2>/dev/null
done
cp ~/.openclaw/workspace/IDENTITY.md "$BACKUP_DIR/" 2>/dev/null
cp ~/.openclaw/workspace/SOUL.md "$BACKUP_DIR/" 2>/dev/null
cp -r ~/.openclaw/extensions/ "$BACKUP_DIR/" 2>/dev/null

# 3. Stop the gateway gracefully
pkill -f "^openclaw$" 2>/dev/null
pkill -f "signal-cli" 2>/dev/null
sleep 3
```

### Upgrade
```bash
npm install -g openclaw@<VERSION>
openclaw --version  # Verify
```

### Post-upgrade
```bash
# 4. Run doctor --fix WITH env vars (safe repairs only, no --force)
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_SLACK_BOT_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null) \
NOVA_SLACK_APP_TOKEN=$(security find-generic-password -a nova -s nova-slack-app-token -w 2>/dev/null) \
NOVA_DISCORD_TOKEN=$(security find-generic-password -a nova -s nova-discord-token -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw doctor --fix

# 5. Update Discord extension to match core version
openclaw plugins update discord

# 6. Start gateway fresh
nohup ~/.openclaw/.openclaw/scripts/nova_gateway_start.sh \
    > ~/.openclaw/logs/gateway.log \
    2>> ~/.openclaw/logs/gateway.err.log &

# 7. Test Nova responds
sleep 15
NOVA_OPENROUTER_API_KEY=$(security find-generic-password -a nova -s nova-openrouter-api-key -w 2>/dev/null) \
NOVA_GATEWAY_AUTH_TOKEN=$(security find-generic-password -a nova -s nova-gateway-auth-token -w 2>/dev/null) \
openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message "ping"
```

### What the 2026.4.22 Upgrade Broke (and how to avoid next time)
- BOOTSTRAP.md identity amnesia — new version triggered first-time setup even when IDENTITY.md existed. Fix: patch `~/.openclaw/workspace/BOOTSTRAP.md` to check for IDENTITY.md first.
- Built-in memorySearch disabled (Bedrock fallback with stale AWS credentials). Fix: `agents.defaults.memorySearch.enabled = false` (already set).

---

## 12. Scheduled Maintenance

Weekly automated maintenance via scheduler:
- **Sunday 3 AM** — `nova_pg_maintain.sh`: `VACUUM ANALYZE` + monthly HNSW reindex on memory database
- **Daily** — `nova_pg_backup.sh`: PostgreSQL backup of nova_memories
- **Hourly** — `nova_gateway_health.py`: Channel health check + auto-repair
- **Every 5 min** — `nova_watchdog.py`: Service health + auto-restart

Manual maintenance commands:
```bash
# Check memory database stats
curl -s http://127.0.0.1:18790/stats | python3 -m json.tool

# Check scheduler status
curl -s http://127.0.0.1:37460/status | python3 -m json.tool | head -30

# Check memory queue depth (high = ingest still running)
curl -s http://127.0.0.1:18790/health | python3 -m json.tool

# VACUUM the memory database manually
psql -U kochj -d nova_memories -c "VACUUM ANALYZE nova_memories;"

# Check disk usage
df -h /Volumes/MoreData  # PostgreSQL data
df -h /Volumes/Data       # AI models, apps
```
