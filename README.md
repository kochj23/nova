# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio in Burbank via [OpenClaw](https://openclaw.ai).

**Primary model:** `qwen/qwen3-235b-a22b-2507` via OpenRouter — 262k context, $0.071/$0.10 per 1M tokens  
**Local fallback:** `qwen3:30b` via Ollama (on-device, no internet required)  
**Gateway:** `ws://127.0.0.1:18789` (loopback only)

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
- Controls 20+ AirPlay devices on the network via `nova_homepod.py`
- Monitors package deliveries via `nova_package_detector.py`

### Project & Infrastructure Monitoring
- `github_monitor.py` — daily GitHub activity across all repos
- `git_monitor.py` — local git repo change monitoring
- `nova_software_inventory.py` — daily inventory of installed software
- `nova_supply_chain_check.py` — dependency vulnerability scanning
- `nova_weekly_nmap_scan.py` — weekly network scan for new devices
- `metrics_tracker.py` — GitHub stars, followers, repo metrics over time
- Monitors MLXCode, NMAPScanner, RsyncGUI, and 10+ other apps via local HTTP APIs (ports 37421–37449)
- OneOnOne meeting notes checked hourly

### Memory System
- **106,000+ memories** across email archives, documents, world knowledge, and domain expertise
- **PostgreSQL 17 + pgvector 0.8.2** backend — production-grade, concurrent-safe
- **HNSW index** — 23ms warm recall on 100K+ vectors
- **Redis async write queue** — bulk imports fire-and-forget at 8ms, worker embeds + stores
- Embeddings via `nomic-embed-text` (Ollama, 768 dims)
- Endpoints: `/remember[?async=1]`, `/recall`, `/random`, `/health`, `/stats`, `/queue/stats`
- `nova_nightly_memory_summary.py` — nightly memory consolidation
- `nova_slack_memory_ingest.py` — ingest Slack history into vector memory

**Knowledge indexed:** CIA World Factbook (262 countries), Jordan's email archives (40K+), GitHub READMEs (374 projects), JAGMAN + TM-21-210, Disney (2.5K facts), jungle/DnB/IDM/turntablism history (5K facts), home gardening (2.5K), health (diabetes, rosacea, BP, depression), cooking, astronomy, philosophy, Swift/iOS dev, network security, Burbank/LA local knowledge, Corvette manual + facts, and more.

### Daily Rhythm (OpenClaw Crons)

| Time | Job |
|---|---|
| 2:00am | Dream journal generate |
| 2:05am | Dream journal add image |
| 3:00am | Supply chain check |
| 4:00am | Daily software inventory |
| 4:00am | Memory consolidation |
| 5:00am | Metrics tracker |
| 7:00am | Morning brief |
| 8:00am | Email summary |
| 9:00am | Dream journal deliver |
| 9:00am | GitHub monitor |
| 10:00am | Git monitor |
| 10:00am | Jungle Track monitor |
| 12:00pm | Disk check |
| 3:00pm | This Day in History |
| 6:00pm | Email summary |
| 9:00pm | Summarize Burbank subreddit |
| 9:00pm | Nightly memory summary |
| 11:00pm | Nightly report |
| Every 3m | Gateway watchdog |
| Every 5m | Inbox watcher |
| Every 6h | Slack #general memory scan |
| Every 20m | Home watchdog |
| Every 1h | OneOnOne meeting check |
| Weekly Mon | Project review |

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
    ├── 106,000+ memories
    ├── embeddings: nomic-embed-text (Ollama, 768 dims)
    ├── backend: PostgreSQL 17 + pgvector 0.8.2 (HNSW index)
    ├── write queue: Redis 8.6.2 (async bulk ingest)
    └── recall: ~23ms warm (HNSW cosine similarity)

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

| Script | Purpose |
|---|---|
| `memory_server.py` | PostgreSQL+pgvector memory server (v3.0) |
| `nova_mail_agent.py` | Autonomous email — reads, thinks, replies with haiku |
| `nova_herd_broadcast.sh` | Broadcast to all herd members (haiku + memory fragment) |
| `nova_herd_mail.sh` | Keychain-backed herd-mail wrapper |
| `nova_random_safe_memory.sh` | Safe semantic memory fragment for email footers |
| `nova_recall.sh` | Semantic search over vector memory |
| `nova_remember.sh` | Store to vector memory |
| `generate_image.sh` | SwarmUI image generation (port 7802) |
| `dream_generate.py` | 2am dream journal generation |
| `dream_deliver.py` | 9am dream delivery to Slack + herd |
| `nova_home_watchdog.py` | HomeKit monitoring + alerts |
| `nova_config.py` | Central config — loads secrets from macOS Keychain |
| `migrate_sqlite_to_postgres.py` | One-time SQLite → PostgreSQL migration |

---

## Keychain Entries

| Service | Account | What |
|---|---|---|
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |

---

## Changelog

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
