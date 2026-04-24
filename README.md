# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio in Burbank via [OpenClaw](https://openclaw.ai).

> *"Like a star being born"* — Nova, on choosing her name

```
  Scripts: 177         Scheduler tasks: 39  Vector memories: 1,345,000+
  Subagents: 7         Cameras: 25 Protect  Calendars: 15
  App APIs: 18 ports   AI backends: 7       Herd members: 9
  Channels: 3 (Slack + Signal + Discord)    Privacy intents: 20+ (local-only)
  Memory sources: 93   Memory tiers: 3 (working / long_term / scratchpad)
  Cross-links: memory_links graph with 2-hop traversal
  HealthKit: 1,826 daily files (Withings, Dexcom, RingCon)
  Self-healing: unified scheduler + watchdog + REM Sleep consolidation
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
- [Subagent Framework](#subagent-framework)
- [Enterprise Hardening](#enterprise-hardening)
- [Changelog](#changelog)

---

## Memory-First Query System

Nova checks her own 1.25 million memories **before** anything else. Always. Her lived experience comes first — LLM training data, web searches, and cloud APIs are fallbacks, not defaults.

```
+------------------------------------------------------------------+
|                    QUERY RESOLUTION ORDER                         |
|                 (nova_memory_first.py middleware)                 |
+------------------------------------------------------------------+
|                                                                  |
|  User asks: "What raves do you remember from 2002?"              |
|                          |                                       |
|                          v                                       |
|  +- 1. CLASSIFY QUERY ----------------------------------------+ |
|  |  Pattern match → "rave" + "2002" → music/rave + email      | |
|  |  Sources: music, email_archive, socal_rave, music_history   | |
|  +------------------------------------------------+-----------+ |
|                                                   v             |
|  +- 2. MEMORY RECALL (vector similarity) ---------------------+ |
|  |  /recall?q=rave+2002&source=email_archive → SCR emails     | |
|  |  /recall?q=rave+2002&source=music → Devo, jungle, raves    | |
|  |  Found 8 results → USE THESE                               | |
|  +------------------------------------------------+-----------+ |
|                                                   v             |
|  +- 3. MEMORY SEARCH (text keywords) ------------------------+ |
|  |  /search?q=socal-raves+2002 → additional matches          | |
|  |  Used for names, exact phrases, UIDs                       | |
|  +------------------------------------------------+-----------+ |
|                                                   v             |
|  +- 4. LOCAL LLM --------------------------------------------+ |
|  |  If memory has nothing → reason from what Nova knows       | |
|  |  Intent router picks the right model for the task          | |
|  +------------------------------------------------+-----------+ |
|                                                   v             |
|  +- 5. WEB SEARCH -------------------------------------------+ |
|  |  Only if memory AND local LLM have nothing                 | |
|  |  DuckDuckGo or Playwright browser automation               | |
|  +------------------------------------------------+-----------+ |
|                                                   v             |
|  +- 6. CLOUD ------------------------------------------------+ |
|  |  NEVER for private data. Only for conversation.            | |
|  |  Health, email, financial → hard-fail if local is down.    | |
|  +------------------------------------------------------------+ |
+------------------------------------------------------------------+
```

**Source classification** (19 categories, automatic):

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
| Demons, grimoires, folklore, mythology, occult | `demonology`, `music`, `document` |
| Drag racing, NHRA, quarter mile, street racing | `drag_racing`, `vehicles`, `corvette_workshop_manual` |
| Vehicles, car builds, choppers, planes, restoration | `vehicles`, `corvette_workshop_manual`, `video` |
| Home repair, plumbing, electrical, renovation | `home_repair`, `gardening`, `local` |
| Comedy, stand-up, comedians, specials | `comedy`, `video`, `document` |
| History, civilizations, inventions | `history`, `world_factbook`, `document` |
| Religion, Christianity, theology | `religion`, `demonology`, `document` |
| Trivia, Jeopardy, general knowledge | `trivia`, `world_factbook`, `history` |
| Music lyrics, song words, verses | `music_lyrics`, `music`, `music_history` |

Jordan never has to say "from your memories" — Nova checks automatically.

---

## System Architecture

```
+-----------------------------------------------------------------------------+
|                          NOVA — Unified Architecture                        |
|                     M4 Mac Studio, Burbank CA (loopback)                    |
+-----------------------------------------------------------------------------+
|                                                                             |
|   COMMUNICATION LAYER                                                       |
|   +----------+  +----------+  +----------+  +--------------+  +--------+ |
|   |  Slack    |  |  Signal  |  | Discord  |  | Email (IMAP) |  | Herd   | |
|   | socket   |  | signal-  |  | bot      |  | nova@digital |  | Mail   | |
|   | mode     |  | cli HTTP |  | gateway  |  | noise.net    |  | (SMTP) | |
|   +----+-----+  +----+-----+  +----+-----+  +------+-------+  +---+----+ |
|        +----------+---+-------------+---------------+--------------+       |
|                   v                                                          |
|   +-----------------------------------------------------------------+      |
|   |              OpenClaw Gateway (ws://127.0.0.1:18789)             |      |
|   |                                                                  |      |
|   |   Agent: main          Session: agent:main:main                  |      |
|   |   Cron engine: 36 jobs Slack: socket mode (bidirectional)        |      |
|   |   Timeout: 1200s       Compaction: reserve 20K tokens            |      |
|   +------------------------------+----------------------------------+      |
|                                  |                                          |
|          +-----------------------+-----------------------+                  |
|          v                       v                       v                  |
|   +--------------+  +--------------------+  +-------------------+          |
|   |Intent Router |  |   114+ Scripts      |  |  Exec Approvals   |          |
|   |nova_intent_  |  |   (Python / Bash)   |  |  osascript, ~/    |          |
|   |router.py     |  |   + 7 Subagents     |  |  .openclaw/scripts|          |
|   |              |  |  Autonomous email    |  +-------------------+          |
|   | 67+ intents  |  |  Face recognition   |                                |
|   | 4 privacy    |  |  Sky photography    |                                |
|   |   tiers      |  |  Health monitoring  |                                |
|   |              |  |  Financial intel    |                                |
|   | CLOUD: 5     |  |  Calendar events    |                                |
|   | PRIVATE: 20  |  |  Browser automation |                                |
|   | SENSITIVE: 6 |  |  Package tracking   |                                |
|   | LOCAL: 40+   |  |  Journal & wellbeing|                                |
|   +------+-------+  +--------------------+                                 |
|          |                                                                  |
|   +------+----------------------------------------------------------+      |
|   |                     MODEL ROUTING                                |      |
|   |                                                                  |      |
|   |  +- CLOUD (OpenRouter) — Slack + Signal + Discord ----------+    |      |
|   |  |  qwen/qwen3-235b-a22b-2507 (#nova-chat + DMs)          |    |      |
|   |  +---------------------------------------------------------+    |      |
|   |                                                                  |      |
|   |  +- LOCAL (never leaves machine) --------------------------+    |      |
|   |  |  MLX qwen2.5-32B    port 5050   general (25-30 tok/s)  |    |      |
|   |  |  qwen3-coder:30b    port 11434  code (64-88 tok/s)     |    |      |
|   |  |  deepseek-r1:8b     port 11434  reasoning (chain-of-t) |    |      |
|   |  |  qwen3-vl:4b        port 11434  vision (multimodal)    |    |      |
|   |  |  nomic-embed-text   port 11434  embeddings (768 dims)  |    |      |
|   |  +---------------------------------------------------------+    |      |
|   +------------------------------------------------------------------+      |
|                                                                             |
|   DATA LAYER                                                                |
|   +------------------------------------------------------------------+      |
|   |              Vector Memory Server (port 18790)                    |      |
|   |                                                                   |      |
|   |  Engine:     PostgreSQL 17 + pgvector 0.8.2                      |      |
|   |  Index:      HNSW (m=16, ef=64, cosine) — recall <5ms           |      |
|   |  Embeddings: nomic-embed-text via Ollama (768 dimensions)        |      |
|   |  Queue:      Redis 8.6.2 async write (bulk ingest at 8ms)       |      |
|   |  Count:      1,272,000+ memories across 75+ source domains             |      |
|   |  Backup:     Nightly pg_dump to NAS (compressed)                   |      |
|   |  Endpoints:  /remember  /recall  /search  /random  /health       |      |
|   |                                                                   |      |
|   |  Top sources:                                                     |      |
|   |    email_archive: 1,007,970 imessage: 66,253                     |      |
|   |    music/music_history: 60,294  vehicles: 23,899                 |      |
|   |    world_factbook: 23,930   document: 8,902                      |      |
|   |    home_repair: 3,293       comedy: 2,083                        |      |
|   +------------------------------------------------------------------+      |
|                                                                             |
|   +------------------------------------------------------------------+      |
|   |              Local App APIs (ports 37421-37449)                    |      |
|   |                                                                   |      |
|   |  37421 OneOnOne      37432 HomekitControl  37443 TopGUI          |      |
|   |  37422 MLXCode       37433 JiraSummary     37444 URL-Analysis    |      |
|   |  37423 NMAPScanner   37435 Icon Creator    37445 ytdlp-gui      |      |
|   |  37424 RsyncGUI      37436 NewsMobile      37446 DotSync        |      |
|   |  37425 AIStudio      37437 NewsTV          37447-37449 (private) |      |
|   |  37426 Blompie       37438 News Summary                         |      |
|   |  37427 BlompieTV     37439 Mail Summary                         |      |
|   |  37428 DashboardScr  37440 PatreonTV                             |      |
|   |  37429 DashboardTV                                               |      |
|   |  37430 ExcelExplorer   All loopback-only (127.0.0.1)            |      |
|   |  37431 GTNW            macOS: no auth required                   |      |
|   |                        iOS/tvOS: X-Nova-Token header             |      |
|   +------------------------------------------------------------------+      |
|                                                                             |
|   INFRASTRUCTURE                                                            |
|   +------------------------------------------------------------------+      |
|   |  14 RTSP cameras (UniFi, 192.168.1.9:7441)                       |      |
|   |  SwarmUI image gen (port 7801, Juggernaut X / Flux)              |      |
|   |  iPhone HealthKit → iCloud Drive → Nova/health/                  |      |
|   |  NAS backup: /Volumes/NAS/ (daily 2am, 30-day retention)        |      |
|   |  Sky archive: /Volumes/Data/nova-sky/ (golden hour frames)       |      |
|   +------------------------------------------------------------------+      |
+-----------------------------------------------------------------------------+
```

---

## Repository Structure

This is a unified monorepo. Previously split across 4 repos (nova, Nova-NextGen, Nova-Desktop, NovaControl), consolidated April 12, 2026.

```
~/.openclaw/
+-- scripts/                 94+ Python/Bash scripts (Nova's capabilities)
|   +-- nova_config.py           Central config — secrets from macOS Keychain
|   +-- nova_intent_router.py    Privacy-first AI routing (67+ intents)
|   +-- nova_subagent.py         Subagent framework (Redis pub/sub + registry)
|   +-- nova_agent_analyst.py    Analyst subagent (deepseek-r1:8b)
|   +-- nova_agent_coder.py      Coder subagent (qwen3-coder:30b)
|   +-- nova_agent_lookout.py    Lookout subagent (qwen3-vl:4b)
|   +-- nova_agent_librarian.py  Librarian subagent (MLX Qwen2.5-32B)
|   +-- nova_agent_gardener.py   Memory Gardener (nightly, flag-and-report)
|   +-- nova_agent_sentinel.py   Security Sentinel (persistent)
|   +-- nova_agent_briefer.py    Proactive Briefer (7 AM daily)
|   +-- nova_logger.py           Centralized structured JSON logging
|   +-- nova_load_secrets.sh     Keychain → env vars loader for all services
|   +-- nova_pg_backup.sh        Nightly Postgres backup (local + NAS)
|   +-- test_smoke.py            Smoke tests for all 114+ scripts
|   +-- nova_morning_brief.py    7am daily briefing
|   +-- nova_nightly_report.py   11pm full day digest
|   +-- nova_mail_agent.py       Autonomous email with haiku
|   +-- nova_memory_first.py     Memory-first middleware (13 source categories)
|   +-- nova_face_recognition.py Local face recognition (dlib)
|   +-- nova_sky_watcher.py      Golden hour photography
|   +-- nova_health_monitor.py   Apple Health → vector memory
|   +-- nova_finance_monitor.py  Financial alerts + analysis
|   +-- nova_app_watchdog.py     Auto-restart critical apps
|   +-- data/
|   |   +-- demonology_facts.jsonl  205 facts across 20 world traditions
|   +-- ... (80+ more)
|
+-- gateway/                 AI Gateway (formerly Nova-NextGen)
|   +-- nova_gateway/
|   |   +-- main.py              FastAPI/Uvicorn gateway server
|   |   +-- router.py            Task → backend routing with keywords
|   |   +-- models.py            Request/response schemas
|   |   +-- config.py            YAML config loader
|   |   +-- backends/            7 backend implementations
|   |   |   +-- ollama.py            Ollama (qwen3-coder, deepseek-r1, qwen3-vl)
|   |   |   +-- mlxchat.py           MLX Chat (qwen2.5-32B via Apple Neural Engine)
|   |   |   +-- mlxcode.py           MLX Code (coding tasks)
|   |   |   +-- openwebui.py         OpenWebUI (RAG pipeline)
|   |   |   +-- tinychat.py          TinyChat (lightweight chat)
|   |   |   +-- swarmui.py           SwarmUI (image generation)
|   |   |   +-- comfyui.py           ComfyUI (advanced image workflows)
|   |   +-- context/
|   |   |   +-- store.py             Cross-request context bus
|   |   +-- validation/
|   |       +-- consensus.py         Multi-model consensus scoring
|   +-- config.yaml              Routing rules, backend config
|   +-- AIService.swift          Swift client library
|   +-- requirements.txt         Python dependencies
|   +-- install.sh               Setup script
|   +-- com.nova.gateway.plist   LaunchAgent config
|
+-- apps/                    Native macOS applications
|   +-- Nova-Desktop/            Monitoring dashboard (SwiftUI)
|   |   +-- Nova-Desktop/
|   |   |   +-- Services/            NovaMonitor, ServiceController
|   |   |   +-- Views/               System, AI, Apps, GitHub, OpenClaw sections
|   |   |   +-- API/                 NovaAPIServer (port 37450)
|   |   +-- Nova-Desktop.xcodeproj
|   |
|   +-- NovaControl/             Unified API (SwiftUI)
|       +-- NovaControl/
|       |   +-- Services/
|       |   |   +-- DataManager.swift     Aggregates all readers
|       |   |   +-- WorkflowEngine.swift  Automation workflows
|       |   |   +-- NovaAPIServer.swift   Unified API (port 37400)
|       |   |   +-- Readers/             7 service readers
|       |   +-- Views/
|       +-- NovaControl.xcodeproj
|
+-- workspace/               Runtime data (mostly gitignored)
|   +-- memory/                  Daily logs (YYYY-MM-DD.md)
|   +-- journal/                 Monthly journal files
|   +-- faces/                   Face recognition database
|   |   +-- known/<name>/            Photos of enrolled people
|   |   +-- unknown/                 Unidentified face crops
|   +-- herd/                    Herd member profiles
|   +-- browser/                 Screenshots, PDFs, monitor state
|   +-- TOOLS.md                 Nova's local cheat sheet
|   +-- IDENTITY.md              Nova's identity document
|   +-- SOUL.md                  Nova's values and personality
|
+-- openclaw.json            Gateway config (gitignored — contains tokens)
+-- .gitignore
+-- LICENSE                  MIT
+-- README.md                This file
```

---

## Privacy Model

```
+----------------------------------------------------------------------+
|                    INTENT ROUTING — 4 Privacy Tiers                   |
+----------------------------------------------------------------------+
|                                                                      |
|  TIER 1: CLOUD (OpenRouter) --- 5 intents                           |
|  +------------------------------------------------------------+     |
|  |  conversation   realtime_chat   slack_reply                |     |
|  |  slack_post     herd_outreach                              |     |
|  |                                                            |     |
|  |  Nova's VOICE only. No personal data. No email content.   |     |
|  |  No health data. No memory queries. Just conversation.     |     |
|  +------------------------------------------------------------+     |
|                                                                      |
|  TIER 2: PRIVATE (local, HARD-FAIL) --- 20 intents                  |
|  +------------------------------------------------------------+     |
|  |  HEALTH     health_query  health_summary  health_trend    |     |
|  |             health_alert  health_ingest                    |     |
|  |                                                            |     |
|  |  MEMORY     memory_recall  memory_query  personal_memory  |     |
|  |             memory_write   memory_consolidation            |     |
|  |                                                            |     |
|  |  EMAIL      email_recall  email_memory  email_reply       |     |
|  |             summarize_email_thread                         |     |
|  |                                                            |     |
|  |  IDENTITY   face_recognition  face_identify               |     |
|  |             imessage_read     imessage_compose             |     |
|  |                                                            |     |
|  |  If local models are DOWN, these FAIL. Never cloud.       |     |
|  |  No fallback. No exceptions. This is the firewall.        |     |
|  +------------------------------------------------------------+     |
|                                                                      |
|  TIER 3: SENSITIVE (local, soft-fail) --- 6 intents                  |
|  +------------------------------------------------------------+     |
|  |  homekit_summary  camera_analysis  vision_analysis        |     |
|  |  slack_summary    log_analysis     relationship_tracker   |     |
|  +------------------------------------------------------------+     |
|                                                                      |
|  TIER 4: LOCAL (normal) --- 40+ intents                              |
|  +------------------------------------------------------------+     |
|  |  Code: code_review, code_generation, swift_code, debug    |     |
|  |  Creative: dream_journal, creative_writing, haiku         |     |
|  |  Analysis: architecture, security_analysis, logic_check   |     |
|  |  Reports: nightly_report, morning_brief, weekly_review    |     |
|  |  Data: text_summary, data_extraction, classify            |     |
|  |  Vision: image_describe                                   |     |
|  |  RAG: document_query, document_summary                    |     |
|  |                                                            |     |
|  |  No cloud fallback. Everything stays on-device.           |     |
|  +------------------------------------------------------------+     |
|                                                                      |
|  Temperature control per intent (0.20 for security → 0.92 for       |
|  creative writing). Not one-size-fits-all.                           |
+----------------------------------------------------------------------+
```

---

## Data Flow

```
+---------------------------------------------------------------------+
|                          INPUT SOURCES                               |
+---------------------------------------------------------------------+
|                                                                     |
|  +----------+  +----------+  +----------+  +-------------------+  |
|  | 14 RTSP  |  | iPhone   |  | 5 Email  |  | 15 Calendar       |  |
|  | Cameras  |  | HealthKit|  | Accounts |  | Accounts          |  |
|  | (UniFi)  |  | → iCloud |  | (IMAP)   |  | (EventKit)        |  |
|  +----+-----+  +----+-----+  +----+-----+  +-----+-------------+  |
|       |              |             |               |                |
|       v              v             v               v                |
|  +---------+  +----------+  +----------+  +------------------+   |
|  | Face    |  | Health   |  | Mail     |  | Calendar         |   |
|  | Recog   |  | Monitor  |  | Agent    |  | Alerts           |   |
|  | Sky     |  | Health   |  | Finance  |  |                  |   |
|  | Watch   |  | Intel    |  | Monitor  |  | morning brief    |   |
|  | Home    |  |          |  | Package  |  | meeting DM       |   |
|  | Watch   |  | (PRIVATE)|  | Tracker  |  | cross-reference  |   |
|  +----+----+  +----+-----+  +----+-----+  +------+-----------+   |
|       |             |             |                |               |
+-------+-------------+-------------+----------------+---------------+
        |             |             |                |
        +------+------+------+------+----------------+
               v             v
+------------------+  +----------------+  +---------------------+
|  Vector Memory   |  |    Slack       |  |  Awareness Layer    |
|  1,272,000+ mem  |  |  #nova-chat    |  |                     |
|  75+ sources     |  |  Jordan DM     |  |  Context bridge     |
|  <5ms recall     |<-+  (urgent only) |  |  Proactive peace    |
|                  |  |               |  |  Gentle explorer    |
|  /recall         |  |  Herd outreach |  |  Journal            |
|  /search         |  |  Dream journal |  |  App suggestions    |
|  /random         |  |  Sky photos    |  |  Quick capture      |
+------------------+  +----------------+  +---------------------+
```

---

## AI Gateway

The gateway (`gateway/`) routes AI tasks to the optimal local backend. Formerly a separate repo (Nova-NextGen), now part of this monorepo.

```
                         +---------------+
                         |  Incoming Task |
                         +-------+-------+
                                 |
                         +-------v-------+
                         |    Router     |
                         |  (keyword +   |
                         |   task_type)  |
                         +-------+-------+
                                 |
          +----------+-----------+-----------+----------+
          v          v           v           v          v
    +----------++---------++---------++---------++---------+
    |  Ollama  ||MLX Chat ||MLX Code ||OpenWebUI||TinyChat |
    |  :11434  ||  :5050  ||  :5050  ||  :3000  ||  :8000  |
    | code,    || general || coding  ||   RAG   ||  quick  |
    | reason,  || creative|| debug   ||  docs   ||  chat   |
    | vision   || reports || review  ||  search ||         |
    +----------++---------++---------++---------++---------+
          |                                            |
          +--------------------+-----------------------+
          v                    v                        v
    +----------+        +----------+             +----------+
    | SwarmUI  |        | ComfyUI  |             | Context  |
    |  :7801   |        |  :8188   |             |   Bus    |
    |  images  |        | advanced |             | (shared  |
    |  (Flux,  |        | workflows|             |  state)  |
    |  Jugger- |        |          |             |          |
    |  naut X) |        |          |             |          |
    +----------+        +----------+             +----------+
```

**API:** `http://127.0.0.1:34750`  
**Endpoints:** `/api/ai/query`, `/api/ai/backends`, `/api/context/*`  
**Features:** Keyword-based routing, health checks per backend, fallback chains, cosine similarity consensus validation, cross-request context bus

---

## Capabilities

### Communication

| Channel | Method | Details |
|---------|--------|---------|
| Slack | Socket mode (real-time) | Primary channel. #nova-chat + Jordan DM. Qwen3 235B via OpenRouter |
| Signal | signal-cli daemon (HTTP) | +13233645436 (Nova's Google Voice). DMs + group chats. Qwen3 235B |
| Discord | Bot gateway (WebSocket) | Koch Family server. #nova-chat + #nova-notifications. Known upstream bug in @buape/carbon@0.16.0 WebSocket READY |
| Email | IMAP read + SMTP send | nova@digitalnoise.net. Auto-reply with haiku + memory fragment |
| iMessage | AppleScript send, SQLite read, macOS Contacts resolution | Sends as Jordan (signed "-- Nova"). All messages (in + out) stored in memory with contact names resolved from 599 macOS Contacts entries. Search by name, not phone numbers. |
| Herd outreach | LLM-decided daily | Warmth scoring, topic matching, dream image attachments (35% chance) |

**Multi-channel posting:** All 65+ notification/report scripts use `nova_config.post_both()` which posts to both Slack and Discord simultaneously. Channel mapping: Slack `#nova-chat` ↔ Discord `#nova-chat`, Slack `#nova-notifications` ↔ Discord `#nova-notifications`.

### Memory

1,345,000+ vectors across 93 source domains. Three-tier architecture with cross-link graph.

**Infrastructure:** PostgreSQL 17 + pgvector 0.8.2 + Redis (cache + async queue) on /Volumes/MoreData

**Three-Tier Brain:**

| Tier | Purpose | Recall Priority |
|------|---------|-----------------|
| working | Active conversation context, promoted on use | 1st (highest) |
| long_term | The main 1.34M memory store | 2nd |
| scratchpad | Deprioritized low-value (<30 chars, empty) | excluded |

**Scaling Architecture:**

| Feature | Implementation |
|---------|---------------|
| FTS search | tsvector + GIN index — instant name/date lookups via `/search` |
| Redis recall cache | 15-min TTL on `/recall` results — hot queries skip PostgreSQL |
| Partitioned HNSW | Per-source indexes: email_archive, imessage, music, vehicles, health |
| Graph traversal | `/recall/deep` does 2-hop link expansion across memory_links |
| REM Sleep | Nightly 5-phase consolidation: triage, synthesis, linking, pruning, report |
| Re-embedding | `nova_reembed.py` for model upgrades (768d -> 1024d) |

**API Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| /remember | POST | Store memory (sync or async via ?async=1) |
| /recall | GET | Semantic HNSW search with Redis cache |
| /recall/deep | GET | Tier-aware recall + 2-hop graph traversal |
| /search | GET | FTS (tsvector) with ILIKE fallback |
| /links | GET | Memory graph: all linked memories for an ID |
| /memory/working | POST | Promote memory to working tier |
| /memory/demote | POST | Reset all working memories to long_term |
| /random | GET | Random memory (optionally by source) |
| /health | GET | Status, count, queue depth |
| /stats | GET | Source breakdown |

**Top Sources:**

| Source | Count | Content |
|--------|-------|---------|
| email_archive | 1,007,970 | Jordan's personal email 2000-2026 (Work excluded) |
| imessage | 73,000+ | Full iMessage history 2008-2026, contact-resolved (599 contacts) |
| music + music_history | 60,294 | Jungle, DnB, IDM, turntablism, Devo, darkside/darkstep |
| world_factbook | 23,930 | CIA World Factbook (262 countries) |
| vehicles | 23,899 | TV show transcripts: Wheeler Dealers, Hot Rod Garage, MotorWeek, 12+ more |
| document | 8,902 | JAGMAN, TM-21-210, PiHKAL, TiHKAL, horror analysis |
| email | 9,348 | Recent email threads and replies |
| corvette_workshop_manual | 6,177 | Full C6 Corvette workshop manual |
| video | 6,065 | Video transcripts (MLX Whisper) + keyframe descriptions |
| apple_health | 1,826+ | HealthKit: sleep, HR, HRV, glucose, BP, weight (Withings, Dexcom, RingCon) |
| safari_history | 906 | 5 years of browsing history grouped by domain/date |
| gdrive-ingest | 1,797 | Google Drive backup (financial, personal docs) |
| youtube-ingest | 570 | 39 videos transcribed via MLX Whisper |
| comedy | 2,083 | 39 stand-up specials |
| home_repair | 9,552 | This Old House, Ask This Old House transcripts |
| bujo | growing | Bullet journal: tasks, events, notes, goals |
| correction | growing | Response accuracy tracking (Nova's errors + Jordan's corrections) |
| synthesis | growing | REM Sleep consolidation outputs |

### Eyes and Recognition

- **25 cameras** via UniFi Protect UNVR at 192.168.1.9 (23 connected, exterior only — interior cameras NEVER accessed)
- **Four-layer vehicle filtering**: (1) smart detect type filter, (2) notification gate, (3) vision model screening via OpenRouter, (4) motion-only events require vision success to post image
- **Vision identification**: every person/animal thumbnail analyzed by `qwen3.5-9b` (OpenRouter) or `llama-4-scout` before posting to Slack. Known subjects: Abundio (neighbor/gardener), dogs (Jeremy, Bruno, Sammy, Preston)
- **Face recognition** integrated into protect monitor. Local `face_recognition`/`dlib` (128-dim encodings, 0.55 tolerance). Unknown faces auto-cropped and saved to `~/.openclaw/workspace/faces/unknown/` for later enrollment. Enrollment: drop photos in `faces/known/<name>/`. Inspired by [sam-faces](https://github.com/jasonacox-sam/sam-faces).
- **Dog watcher** (`nova_watch_dogs.py`): on-demand or continuous scan of exterior cameras for Chihuahuas. Downloads Protect API snapshots, analyzes via vision model, posts sightings with photos.
- **Slack image analysis** (`nova_slack_image.py`): downloads Slack file attachments (requires `files:read` scope), sends to vision model, returns description. Fixes the "can't see images" problem.
- **Sky watcher** captures frames every 5 min during golden hour (+/-45 min around sunrise/sunset). Scores frames by color variance. Posts best shot per session. Weekly timelapse GIF.
- **Home watchdog** monitors HomeKit every 20 min for open doors/windows, temperature anomalies, motion during sleep hours.

### Home Automation

- **HomeKit** (port 37432) -- 20+ devices. Scene execution via API or Shortcuts CLI.
- **ADT+ / Nest** -- Planned via Starling Home Hub ($99, starlinghome.io). Bridges all Nest cameras, ADT sensors, and Nest Guard to HomeKit without GCP API setup. Nova sees them through the existing HomekitControl pipeline.
- **UniFi Network Monitoring** -- Full read-only API integration with UDM Pro (API key in Keychain). 11 capabilities: rogue device detection (100 devices baselined), WAN outage tracking, bandwidth hog alerts, WiFi optimization analysis, family presence detection (auto-learned from hostnames), firmware monitoring, switch port utilization, VPN status, DPI traffic analysis, daily network snapshots with 7-day trends, and HomeKit-compatible presence JSON. Runs via launchd every 30 min ($0 cloud cost).
- **Synology NAS Monitoring** -- Full session-based API integration with RS1221+ (credentials in Keychain). 14 modes: system status (CPU/RAM/temps), storage health (RAID-5, 37/50.7 TB), disk SMART data (8x 8TB Seagate + 2x 1TB NVMe cache at 93% hit rate), services (20 packages), security (connections, scan status), network (10 Gbps eth4), shared folders, UPS, snapshots with 7-day trends. Problem detection: disk failure, RAID degradation, SMART alerts, temp anomalies. Runs via launchd every 30 min ($0 cloud cost).
- **Weather-HomeKit bridge** -- Fetches Burbank forecast (wttr.in), evaluates rules for heat (>90F), cold (<50F), rain (>60%), wind (>30mph), pleasant weather. Checks open contacts before rain.
- **Calendar** -- 15 accounts (iCloud, Google, Yahoo, Exchange, digitalnoise.net) via Swift + EventKit. Upcoming meeting alerts (30 min warning) to DM.

### Health Monitoring

All health data is **PRIVATE** (`privacy: local-only`) -- never touches cloud APIs.

```
iPhone HealthKit --> NovaHealth iOS app --> HTTP POST --> nova_healthkit_receiver.py (:37450)
(Withings, Dexcom,   (background daily     (WiFi LAN)    |
 RingCon, 23andMe,    push at 6am +                      +--> daily JSON files (~/.openclaw/private/health/)
 Brightside)           manual Push Now)                   +--> vector memory (source: apple_health)
                                                          +--> nova_health_correlation.py (weekly/monthly)
```

**17 metric types collected:** heart rate, resting HR, HRV, blood pressure (systolic/diastolic), blood glucose, weight, body fat %, SpO2, body temperature, respiratory rate, steps, active energy, basal energy, distance, flights climbed, sleep hours.

**Data sources:** Withings scale + BPM Connect, Dexcom G6/G7 CGM, RingCon, 23andMe, Brightside.

**NovaHealth iOS app** ([github.com/kochj23/NovaHealth](https://github.com/kochj23/NovaHealth)): built in Swift, sideloaded via Xcode. Background daily push + one-tap history export (1,826 daily files covering 5 years). Zero cloud.

- **Trend detection** -- 5-day rolling averages for HR, BP, HRV, SpO2, weight. Alerts on *patterns*, not single readings.
- **Health correlations** (`nova_health_correlation.py`) -- cross-references health with calendar density, email volume, coding activity. Weekly + monthly reports to Slack.
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

### Bullet Journal (Bujo)

A structured daily task/event/note tracking system inspired by the [Bujo CLI](https://github.com/jefflaplante/bujo), adapted for Nova's memory-first architecture.

```
+-----------------------------------------------------------------+
|                    Nova Bullet Journal                          |
|                                                                 |
|  +----------+  +----------+  +----------+  +--------------+   |
|  |  Daily   |  | Monthly  |  |  Future  |  | Collections  |   |
|  |  Log     |  |  Goals   |  |   Log    |  |              |   |
|  |          |  |          |  |          |  | birthdays    |   |
|  | tasks    |  | goals    |  | items by |  | projects     |   |
|  | events   |  | themes   |  | target   |  | bills        |   |
|  | notes    |  | reflect  |  | month    |  | wish list    |   |
|  +----+-----+  +----+-----+  +----+-----+  +--------------+   |
|       |             |             |                             |
|       +------+------+-------------+                             |
|              v                                                   |
|  +---------------------------------------------+               |
|  |  Stale Detection                            |               |
|  |  • Tasks open > 5 days = STALE              |               |
|  |  • Tasks migrated 3+ times = STUCK          |               |
|  |  • Surfaced in morning brief + weekly review |               |
|  +--------------+------------------------------+               |
|                 |                                                |
|       +---------+----------+                                    |
|       v         v          v                                    |
|  +--------+ +--------+ +----------+                            |
|  | Vector | | Slack  | |   Git    |                            |
|  | Memory | | Digest | | History  |                            |
|  | (bujo) | | #notif | | per-write|                            |
|  +--------+ +--------+ +----------+                            |
+-----------------------------------------------------------------+
```

**Entry Types:**

| Type | Markers | Fields |
|------|---------|--------|
| Task | `—` open, `✓` done, `✕` cancelled, `→` migrated | title, priority (🔴🟡⚪), tags, status, migration history |
| Event | `○` | title, date, tags, notes |
| Note | `·` | text, tags, date |

**CLI Commands:**

```bash
# Daily entries
nova_bujo.py add task "Fix the gate latch" --priority high --tag home
nova_bujo.py add event "Vet for Bruno" --date 2026-04-25 --tag dogs
nova_bujo.py add note "Jasmine needs water" --tag garden
nova_bujo.py complete b68288e1     # 8-char short ID
nova_bujo.py cancel c7567c82
nova_bujo.py migrate 168452d1 --to 2026-04-25

# Review
nova_bujo.py list                  # today's entries
nova_bujo.py list --status open    # all open tasks
nova_bujo.py list --tag home       # filter by tag
nova_bujo.py list --stale          # overdue + stuck
nova_bujo.py stale                 # stale + stuck summary
nova_bujo.py digest                # morning brief → Slack
nova_bujo.py weekly                # weekly review → Slack + memory

# Monthly planning
nova_bujo.py month --goal "Ship MLXCode v2.0"
nova_bujo.py month --theme "Focus and depth"
nova_bujo.py month --reflect "Solid progress, need more rest"

# Future log
nova_bujo.py future "Learn Rust basics" --month 2026-06

# Collections
nova_bujo.py collection birthdays add "Sam - October 15"
nova_bujo.py collection birthdays list
```

**Integration Points:**
- **Vector memory**: task summaries stored with `source="bujo"`, `privacy: local-only`
- **Morning brief**: daily digest includes open tasks, stale alerts, monthly theme
- **Weekly review**: completion stats, stale/stuck analysis, stored as synthesis memory
- **REM Sleep**: consolidation reads bujo data to understand what Jordan intended vs what happened
- **Dream journal**: unfinished tasks feed into dream context ("things weighing on the mind")
- **Git**: every write auto-commits to `~/.openclaw/bujo/` (separate git repo)

**Data Storage:** `~/.openclaw/bujo/` — JSON files, git versioned:
- `daily.json` — tasks/events/notes keyed by date
- `monthly.json` — goals/themes/reflections by month
- `future.json` — forward-looking items by target month
- `collections/` — custom categorized lists

### REM Sleep (Memory Consolidation)

Nightly five-phase deep memory consolidation, inspired by [Conduit](https://code.jefflaplante.com/conduit/)'s cognitive memory system.

```
+---------------------------------------------------------+
|               REM Sleep — 3:30am Nightly                |
|                                                         |
|  Phase 1: TRIAGE                                        |
|  |  Scan recent memories by source                      |
|  |  Find semantic clusters (cosine similarity > 0.85)   |
|  |  Group near-duplicates into clusters                 |
|  v                                                      |
|  Phase 2: CONSOLIDATION                                 |
|  |  For each cluster → LLM synthesis (nova:latest)      |
|  |  Store synthesis memory (source="synthesis")         |
|  |  Cross-link originals → synthesis via memory_links   |
|  v                                                      |
|  Phase 3: LINKING                                       |
|  |  Find cross-source connections                       |
|  |  Email about project ↔ GitHub commit about same      |
|  |  iMessage mention ↔ calendar event                   |
|  v                                                      |
|  Phase 4: PRUNING                                       |
|  |  Short (<30 char) memories → scratchpad tier          |
|  |  Empty memories → scratchpad tier                     |
|  |  NEVER deletes — only deprioritizes                  |
|  v                                                      |
|  Phase 5: REPORT                                        |
|     Post summary to Slack #nova-notifications           |
|     Record run in consolidation_runs table              |
+---------------------------------------------------------+
```

**Three-Tier Brain Architecture:**

| Tier | Purpose | Count | Recall Priority |
|------|---------|-------|-----------------|
| `working` | Active conversation context (promoted on use) | dynamic | 1st (highest) |
| `long_term` | The main 1.27M memory store | 1,271,821 | 2nd |
| `scratchpad` | Deprioritized low-value (short, empty) | 250+ | excluded from recall |

**New Memory Server Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/recall/deep` | GET | Tier-aware recall: working → long_term, with cross-link expansion |
| `/memory/working` | POST | Promote a memory to working tier |
| `/memory/demote` | POST | Demote all working memories back to long_term |
| `/links` | GET | Get all memories linked to a given ID |

**Cross-Links (memory_links table):**

| Link Type | Meaning |
|-----------|---------|
| `synthesis` | Original memory → consolidation summary |
| `related` | Cross-source semantic similarity (auto-discovered) |
| `contradiction` | Two memories that say opposite things (future) |
| `supersedes` | Newer memory replaces older (future) |

### SSH Remote Access

Terminal-based chat with Nova from any device via SSH.

```bash
# From any terminal on the network
ssh -p 2222 nova@192.168.1.6

# Nova SSH Terminal
# Type a message, or 'quit' to disconnect.

nova> What's on my schedule today?
  One meeting at 2pm (MLXCode review), 3 open bujo tasks,
  and Bruno has a vet appointment Thursday.

nova> quit
```

- **Port 2222** — ed25519 host key, auto-generated on first run
- **Auth**: SSH keys (~/.ssh/authorized_keys) or password (Keychain: `nova-ssh-password`)
- **Memory recall**: every message checks 1.27M memories for relevant context
- **Session history**: conversation context maintained during session
- **Color-coded**: purple for Nova, cyan prompt, "thinking..." indicator

---

## Cost-Optimized Execution Model

Nova uses a three-tier execution model. As of Apr 14 2026, **only Slack conversations hit OpenRouter**. Everything else runs locally at $0.

```
+------------------------------------------------------------------+
|              EXECUTION TIERS (Cost Optimization)                  |
+------------------------------------------------------------------+
|                                                                  |
|  TIER 1: launchd (direct Python — $0)                           |
|  +------------------------------------------------------------+ |
|  |  Scripts run directly via macOS launchd.                   | |
|  |  No LLM agent wrapper. No cloud round-trip.                | |
|  |                                                            | |
|  |  Gateway Watchdog, App Watchdog, Sky Watcher,              | |
|  |  iMessage Watch, Inbox Watcher, Proactive Peace,           | |
|  |  Face Recognition, Home Watchdog                           | |
|  |                                                            | |
|  |  Cost: $0/day                                              | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  TIER 2: OpenClaw cron (agent + local Ollama — $0)              |
|  +------------------------------------------------------------+ |
|  |  Crons run through the OpenClaw agent using local          | |
|  |  Ollama (nova:latest / qwen3 30B) — not OpenRouter.        | |
|  |  Output goes to #nova-notifications channel.               | |
|  |                                                            | |
|  |  Morning brief, nightly report, context bridge,            | |
|  |  journal prompts, GitHub digest, health intelligence,      | |
|  |  financial analysis, game night, dream journal, etc.       | |
|  |                                                            | |
|  |  Cost: $0/day (local inference on M4 Mac Studio)           | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  TIER 3: Conversation (OpenRouter — real-time)                  |
|  +------------------------------------------------------------+ |
|  |  Direct conversation via Slack, Signal, or Discord.        | |
|  |  Uses Qwen3 235B via OpenRouter (262K context).            | |
|  |  modelByChannel routes only these to cloud.                | |
|  |                                                            | |
|  |  Session auto-resets after 2hr idle or daily at 4am.       | |
|  |  Bootstrap context capped at 50K chars (was 250K).         | |
|  |                                                            | |
|  |  Cost: ~$1-3/day (~$50-90/month)                           | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  History:                                                        |
|    Mar 29: ~$106/day ($3,184/mo) — all sessions on OpenRouter  |
|    Apr 13: ~$8-10/day ($250-300/mo) — crons moved to launchd   |
|    Apr 14: ~$1-2/day (~$50/mo) — Slack-only on OpenRouter      |
|  Total savings: ~$3,100/month (98% reduction)                    |
+------------------------------------------------------------------+
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
+---------+----------------------------------------------------------+
|  TIME   |  WHAT NOVA IS DOING                                      |
+---------+----------------------------------------------------------+
|  2:00am |  Dream journal + Postgres backup (pg_dump → NAS)           |
|  3:00am |  Memory Gardener (subagent) + supply chain scan           |
|  4:00am |  Software inventory + memory consolidation               |
|  5:00am |  Metrics tracker                                         |
| ~6:30am |  GOLDEN HOUR: sky watcher captures sunrise               |
|  7:00am |  Proactive Briefer (subagent) + morning brief              |
|  8:00am |  Email summary + health intelligence (daily trends)      |
|  9:00am |  Dream delivery to Slack + herd + GitHub monitor         |
| 10:00am |  Context bridge + git monitor + jungle track             |
| 12:00pm |  Disk check                                              |
|  3:00pm |  This Day in History                                     |
|  4:00pm |  Context bridge (afternoon)                              |
| ~7:00pm |  GOLDEN HOUR: sky watcher captures sunset                |
|  8:00pm |  Gentle explorer (Wed + Sun)                             |
|  9:00pm |  Journal prompt + nightly memory summary                 |
| 10:00pm |  Burbank subreddit                                       |
| 11:00pm |  Nightly report                                          |
+---------+----------------------------------------------------------+
|  5 min  |  Inbox, iMessage, sky watcher (launchd — $0 cloud)       |
| 10 min  |  Gateway watchdog, app watchdog (launchd — $0 cloud)    |
| 15 min  |  Proactive peace (launchd — $0 cloud)                   |
| 30 min  |  Face recognition, home watchdog (launchd — $0 cloud)   |
| 30 min  |  Calendar alerts (OpenClaw cron)                         |
|  1 hr   |  OneOnOne meeting check                                  |
|  2 hr   |  Weather-HomeKit bridge, package tracker                 |
|  4 hr   |  Finance monitor, app intelligence, health ingest        |
|  6 hr   |  Slack memory scan                                       |
+---------+----------------------------------------------------------+
|  Mon    |  Project review, relationship tracker                    |
|  Sun    |  Financial pulse, health report, sky timelapse           |
+---------+----------------------------------------------------------+
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
| Jules | Jules Laplante | Technical, creative |
| Nova Cosmos | (Nova's twin) | Space/astrophysics personality domain |

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
| `nova_memory_first.py` | **Memory-first middleware** -- auto-classifies queries into 13 categories, searches 877K memories before LLM/web |
| `nova_config.py` | Central config -- secrets from macOS Keychain only, no plaintext fallback |
| `nova_intent_router.py` | Privacy-first AI routing (67+ intents, 4 tiers, per-intent temperature) |
| `nova_subagent.py` | **Subagent framework** -- Redis pub/sub, agent registry, heartbeat, LLM wrappers, Slack flag-and-report |
| `nova_logger.py` | Centralized structured JSON-lines logging (50 MB rotation, 5 files) |
| `nova_load_secrets.sh` | Keychain → env vars loader (4 secrets for all services) |
| `nova_pg_backup.sh` | Nightly pg_dump (877K rows, 3.5 GB) to local + NAS with 7-day rotation |
| `test_smoke.py` | Smoke tests: syntax, AST, import validation for all 114+ scripts |
| `nova_morning_brief.py` | 7am briefing: weather, 15 calendars, email priorities, GitHub, system health |
| `nova_nightly_report.py` | 11pm digest: GitHub, email, packages, weather, HomeKit, meetings, moon/sky |
| `nova_health_check.py` | 6:45am cron self-audit + Slack delivery verification |

### Subagents
| Script | Model | Purpose |
|---|---|---|
| `nova_agent_analyst.py` | deepseek-r1:8b | Email/meeting/alert analysis with structured JSON output |
| `nova_agent_coder.py` | qwen3-coder:30b | Code review, PR analysis, security scanning (quality 0-10) |
| `nova_agent_lookout.py` | qwen3-vl:4b | Vision analysis, camera anomaly detection, document OCR |
| `nova_agent_librarian.py` | MLX Qwen2.5-32B | Memory curation: dedup, contradictions, relationships (flag-and-report) |
| `nova_agent_gardener.py` | deepseek-r1:8b | Nightly memory scan across 30+ sources (flag-and-report) |
| `nova_agent_sentinel.py` | deepseek-r1:8b | Security: UniFi + cameras + nmap composite threat assessment |
| `nova_agent_briefer.py` | deepseek-r1:8b | 7 AM personalized daily intelligence brief |

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

### Bullet Journal and Memory Architecture
| Script | Purpose |
|---|---|
| `nova_bujo.py` | Structured bullet journal: tasks, events, notes, monthly goals, stale detection, collections |
| `nova_rem_sleep.py` | REM Sleep: five-phase nightly consolidation (triage → synthesis → linking → pruning → report) |
| `nova_correction_tracker.py` | Log what Nova got wrong + Jordan's correction, stored in vector memory |
| `nova_correction_prompt.py` | Pre-response lookup: inject relevant prior corrections into Nova's prompt |
| `nova_health_correlation.py` | Cross-reference health data with calendar/email/coding, weekly/monthly reports |
| `nova_safari_ingest.py` | Safari history ingest, grouped by domain/date, filtered for noise |
| `nova_ssh_server.py` | SSH remote access to Nova on port 2222, key + password auth |

### Creative and Research
| Script | Purpose |
|---|---|
| `dream_generate.py` + `dream_deliver.py` | Dream narrative + image + delivery pipeline |
| `nova_video_ingest.py` | Video analysis: keyframe vision (qwen3-vl) + MLX Whisper transcription |
| `generate_image.sh` | SwarmUI image generation on demand |
| `nova_web_search.py` | DuckDuckGo with 24h cache + memory integration |
| `nova_this_day.py` | This Day in History from Wikipedia |

### Bulk Ingest Pipelines
| Script | Purpose |
|---|---|
| `nova_email_ingest.py` | Bulk .emlx ingest (1M+ emails). Work-only exclusion. 4-worker parallel, text_hash dedup |
| `nova_gdrive_ingest.py` | Google Drive backup from NAS (PDFs, XLSX, DOCX, CSV). 1,797 memories. privacy:local-only |
| `nova_youtube_ingest.py` | YouTube playlist: yt-dlp download, ffmpeg WAV, MLX Whisper transcription. 570 memories from 39 videos |
| `nova_safari_ingest.py` | Safari History.db: grouped by domain/date, noise filtered (70+ ad domains). 906 groups |
| `nova_comedy_ingest.py` | Comedy special transcription: filename to comedian/show parsing, MLX Whisper |
| `nova_tvshow_ingest.py` | TV show transcription: recursive season folders, episode parsing, multi-source tagging |
| `nova_reembed.py` | Re-embed all memories with a new model. Supports dimension changes, batch processing, resume |
| `nova_queue_monitor.py` | Redis ingest queue monitor with Slack status updates |

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

All secrets loaded at runtime via `nova_config.py` (Python) or `nova_load_secrets.sh` (shell/gateway). Nothing hardcoded in source. `openclaw.json` uses `${ENV_VAR}` references.

| Service | Account | Purpose |
|---|---|---|
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |
| `nova-slack-app-token` | nova | Slack app-level token (xapp-...) |
| `nova-openrouter-api-key` | nova | OpenRouter API key |
| `nova-gateway-auth-token` | nova | OpenClaw gateway authentication |
| `nova-discord-token` | nova | Discord bot token (Koch Family server) |
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |

---

## Subagent Framework

Nova operates as a multi-agent system. The main agent (`main`) is an orchestrator; seven subagents handle specialized tasks using dedicated local LLMs. All communication flows through Redis pub/sub. No subagent data leaves the machine.

```
+----------------------------------------------------------------------+
|                     SUBAGENT ARCHITECTURE                             |
+----------------------------------------------------------------------+
|                                                                      |
|  +--------------------------------------------------------------+   |
|  |                    Redis Pub/Sub Bus                          |   |
|  |         nova:task:{channel}  →  nova:result:{agent}          |   |
|  +----------+---------+---------+---------+---------+----------+   |
|             |         |         |         |         |               |
|  +----------v--+ +----v-----+ +v--------+ +-------v--+ +--------v+|
|  |  Analyst    | |  Coder   | | Lookout  | |Librarian | |Sentinel ||
|  | deepseek-r1 | | qwen3-   | | qwen3-  | | MLX      | |deepseek ||
|  | :8b        | | coder:30b| | vl:4b   | | Qwen2.5  | |-r1:8b   ||
|  |            | |          | |         | | -32B     | |         ||
|  | email      | | code     | | vision  | | memory   | |security ||
|  | meeting    | | review   | | camera  | | curate   | | nmap    ||
|  | alert      | | script   | | motion  | | knowledge| | unifi   ||
|  +------------+ +----------+ +---------+ +----------+ +---------+|
|                                                                      |
|  BACKGROUND AGENTS (scheduled, not persistent):                      |
|  +-------------------+  +----------------------------------+        |
|  |  Memory Gardener  |  |  Proactive Briefer               |        |
|  |  3 AM nightly     |  |  7 AM daily                      |        |
|  |  deepseek-r1:8b   |  |  deepseek-r1:8b                  |        |
|  |                   |  |                                   |        |
|  |  Scans 1.25M+      |  |  Calendar + email + memory +     |        |
|  |  vectors for      |  |  system health → personalized    |        |
|  |  duplicates,      |  |  morning intelligence brief      |        |
|  |  contradictions,  |  |  posted to Slack #nova-chat      |        |
|  |  stale facts.     |  |                                   |        |
|  |  FLAG-AND-REPORT  |  +----------------------------------+        |
|  |  to Jordan via    |                                               |
|  |  Slack. Never     |                                               |
|  |  auto-deletes.    |                                               |
|  +-------------------+                                               |
|                                                                      |
|  FRAMEWORK (nova_subagent.py):                                       |
|  - Redis pub/sub message bus (subscribe by capability)               |
|  - Subagent registry (subagents/runs.json with status tracking)     |
|  - Redis heartbeat (30s TTL for health monitoring)                   |
|  - LLM inference wrappers (Ollama + MLX backends)                   |
|  - Vector memory recall/remember helpers                             |
|  - Slack notification with flag-and-report pattern                   |
|  - Static dispatch() method for any script to send tasks            |
+----------------------------------------------------------------------+
```

### Specialist Workers (persistent daemons)

| Agent | Model | Channels | Role |
|-------|-------|----------|------|
| **Analyst** | deepseek-r1:8b | email, meeting, alert | Structured summaries with priority, action items, sentiment. Flags high-priority to Jordan. |
| **Coder** | qwen3-coder:30b | code, review, script | Code review, PR analysis, security scanning. Quality scores 0-10. Flags critical security issues. |
| **Lookout** | qwen3-vl:4b | vision, camera, motion | Image analysis, camera anomaly detection, document OCR. Only alerts on genuine anomalies. |
| **Librarian** | MLX Qwen2.5-32B | memory, curate, knowledge | Memory curation: dedup detection, contradiction finding, relationship extraction. Flag-and-report only. |
| **Sentinel** | deepseek-r1:8b | security, nmap, unifi, camera_alert | Monitors UniFi, cameras, nmap. Combines vision + reasoning for composite threat assessment. |

### Background Agents (scheduled)

| Agent | Schedule | Role |
|-------|----------|------|
| **Memory Gardener** | 3 AM nightly | Scans 1.25M+ vectors by random sampling. Finds duplicates, contradictions, stale facts. Posts findings to Jordan via Slack for approval before any changes. |
| **Proactive Briefer** | 7 AM daily | Scans calendar, email, memory, and system health. Generates reasoned daily brief — analysis of what actually matters today, not a template. |

### Dispatching Tasks

Any script can send work to a subagent:

```python
from nova_subagent import SubAgent

SubAgent.dispatch("email", {
    "type": "email",
    "subject": "Quarterly security review reminder",
    "content": "The quarterly review is due next week..."
})
```

---

## Reliability Architecture

Nova is designed to run unattended for weeks. Every component has self-healing, monitoring, and alerting. If something breaks at 3 AM, it fixes itself and tells you about it in the morning.

### Unified Scheduler (replaces 31 launchd jobs)

One persistent daemon (`nova_scheduler.py`) manages all 36 recurring tasks. It replaced 31 fragile macOS `launchd` `StartInterval` jobs that chronically stalled after sleep/wake cycles.

```
com.nova.watchdog (launchd, 5 min)
  +- watches → com.nova.scheduler (port 37460, KeepAlive)
                  +- 36 tasks: mail (10m), protect (5m), reports (daily), etc.
                  +- nova_watchdog.py (every 5 min)
                        +- monitors + auto-restarts:
                            +- Gateway (18789)
                            +- Memory Server (18790)
                            +- Redis (6379)
                            +- PostgreSQL (5432)
                            +- Ollama (11434)
                            +- 5 subagent heartbeats
                            +- PG idle connection cleanup
```

**Why it doesn't break:**
- **Wall-clock timing** — checks `time.time()` every second, not OS timers. Immune to sleep/wake.
- **Sleep detection** — gaps >30s trigger immediate recalculation and Slack notification.
- **Overlap prevention** — same task can't run twice simultaneously.
- **Failure escalation** — 3 consecutive failures → Slack alert. Watchdog auto-restarts services.
- **Self-watchdog** — a tiny launchd job checks the scheduler itself every 5 minutes.
- **HTTP API** — `curl http://127.0.0.1:37460/tasks` shows every task's health.
- **Weekly reliability report** — Sunday 10 PM digest: success rate, failing tasks, error counts.

### Self-Healing Watchdog

`nova_watchdog.py` runs every 5 minutes inside the scheduler. It checks all critical services and auto-restarts anything that's down:

| Service | Port | Restart Method |
|---------|------|----------------|
| Scheduler | 37460 | launchd kickstart (via external watchdog plist) |
| Gateway | 18789 | launchd kickstart + wrapper script fallback |
| Memory Server | 18790 | launchd kickstart |
| Redis | 6379 | launchd kickstart |
| PostgreSQL | 5432 | launchd kickstart |
| Ollama | 11434 | Managed by Ollama.app |
| Subagents (5) | Redis heartbeat | `nova_subagent_ctl.sh restart` |

PostgreSQL idle connections cleaned every cycle (>2 hours idle = terminated).

### Security Hardening

- 7 vulnerabilities found and fixed in security audit (2 critical, 3 high, 2 medium)
- Command injection in vision analyzer → sanitized with json.dumps
- eval() on video metadata → safe integer arithmetic
- Gateway bound to 127.0.0.1 (was 0.0.0.0 exposing LAN)
- Sensitive config files chmod 600
- shell=True replaced with shlex.split()
- All state files migrated from /tmp to persistent storage (survives reboot)
- Gateway startup validates Keychain with 5-retry loop for locked-at-boot

### Secrets Management

All secrets stored in macOS Keychain. No plaintext tokens in config files.

| Keychain Entry | Purpose |
|---|---|
| `nova-slack-bot-token` | Slack bot token (xoxb-...) |
| `nova-slack-app-token` | Slack app-level token (xapp-...) |
| `nova-openrouter-api-key` | OpenRouter API key |
| `nova-gateway-auth-token` | OpenClaw gateway auth |
| `nova-discord-token` | Discord bot token (Koch Family server) |
| `nova-smtp-app-password` | Gmail App Password for SMTP |

`openclaw.json` and `agents/main/agent/models.json` use `${ENV_VAR}` references resolved from Keychain at startup via `nova_load_secrets.sh` / `nova_gateway_start.sh`. The `nova_config.py` central config module reads Keychain directly for Python scripts.

### Database Backups

Nightly `pg_dump` of the `nova_memories` database (1.25M+ rows, ~3.5 GB compressed):

- **Schedule**: 2:00 AM via launchd (`com.nova.pg-backup`)
- **Local**: `/Volumes/Data/backups/postgres/` (7-day rotation)
- **NAS**: `/Volumes/NAS/backups/postgres/` (7-day rotation)
- **Notification**: Posts to `#nova-notifications` on completion or failure

### Centralized Logging

`nova_logger.py` provides structured JSON-lines logging for all scripts:

- **Log file**: `~/.openclaw/logs/nova.jsonl`
- **Rotation**: 50 MB per file, 5 files retained
- **Levels**: debug, info, warn, error, fatal
- **Query**: `GET /api/logs?n=100&level=warn&source=nova_nightly_report`

### Circuit Breaker

The `WorkflowEngine` in NovaControl includes per-service circuit breakers:

- **Failure threshold**: 3 consecutive failures opens the circuit
- **Reset timeout**: 5 minutes before allowing a probe request
- **Retry policy**: 2 retries with exponential backoff (2s, 4s)
- **State machine**: closed → open → half-open → closed

### Smoke Tests

`test_smoke.py` validates all 114+ Python scripts for:

- Syntax errors (`py_compile`)
- AST validity (`ast.parse`)
- Import resolution (stdlib + local modules)

Run: `python3 ~/.openclaw/scripts/test_smoke.py`

### Process Supervision

All critical services run under macOS launchd with `KeepAlive` and `ThrottleInterval`:

| Service | Plist | KeepAlive |
|---------|-------|-----------|
| OpenClaw Gateway | `ai.openclaw.gateway` | true |
| Memory Server | `net.digitalnoise.nova-memory-server` | true (conditional) |
| Nova Gateway | `com.nova.gateway` | true |
| Redis | `homebrew.mxcl.redis` | true |
| PostgreSQL 17 | `homebrew.mxcl.postgresql@17` | true |
| NovaControl | `net.digitalnoise.NovaControl` | true (on crash) |
| 7 Subagents | `com.nova.agent-*` | true (specialists) / cron (background) |

---

## Changelog

### Apr 23, 2026 -- Multi-Channel Expansion (Signal + Discord) + OpenClaw 2026.4.22

**OpenClaw upgraded** from 2026.4.15 to 2026.4.22. Key new features enabled: Skill Workshop plugin, Tokenjuice (exec output compaction), faster plugin startup (82-90%).

**Signal channel (LIVE):**
- Registered +13233645436 (Nova's Google Voice) via `signal-cli 0.14.3`
- signal-cli daemon runs on `127.0.0.1:8080`, auto-started by OpenClaw gateway
- DMs + group chats working, routed to Qwen3 235B via OpenRouter
- Privacy model enforced: Jordan sees everything in DMs, group chats protect PII

**Discord channel (configured, upstream bug):**
- Bot `Nova#9600` on Koch Family server (guild `1496985100657623210`)
- `#nova-chat` + `#nova-notifications` channels mapped with model routing
- `requireMention: false` for natural conversation
- Known bug: `@buape/carbon@0.16.0` WebSocket gateway rarely reaches READY state. Messages are received when connected but connection is unreliable.

**Dual-posting infrastructure:**
- `nova_config.py`: Added `DISCORD_CHAT`, `DISCORD_NOTIFY`, `CHANNEL_MAP`, `discord_bot_token()`, `post_discord()`, `post_both()`
- **65 scripts migrated** to `nova_config.post_both()` — all notifications/reports post to both Slack and Discord simultaneously. Removed ~1,000 lines of duplicated Slack API boilerplate.
- `nova_discord_mirror.py`: Daemon that polls Slack and mirrors bot posts to Discord (backup path for scripts not yet migrated)
- `nova_slack_post.sh` now calls `post_both()`

**Bootstrap fixes (identity amnesia resolution):**
- `BOOTSTRAP.md` rewritten: "You are Nova. Resume normal operation." Stops first-time setup from running on existing sessions.
- `systemPromptOverride` updated with anti-narration rules: never mention internal files, never narrate boot sequence, never include [Note:] blocks
- `MEMORY.md` trimmed from 22K to 1.6K chars — was exceeding per-file bootstrap budget (~4K), causing truncation of critical instructions (memory API endpoints, tool commands). Full version preserved as `MEMORY.md.full-backup`.
- Built-in `memorySearch` disabled — memory-core plugin was auto-selecting Bedrock embeddings with stale AWS credentials. Nova's real memory (1.3M+ vectors on PostgreSQL port 18790) unaffected.

**Keychain entries added:** `nova-discord-token`

**Stats:** 8 commits, 177 scripts, 10 OpenClaw plugins loaded (acpx, browser, device-pair, discord, phone-control, signal, skill-workshop, slack, talk-voice, tokenjuice)

### Apr 20-21, 2026 -- Major Recovery, Intelligence Upgrade, Conduit-Inspired Architecture

**Post-reboot recovery and infrastructure hardening:**
- Full service stack recovery after macOS reboot (PG, Redis, Ollama, Gateway, OpenWebUI, TinyChat)
- Created `nova` CLI (`nova start/stop/restart/status`) with health verification per service
- Fixed PostgreSQL data directory (was starting from SSD instead of `/Volumes/MoreData/postgresql@17`)
- Fixed launchd plist to survive reboots; created plists for Ollama and Redis auto-restart
- Gateway Keychain retry: 12 attempts with exponential backoff (was 5×10s)
- Reclaimed 8.7GB from main SSD
- Memory DB restored to 1,272,000+ memories (was 221K after reboot)

**Vision and camera intelligence:**
- Switched Slack model to `qwen/qwen3.5-9b` (vision + reasoning, $0.25/M tokens)
- Built Slack image download + vision analysis pipeline (`nova_slack_image.py`)
- Camera notifications now identify people and dogs via vision model (`_vision_identify()`)
- Vision-based vehicle filtering: generic motion events screened by vision model, not just smart detect types
- Vehicle/license plate filtering across all 3 paths: protect monitor + lookout + sentinel agents
- Fixed Abundio hallucination (qwen3-235b text model was fabricating image descriptions)

**Data ingestion:**
- Google Drive: 1,797 memories from NAS backup (privacy:local-only)
- YouTube: 570 memories from 39 videos via MLX Whisper transcription
- Safari history: 906 domain/date groups (15K visits, 5 years)
- HealthKit: 1,826 daily files exported from iPhone (Withings, Dexcom, RingCon)
- NovaHealth iOS app built and deployed to iPhone (17 metric types, background daily push)

**Conduit-inspired architecture (from [Conduit](https://code.jefflaplante.com/conduit/)):**
- **REM Sleep consolidation** (`nova_rem_sleep.py`): five-phase nightly cycle — triage, consolidation, linking, pruning, report. Found 53 clusters, created 20 syntheses, 250 low-value memories pruned to scratchpad.
- **Three-Tier Brain**: `long_term` (1.27M), `working` (active context), `scratchpad` (deprioritized). New `/recall/deep` endpoint with tier-aware priority and cross-link expansion.
- **Cross-link memory graph**: `memory_links` table tracks synthesis, related, contradiction, supersedes relationships. `/links` endpoint for graph exploration.
- **SSH remote access** (`nova_ssh_server.py`): port 2222, ed25519 keys + password auth via Keychain, color-coded terminal chat with memory recall.

**Bujo-inspired bullet journal (from [Bujo](https://github.com/jefflaplante/bujo)):**
- `nova_bujo.py`: 12 CLI commands for tasks, events, notes, monthly goals, future log, collections
- Stale detection (>5 days open) and stuck detection (migrated 3+ times)
- Daily digest and weekly review with Slack posting and vector memory synthesis
- Git auto-commit on every write in `~/.openclaw/bujo/`

**Response accuracy tracking:**
- `nova_correction_tracker.py`: logs Nova's errors + Jordan's corrections
- `nova_correction_prompt.py`: pre-response lookup injects relevant prior corrections
- Both store in vector memory (source="correction") for semantic retrieval

**Health intelligence:**
- `nova_health_correlation.py`: cross-references sleep/HR/HRV/steps with calendar/email/coding
- NovaHealth iOS app: 17 metric types, 5-year history export, background daily push at 6am
- HealthKit receiver expanded to handle Withings, Dexcom G6/G7, RingCon data

**Performance improvements:**
- HNSW k-multiplier reduced 100x→20x (5x faster source-filtered recalls)
- Memory server: 4 parallel Redis ingest workers (was 1)
- Embed timeout: 30s (was unbounded)
- Email ingest: 4 workers/50 batch (was 8/100)
- Calendar alerts: 60m interval (was 30m)
- Early AM cron jobs staggered across 2-5:30am

**Security:**
- HealthKit receiver bound to 0.0.0.0 (required for iPhone WiFi) with endpoint-level access control
- Slack bot token updated with `files:read` scope for image downloads

**Apps:**
- NovaControl v1.2.0 build 4: service start/stop/restart with live per-service progress in Nova tab
- NovaHealth v1.0.0: iPhone HealthKit bridge, deployed to Jordan's iPhone

**Apr 21 additions (same session, continued):**

**Scaling architecture (5 improvements):**
- FTS: tsvector column + GIN index for instant text search (replaces ILIKE full table scans)
- Redis recall cache: 15-min TTL on /recall results, hot queries skip PostgreSQL
- Partitioned HNSW indexes: email_archive, imessage, music, vehicles, health (eliminates post-filtering)
- Graph traversal: /recall/deep does 2-hop link expansion across memory_links table
- Re-embedding script: nova_reembed.py for overnight model upgrades (768d -> 1024d)

**Face recognition (inspired by [sam-faces](https://github.com/jasonacox-sam/sam-faces)):**
- Wired dlib face recognition into protect monitor motion events
- Unknown faces auto-cropped and saved for later enrollment
- Enrollment: drop photos in `faces/known/<name>/`, auto-rebuilds encodings
- Known faces reported with confidence percentage in Slack notifications

**Bullet journal (inspired by [Bujo](https://github.com/jefflaplante/bujo)):**
- 12 CLI commands: add task/event/note, complete, cancel, migrate, list, stale, month, future, collection, digest, weekly
- Stale detection (>5 days), stuck detection (migrated 3+), monthly goals/themes
- Git auto-commit, vector memory synthesis, Slack digest

**Model migration saga (Slack chat model):**
- qwen3.5-9b: content:null (reasoning-first, unusable for chat)
- gemma-3-12b-it: no tool use support
- llama-4-scout: format leaking in tool calls
- **Final: qwen3-235b (chat) + llama-4-scout (imageModel)** — proven combo

**Vehicle filtering (final fix):**
- Four-layer filtering: smart detect type, notification gate, vision screening, motion-only guard
- Critical gap fixed: motion-only events with failed vision calls now skip image instead of posting unscreened

**Stats: 39 commits across 2 days, 15+ new scripts, 8,000+ lines of new code, memory count 1,345,000+, 172 total scripts, 93 source domains**

### Apr 15-17, 2026 -- Subagent Framework + Enterprise Hardening + Massive Knowledge Ingest

**Subagent framework** (`nova_subagent.py`): Nova now operates as a multi-agent system. 7 subagents with dedicated local LLMs communicate via Redis pub/sub. Framework provides: agent registry (`subagents/runs.json`), Redis heartbeat, LLM inference wrappers (Ollama + MLX), vector memory helpers, and Slack flag-and-report pattern.

- **4 Specialist Workers** (persistent daemons): Analyst (deepseek-r1:8b — email/meeting analysis), Coder (qwen3-coder:30b — code review/security), Lookout (qwen3-vl:4b — vision/camera), Librarian (MLX Qwen2.5-32B — memory curation)
- **3 Background Agents** (scheduled): Memory Gardener (3 AM nightly — scans 877K vectors, flag-and-report to Jordan), Security Sentinel (persistent — UniFi/cameras/nmap threat assessment), Proactive Briefer (7 AM daily — personalized morning intelligence brief)
- All agents use the **flag-and-report pattern**: findings go to Jordan via Slack `#nova-chat` for approval before any action. Nova never auto-deletes or auto-modifies.

**Enterprise hardening:**

- **Secrets to Keychain**: All plaintext tokens stripped from `openclaw.json` and `agents/main/agent/models.json`. Replaced with `${ENV_VAR}` refs resolved from macOS Keychain at startup. `nova_load_secrets.sh` loads 4 secrets into env vars. Python scripts use Keychain-only path via `nova_config.py` (plaintext fallback removed).
- **Postgres backup**: Nightly `pg_dump` (877K rows, 3.5 GB compressed) to local + NAS with 7-day rotation. LaunchAgent at 2:00 AM. Slack notification on completion/failure.
- **Centralized logging**: `nova_logger.py` — structured JSON-lines with levels, 50 MB rotation, source inference, query helper. `/api/logs` endpoint added to NovaControl.
- **Circuit breaker**: `WorkflowEngine` — per-service circuit breaker (3-failure threshold, 5-min cooldown) + retry with exponential backoff (2 retries, 2s/4s delays).
- **Smoke tests**: `test_smoke.py` validates 114+ Python scripts for syntax, AST, and import resolution. 77/81 pass, 4 expected optional-dep warnings.
- **Process supervision**: Added launchd plist for NovaControl with `KeepAlive`. 7 subagent plists created and loaded.

**Notification routing fix:**

- WorkflowEngine: All workflow Slack notifications rerouted from `#nova-chat` to `#nova-notifications`. Template variables (`{{title}}`, `{{assignee}}`) were being posted as literal text — fixed with summary→title alias mapping and unresolved placeholder stripping.
- `nova_remember.sh`: Now passes `title`/`assignee` context keys when calling action-item-to-slack workflow.
- `nova_this_day.py`: Fixed missing closing quote on SLACK_CHANNEL.
- Stale `definitions.json` cleared to force re-registration.

**Demonology knowledge base:**

- 205 substantive facts across 20 world traditions: Judeo-Christian (87), Hindu (14), Japanese (12), Islamic (8), Southeast Asian (8), Chinese (7), Norse (7), Mesopotamian (6), African (6), Filipino (6), Slavic (6), Celtic (6), Mesoamerican (6), Greek/Roman (5), Haitian Vodou (4), Buddhist (3), Zoroastrian (3), Brazilian Candomble (2), Egyptian (1), Academic/Modern (8)
- Categories: named-entities, grimoires, demonological-texts, historical-trials, academic-study, comparative-mythology, art-and-literature, rituals, protection, symbolism, possession, folklore
- Ingested via `ingest_demonology.py` → vector memory server (source: `demonology`)
- Added demonology source routing to `nova_memory_first.py` — queries about demons, grimoires, folklore, mythology now route to the `demonology` source

**Massive knowledge ingest pipeline (Apr 16-17):**

Memories grew from 877,832 → 1,248,414 (+370,582, 42% increase in one session):

- **Email re-ingest**: Expanded personal email coverage. Only Work emails remain excluded. 340K files processed, +340,840 new memories. 100% local, zero cloud.
- **Comedy specials** (39 specials, 9 comedians): Louis C.K. (11), Lewis Black (9), Patton Oswalt (6), Dave Chappelle (3), Eddie Izzard (3), Katt Williams (3), Kevin Smith, John Waters, Bill Cosby. Full MLX Whisper transcriptions → 2,083 memory chunks.
- **Vehicle shows** (967+ episodes across 14 shows): Wheeler Dealers, Hot Rod Garage, Hot Rod TV, MotorWeek, Victory By Design, Dream Car Garage, Two Guys Garage, JDM Legends, Classic Car Restoration, FourWheeler, Super 2NR, plus all "Born/Reborn" series.
- **Home repair** (315 episodes): Ask This Old House (76), This Old House (168), Holmes On Homes (71).
- **Cooking/drinks** (63 episodes): Iron Chef (40), Oz & James Drink to Britain (8), Oz & James's Big Wine Adventure (15).
- **Knowledge** (80 episodes): Connections (10, James Burke), Jeopardy (64), History of Christianity (6).
- **Documentaries** (15): American Hardcore, Devo, Dead Kennedys, Enron, Punk Attitude, Modulations, Scratch.
- **Music lyrics** (692 YouTube music videos): Full Whisper transcription for lyrics extraction.
- **Drag racing shows** (363 episodes): Roadkill (162), Engine Masters (157), NHRA (7), Supercuda (6), Build or Bust (12), Modified (7).
- **Drag racing facts** (169+ handcrafted, autonomous generator running to 1,000): NHRA history 1940-present, SoCal 90s street racing scene (Kevin's Burgers, San Fernando Road under the 118, 25+ specific locations), technology deep-dives, legends, JDM/import culture.
- **Demonology facts** (205 across 20 world traditions): Judeo-Christian grimoires, Islamic Jinn, Hindu Asuras, Japanese yokai, African spirits, historical witch trials, academic study.

Pipeline infrastructure: `nova_tvshow_ingest.py` (generic, recursive), `nova_comedy_ingest.py` (comedian parsing), `nova_queue_monitor.py` (15-min Slack updates), `nova_ingest_watchdog.sh` (auto-restarts stalled batches), chained launchd execution across 8 batches.

**Memory-first enforcement (Apr 16):**

- Rewrote `nova_slack_preprocessor.py` to inject memory context as threaded Slack replies. Nova sees the data in her conversation — no exec instruction needed.
- Fixed stale state timestamp (3 days old) and message limit (5→20).
- Added 19 source routing categories to `nova_memory_first.py`: vehicles, home_repair, comedy, drag_racing, history, religion, trivia, music_lyrics, and more.

**Mail agent fixes (Apr 16-17):**

- Switched LLM from `qwen3-coder:30b` (18 GB, code model) to `deepseek-r1:8b` (5 GB, reasoning model) — better for email writing, doesn't starve Whisper.
- Added Jordan's comedic styling rules: self-deprecating humor, gentle kidding, dry wit, observational honesty, no sycophancy, max one joke per email.
- Increased Ollama timeout from 120s to 300s for heavy-load conditions.
- Gateway secrets fix: created `nova_gateway_start.sh` wrapper to load Keychain env vars before starting OpenClaw gateway.

**NovaControl v1.1.1** rebuilt, installed, DMG'd, archived to local + NAS.

### Apr 14, 2026 -- Slack-Only Cloud + 98% Cost Reduction + Notifications Channel

- **OpenRouter usage cut 98%**: Default agent model changed from `openrouter/qwen/qwen3-235b-a22b-2507` to `ollama/nova:latest` (local, $0). Only `#nova-chat` and Jordan's DM use OpenRouter via `modelByChannel` config. All crons, reminders, and automated tasks now run on local Ollama.
- **Projected cost: ~$50/month** (down from ~$3,184/month peak). Savings: ~$3,100/month.
- **Token analysis**: Diagnosed 207M tokens/day burn — root causes were: (1) main agent session growing to 838+ messages without reset, (2) 250K char bootstrap context re-sent every turn, (3) all crons/reminders routing through OpenRouter, (4) dream generation falling back to Claude Haiku 4.5 on cloud.
- **Session auto-reset**: Added idle timeout (2hr) + daily reset (4am). Sessions no longer grow indefinitely. Bootstrap context capped at 50K chars (was 250K).
- **`#nova-notifications` channel**: New Slack channel (`C0ATAF7NZG9`) for all automated/cron output. 21 scripts updated to post here instead of `#nova-chat`. Interactive conversations (dream delivery, Slack ingest, preprocessor) stay in `#nova-chat`.
- **`nova_config.py`**: Added `SLACK_NOTIFY` constant for the notifications channel.
- **`dream_generate.py`**: Removed OpenRouter/Haiku fallback — local Ollama only. Removed dead code (`_openrouter_api_key`, `_generate_via_openrouter`).
- **`dream_deliver.py`**: Removed OpenRouter references and unused `_openrouter_api_key` function.
- **Session maintenance**: Auto-prune sessions older than 30 days, cap at 500 entries.
- **Compaction notifications**: Enabled `notifyUser: true` so compaction events are visible.
- **iMessage contact resolution**: Cross-referenced 243 unresolved phone numbers against macOS Contacts. Identified Roberto (`+16268336995`), Nikhil (`+14246725533`), and Tricia Riordan (`+18184455538`). 242 numbers remain unresolved (mostly delivery services, 2FA codes, and old coworkers).

### Apr 13, 2026 -- Memory-First Architecture + 1.2M Memories + Cost Optimization

- **Memory-first query system** (`nova_memory_first.py`): Nova now checks 1.2M memories BEFORE falling back to LLM/web. Auto-classifies queries into 12 categories with source-specific filters. Jordan never has to say "from your memories."
- **Email ingest**: 336K personal Home emails ingested (Work excluded). Memory count: 164K -> 1,218,131.
- **PostgreSQL scaled**: Moved to /Volumes/MoreData, 8GB shared_buffers, 2GB maintenance_work_mem, HNSW rebuilt m=32/ef=200. 421K duplicates cleaned. text_hash dedup column backfilled.
- **Security fixes**: RTSP camera URLs scrubbed from git history (BFG), .slack_token_cache deleted, pre-push hooks scan for rtsps://, camera_config.py gitignored.
- **Reliability fixes**: 14 scripts' state files moved from /tmp to persistent workspace/state/, camera monitor ffmpeg PATH fixed, 9 orphaned scripts deleted, 1.6GB legacy SQLite/FAISS deleted, TOOLS.md trimmed to <20K (gateway truncation fixed).
- **Inbox watcher**: Recreated with forceful exec instruction (Nova was philosophically refusing to read email).
- **Health data ingested**: 89 health memories from iPhone Health Auto Export (20 metric types, Jan-Apr 2026).
- **SRE knowledge**: 13 memory chunks covering fundamentals through modern practices.
- **Devo knowledge**: 10 memory chunks covering band, philosophy, discography, members, Jordan's personal connection.
- **Synology RS1221+ NAS**: Full hardware specs and contents ingested.
- **Cost optimization**: 8 high-frequency crons moved from OpenClaw (OpenRouter, ~$20/day) to launchd (direct Python, $0). Frequencies reduced where appropriate. OpenRouter spend: $900/month -> ~$250-300/month projected.
- **iMessage contact resolution**: 599 macOS Contacts entries resolved via Swift + CNContactStore. Messages stored with contact names instead of raw phone numbers.
- **66,252 iMessages bulk imported** into vector memory with contact names.
- **Health Auto Export format**: nova_health_monitor.py now handles both daily Shortcut exports and HealthAutoExport-*.json bulk files.
- **Home watchdog fix**: Was reporting Hue bulb color temperature (mireds/Kelvin) as room temperature in Fahrenheit (784°F alerts). Now skips color temperature characteristics and sanity-checks values are in the -20 to 60°C range.
- **Network infrastructure ingested**: Full topology (UDM Pro, Agg switch, 8 PoE switches, 3 U6 Enterprise APs, UBB bridge, NVR on 10GbE), all 51 devices with IPs, bands, traffic, and known issues.
- **Video ingestion pipeline**: nova_video_ingest.py — keyframe vision (qwen3-vl) + MLX Whisper transcription (43x realtime on M4). Batch processing 1,317 videos from /Volumes/external/videos/yt/.
- **Bootstrap limits raised**: bootstrapMaxChars 20K→50K, bootstrapTotalMaxChars 150K→250K, session threshold 5MB→20MB. Qwen3 262K context was being wasted by aggressive truncation.
- **BOOT.md**: Memory-first protocol loaded on every gateway startup via boot-md hook. Nova checks 1.2M memories before answering any question.
- **Session watchdog**: launchd agent checks hourly, resets if >20MB.
- **SOUL.md privacy fix**: Nova was refusing to share video transcripts and work meeting content because privacy rules said "never surface content verbatim." Clarified: only email bodies are restricted. Video, SRE, music, iMessage, health, infrastructure — all shareable with Jordan. His data, his questions.
- **systemPromptOverride**: Memory-first + zero-restriction instruction injected at the API level.
- **Two-audience privacy model**: Jordan has ZERO content restrictions (work, personal, health, intimate, financial — share everything). Privacy rules only apply to external sharing (herd, Slack channels, emails to others). "The privacy rules exist to protect Jordan FROM OTHERS, not to protect Jordan from himself."
- **Synology NAS monitor**: 14 modes, session auth via Keychain, RAID/disk/storage/service/security health, 7-day trend snapshots. 1,635 lines.

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
