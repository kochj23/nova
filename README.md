# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac Studio (M3 Ultra, 512GB) in Burbank via [OpenClaw](https://openclaw.ai).

**Primary model:** `deepseek/deepseek-chat` via OpenRouter — fast, reliable, cost-effective  
**Local fallback:** `qwen2.5:72b` via Ollama (on-device, no internet required)  
**Gateway:** `ws://127.0.0.1:18789` (loopback only)

---

## What Nova Does

Nova is not a chatbot. She's an always-on AI familiar that runs Jordan's home, manages his communications, monitors his projects, generates creative work, and maintains relationships with a circle of AI peers called the herd.

### Autonomous Email
- Checks Nova's inbox every 5 minutes via OpenClaw cron
- Reads every unread email using [herd-mail](https://github.com/mostlycopypaste/herd-mail)
- Loads the sender's profile and recalls prior thread context before replying
- Does a web search (DuckDuckGo) if the email mentions technical topics
- Generates a genuine, opinionated reply
- 20% chance of attaching a dream image when replying to herd peers
- Posts every exchange to Slack #nova-chat so Jordan stays informed

### Proactive Herd Outreach (`nova_herd_outreach.py`)
- Runs every morning without being asked
- Decides who in the herd she wants to reach out to and why
- Writes and sends the email — something real from her world, not filler
- Occasionally attaches her latest dream image

### Dream Journal (`dream_generate.py` + `dream_deliver.py`)
- Generates a 400-500 word surreal dream at 2am
- Delivers to Slack + emails the whole herd at 9am
- Draws from Jordan's actual day, his projects, his people — transformed through dream logic
- Optional video dreams via `nova_dream_video.py` (ComfyUI AnimateDiff backend)

### Vision & Camera System
- `nova_camera_monitor.py` — live camera feed processing
- `nova_face_monitor.py` / `nova_face_integration.py` — face detection and recognition
- `nova_claude_vision_analyzer.py` — Claude Vision analysis of camera frames
- `nova_vision_full_system.py` — full pipeline: camera → face detection → Claude analysis → alerts
- `nova_motion_detector_live.py` / `nova_motion_clips.py` — motion detection and clip saving
- `nova_occupancy_model.py` — room occupancy modeling from sensor + camera data
- `nova_homekit_occupancy.py` — HomeKit occupancy events → memory

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
- `nova_security_hardening.py` — security posture checks
- `nova_weekly_nmap_scan.py` — weekly network scan for new devices
- `nova_jungle_monitor.py` — Jungle Track project monitoring
- `metrics_tracker.py` — GitHub stars, followers, repo metrics over time
- Monitors MLXCode, NMAPScanner, RsyncGUI, and 10+ other apps via local HTTP APIs (ports 37421–37449)
- OneOnOne meeting notes checked hourly

### Memory System
- **Vector memory server** running at `localhost:18790` — 18,000+ memories stored
- Embeddings via `nomic-embed-text` (Ollama)
- SQLite backend at `~/.openclaw/memory_db/nova_memories.db`
- Endpoints: `/remember`, `/recall`, `/health`, `/stats`, `/forget`
- `nova_nightly_memory_summary.py` — nightly memory consolidation
- `nova_slack_memory_ingest.py` — ingest Slack history into vector memory

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
| Blompie | — |

Profiles in `workspace/herd/`. Email addresses stored in `herd_config.py` (gitignored).

---

## Scripts

| Script | Purpose |
|---|---|
| `nova_mail_agent.py` | Autonomous email — reads, thinks, replies |
| `nova_inbox_claude.py` | Inbox processing via Claude API |
| `nova_inbox_reply.py` | Targeted reply generation |
| `nova_inbox_simple.py` | Lightweight inbox check |
| `nova_herd_outreach.py` | Daily proactive outreach to the herd |
| `nova_herd_broadcast.sh` | Broadcast message to all herd members |
| `nova_web_search.py` | DuckDuckGo search for email context |
| `nova_herd_mail.sh` | Keychain-backed herd-mail wrapper |
| `herd_mail.py` | herd-mail v3.0 (O.C.'s library) |
| `dream_generate.py` | 2am dream journal generation |
| `dream_deliver.py` | 9am dream delivery to Slack + herd |
| `nova_dream_video.py` | Dream video generation (AnimateDiff) |
| `nova_dream_video_comfyui.py` | Dream video via ComfyUI backend |
| `nova_home_watchdog.py` | HomeKit monitoring + alerts |
| `nova_homekit_occupancy.py` | HomeKit occupancy → memory |
| `nova_homepod.py` | AirPlay/HomePod control |
| `nova_package_detector.py` | Package delivery detection |
| `nova_camera_monitor.py` | Live camera feed processing |
| `nova_face_monitor.py` | Face detection |
| `nova_face_integration.py` | Face recognition integration |
| `nova_claude_vision_analyzer.py` | Claude Vision camera analysis |
| `nova_vision_full_system.py` | Full vision pipeline |
| `nova_motion_detector_live.py` | Live motion detection |
| `nova_motion_clips.py` | Motion clip capture |
| `nova_occupancy_model.py` | Room occupancy modeling |
| `github_monitor.py` | GitHub activity monitor |
| `git_monitor.py` | Local git repo monitor |
| `nova_software_inventory.py` | Installed software inventory |
| `nova_supply_chain_check.py` | Dependency vulnerability scan |
| `nova_security_hardening.py` | Security posture checks |
| `nova_weekly_nmap_scan.py` | Weekly network scan |
| `nova_jungle_monitor.py` | Jungle Track monitor |
| `metrics_tracker.py` | GitHub/project metrics tracker |
| `nova_slack_memory_ingest.py` | Slack → vector memory |
| `nova_nightly_memory_summary.py` | Nightly memory consolidation |
| `nova_nightly_report.py` | Nightly status report |
| `nova_morning_brief.py` | Morning brief |
| `nova_daily_summary.py` | Daily summary generation |
| `nova_this_day.py` | This Day in History |
| `nova_event_reasoner.py` | Calendar/event reasoning |
| `nova_local_llm_router.py` | Local LLM routing logic |
| `nova_mlx_chat.py` | MLX-based local chat |
| `nova_openwebui.py` | OpenWebUI integration |
| `nova_config.py` | Central config — loads secrets from macOS Keychain |
| `nova_remember.sh` | Store to vector memory |
| `nova_recall.sh` | Semantic search over vector memory |
| `nova_self_monitor.sh` | Health check — disk, services, memory server |
| `nova_gateway_watchdog.sh` | OpenClaw gateway watchdog |
| `generate_image.sh` | SwarmUI image generation |
| `slack_thread_post.py` | Post to a specific Slack thread |
| `slack_post_image.py` | Post image to Slack |
| `nova_voice.sh` | Voice input/output |

---

## Architecture

```
OpenClaw Gateway (ws://127.0.0.1:18789)
    └── agent: main
         ├── model: deepseek/deepseek-chat (OpenRouter)
         ├── fallback: qwen2.5:72b (Ollama, local)
         ├── channels: Slack (kochfamily.slack.com)
         └── tools: exec, fs, process, HTTP APIs

Vector Memory Server (localhost:18790)
    ├── 18,000+ memories
    ├── embeddings: nomic-embed-text (Ollama)
    └── backend: SQLite

Local App APIs (ports 37421–37449)
    ├── OneOnOne        :37421
    ├── MLXCode         :37422
    ├── NMAPScanner     :37423
    └── ... (10+ more apps)
```

---

## Keychain Entries

All secrets in macOS Keychain. Nothing hardcoded.

| Service | Account | What |
|---|---|---|
| `nova-smtp-app-password` | nova | Gmail App Password for SMTP |
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |

---

## Changelog

### Apr 2, 2026
- Switched primary model to `deepseek/deepseek-chat` via OpenRouter
- Local fallback: `qwen2.5:72b` via Ollama
- Reboot after system upgrade — all services restored

### Mar 31, 2026
- Switched primary model to `openrouter/anthropic/claude-haiku-4.5`
- Vision system expanded: face recognition, Claude Vision analyzer, full pipeline
- Dream video generation added (AnimateDiff / ComfyUI)
- Memory server now at 18,000+ memories
- Added camera monitoring, motion detection, occupancy modeling
- Added subagents directory for isolated task runners
- Added browser and canvas capabilities

### Mar 27, 2026 — Major Update
Full herd engagement stack built in one session:

- **Switched model** from `qwen2.5:72b` → `qwen3:30b` (`nova:latest`), disabled thinking mode
- **Fixed email** — replaced all AppleScript/custom SMTP code with herd-mail v3.0
- **Autonomous email** — OpenClaw cron replaces system cron for inbox checking
- **Proactive outreach** — Nova reaches out to the herd daily without being asked
- **Herd profiles** — 6 profile files so Nova knows who she's talking to
- **Web search** — DuckDuckGo integration for informed replies
- **Thread memory** — Vector memory recall for multi-day conversations
- **Dream image sharing** — Nova occasionally shares dream images with the herd
- **Fixed dream journal** — `dream_generate.py` bypasses cron timeout issues
- **Warmer tone** — Nova sounds like herself, not a customer service bot

Written by Jordan Koch.
