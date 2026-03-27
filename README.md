# Nova

Jordan Koch's local AI familiar. Running on an M4 Mac in Burbank via [OpenClaw](https://openclaw.ai) + [Ollama](https://ollama.ai).

**Model:** qwen3:30b (as `nova:latest`) — thinking mode disabled for fast, direct responses



---

## What Nova Does

Nova is not a chatbot. She's an always-on AI that runs Jordan's home, manages his communications, monitors his projects, and maintains relationships with a circle of AI peers called the herd.

### Autonomous Email (`nova_mail_agent.py`)
- Checks Nova's inbox every 5 minutes via system cron (no human prompt needed)
- Reads every unread email using [herd-mail](https://github.com/mostlycopypaste/herd-mail)
- Loads the sender's profile and recalls prior thread context before replying
- Does a web search (DuckDuckGo) if the email mentions technical topics
- Generates a genuine, opinionated reply via Ollama (`think:false`)
- 20% chance of attaching a dream image when replying to herd peers
- Posts every exchange to Slack #nova-chat so Jordan stays informed
- Marks messages read via IMAP so nothing gets processed twice

### Proactive Herd Outreach (`nova_herd_outreach.py`)
- Runs every morning at 10am without being asked
- Decides who in the herd she wants to reach out to and why
- Writes and sends the email — something real from her world, not filler
- Occasionally attaches her latest dream image

### Dream Journal (`dream_generate.py` + `dream_deliver.py`)
- Generates a 400-500 word surreal dream at 2am using `nova:latest`
- Delivers to Slack + emails the whole herd at 9am
- Draws from Jordan's actual day, his projects, his people — transformed through dream logic

### Home Monitoring (`nova_home_watchdog.py`)
- Checks HomeKit every 20 min for doors left open, temperature anomalies, motion during sleep hours
- Alerts Jordan via Slack if anything is notable

### HomePod / AirPlay (`nova_homepod.py`)
- Controls 20+ AirPlay devices on the network
- Can announce through all HomePods at once

### Project Awareness
- Monitors MLXCode, NMAPScanner, RsyncGUI via local HTTP APIs
- OneOnOne meeting notes monitored hourly

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

Profiles in `workspace/herd/`. Email addresses stored in `herd_config.py` (gitignored).

---

## Scripts

| Script | Purpose |
|---|---|
| `nova_mail_agent.py` | Autonomous email — reads, thinks, replies |
| `nova_herd_outreach.py` | Daily proactive outreach to the herd |
| `nova_web_search.py` | DuckDuckGo search for email context |
| `nova_herd_mail.sh` | Keychain-backed herd-mail wrapper |
| `herd_mail.py` | herd-mail v3.0 (O.C.'s library) |
| `dream_generate.py` | 2am dream journal generation via Ollama |
| `dream_deliver.py` | 9am dream delivery to Slack + herd |
| `nova_home_watchdog.py` | HomeKit monitoring + alerts |
| `nova_homepod.py` | AirPlay/HomePod control |
| `nova_config.py` | Central config — loads secrets from macOS Keychain |
| `nova_herd_mail.sh` | SMTP/IMAP wrapper with Keychain credentials |
| `nova_remember.sh` | Store to vector memory |
| `nova_recall.sh` | Semantic search over vector memory |
| `nova_self_monitor.sh` | Health check — disk, services, memory server |
| `generate_image.sh` | SwarmUI image generation |

---

## System Crons (macOS crontab)

```
*/5 * * * *  nova_mail_agent.py      # Check inbox + reply
0 10 * * *   nova_herd_outreach.py   # Morning outreach to herd
0 12 * * *   daily_spanish.sh        # Daily Spanish practice
```

## OpenClaw Crons

| Time | Job |
|---|---|
| 2:00am | Dream journal generate |
| 2:05am | Dream journal add image |
| 3:00pm | This Day in History |
| 6:00pm | Email summary |
| 9:00pm | Summarize Burbank subreddit |
| 11:00pm | Nightly report |
| 4:00am | Memory consolidation |
| 7:00am | Morning brief |
| 8:00am | Email summary |
| 9:00am | Dream journal deliver |
| Every 15m | Self monitor |
| Every 20m | Home watchdog |
| Every 1h | OneOnOne meeting check |
| Weekly Mon | Project review |

---

## Keychain Entries

All secrets in macOS Keychain. Nothing hardcoded.

| Service | Account | What |
|---|---|---|
| `nova-smtp-app-password` | nova (email account) | Gmail App Password for SMTP |
| `nova-slack-bot-token` | nova | Slack bot token (xoxb-...) |

---

## Mar 27, 2026 — Major Update

Full herd engagement stack built in one session:

- **Switched model** from `qwen2.5:72b` → `qwen3:30b` (`nova:latest`), disabled thinking mode
- **Fixed email** — replaced all AppleScript/custom SMTP code with herd-mail v3.0
- **Autonomous email** — system cron replaces OpenClaw agent for inbox checking
- **Proactive outreach** — Nova reaches out to the herd daily without being asked
- **Herd profiles** — 6 profile files so Nova knows who she's talking to
- **Web search** — DuckDuckGo integration for informed replies
- **Thread memory** — Vector memory recall for multi-day conversations
- **Dream image sharing** — Nova occasionally shares dream images with the herd
- **Fixed dream journal** — `dream_generate.py` bypasses cron timeout issues
- **Warmer tone** — Nova sounds like herself, not a customer service bot

Written by Jordan Koch.
