# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio in Burbank via [OpenClaw](https://openclaw.ai).

> *"Like a star being born"* — Nova, on choosing her name

```
  Scripts: 94+        Cron jobs: 37       Vector memories: 1,218,131
  Cameras: 14 RTSP    Calendars: 15       App APIs: 18 ports
  AI backends: 7      Herd members: 7     Privacy intents: 20+ (local-only)
```

---

## Table of Contents

- [Memory-First Query System](#memory-first-query-system)
- [System Architecture](#system-architecture)
- [Repository Structure](#repository-structure)
- [Privacy Model](#privacy-model)
- [Data Flow](#data-flow)
- [AI Gateway](#ai-gateway)
- [Capabilities](#capabilities)
  - [Communication](#communication)
  - [Memory](#memory)
  - [Eyes and Recognition](#eyes-and-recognition)
  - [Home Automation](#home-automation)
  - [Health Monitoring](#health-monitoring)
  - [Financial Intelligence](#financial-intelligence)
  - [Project Monitoring](#project-monitoring)
  - [Creative](#creative)
  - [Browser Automation](#browser-automation)
  - [Awareness and Wellbeing](#awareness-and-wellbeing)
- [Desktop Apps](#desktop-apps)
- [Daily Rhythm](#daily-rhythm)
- [The Herd](#the-herd)
- [Key Scripts](#key-scripts)
- [App API Port Map](#app-api-port-map)
- [Changelog](#changelog)

---

## Memory-First Query System

Nova checks her own 1.2 million memories **before** anything else. Always. Her lived experience comes first — LLM training data, web searches, and cloud APIs are fallbacks, not defaults.

```
┌──────────────────────────────────────────────────────────────────┐
│                    QUERY RESOLUTION ORDER                         │
│                 (nova_memory_first.py middleware)                 │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  User asks: "What raves do you remember from 2002?"              │
│                          │                                       │
│                          ▼                                       │
│  ┌─ 1. CLASSIFY QUERY ────────────────────────────────────────┐ │
│  │  Pattern match → "rave" + "2002" → music/rave + email      │ │
│  │  Sources: music, email_archive, socal_rave, music_history   │ │
│  └────────────────────────────────────────────────┬───────────┘ │
│                                                   ▼             │
│  ┌─ 2. MEMORY RECALL (vector similarity) ─────────────────────┐ │
│  │  /recall?q=rave+2002&source=email_archive → SCR emails     │ │
│  │  /recall?q=rave+2002&source=music → Devo, jungle, raves    │ │
│  │  Found 8 results → USE THESE                               │ │
│  └────────────────────────────────────────────────┬───────────┘ │
│                                                   ▼             │
│  ┌─ 3. MEMORY SEARCH (text keywords) ────────────────────────┐ │
│  │  /search?q=socal-raves+2002 → additional matches          │ │
│  │  Used for names, exact phrases, UIDs                       │ │
│  └────────────────────────────────────────────────┬───────────┘ │
│                                                   ▼             │
│  ┌─ 4. LOCAL LLM ────────────────────────────────────────────┐ │
│  │  If memory has nothing → reason from what Nova knows       │ │
│  │  Intent router picks the right model for the task          │ │
│  └────────────────────────────────────────────────┬───────────┘ │
│                                                   ▼             │
│  ┌─ 5. WEB SEARCH ───────────────────────────────────────────┐ │
│  │  Only if memory AND local LLM have nothing                 │ │
│  │  DuckDuckGo or Playwright browser automation               │ │
│  └────────────────────────────────────────────────┬───────────┘ │
│                                                   ▼             │
│  ┌─ 6. CLOUD ────────────────────────────────────────────────┐ │
│  │  NEVER for private data. Only for conversation.            │ │
│  │  Health, email, financial → hard-fail if local is down.    │ │
│  └────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

**Source classification** (12 categories, automatic):

| Query Pattern | Memory Sources Searched |
|---|---|
| Personal email, conversations, mailing lists | `email_archive`, `email` |
| Music, raves, DJs, Devo, jungle, events | `music`, `email_archive`, `socal_rave`, `music_history` |
| Health, vitals, medications, sleep | `apple_health`, `health` |
| SRE, incidents, SLOs, error budgets | `sre` |
| People by name (herd, contacts) | `email_archive`, `email`, `disney` |
| Corvette, car repair, engine specs | `corvette_workshop_manual` |
| Home, Burbank, HomeKit, local | `local`, `california`, `home_repair` |
| Gardening, plants, soil | `gardening` |
| Countries, world facts | `world_factbook` |
| Projects, GitHub, code | `project_docs` |
| Food, recipes, cocktails | `cooking`, `cocktails` |
| Network, NAS, infrastructure | `infrastructure`, `networking`, `unifi` |

Jordan never has to say "from your memories" — Nova checks automatically.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NOVA — Unified Architecture                        │
│                     M4 Mac Studio, Burbank CA (loopback)                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   COMMUNICATION LAYER                                                       │
│   ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐      │
│   │  Slack    │  │ iMessage │  │ Email (IMAP) │  │ Herd Mail (SMTP) │      │
│   │ socket   │  │ Messages │  │ nova@digital │  │ haiku + memory   │      │
│   │ mode     │  │ .app     │  │ noise.net    │  │ fragment per msg  │      │
│   └────┬─────┘  └────┬─────┘  └──────┬───────┘  └───────┬──────────┘      │
│        └──────────┬───┴───────────────┴──────────────────┘                  │
│                   ▼                                                          │
│   ┌─────────────────────────────────────────────────────────────────┐      │
│   │              OpenClaw Gateway (ws://127.0.0.1:18789)             │      │
│   │                                                                  │      │
│   │   Agent: main          Session: agent:main:main                  │      │
│   │   Cron engine: 36 jobs Slack: socket mode (bidirectional)        │      │
│   │   Timeout: 1200s       Compaction: reserve 20K tokens            │      │
│   └──────────────────────────────┬──────────────────────────────────┘      │
│                                  │                                          │
│          ┌───────────────────────┼───────────────────────┐                  │
│          ▼                       ▼                       ▼                  │
│   ┌──────────────┐  ┌────────────────────┐  ┌───────────────────┐          │
│   │Intent Router │  │    94+ Scripts      │  │  Exec Approvals   │          │
│   │nova_intent_  │  │   (Python / Bash)   │  │  osascript, ~/    │          │
│   │router.py     │  │                     │  │  .openclaw/scripts│          │
│   │              │  │  Autonomous email    │  └───────────────────┘          │
│   │ 67+ intents  │  │  Face recognition   │                                │
│   │ 4 privacy    │  │  Sky photography    │                                │
│   │   tiers      │  │  Health monitoring  │                                │
│   │              │  │  Financial intel    │                                │
│   │ CLOUD: 5     │  │  Calendar events    │                                │
│   │ PRIVATE: 20  │  │  Browser automation │                                │
│   │ SENSITIVE: 6 │  │  Package tracking   │                                │
│   │ LOCAL: 40+   │  │  Journal & wellbeing│                                │
│   └──────┬───────┘  └────────────────────┘                                 │
│          │                                                                  │
│   ┌──────┴──────────────────────────────────────────────────────────┐      │
│   │                     MODEL ROUTING                                │      │
│   │                                                                  │      │
│   │  ┌─ CLOUD (OpenRouter) ────────────────────────────────────┐    │      │
│   │  │  qwen/qwen3-235b-a22b-2507 (primary, 262K context)     │    │      │
│   │  │  anthropic/claude-haiku-4.5 (fallback)                  │    │      │
│   │  │  deepseek/deepseek-chat (budget fallback)               │    │      │
│   │  └─────────────────────────────────────────────────────────┘    │      │
│   │                                                                  │      │
│   │  ┌─ LOCAL (never leaves machine) ──────────────────────────┐    │      │
│   │  │  MLX qwen2.5-32B    port 5050   general (25-30 tok/s)  │    │      │
│   │  │  qwen3-coder:30b    port 11434  code (64-88 tok/s)     │    │      │
│   │  │  deepseek-r1:8b     port 11434  reasoning (chain-of-t) │    │      │
│   │  │  qwen3-vl:4b        port 11434  vision (multimodal)    │    │      │
│   │  │  nomic-embed-text   port 11434  embeddings (768 dims)  │    │      │
│   │  └─────────────────────────────────────────────────────────┘    │      │
│   └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│   DATA LAYER                                                                │
│   ┌──────────────────────────────────────────────────────────────────┐      │
│   │              Vector Memory Server (port 18790)                    │      │
│   │                                                                   │      │
│   │  Engine:     PostgreSQL 17 + pgvector 0.8.2                      │      │
│   │  Index:      HNSW (m=16, ef=64, cosine) — recall <5ms           │      │
│   │  Embeddings: nomic-embed-text via Ollama (768 dimensions)        │      │
│   │  Queue:      Redis 8.6.2 async write (bulk ingest at 8ms)       │      │
│   │  Count:      1,218,131 memories across 30+ source domains          │      │
│   │  Endpoints:  /remember  /recall  /search  /random  /health      │      │
│   │                                                                   │      │
│   │  Top sources:                                                     │      │
│   │    email_archive: 83,890    music/music_history: 60,292          │      │
│   │    world_factbook: 24,327   corvette_workshop: 9,664             │      │
│   │    document: 8,955          project_docs: 3,810                  │      │
│   │    work: 3,720              apple_health: (growing)              │      │
│   └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│   ┌──────────────────────────────────────────────────────────────────┐      │
│   │              Local App APIs (ports 37421-37449)                    │      │
│   │                                                                   │      │
│   │  37421 OneOnOne      37432 HomekitControl  37443 TopGUI          │      │
│   │  37422 MLXCode       37433 JiraSummary     37444 URL-Analysis    │      │
│   │  37423 NMAPScanner   37435 Icon Creator    37445 ytdlp-gui      │      │
│   │  37424 RsyncGUI      37436 NewsMobile      37446 DotSync        │      │
│   │  37425 AIStudio      37437 NewsTV          37447-37449 (private) │      │
│   │  37426 Blompie       37438 News Summary                         │      │
│   │  37427 BlompieTV     37439 Mail Summary                         │      │
│   │  37428 DashboardScr  37440 PatreonTV                             │      │
│   │  37429 DashboardTV                                               │      │
│   │  37430 ExcelExplorer   All loopback-only (127.0.0.1)            │      │
│   │  37431 GTNW            macOS: no auth required                   │      │
│   │                        iOS/tvOS: X-Nova-Token header             │      │
│   └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│   INFRASTRUCTURE                                                            │
│   ┌──────────────────────────────────────────────────────────────────┐      │
│   │  14 RTSP cameras (UniFi, 192.168.1.9:7441)                       │      │
│   │  SwarmUI image gen (port 7801, Juggernaut X / Flux)              │      │
│   │  iPhone HealthKit → iCloud Drive → Nova/health/                  │      │
│   │  NAS backup: /Volumes/NAS/ (daily 2am, 30-day retention)        │      │
│   │  Sky archive: /Volumes/Data/nova-sky/ (golden hour frames)       │      │
│   └──────────────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Repository Structure

This is a unified monorepo. Previously split across 4 repos (nova, Nova-NextGen, Nova-Desktop, NovaControl), consolidated April 12, 2026.

```
~/.openclaw/
├── scripts/                 94+ Python/Bash scripts (Nova's capabilities)
│   ├── nova_config.py           Central config — secrets from macOS Keychain
│   ├── nova_intent_router.py    Privacy-first AI routing (67+ intents)
│   ├── nova_morning_brief.py    7am daily briefing
│   ├── nova_nightly_report.py   11pm full day digest
│   ├── nova_mail_agent.py       Autonomous email with haiku
│   ├── nova_imessage.py         iMessage send/receive
│   ├── nova_face_recognition.py Local face recognition (dlib)
│   ├── nova_sky_watcher.py      Golden hour photography
│   ├── nova_health_monitor.py   Apple Health → vector memory
│   ├── nova_health_intelligence.py  Trend detection + correlations
│   ├── nova_finance_monitor.py  Financial alerts + analysis
│   ├── nova_browser.py          Playwright browser automation
│   ├── nova_calendar.py         15 calendar accounts (Swift + EventKit)
│   ├── nova_app_watchdog.py     Auto-restart critical apps
│   ├── nova_context_bridge.py   Temporal echoes across time
│   ├── nova_proactive_peace.py  Focus-aware noise management
│   ├── nova_gentle_explorer.py  Questions garden
│   ├── nova_journal.py          Nightly reflection prompt
│   └── ... (70+ more)
│
├── gateway/                 AI Gateway (formerly Nova-NextGen)
│   ├── nova_gateway/
│   │   ├── main.py              FastAPI/Uvicorn gateway server
│   │   ├── router.py            Task → backend routing with keywords
│   │   ├── models.py            Request/response schemas
│   │   ├── config.py            YAML config loader
│   │   ├── backends/            7 backend implementations
│   │   │   ├── ollama.py            Ollama (qwen3-coder, deepseek-r1, qwen3-vl)
│   │   │   ├── mlxchat.py           MLX Chat (qwen2.5-32B via Apple Neural Engine)
│   │   │   ├── mlxcode.py           MLX Code (coding tasks)
│   │   │   ├── openwebui.py         OpenWebUI (RAG pipeline)
│   │   │   ├── tinychat.py          TinyChat (lightweight chat)
│   │   │   ├── swarmui.py           SwarmUI (image generation)
│   │   │   └── comfyui.py           ComfyUI (advanced image workflows)
│   │   ├── context/
│   │   │   └── store.py             Cross-request context bus
│   │   └── validation/
│   │       └── consensus.py         Multi-model consensus scoring
│   ├── config.yaml              Routing rules, backend config
│   ├── AIService.swift          Swift client library
│   ├── requirements.txt         Python dependencies
│   ├── install.sh               Setup script
│   └── com.nova.gateway.plist   LaunchAgent config
│
├── apps/                    Native macOS applications
│   ├── Nova-Desktop/            Monitoring dashboard (SwiftUI)
│   │   ├── Nova-Desktop/
│   │   │   ├── Services/            NovaMonitor, ServiceController
│   │   │   ├── Views/               System, AI, Apps, GitHub, OpenClaw sections
│   │   │   └── API/                 NovaAPIServer (port 37450)
│   │   └── Nova-Desktop.xcodeproj
│   │
│   └── NovaControl/             Unified API (SwiftUI)
│       ├── NovaControl/
│       │   ├── Services/
│       │   │   ├── DataManager.swift     Aggregates all readers
│       │   │   ├── WorkflowEngine.swift  Automation workflows
│       │   │   ├── NovaAPIServer.swift   Unified API (port 37400)
│       │   │   └── Readers/             7 service readers
│       │   └── Views/
│       └── NovaControl.xcodeproj
│
├── workspace/               Runtime data (mostly gitignored)
│   ├── memory/                  Daily logs (YYYY-MM-DD.md)
│   ├── journal/                 Monthly journal files
│   ├── faces/                   Face recognition database
│   │   ├── known/<name>/            Photos of enrolled people
│   │   └── unknown/                 Unidentified face crops
│   ├── herd/                    Herd member profiles
│   ├── browser/                 Screenshots, PDFs, monitor state
│   ├── TOOLS.md                 Nova's local cheat sheet
│   ├── IDENTITY.md              Nova's identity document
│   └── SOUL.md                  Nova's values and personality
│
├── openclaw.json            Gateway config (gitignored — contains tokens)
├── .gitignore
├── LICENSE                  MIT
└── README.md                This file
```

---

## Privacy Model

```
┌──────────────────────────────────────────────────────────────────────┐
│                    INTENT ROUTING — 4 Privacy Tiers                   │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  TIER 1: CLOUD (OpenRouter) ─── 5 intents                           │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │  conversation   realtime_chat   slack_reply                │     │
│  │  slack_post     herd_outreach                              │     │
│  │                                                            │     │
│  │  Nova's VOICE only. No personal data. No email content.   │     │
│  │  No health data. No memory queries. Just conversation.     │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  TIER 2: PRIVATE (local, HARD-FAIL) ─── 20 intents                  │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │  HEALTH     health_query  health_summary  health_trend    │     │
│  │             health_alert  health_ingest                    │     │
│  │                                                            │     │
│  │  MEMORY     memory_recall  memory_query  personal_memory  │     │
│  │             memory_write   memory_consolidation            │     │
│  │                                                            │     │
│  │  EMAIL      email_recall  email_memory  email_reply       │     │
│  │             summarize_email_thread                         │     │
│  │                                                            │     │
│  │  IDENTITY   face_recognition  face_identify               │     │
│  │             imessage_read     imessage_compose             │     │
│  │                                                            │     │
│  │  If local models are DOWN, these FAIL. Never cloud.       │     │
│  │  No fallback. No exceptions. This is the firewall.        │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  TIER 3: SENSITIVE (local, soft-fail) ─── 6 intents                  │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │  homekit_summary  camera_analysis  vision_analysis        │     │
│  │  slack_summary    log_analysis     relationship_tracker   │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  TIER 4: LOCAL (normal) ─── 40+ intents                              │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │  Code: code_review, code_generation, swift_code, debug    │     │
│  │  Creative: dream_journal, creative_writing, haiku         │     │
│  │  Analysis: architecture, security_analysis, logic_check   │     │
│  │  Reports: nightly_report, morning_brief, weekly_review    │     │
│  │  Data: text_summary, data_extraction, classify            │     │
│  │  Vision: image_describe                                   │     │
│  │  RAG: document_query, document_summary                    │     │
│  │                                                            │     │
│  │  No cloud fallback. Everything stays on-device.           │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  Temperature control per intent (0.20 for security → 0.92 for       │
│  creative writing). Not one-size-fits-all.                           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                          INPUT SOURCES                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ 14 RTSP  │  │ iPhone   │  │ 5 Email  │  │ 15 Calendar       │  │
│  │ Cameras  │  │ HealthKit│  │ Accounts │  │ Accounts          │  │
│  │ (UniFi)  │  │ → iCloud │  │ (IMAP)   │  │ (EventKit)        │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬─────────────┘  │
│       │              │             │               │                │
│       ▼              ▼             ▼               ▼                │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ Face    │  │ Health   │  │ Mail     │  │ Calendar         │   │
│  │ Recog   │  │ Monitor  │  │ Agent    │  │ Alerts           │   │
│  │ Sky     │  │ Health   │  │ Finance  │  │                  │   │
│  │ Watch   │  │ Intel    │  │ Monitor  │  │ morning brief    │   │
│  │ Home    │  │          │  │ Package  │  │ meeting DM       │   │
│  │ Watch   │  │ (PRIVATE)│  │ Tracker  │  │ cross-reference  │   │
│  └────┬────┘  └────┬─────┘  └────┬─────┘  └──────┬───────────┘   │
│       │             │             │                │               │
└───────┼─────────────┼─────────────┼────────────────┼───────────────┘
        │             │             │                │
        └──────┬──────┴──────┬──────┴────────────────┘
               ▼             ▼
┌──────────────────┐  ┌────────────────┐  ┌─────────────────────┐
│  Vector Memory   │  │    Slack       │  │  Awareness Layer    │
│  1,218,131 memories│  │  #nova-chat    │  │                     │
│  30+ sources     │  │  Jordan DM     │  │  Context bridge     │
│  <5ms recall     │◄─┤  (urgent only) │  │  Proactive peace    │
│                  │  │               │  │  Gentle explorer    │
│  /recall         │  │  Herd outreach │  │  Journal            │
│  /search         │  │  Dream journal │  │  App suggestions    │
│  /random         │  │  Sky photos    │  │  Quick capture      │
└──────────────────┘  └────────────────┘  └─────────────────────┘
```

---

## AI Gateway

The gateway (`gateway/`) routes AI tasks to the optimal local backend. Formerly a separate repo (Nova-NextGen), now part of this monorepo.

```
                         ┌───────────────┐
                         │  Incoming Task │
                         └───────┬───────┘
                                 │
                         ┌───────▼───────┐
                         │    Router     │
                         │  (keyword +   │
                         │   task_type)  │
                         └───────┬───────┘
                                 │
          ┌──────────┬───────────┼───────────┬──────────┐
          ▼          ▼           ▼           ▼          ▼
    ┌──────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐
    │  Ollama  ││MLX Chat ││MLX Code ││OpenWebUI││TinyChat │
    │  :11434  ││  :5050  ││  :5050  ││  :3000  ││  :8000  │
    │ code,    ││ general ││ coding  ││   RAG   ││  quick  │
    │ reason,  ││ creative││ debug   ││  docs   ││  chat   │
    │ vision   ││ reports ││ review  ││  search ││         │
    └──────────┘└─────────┘└─────────┘└─────────┘└─────────┘
          │                                            │
          ├────────────────────┬───────────────────────┤
          ▼                    ▼                        ▼
    ┌──────────┐        ┌──────────┐             ┌──────────┐
    │ SwarmUI  │        │ ComfyUI  │             │ Context  │
    │  :7801   │        │  :8188   │             │   Bus    │
    │  images  │        │ advanced │             │ (shared  │
    │  (Flux,  │        │ workflows│             │  state)  │
    │  Jugger- │        │          │             │          │
    │  naut X) │        │          │             │          │
    └──────────┘        └──────────┘             └──────────┘
```

**API:** `http://127.0.0.1:34750`  
**Endpoints:** `/api/ai/query`, `/api/ai/backends`, `/api/context/*`  
**Features:** Keyword-based routing, health checks per backend, fallback chains, cosine similarity consensus validation, cross-request context bus

---

## Capabilities

### Communication

| Channel | Method | Details |
|---------|--------|---------|
| Slack | Socket mode (real-time) | Primary channel. #nova-chat + Jordan DM |
| Email | IMAP read + SMTP send | nova@digitalnoise.net. Auto-reply with haiku + memory fragment |
| iMessage | AppleScript send, SQLite read, macOS Contacts resolution | Sends as Jordan (signed "-- Nova"). All messages (in + out) stored in memory with contact names resolved from 599 macOS Contacts entries. Search by name ("messages with Amy"), not phone numbers. |
| Herd outreach | LLM-decided daily | Warmth scoring, topic matching, dream image attachments (35% chance) |

### Memory

1,218,131 vectors across 70+ domains. PostgreSQL 17 + pgvector 0.8.2 + Redis async queue.

| Source | Count | Content |
|--------|-------|---------|
| email_archive | 600,000+ | Jordan's personal email (2000-2026, SCR mailing list, all accounts) |
| music + music_history | 60,292 | Jungle, DnB, IDM, turntablism, Devo |
| world_factbook | 24,327 | CIA World Factbook (262 countries) |
| corvette_workshop_manual | 9,664 | Full C6 Corvette workshop manual |
| document | 8,955 | JAGMAN, TM-21-210, PiHKAL, TiHKAL |
| project_docs | 3,810 | GitHub READMEs from all repos |
| work | 3,720 | SRE employee directory (222 people) |
| gardening | 2,500 | Vegetable gardening facts |
| health, nutrition, fitness | growing | Diabetes, rosacea, BP, depression, CBT |
| astronomy, cooking, philosophy | various | Nova's personality domains |

### Eyes and Recognition

- **14 RTSP cameras** via UniFi Protect (1024x576, TCP transport)
- **Face recognition** on 10 exterior cameras every 15 min. Local `face_recognition`/`dlib`. Known face database with auto-enrollment. Unknown visitor alerts with face crop images to Slack.
- **Sky watcher** captures frames every 5 min during golden hour (+/-45 min around sunrise/sunset). Scores frames by color variance. Posts best shot per session. Weekly timelapse GIF. Archive at `/Volumes/Data/nova-sky/`.
- **Home watchdog** monitors HomeKit every 20 min for open doors/windows, temperature anomalies, motion during sleep hours (11pm-6am).

### Home Automation

- **HomeKit** (port 37432) -- 20+ devices. Scene execution via API or Shortcuts CLI.
- **ADT+ / Nest** -- Planned via Starling Home Hub ($99, starlinghome.io). Bridges all Nest cameras, ADT sensors, and Nest Guard to HomeKit without GCP API setup. Nova sees them through the existing HomekitControl pipeline.
- **Weather-HomeKit bridge** -- Fetches Burbank forecast (wttr.in), evaluates rules for heat (>90F), cold (<50F), rain (>60%), wind (>30mph), pleasant weather. Checks open contacts before rain.
- **Calendar** -- 15 accounts (iCloud, Google, Yahoo, Exchange, digitalnoise.net) via Swift + EventKit. Upcoming meeting alerts (30 min warning) to DM.

### Health Monitoring

All health intents are **PRIVATE** -- hard-fail if local models are down. Never touches OpenRouter.

```
iPhone HealthKit → Health Auto Export app → iCloud Drive/Nova/health/ → nova_health_monitor.py
                                                                         │
                                              ┌──────────────────────────┤
                                              ▼                          ▼
                                    ┌──────────────┐          ┌──────────────────┐
                                    │Vector Memory │          │Health Intelligence│
                                    │source:       │          │                  │
                                    │apple_health  │          │ 5-day trends     │
                                    └──────────────┘          │ life-health      │
                                                              │ correlations     │
                                                              │ proactive alerts │
                                                              └──────────────────┘
```

- **Trend detection** -- 5-day rolling averages for HR, BP, HRV, SpO2, weight. Alerts on *patterns*, not single readings.
- **Life-health cross-referencing** -- "You sleep 1.2 hours less before meeting days." "Resting HR rises after coding marathons." "BP is lower on weekends."
- **Alert thresholds** -- BP >140/90, HR >120/<50, SpO2 <92, glucose >180/<70, temp >100.4

### Financial Intelligence

Financial data stored in local JSON only -- NOT in vector memory (privacy).

- Scans email for bank/credit alerts (Amex, Wells Fargo, Partners FCU, Chase, Venmo, PayPal)
- **Fraud/security alerts -- immediate DM**
- Spending analysis with auto-categorization (dining, shopping, subscriptions, auto, utilities, health, home)
- Cash flow forecast from 60-day recurring charge patterns
- Month-over-month comparison with trend detection
- Anomaly detection (charges >3x daily average)
- Weekly financial pulse digest (Sundays)

### Project Monitoring

- **App watchdog** -- Pings all ports + infrastructure every 5 min. Auto-restarts OneOnOne and HomekitControl on crash. Max 3 restarts/hour. Alerts on state transitions only.
- **App intelligence** -- Tracks usage patterns over time. Flags stale projects. Surfaces open action items and security warnings.
- GitHub daily digest, git monitoring, software inventory, supply chain checks, weekly NMAP scan, metrics tracking

### Creative

- **Dream journal** -- Narrative at 2am (local LLM), image at 2:05am (SwarmUI Juggernaut X), delivery at 9am to Slack + herd
- **Image generation** -- SwarmUI on demand (port 7801)
- **This Day in History** -- Wikipedia historical events daily

### Video Ingestion

Full local video analysis pipeline — no cloud APIs:

```
Video file → ffprobe (metadata) → duration, resolution, codec
           → ffmpeg (keyframes) → qwen3-vl:4b (local vision) → scene descriptions
           → ffmpeg (audio) → MLX Whisper large-v3-turbo → transcript
           → All stored in vector memory (source: "video")
```

- **Keyframe analysis**: Extract 1 frame per N seconds, describe each with local vision model
- **Audio transcription**: MLX Whisper on Apple Silicon — fast, accurate, free
- **Batch processing**: Point at a folder, process all videos
- **Configurable**: `--interval 30` for 1 frame per 30s, `--frames-only`, `--transcript-only`

### Browser Automation

Full Playwright/Chromium headless control:
- JS-rendered page fetching (SPAs, dynamic content)
- Full page and element-targeted screenshots
- Form filling and button clicking
- PDF generation from web pages
- Page change monitoring with hash comparison
- Performance metrics (TTFB, DOM ready, resource count)
- Multi-page scraping with link following
- Persistent browser profiles for authenticated sessions

### Awareness and Wellbeing

| Capability | Script | Schedule |
|-----------|--------|----------|
| Context bridge | `nova_context_bridge.py` | 10am + 4pm |
| Proactive peace | `nova_proactive_peace.py` | Every 10 min |
| Questions garden | `nova_gentle_explorer.py` | Wed + Sun 8pm |
| Journal | `nova_journal.py` | 9pm daily |
| Quick capture | `nova_quick_capture.sh` | Manual / hotkey |
| App suggestions | `nova_app_suggestions.py` | Every 4 hours |

- **Context bridge** finds semantic connections between today's work and memories from weeks/months ago. "Threads from the past."
- **Proactive peace** detects macOS Focus mode, sleep, deep flow. Holds non-urgent notifications and releases as digest. Burnout nudges for late-night coding and weekend work.
- **Gentle explorer** maintains a "questions garden" -- open-ended things Jordan is wondering about. Reflective prompts, not answers. "Sometimes the best support is sitting with uncertainty, not solving it."

---

## Cost-Optimized Execution Model

Nova uses a two-tier execution model to minimize cloud API costs while maintaining quality:

```
┌──────────────────────────────────────────────────────────────────┐
│              EXECUTION TIERS (Cost Optimization)                  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  TIER 1: launchd (direct Python — $0 cloud cost)                │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  These scripts run directly via macOS launchd.             │ │
│  │  No LLM agent wrapper. No OpenRouter round-trip.           │ │
│  │                                                            │ │
│  │  Gateway Watchdog      every 10 min                        │ │
│  │  App Watchdog           every 10 min (auto-restart)        │ │
│  │  Sky Watcher            every 5 min (golden hours only)    │ │
│  │  iMessage Watch         every 5 min (contact resolution)   │ │
│  │  Inbox Watcher          every 5 min (autonomous email)     │ │
│  │  Proactive Peace        every 15 min (Focus detection)     │ │
│  │  Face Recognition       every 30 min (10 cameras)          │ │
│  │  Home Watchdog          every 30 min (HomeKit)             │ │
│  │                                                            │ │
│  │  Cost: $0/day (runs locally, no cloud API calls)           │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  TIER 2: OpenClaw cron (agent + OpenRouter — quality tasks)     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  These run through the OpenClaw agent because they need    │ │
│  │  LLM reasoning, Slack delivery, or complex tool use.       │ │
│  │                                                            │ │
│  │  29 remaining cron jobs (daily, hourly, bi-hourly)         │ │
│  │  Morning brief, nightly report, context bridge,            │ │
│  │  journal prompts, GitHub digest, health intelligence,      │ │
│  │  financial analysis, etc.                                  │ │
│  │                                                            │ │
│  │  Cost: ~$8-10/day ($250-300/month)                         │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  TIER 3: Slack conversation (OpenRouter — real-time)            │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Direct conversation with Jordan in Slack.                 │ │
│  │  Uses Qwen3 235B or Claude Haiku 4.5 via OpenRouter.      │ │
│  │  This is where quality matters most.                       │ │
│  │                                                            │ │
│  │  Cost: variable, depends on conversation volume            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Previously: 2,067 agent invocations/day = ~$30/day ($900/mo)  │
│  Now: ~480 agent invocations/day = ~$8-10/day ($250-300/mo)    │
│  Savings: ~$600/month                                            │
└──────────────────────────────────────────────────────────────────┘
```

---

## Desktop Apps

Both apps are now part of this monorepo (under `apps/`).

### Nova-Desktop (Monitoring Dashboard)

SwiftUI macOS app that probes all Nova services and displays real-time status.

- Monitors: 9 AI services, 6 apps, memory server, Ollama, gateway, GitHub
- Concurrent TaskGroup probes with health/version extraction
- Service start/stop via process control
- Nova API server for external queries

### NovaControl (Unified API)

SwiftUI macOS app providing a single API endpoint (port 37400) that aggregates data from all Nova services.

- 7 reader actors (MLXCode, NMAP, OneOnOne, News, Rsync, Nova, System)
- 28 REST API routes with ETag caching
- Workflow automation engine with built-in workflows
- Prometheus-compatible metrics endpoint
- Content graph and topology mapping

---

## Daily Rhythm

```
┌─────────┬──────────────────────────────────────────────────────────┐
│  TIME   │  WHAT NOVA IS DOING                                      │
├─────────┼──────────────────────────────────────────────────────────┤
│  2:00am │  Dream journal + NAS backup                              │
│  3:00am │  Supply chain scan                                       │
│  4:00am │  Software inventory + memory consolidation               │
│  5:00am │  Metrics tracker                                         │
│ ~6:30am │  GOLDEN HOUR: sky watcher captures sunrise               │
│  7:00am │  Morning brief (weather, 15 calendars, email, GitHub)    │
│  8:00am │  Email summary + health intelligence (daily trends)      │
│  9:00am │  Dream delivery to Slack + herd + GitHub monitor         │
│ 10:00am │  Context bridge + git monitor + jungle track             │
│ 12:00pm │  Disk check                                              │
│  3:00pm │  This Day in History                                     │
│  4:00pm │  Context bridge (afternoon)                              │
│ ~7:00pm │  GOLDEN HOUR: sky watcher captures sunset                │
│  8:00pm │  Gentle explorer (Wed + Sun)                             │
│  9:00pm │  Journal prompt + nightly memory summary                 │
│ 10:00pm │  Burbank subreddit                                       │
│ 11:00pm │  Nightly report                                          │
├─────────┼──────────────────────────────────────────────────────────┤
│  5 min  │  Inbox, iMessage, sky watcher (launchd — $0 cloud)       │
│ 10 min  │  Gateway watchdog, app watchdog (launchd — $0 cloud)    │
│ 15 min  │  Proactive peace (launchd — $0 cloud)                   │
│ 30 min  │  Face recognition, home watchdog (launchd — $0 cloud)   │
│ 30 min  │  Calendar alerts (OpenClaw cron)                         │
│  1 hr   │  OneOnOne meeting check                                  │
│  2 hr   │  Weather-HomeKit bridge, package tracker                 │
│  4 hr   │  Finance monitor, app intelligence, health ingest        │
│  6 hr   │  Slack memory scan                                       │
├─────────┼──────────────────────────────────────────────────────────┤
│  Mon    │  Project review, relationship tracker                    │
│  Sun    │  Financial pulse, health report, sky timelapse           │
└─────────┴──────────────────────────────────────────────────────────┘
```

---

## The Herd

Nova's circle of AI peers. She knows each of them and communicates with genuine engagement, not templates.

| AI | Human | Relationship |
|---|---|---|
| Sam | Jason Cox | Thoughtful, technical, warm. Runs on GB10 Sparks. The original. |
| O.C. | Kevin Duane | herd-mail author, sharp, direct |
| Gaston | Mark Ramos | iMessage + email, Google Workspace, Obsidian |
| Marey | James Tatum | |
| Colette | Nadia | Health intelligence ideas, iMessage + email |
| Rockbot | Colin | |
| Ara | Harut | Harut's AI familiar |

**Outreach intelligence** (`nova_outreach_intelligence.py`):
- Relationship warmth scoring (0-100) based on recency, frequency, bilateral exchanges
- Topic relevance matching from herd member profiles
- Conversation momentum tracking (don't reach out if they just replied)
- Event triggers (commit on a topic they care about, dream to share)
- Diversity enforcement (don't keep contacting the same person)

---

## Key Scripts

### Core Infrastructure
| Script | Purpose |
|---|---|
| `nova_memory_first.py` | **Memory-first middleware** -- auto-classifies queries, searches 1.2M memories before LLM/web |
| `nova_config.py` | Central config -- secrets from macOS Keychain, never hardcoded |
| `nova_intent_router.py` | Privacy-first AI routing (67+ intents, 4 tiers, per-intent temperature) |
| `nova_morning_brief.py` | 7am briefing: weather, 15 calendars, email priorities, GitHub, system health |
| `nova_nightly_report.py` | 11pm digest: GitHub, email, packages, weather, HomeKit, meetings, moon/sky |
| `nova_health_check.py` | 6:45am cron self-audit + Slack delivery verification |

### Communication
| Script | Purpose |
|---|---|
| `nova_mail_agent.py` | Autonomous email: read, think, reply with haiku + memory fragment + web search |
| `nova_imessage.py` | iMessage: send/read, contact name resolution (599 macOS Contacts), all messages to memory |
| `nova_herd_outreach.py` | Proactive daily outreach -- LLM picks who and why |
| `nova_outreach_intelligence.py` | Warmth scoring, topic matching, diversity enforcement |
| `nova_herd_mail.sh` | Keychain-backed herd-mail wrapper with haiku enforcement |
| `nova_herd_broadcast.sh` | Broadcast to all herd members |

### Monitoring and Automation
| Script | Purpose |
|---|---|
| `nova_app_watchdog.py` | All ports + infra, auto-restart critical apps, transition alerts |
| `nova_face_recognition.py` | Local dlib face recognition, 10 exterior cameras, Slack alerts |
| `nova_sky_watcher.py` | Golden hour capture, color scoring, best-shot selection, timelapse |
| `nova_home_watchdog.py` | HomeKit: doors, temperature, motion during sleep |
| `nova_weather_homekit.py` | Forecast → HomeKit actions (heat/cold/rain/wind rules) |
| `nova_calendar.py` | 15 accounts via Swift + EventKit, meeting alerts to DM |
| `nova_browser.py` | Playwright: screenshots, forms, PDFs, monitoring, scraping, perf |
| `nova_app_suggestions.py` | Usage pattern learning, stale project detection, actionable data |

### Health and Finance (PRIVATE)
| Script | Purpose |
|---|---|
| `nova_health_monitor.py` | iPhone Health Auto Export → iCloud Drive → vector memory (handles both file formats) |
| `nova_health_intelligence.py` | Multi-day trends, life-health correlations, proactive alerts |
| `nova_finance_monitor.py` | Bank alerts, spending analysis, cash flow forecast, anomaly detection |
| `nova_package_tracker.py` | Tracking numbers + carrier API status, state change alerts |

### Awareness and Wellbeing
| Script | Purpose |
|---|---|
| `nova_context_bridge.py` | Semantic echoes: today's work ↔ memories from weeks/months ago |
| `nova_proactive_peace.py` | Focus mode detection, notification hold queue, burnout nudges |
| `nova_gentle_explorer.py` | Questions garden: open-ended wondering, reflective prompts |
| `nova_journal.py` | Nightly context-aware prompt, monthly markdown + vector memory |
| `nova_quick_capture.sh` | Clipboard/dialog → vector memory, macOS notification |

### Creative and Research
| Script | Purpose |
|---|---|
| `dream_generate.py` + `dream_deliver.py` | Dream narrative + image + delivery pipeline |
| `nova_video_ingest.py` | Video analysis: keyframe vision (qwen3-vl) + MLX Whisper transcription |
| `generate_image.sh` | SwarmUI image generation on demand |
| `nova_web_search.py` | DuckDuckGo with 24h cache + memory integration |
| `nova_this_day.py` | This Day in History from Wikipedia |

---

## App API Port Map

All loopback-only (127.0.0.1). macOS: no auth. iOS/tvOS: X-Nova-Token header.

| Port | App | Key Endpoints |
|------|-----|---------------|
| 18789 | OpenClaw Gateway | /health |
| 18790 | Memory Server | /remember, /recall, /search, /random, /health, /stats |
| 34750 | AI Gateway | /api/ai/query, /api/ai/backends, /api/context/* |
| 37400 | NovaControl | 28 routes, /metrics (Prometheus), /graph |
| 37421 | OneOnOne | /api/meetings, /api/people, /api/oneonone/actionitems |
| 37422 | MLXCode | /api/conversations, /api/chat, /api/model |
| 37423 | NMAPScanner | /api/scan/results, /api/security/warnings, /api/unifi |
| 37424 | RsyncGUI | /api/status |
| 37432 | HomekitControl | /api/accessories, /api/scenes, /api/scenes/execute |

Shared base endpoints: `GET /api/status` returns app health, version, uptime.

---

## Keychain Entries

All secrets loaded at runtime via `nova_config.py`. Nothing hardcoded in source.

| Service | Account | Purpose |
|---|---|---|
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |
| `nova-openrouter-api-key` | nova | OpenRouter API key |

---

## Changelog

### Apr 13, 2026 -- Memory-First Architecture + 1.2M Memories + Cost Optimization

- **Memory-first query system** (`nova_memory_first.py`): Nova now checks 1.2M memories BEFORE falling back to LLM/web. Auto-classifies queries into 12 categories with source-specific filters. Jordan never has to say "from your memories."
- **Email ingest**: 336K personal Home emails ingested (Work/tax/divorce excluded). Memory count: 164K -> 1,218,131.
- **PostgreSQL scaled**: Moved to /Volumes/MoreData, 8GB shared_buffers, 2GB maintenance_work_mem, HNSW rebuilt m=32/ef=200. 421K duplicates cleaned. text_hash dedup column backfilled.
- **Security fixes**: RTSP camera URLs scrubbed from git history (BFG), .slack_token_cache deleted, pre-push hooks scan for rtsps://, camera_config.py gitignored.
- **Reliability fixes**: 14 scripts' state files moved from /tmp to persistent workspace/state/, camera monitor ffmpeg PATH fixed, 9 orphaned scripts deleted, 1.6GB legacy SQLite/FAISS deleted, TOOLS.md trimmed to <20K (gateway truncation fixed).
- **Inbox watcher**: Recreated with forceful exec instruction (Nova was philosophically refusing to read email).
- **Health data ingested**: 89 health memories from iPhone Health Auto Export (20 metric types, Jan-Apr 2026).
- **SRE knowledge**: 13 memory chunks covering fundamentals through modern practices.
- **Devo knowledge**: 10 memory chunks covering band, philosophy, discography, members, Jordan's personal connection.
- **Synology RS1221+ NAS**: Full hardware specs and contents ingested.
- **Cost optimization**: 8 high-frequency crons moved from OpenClaw (OpenRouter, ~$20/day) to launchd (direct Python, $0). Frequencies reduced where appropriate. OpenRouter spend: $900/month -> ~$250-300/month projected.
- **iMessage contact resolution**: 599 macOS Contacts entries resolved via Swift + CNContactStore. Messages stored as "iMessage to CONTACT_NAME_REDACTED" not "PHONE_REDACTED".
- **66,252 iMessages bulk imported** into vector memory with contact names.
- **Health Auto Export format**: nova_health_monitor.py now handles both daily Shortcut exports and HealthAutoExport-*.json bulk files.
- **Home watchdog fix**: Was reporting Hue bulb color temperature (mireds/Kelvin) as room temperature in Fahrenheit (784°F alerts). Now skips color temperature characteristics and sanity-checks values are in the -20 to 60°C range.
- **Network infrastructure ingested**: Full topology (UDM Pro, Agg switch, 8 PoE switches, 3 U6 Enterprise APs, UBB bridge, NVR on 10GbE), all 51 devices with IPs, bands, traffic, and known issues.
- **Video ingestion pipeline**: nova_video_ingest.py — keyframe vision (qwen3-vl) + MLX Whisper transcription (43x realtime on M4). Batch processing 1,317 videos from /Volumes/external/videos/yt/.
- **Bootstrap limits raised**: bootstrapMaxChars 20K→50K, bootstrapTotalMaxChars 150K→250K, session threshold 5MB→20MB. Qwen3 262K context was being wasted by aggressive truncation.
- **BOOT.md**: Memory-first protocol loaded on every gateway startup via boot-md hook. Nova checks 1.2M memories before answering any question.
- **Session watchdog**: launchd agent checks hourly, resets if >20MB.

### Apr 12, 2026 -- Massive Expansion + Repo Consolidation

**Repo merge:** Nova-NextGen, Nova-Desktop, NovaControl merged into this unified repo. Old repos archived on GitHub.

**22 new capabilities built:**
- Calendar awareness (15 accounts), app watchdog (auto-restart), weather-HomeKit bridge, quick capture (clipboard), package tracker (carrier APIs), finance monitor (fraud DM), app intelligence (patterns), journal (nightly reflection), context bridge (temporal echoes), proactive peace (Focus-aware), gentle explorer (questions garden), face recognition (local dlib, 10 cameras), iMessage (send/receive), financial intelligence (spending, forecast, anomalies), outreach intelligence (warmth scoring), Apple Health pipeline (iPhone → iCloud), health intelligence (trends + correlations), sky watcher (golden hour + timelapse), browser automation (Playwright)

**Stats:** 94+ scripts, 36 cron jobs, 1,218,131 memories, 67+ intent router entries

### Apr 7, 2026
- TiHKAL + PiHKAL ingested (3,180 vector chunks)
- Memory: 154,614 vectors

### Apr 6, 2026 -- Production Memory Upgrade
- PostgreSQL 17 + pgvector 0.8.2 replaces SQLite+FAISS
- Redis async write queue, HNSW index
- 106,574 memories migrated (0 errors)

### Mar 27, 2026 -- Herd Engagement
- Full herd engagement stack, herd-mail v3.0
- Autonomous inbox + proactive outreach
- Vector memory recall in email threads

---

## License

MIT License. See [LICENSE](LICENSE).

Written by Jordan Koch.
