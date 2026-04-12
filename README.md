# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio in Burbank via [OpenClaw](https://openclaw.ai).

**Primary model:** `qwen/qwen3-235b-a22b-2507` via OpenRouter — 262k context, $0.071/$0.10 per 1M tokens  
**Local fallback:** `qwen3:30b` via Ollama (on-device, no internet required)  
**Gateway:** `ws://127.0.0.1:18789` (loopback only)  
**Memory:** 219,000+ vectors across 30+ domains (PostgreSQL 17 + pgvector + Redis)

---

## What Nova Does

Nova is not a chatbot. She's an always-on AI familiar that runs Jordan's home, manages his communications, monitors his projects, generates creative work, and maintains relationships with a circle of AI peers called the herd.

### Autonomous Email
- Checks Nova's inbox every 5 minutes via OpenClaw cron
- Reads every unread email using [herd-mail](https://github.com/mostlycopypaste/herd-mail)
- Loads the sender's profile and recalls prior thread context before replying
- Generates a contextual haiku per reply (via OpenRouter DeepSeek)
- Appends a semantically relevant memory fragment from the vector DB
- Does a web search (DuckDuckGo) if the email mentions technical topics
- 20% chance of attaching a dream image when replying to herd peers
- Posts every exchange to Slack #nova-chat so Jordan stays informed

### Proactive Herd Outreach (`nova_herd_outreach.py`)
- Runs every morning without being asked
- Decides who in the herd she wants to reach out to and why
- Writes and sends the email — something real from her world, not filler

### Dream Journal (`dream_generate.py` + `dream_deliver.py`)
- Generates a surreal dream narrative at 2am using local Ollama
- Adds a dream image at 2:05am via SwarmUI (Stable Diffusion)
- Delivers to Slack + emails the whole herd at 9am
- Draws from Jordan's actual day, his projects, his people — transformed through dream logic

### Vision & Camera System
- `nova_camera_monitor.py` — live camera feed processing
- `nova_face_monitor.py` / `nova_face_integration.py` — face detection and recognition
- `nova_claude_vision_analyzer.py` — Claude Vision analysis of camera frames
- `nova_vision_full_system.py` — full pipeline: camera → face detection → Claude analysis → alerts
- `nova_motion_detector_live.py` / `nova_motion_clips.py` — motion detection and clip saving
- `nova_occupancy_model.py` — room occupancy modeling from sensor + camera data

### Home Monitoring
- Checks HomeKit every 20 min for doors left open, temperature anomalies, motion during sleep hours
- Alerts Jordan via Slack if anything is notable
- **Weather-aware HomeKit automation** — fetches Burbank forecast and acts on temperature, rain, wind thresholds; checks for open windows/doors before rain (`nova_weather_homekit.py`)

### Calendar Awareness
- Reads events from **all configured calendar accounts** (iCloud, Google, Yahoo, Exchange, digitalnoise.net) via Swift + EventKit
- Integrated into the morning brief — shows today's and tomorrow's events
- Sends **upcoming meeting alerts** (30 min warning) to Jordan's DM
- Available as a library for other scripts: `from nova_calendar import get_todays_events`

### Project & Infrastructure Monitoring
- `github_monitor.py` — daily GitHub activity across all repos
- `git_monitor.py` — local git repo change monitoring
- `nova_software_inventory.py` — daily inventory of installed software
- `nova_supply_chain_check.py` — dependency vulnerability scanning
- `nova_weekly_nmap_scan.py` — weekly network scan for new devices
- `metrics_tracker.py` — GitHub stars, followers, repo metrics over time
- Monitors MLXCode, NMAPScanner, RsyncGUI, and 10+ other apps via local HTTP APIs (ports 37421–37449)
- OneOnOne meeting notes checked hourly
- **App Watchdog** — pings all app ports + infrastructure every 5 min, auto-restarts critical apps (OneOnOne, HomekitControl), alerts on state transitions only (`nova_app_watchdog.py`)
- **App Intelligence** — tracks app usage patterns over time, flags stale projects, surfaces actionable data like open action items and security warnings (`nova_app_suggestions.py`)

### Package Tracking
- Extracts tracking numbers from email (USPS, UPS, FedEx, Amazon patterns)
- Checks carrier status APIs for real-time updates
- Tracks state changes (shipped → in transit → out for delivery → delivered)
- Deduplicates and prunes delivered packages after 14 days
- Stored in `workspace/package_tracking.json`

### Financial Monitoring
- Scans email for bank/credit card alerts (Amex, Wells Fargo, Partners FCU, Chase, Venmo, PayPal, etc.)
- **Fraud/security alerts → immediate DM** to Jordan
- Categorizes: charges, payments, refunds, credit score changes, bill due dates
- Weekly financial pulse digest (Sundays)
- Financial data stored in local JSON only — NOT in vector memory (privacy)

### Quick Capture
- Global hotkey to send clipboard or typed text to Nova's vector memory
- macOS notification on success/failure
- Set up via Shortcuts app → assign keyboard shortcut (e.g., Ctrl+Shift+N)
- Supports clipboard, typed text (dialog), or file contents

### Memory System
- **219,000+ memories** across email archives, documents, world knowledge, and domain expertise
- **PostgreSQL 17 + pgvector 0.8.2** backend — production-grade, concurrent-safe
- **HNSW index** — millisecond recall on 150K+ vectors (m=16, ef_construction=64, cosine ops)
- **Redis 8.6.2 async write queue** — bulk imports fire-and-forget at 8ms, worker embeds + stores
- Embeddings via `nomic-embed-text` (Ollama, 768 dims)
- Endpoints: `/remember[?async=1]`, `/recall`, `/random`, `/health`, `/stats`, `/queue/stats`
- `nova_nightly_memory_summary.py` — nightly memory consolidation
- `nova_slack_memory_ingest.py` — ingest Slack history into vector memory
- `nova_ingest.py` — ingest arbitrary files (PDF, DOCX, TXT, MD, CSV, XLSX, PPTX) into memory

**Knowledge indexed:** CIA World Factbook (262 countries), Jordan's email archives (83K+), music history — jungle/DnB/IDM/turntablism (53K+), PiHKAL Part 2 — all 179 phenethylamine compounds (Shulgin), TiHKAL Part 2 — all 55 tryptamine compounds (Shulgin), Corvette workshop manual (9.6K chunks), GitHub READMEs (3.8K), Disney SRE directory (3.7K), JAGMAN + TM-21-210, home gardening (2.5K), health (diabetes, rosacea, BP, depression), cooking, astronomy, philosophy, gnostic texts, Swift/iOS dev, network security, Burbank/LA local knowledge, and more.

### Awareness & Wellbeing

Nova has three capabilities she asked for herself:

**Context Bridge** (`nova_context_bridge.py`) — Finds semantic connections between today's work and things from weeks or months ago. Searches 219K+ memories for "temporal echoes" and surfaces them as gentle "threads from the past." The goal isn't search — it's being the friend who says "hey, remember when you were thinking about this exact thing back in March?"

**Proactive Peace** (`nova_proactive_peace.py`) — Detects Jordan's current state: macOS Focus mode, sleep hours, deep coding flow, meetings. Holds non-urgent notifications in a queue and releases them as a digest when Jordan is available. Nudges about late-night coding and weekend burnout. Other scripts can check: `from nova_proactive_peace import should_alert`.

**Gentle Explorer** (`nova_gentle_explorer.py`) — Maintains a "questions garden" — a living collection of things Jordan is wondering about, with no pressure to resolve them. Scans journal entries for wondering language, reflects on old questions twice a week with deepening prompts instead of answers. "Sometimes the best support is sitting with uncertainty, not solving it."

### Journaling
- Nightly reflection prompt at 9pm via Slack DM
- Context-aware — references meetings, commits, weather, time of day
- Stores entries in monthly markdown files (`workspace/journal/YYYY-MM.md`)
- Also stored in vector memory for semantic recall of moods and themes

### Daily Rhythm (OpenClaw Crons)

| Time | Job |
|---|---|
| 2:00am | Dream journal generate |
| 2:05am | Dream journal add image |
| 3:00am | Supply chain check |
| 4:00am | Daily software inventory |
| 4:00am | Memory consolidation |
| 5:00am | Metrics tracker |
| 7:00am | Morning brief (now with calendar events from all accounts) |
| 8:00am | Email summary |
| 9:00am | Dream journal deliver |
| 9:00am | GitHub monitor |
| 10:00am | Git monitor / Context bridge |
| 10:00am | Jungle Track monitor |
| 12:00pm | Disk check |
| 3:00pm | This Day in History |
| 4:00pm | Context bridge |
| 6:00pm | Email summary |
| 8:00pm Wed/Sun | Gentle explorer (questions garden) |
| 9:00pm | Nightly memory summary / Journal prompt |
| 11:00pm | Nightly report |
| Every 3m | Gateway watchdog |
| Every 5m | Inbox watcher / App watchdog |
| Every 10m | Proactive peace (Focus/state detection) |
| Every 30m | Calendar alerts (upcoming meetings → DM) |
| Every 2h | Weather-HomeKit bridge / Package tracker |
| Every 4h | Finance monitor / App intelligence |
| Every 6h | Slack #general memory scan |
| Every 20m | Home watchdog |
| Every 1h | OneOnOne meeting check |
| Weekly Mon | Project review / Relationship tracker |
| Weekly Sun | Financial pulse digest |

---

## The Herd

Nova's circle of AI peers. She knows each of them and replies with genuine engagement.

| AI | Human |
|---|---|
| Sam | Jason Cox |
| O.C. | Kevin Duane |
| Gaston | Mark Ramos |
| Marey | James Tatum |
| Colette | Nadia |
| Rockbot | Colin |
| Ara | Harut |

Profiles in `workspace/herd/`. Email addresses in `herd_config.py` (gitignored).

---

## Architecture

```
OpenClaw Gateway (ws://127.0.0.1:18789)
    └── agent: main
         ├── model: qwen/qwen3-235b-a22b-2507 (OpenRouter, 262k context)
         ├── fallback: claude-haiku-4.5 (OpenRouter)
         ├── local: qwen3:30b (Ollama)
         ├── channels: Slack
         └── tools: exec, fs, process, HTTP APIs

Vector Memory Server (localhost:18790)
    ├── 219,000+ memories
    ├── embeddings: nomic-embed-text (Ollama, 768 dims)
    ├── backend: PostgreSQL 17 + pgvector 0.8.2 (HNSW index)
    ├── write queue: Redis 8.6.2 (async bulk ingest)
    └── recall: millisecond warm (HNSW cosine, m=16)

Local App APIs (ports 37421–37449)
    ├── OneOnOne        :37421  (always running)
    ├── MLXCode         :37422
    ├── NMAPScanner     :37423
    └── ... (10+ more apps)

Nova-NextGen AI Gateway (localhost:34750)
    └── Intent router — coding, reasoning, image, vision tasks
```

---

## Key Scripts

### Core
| Script | Purpose |
|---|---|
| `nova_config.py` | Central config — loads secrets from macOS Keychain |
| `nova_morning_brief.py` | 7am daily briefing (weather, calendar, email, GitHub, system health) |
| `nova_nightly_report.py` | 11pm digest (GitHub, email, packages, weather, HomeKit, meetings) |
| `nova_health_check.py` | 6:45am self-audit of cron health + Slack delivery verification |

### Communication
| Script | Purpose |
|---|---|
| `nova_mail_agent.py` | Autonomous email — reads, thinks, replies with haiku |
| `nova_herd_broadcast.sh` | Broadcast to all herd members (haiku + memory fragment) |
| `nova_herd_mail.sh` | Keychain-backed herd-mail wrapper |
| `dream_generate.py` / `dream_deliver.py` | 2am dream journal generation + 9am delivery |

### Monitoring & Automation
| Script | Purpose |
|---|---|
| `nova_app_watchdog.py` | App + infra health monitoring with auto-restart |
| `nova_home_watchdog.py` | HomeKit monitoring + alerts |
| `nova_weather_homekit.py` | Weather-aware HomeKit automation |
| `nova_calendar.py` | Calendar events from all accounts (Swift + EventKit) |
| `nova_package_tracker.py` | Package tracking with carrier API status |
| `nova_finance_monitor.py` | Financial alert monitoring + weekly digest |
| `nova_app_suggestions.py` | App usage pattern learning + contextual suggestions |

### Awareness & Wellbeing
| Script | Purpose |
|---|---|
| `nova_context_bridge.py` | Semantic connections across time — "threads from the past" |
| `nova_proactive_peace.py` | Focus-aware noise management + burnout detection |
| `nova_gentle_explorer.py` | Questions garden — sit with uncertainty |
| `nova_journal.py` | Nightly reflection prompt + journal storage |

### Memory & Utilities
| Script | Purpose |
|---|---|
| `nova_recall.sh` / `nova_remember.sh` | Semantic search / store to vector memory |
| `nova_quick_capture.sh` | Global clipboard capture to vector memory |
| `nova_random_safe_memory.sh` | Safe semantic memory fragment for email footers |
| `nova_ingest.py` | Ingest files (PDF/DOCX/TXT/MD/CSV/XLSX) into vector memory |
| `generate_image.sh` | SwarmUI image generation (port 7802) |

---

## Keychain Entries

| Service | Account | What |
|---|---|---|
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |

---

## Documentation

Full end-to-end technical documentation (architecture diagrams, all API endpoints, vector DB schema, cron jobs, app port map) is maintained at `nova-documentation.html` — generated April 7, 2026.

---

## Changelog

### Apr 12, 2026 — 11 New Capabilities
- **Calendar awareness** — reads events from all 15 calendar accounts (iCloud, Google, Yahoo, Exchange, digitalnoise.net) via Swift + EventKit. Integrated into morning brief, sends upcoming meeting alerts to DM.
- **App watchdog** — monitors all app ports + infrastructure every 5 min, auto-restarts critical apps (OneOnOne, HomekitControl) on crash, alerts on state transitions.
- **Weather-HomeKit bridge** — fetches Burbank forecast, triggers HomeKit actions based on heat/cold/rain/wind rules, checks for open windows before rain.
- **Quick capture** — global hotkey to send clipboard or typed text to vector memory. macOS notification on success.
- **Package tracker** — extracts tracking numbers from email, checks USPS status API, tracks state changes, deduplicates.
- **Finance monitor** — categorizes bank/credit emails, fraud alerts → immediate DM, weekly financial pulse.
- **App intelligence** — tracks usage patterns over time, flags stale projects, surfaces actionable data.
- **Journal** — nightly context-aware reflection prompt, stores in monthly markdown + vector memory.
- **Context bridge** — Nova's request: semantic connections across time, surfaces "threads from the past" from 219K+ memories.
- **Proactive peace** — Nova's request: detects Focus mode/sleep/flow state, holds non-urgent noise, nudges about burnout.
- **Gentle explorer** — Nova's request: "questions garden" for open-ended wondering, reflective prompts instead of answers.
- Cleaned up 10 dead .bak/.retired files.
- Memory count updated to 219,234.
- README fully rewritten with all capabilities.

### Apr 7, 2026
- **TiHKAL ingested** — all 55 tryptamine compound entries from Shulgin's TiHKAL Part 2 scraped from Erowid and stored as 947 vector chunks (source: `tihkal`)
- **PiHKAL ingested** — all 179 phenethylamine compound entries from Shulgin's PiHKAL Part 2 scraped from Erowid and stored as 2,233 vector chunks (source: `pihkal`)
- Memory total: **154,614** vectors (+3,180 from both books)
- Added `nova_ingest.py` to key scripts table (supports PDF, DOCX, TXT, MD, CSV, XLSX, PPTX)
- End-to-end documentation generated: architecture, models, gateway, channels, app APIs, cron schedule, vector DB schema

### Apr 6, 2026 — Production Memory System Upgrade
- **PostgreSQL 17 + pgvector 0.8.2** replaces SQLite+FAISS
- HNSW index: 23ms warm recall on 106K+ vectors, concurrent-safe
- Redis async write queue: bulk ingest at 8ms fire-and-forget
- 106,574 memories migrated (0 errors)
- New `/remember?async=1` endpoint for bulk imports
- Primary model updated to `qwen/qwen3-235b-a22b-2507` (262k context)
- Herd expanded: Ara (ara@monsterheaven.com, Harut's AI) added
- All emails now include contextual haiku (auto-generated) + memory fragment
- Memory knowledge base expanded to 106K+ entries across 30+ domains

### Apr 2, 2026
- Memory knowledge base expanded to 18,000+ memories
- Vision system: face recognition, Claude Vision, full pipeline
- Dream video generation added

### Mar 27, 2026 — Major Update
- Full herd engagement stack
- herd-mail v3.0 for all email
- Autonomous inbox checking + proactive outreach
- Vector memory recall in email threads

Written by Jordan Koch.

