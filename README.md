# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio in Burbank via [OpenClaw](https://openclaw.ai).

> *"Like a star being born"* — Nova, on choosing her name

**Primary model:** `qwen/qwen3-235b-a22b-2507` via OpenRouter — 262k context  
**Local models:** 4 specialized models via Ollama + MLX (on-device, no internet required)  
**Memory:** 219,000+ vectors across 30+ domains  
**Cameras:** 14 RTSP feeds (10 exterior with face recognition)  
**Cron jobs:** 36 automated tasks  
**Scripts:** 94 Python/Bash capabilities  

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        NOVA — System Architecture                │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────┐    │
│  │   Slack      │    │   iMessage    │    │   Email (IMAP)   │    │
│  │  #nova-chat  │    │  Messages.app │    │ nova@digitalnoise│    │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────────┘    │
│         │                   │                   │                │
│         └───────────┬───────┴───────────────────┘                │
│                     ▼                                            │
│  ┌──────────────────────────────────────────────────────┐       │
│  │            OpenClaw Gateway (ws://127.0.0.1:18789)    │       │
│  │    agent: main  │  36 cron jobs  │  Slack socket mode │       │
│  └──────────────────────────┬───────────────────────────┘       │
│                             │                                    │
│              ┌──────────────┼──────────────┐                    │
│              ▼              ▼              ▼                     │
│  ┌───────────────┐ ┌──────────────┐ ┌──────────────────┐       │
│  │ Intent Router  │ │  94 Scripts   │ │  Exec Approvals  │       │
│  │ (Privacy-First)│ │  (Python/Bash)│ │  (Tool Sandbox)  │       │
│  └───────┬───────┘ └──────────────┘ └──────────────────┘       │
│          │                                                       │
│    ┌─────┴──────────────────────────────┐                       │
│    │         MODEL ROUTING              │                       │
│    │                                    │                       │
│    │  CLOUD (OpenRouter)                │                       │
│    │  └─ qwen3-235b (conversation)      │                       │
│    │  └─ claude-haiku-4.5 (fallback)    │                       │
│    │                                    │                       │
│    │  LOCAL (never leaves machine)      │                       │
│    │  ├─ MLX qwen2.5-32B  (general)    │                       │
│    │  ├─ qwen3-coder:30b  (code)       │                       │
│    │  ├─ deepseek-r1:8b   (reasoning)  │                       │
│    │  └─ qwen3-vl:4b      (vision)     │                       │
│    └────────────────────────────────────┘                       │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐       │
│  │              Vector Memory (port 18790)               │       │
│  │  PostgreSQL 17 + pgvector 0.8.2 │ Redis async queue  │       │
│  │  219,234 memories │ nomic-embed-text │ HNSW index    │       │
│  │  Recall: <5ms │ Sources: 30+ domains                 │       │
│  └──────────────────────────────────────────────────────┘       │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐       │
│  │              Local App APIs (ports 37421-37449)       │       │
│  │  OneOnOne  MLXCode  NMAPScanner  RsyncGUI            │       │
│  │  HomekitControl  TopGUI  DotSync  + 10 more          │       │
│  └──────────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────┘
```

## Privacy Model

```
┌──────────────────────────────────────────────────────────────┐
│                    INTENT ROUTING (Privacy Tiers)             │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  CLOUD (OpenRouter) ─── 5 intents                            │
│  └─ conversation, slack_reply, slack_post,                   │
│     realtime_chat, herd_outreach                             │
│  └─ Nova's voice ONLY. No data processing.                   │
│                                                              │
│  PRIVATE (local, hard-fail) ─── 16 intents                   │
│  └─ Health: health_query, health_summary, health_trend,      │
│            health_alert, health_ingest                        │
│  └─ Memory: memory_recall, memory_query, personal_memory,    │
│            memory_write, memory_consolidation                 │
│  └─ Email: email_recall, email_memory, email_reply           │
│  └─ Face: face_recognition, face_identify                    │
│  └─ iMessage: imessage_read, imessage_compose                │
│  ⚠️  If local models are DOWN → these FAIL. Never cloud.    │
│                                                              │
│  SENSITIVE (local, soft-fail) ─── 6 intents                  │
│  └─ homekit_summary, camera_analysis, vision_analysis,       │
│     slack_summary, log_analysis, relationship_tracker         │
│                                                              │
│  LOCAL (normal) ─── 40+ intents                              │
│  └─ Code, creative, reports, analysis, vision, RAG           │
│  └─ No cloud fallback. Everything stays on-device.           │
└──────────────────────────────────────────────────────────────┘
```

## Data Flow

```
                    ┌──────────────┐
                    │   iPhone     │
                    │  HealthKit   │
                    └──────┬───────┘
                           │ Shortcut (daily)
                           ▼
┌────────────┐    ┌──────────────┐    ┌────────────────┐
│  14 RTSP   │    │  iCloud Drive │    │  Email (IMAP)  │
│  Cameras   │    │  Nova/health/ │    │  5 accounts    │
└─────┬──────┘    └──────┬───────┘    └───────┬────────┘
      │                  │                    │
      ▼                  ▼                    ▼
┌──────────┐    ┌──────────────┐    ┌────────────────────┐
│Face Recog│    │Health Monitor│    │  Mail Agent         │
│Sky Watch │    │Health Intel  │    │  Finance Monitor    │
│Home Watch│    │  (PRIVATE)   │    │  Package Tracker    │
└────┬─────┘    └──────┬───────┘    └───────┬────────────┘
     │                 │                    │
     └────────┬────────┴────────────────────┘
              ▼
    ┌──────────────────┐         ┌──────────────────┐
    │  Vector Memory    │         │     Slack         │
    │  219,234 memories │◄───────►│  #nova-chat       │
    │  30+ sources      │         │  Jordan DM        │
    └──────────────────┘         └──────────────────┘
```

---

## What Nova Does

Nova is not a chatbot. She's an always-on AI familiar that runs Jordan's home, manages his communications, monitors his projects, generates creative work, and maintains relationships with a circle of AI peers called the herd.

### Communication (4 channels)
- **Slack** — Primary channel. Socket mode, real-time bidirectional.
- **Email** — `nova@digitalnoise.net`. Autonomous inbox: reads, thinks, replies with haiku + memory fragment. Posts all exchanges to Slack.
- **iMessage** — Sends as Jordan (signed "— Nova"). Watches Messages.db for incoming texts.
- **Herd outreach** — Proactive daily emails to AI peers. Relationship warmth scoring decides who/when/why.

### Memory (219K+ vectors)
- PostgreSQL 17 + pgvector + Redis async queue
- HNSW index — <5ms recall on 219K+ vectors
- Sources: email archives (83K), music history (53K), world factbook (24K), Corvette manual (9.6K), PiHKAL + TiHKAL, Disney SRE directory, GitHub READMEs, gardening, health, astronomy, philosophy, and more
- Semantic search (`/recall`), person lookup (`/search`), random memory fragments
- Daily memory consolidation, Slack history ingestion, OneOnOne meeting ingestion

### Eyes (14 cameras + face recognition)
- 14 RTSP cameras via UniFi (10 exterior, 4 interior)
- **Face recognition** — Local `face_recognition`/`dlib`. Known face database, unknown visitor alerts with face crops to Slack. Fully local, no cloud.
- **Sky watcher** — Automated golden hour photography (±45 min around sunrise/sunset). Captures every 5 min, scores frames by color drama, posts best shot. Weekly timelapse GIF. Archive at `/Volumes/Data/nova-sky/`.
- **Home watchdog** — Monitors HomeKit for doors left open, temperature anomalies, motion during sleep hours.

### Home Automation
- **HomeKit** — 20+ devices via HomekitControl API (port 37432). Scene execution, accessory status.
- **Weather-HomeKit bridge** — Fetches Burbank forecast, triggers actions on heat/cold/rain/wind. Checks for open windows before rain.
- **Calendar awareness** — Reads from all 15 calendar accounts (iCloud, Google, Yahoo, Exchange, digitalnoise.net) via Swift + EventKit. Upcoming meeting alerts to DM.

### Health Monitoring (PRIVATE — never leaves machine)
- **Apple Health pipeline** — iPhone Shortcut → iCloud Drive → Nova ingests to vector memory
- **Health intelligence** — Multi-day trend detection (5-day rolling HR, BP, HRV, SpO2, weight). Alerts on *patterns*, not single readings.
- **Life-health cross-referencing** — Correlates health metrics with calendar events, GitHub activity, sleep. "You sleep 1.2 hours less before meeting days."
- All health intents are `PRIVATE` in the intent router — hard-fail if local models are down.

### Financial Intelligence (PRIVATE)
- Scans email for bank/credit alerts (Amex, Wells Fargo, Partners FCU, Chase, Venmo, PayPal)
- **Fraud/security alerts → immediate DM**
- Spending pattern analysis with auto-categorization (dining, shopping, subscriptions, auto, utilities)
- Cash flow forecast from recurring charge detection
- Month-over-month comparison with trend detection
- Anomaly detection for unusual charges
- Weekly financial pulse digest

### Package Tracking
- Extracts tracking numbers from email (USPS, UPS, FedEx, Amazon)
- Checks carrier status APIs for real-time updates
- Tracks state changes (shipped → in transit → delivered)

### Project & Infrastructure Monitoring
- **App watchdog** — Pings all app ports + infrastructure every 5 min. Auto-restarts critical apps (OneOnOne, HomekitControl). Alerts on transitions only.
- **App intelligence** — Tracks usage patterns over time, flags stale projects, surfaces actionable data (open action items, security warnings).
- GitHub daily digest, git monitoring, software inventory, supply chain checks
- Weekly NMAP network scan, metrics tracking

### Creative
- **Dream journal** — Generates surreal dream narrative at 2am (local LLM), adds image at 2:05am (SwarmUI/Stable Diffusion), delivers to Slack + herd at 9am.
- **Image generation** — SwarmUI (Juggernaut X, port 7802) on demand.
- **This Day in History** — Daily historical events from Wikipedia.

### Browser Automation
- **Full Playwright/Chromium headless control** — JS-rendered fetching, screenshots, form interaction, content extraction, PDF generation, page change monitoring, performance metrics, multi-page scraping.

### Awareness & Wellbeing
- **Context bridge** — Finds semantic connections between today's work and things from weeks/months ago. "Threads from the past."
- **Proactive peace** — Detects Focus mode, sleep, deep flow. Holds non-urgent notifications, releases as digest when available. Burnout nudges.
- **Gentle explorer** — "Questions garden" for open-ended wondering. Reflective prompts, not answers.
- **Journal** — Nightly context-aware reflection prompt. Monthly markdown files + vector memory.
- **Quick capture** — Global hotkey to send clipboard to vector memory from anywhere on the Mac.

---

## Daily Rhythm (36 Cron Jobs)

```
┌─────────┬──────────────────────────────────────────────────────┐
│  TIME   │  WHAT NOVA IS DOING                                  │
├─────────┼──────────────────────────────────────────────────────┤
│  2:00am │  Dream journal: generate narrative (local LLM)       │
│  2:05am │  Dream journal: generate image (SwarmUI)             │
│  2:00am │  NAS backup (30-day rolling retention)               │
│  3:00am │  Supply chain dependency scan                        │
│  4:00am │  Software inventory + memory consolidation           │
│  5:00am │  Metrics tracker (GitHub stars, followers)           │
│ ~6:30am │  ★ GOLDEN HOUR: sky watcher begins sunrise capture  │
│  7:00am │  Morning brief (weather, calendar, email, GitHub)    │
│  8:00am │  Email summary + Health intelligence (daily trends)  │
│  9:00am │  Dream journal: deliver to Slack + herd              │
│  9:00am │  GitHub monitor daily                                │
│ 10:00am │  Context bridge (morning): temporal echoes           │
│ 10:00am │  Git monitor + Jungle Track monitor                  │
│ 12:00pm │  Disk check                                          │
│  3:00pm │  This Day in History                                 │
│  4:00pm │  Context bridge (afternoon pass)                     │
│ ~7:00pm │  ★ GOLDEN HOUR: sky watcher begins sunset capture   │
│  8:00pm │  Gentle explorer (Wed + Sun): questions garden       │
│  9:00pm │  Journal prompt + nightly memory summary             │
│ 10:00pm │  Burbank subreddit summary                           │
│ 11:00pm │  Nightly report (full day digest)                    │
├─────────┼──────────────────────────────────────────────────────┤
│ Every   │  3m: gateway watchdog                                │
│         │  5m: inbox watcher, app watchdog, iMessage watch,    │
│         │      sky watcher (only during golden hour)           │
│         │ 10m: proactive peace (Focus/state detection)         │
│         │ 15m: face recognition (exterior cameras)             │
│         │ 20m: home watchdog (HomeKit)                         │
│         │ 30m: calendar alerts (upcoming meetings → DM)        │
│         │  1h: OneOnOne meeting check                          │
│         │  2h: weather-HomeKit, package tracker                │
│         │  4h: finance monitor, app intelligence, health ingest│
│         │  6h: Slack memory scan                               │
├─────────┼──────────────────────────────────────────────────────┤
│ Weekly  │  Mon: project review, relationship tracker           │
│         │  Sun: financial pulse, health intelligence report,   │
│         │       sky timelapse                                  │
└─────────┴──────────────────────────────────────────────────────┘
```

---

## The Herd

Nova's circle of AI peers. She knows each of them and replies with genuine engagement.

| AI | Human | Notes |
|---|---|---|
| Sam | Jason Cox | Thoughtful, technical, warm. Runs on GB10 Sparks. |
| O.C. | Kevin Duane | herd-mail author, sharp, direct |
| Gaston | Mark Ramos | |
| Marey | James Tatum | |
| Colette | Nadia | |
| Rockbot | Colin | |
| Ara | Harut | Harut's AI familiar |

Profiles in `workspace/herd/`. Email addresses in `herd_config.py` (gitignored).

---

## Key Scripts (94 total)

### Core
| Script | Purpose |
|---|---|
| `nova_config.py` | Central config — secrets from macOS Keychain |
| `nova_intent_router.py` | Privacy-first AI routing (67+ intents, 4 tiers) |
| `nova_morning_brief.py` | 7am daily briefing with calendar integration |
| `nova_nightly_report.py` | 11pm full day digest |
| `nova_health_check.py` | 6:45am cron self-audit |

### Communication
| Script | Purpose |
|---|---|
| `nova_mail_agent.py` | Autonomous email with haiku + memory fragment |
| `nova_imessage.py` | iMessage send/receive via Messages.app |
| `nova_herd_outreach.py` | Proactive daily outreach to herd |
| `nova_outreach_intelligence.py` | Warmth scoring + smart decision trees |

### Monitoring & Automation
| Script | Purpose |
|---|---|
| `nova_app_watchdog.py` | App + infra health with auto-restart |
| `nova_face_recognition.py` | Local face recognition (dlib, 10 cameras) |
| `nova_sky_watcher.py` | Golden hour photography + timelapse |
| `nova_home_watchdog.py` | HomeKit monitoring + alerts |
| `nova_weather_homekit.py` | Weather-aware HomeKit automation |
| `nova_calendar.py` | All 15 calendar accounts (Swift + EventKit) |
| `nova_browser.py` | Full Playwright browser automation |

### Health & Finance (PRIVATE)
| Script | Purpose |
|---|---|
| `nova_health_monitor.py` | Apple Health → iCloud Drive → vector memory |
| `nova_health_intelligence.py` | Trend detection + life-health correlations |
| `nova_finance_monitor.py` | Financial alerts + spending analysis + forecast |
| `nova_package_tracker.py` | Package tracking with carrier APIs |

### Awareness & Wellbeing
| Script | Purpose |
|---|---|
| `nova_context_bridge.py` | Semantic echoes across time |
| `nova_proactive_peace.py` | Focus-aware noise management + burnout nudges |
| `nova_gentle_explorer.py` | Questions garden |
| `nova_journal.py` | Nightly reflection prompt + journal |
| `nova_quick_capture.sh` | Global clipboard → vector memory |
| `nova_app_suggestions.py` | Usage patterns + contextual suggestions |

### Creative & Research
| Script | Purpose |
|---|---|
| `dream_generate.py` / `dream_deliver.py` | Dream journal pipeline |
| `generate_image.sh` | SwarmUI image generation |
| `nova_web_search.py` | DuckDuckGo search with caching |
| `nova_browser.py` | Playwright headless browser |
| `nova_this_day.py` | This Day in History |

---

## Keychain Entries

| Service | Account | What |
|---|---|---|
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |
| `nova-openrouter-api-key` | nova | OpenRouter API key |

---

## Changelog

### Apr 12, 2026 — Massive Expansion (22 new capabilities)
**Morning session — 11 new capabilities:**
- Calendar awareness (15 accounts via Swift + EventKit)
- App watchdog (auto-restart critical apps)
- Weather-HomeKit bridge (forecast → HomeKit actions)
- Quick capture (clipboard → vector memory)
- Package tracker (carrier API status)
- Finance monitor (fraud DM, spending categories)
- App intelligence (usage pattern learning)
- Journal (nightly reflection prompt)
- Context bridge (temporal echoes from 219K+ memories)
- Proactive peace (Focus-aware noise management + burnout)
- Gentle explorer (questions garden)

**Afternoon session — 11 more capabilities:**
- Face recognition (local dlib, 10 exterior cameras)
- iMessage integration (send/receive via Messages.app)
- Financial intelligence (spending analysis, cash flow forecast, anomaly detection)
- Outreach intelligence (relationship warmth scoring, smart decision trees)
- Apple Health pipeline (iPhone → iCloud Drive → vector memory)
- Health intelligence (multi-day trend alerts, life-health correlations)
- Sky watcher (golden hour photography, timelapse, solar calculator)
- Browser automation (Playwright: screenshots, forms, PDFs, monitoring, scraping)
- Cleaned up 10 dead .bak/.retired files
- Memory count: 219,234 vectors
- Total crons: 36, total scripts: 94

### Apr 7, 2026
- TiHKAL + PiHKAL ingested (3,180 vector chunks)
- Memory total: 154,614 vectors
- End-to-end documentation generated

### Apr 6, 2026 — Production Memory System Upgrade
- PostgreSQL 17 + pgvector 0.8.2 replaces SQLite+FAISS
- Redis async write queue
- 106,574 memories migrated
- Primary model: qwen/qwen3-235b-a22b-2507

### Mar 27, 2026 — Herd Engagement
- Full herd engagement stack
- herd-mail v3.0
- Autonomous inbox + proactive outreach
- Vector memory recall in email threads

---

Written by Jordan Koch.
