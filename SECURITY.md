# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| Current | Yes                |

## Reporting a Vulnerability

If you discover a security vulnerability in Nova's public scripts or configuration, please report it responsibly.

**Contact:** Open a GitHub issue with the label `security` or email the repository owner.

**Response Time:** Security issues will be triaged within 48 hours.

## Security Architecture

Nova runs entirely on local infrastructure with strict security boundaries:

### Network Security
- **Gateway bound to loopback only** — ws://127.0.0.1:18789 (not externally accessible)
- **Memory server loopback only** — http://127.0.0.1:18790
- **All inter-service communication** over localhost
- **No ports exposed to the internet** — everything behind NAT

### Credential Management
- **All secrets stored in macOS Keychain** — never in source files
- **Three-layer pre-push scanning** — pre-commit hook, Claude Code hook, git pre-push hook
- **Automated credential detection** — AWS keys, API tokens, personal emails, hardcoded paths

### Data Privacy
- **4-tier intent routing** — local-first with minimal cloud fallback
- **Health data tagged `privacy: local-only`** — never sent to cloud LLMs
- **Face recognition data stays local** — no cloud facial recognition services
- **Memory vectors stored in local PostgreSQL** — no cloud vector databases

### Dependencies
- **Ollama** for all LLM inference (local)
- **PostgreSQL 17 + pgvector** for memory storage (local)
- **Redis** for caching and queuing (local)
- **SearXNG** for web search (self-hosted)

## Known Security Considerations

- The `serverURL` in health receiver scripts uses RFC 1918 addresses only
- Slack/Discord tokens loaded from OpenClaw config file (not hardcoded)
- UniFi Protect credentials stored in macOS Keychain
- All cron scripts run under the user account (not root)
