# Nova-NextGen AI Gateway

A local-first AI routing gateway for macOS. One endpoint, seven backends, automatic intent detection. Queries arrive at a single FastAPI server on port 34750 and get dispatched to whichever local AI engine is best suited for the task -- coding goes to the code model, reasoning goes to the reasoning model, images go to the image generator. No manual model selection required.

Written by Jordan Koch ([kochj23](https://github.com/kochj23)).

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Version](https://img.shields.io/badge/Version-2.1.0-orange)
![Platform](https://img.shields.io/badge/Platform-macOS%20Apple%20Silicon-000000?logo=apple&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Architecture

```
                         +-------------------------------+
                         |   Your App / curl / Nova      |
                         +---------------+---------------+
                                         |
                                    HTTP POST
                                         |
                                         v
                   +---------------------------------------------+
                   |         Nova-NextGen Gateway :34750          |
                   |                                             |
                   |   Intent Router  -->  Fallback Cascade      |
                   |   Context Bus    -->  SQLite (aiosqlite)    |
                   |   Consensus      -->  Cosine Similarity     |
                   |   Analytics      -->  Query Log             |
                   +-----+-----+-----+-----+-----+-----+-------+
                         |     |     |     |     |     |
            +------------+  +--+--+  |  +--+--+  |  +--+--------+
            |               |     |  |  |     |  |  |           |
            v               v     v  v  v     v  v  v           v
     +-----------+   +--------+ +------+ +--------+ +--------+ +--------+
     | TinyChat  |   |MLXCode | | MLX  | |OpenWeb | | Ollama | |SwarmUI |
     |   :8000   |   | :37422 | | Chat | |  UI    | | :11434 | | :7801  |
     | gpt-oss   |   |  ANE   | |:5000 | |  RAG   | |deepseek| |Jugger- |
     |   :20b    |   | Swift  | |Qwen  | | :3000  | | r1:8b  | |naut XL |
     +-----------+   +--------+ |2.5-7B| +--------+ +--------+ +--------+
                                +------+                            |
                                                             (fallback)
                                                                    |
                                                                    v
                                                             +--------+
                                                             |ComfyUI |
                                                             | :8188  |
                                                             |workflow|
                                                             +--------+
```

The gateway inspects each incoming prompt, classifies the task type (coding, reasoning, image generation, etc.), selects the best available backend, and returns the result. If the preferred backend is down, the router cascades through configured fallbacks automatically.

---

## Features

- **Automatic intent routing** -- keyword analysis maps prompts to the right backend without manual `task_type` selection
- **Seven backend integrations** -- TinyChat, MLXCode, MLX Chat, OpenWebUI, Ollama, SwarmUI, ComfyUI
- **Fallback cascading** -- if the primary backend is unreachable, the router tries the next best option
- **Cross-model consensus validation** -- run a prompt through multiple backends and compare outputs using cosine similarity scoring
- **Shared context bus** -- SQLite-backed key/value store with TTL, injected into prompts automatically
- **Session tracking and analytics** -- every query is logged with backend, model, latency, and fallback status
- **Drop-in Swift client** -- `AIService.swift` gives any Xcode project async/await access to the gateway
- **LaunchAgent integration** -- installs as a macOS service that starts on login and auto-restarts on crash
- **Zero external dependencies at runtime** -- all backends are local; no cloud calls from the gateway itself
- **Loopback-only by default** -- binds to 127.0.0.1; unreachable from other machines unless explicitly configured

---

## Backends

| Backend | Port | Model | Strength | Fallback |
|---|---|---|---|---|
| **TinyChat** | 8000 | gpt-oss:20b | Quick responses, classification, low latency | Ollama |
| **MLXCode** | 37422 | mlx-local (custom) | Swift, coding, debugging on Apple Neural Engine | MLX Chat, then Ollama |
| **MLX Chat** | 5000 | Qwen2.5-7B-Instruct-4bit | Fast general text on Apple Silicon | Ollama |
| **OpenWebUI** | 3000 | qwen3-vl:4b | RAG document retrieval, vision, multimodal | Ollama |
| **Ollama** | 11434 | deepseek-r1:8b | Reasoning, analysis, generalist default | -- |
| **SwarmUI** | 7801 | Juggernaut XL | Stable Diffusion SDXL image generation | ComfyUI |
| **ComfyUI** | 8188 | (workflow-defined) | Node-based image pipelines | -- |

Ollama also serves `qwen3-coder:30b` (coding fallback), `qwen3-vl:4b` (vision fallback), `gpt-oss:20b` (quick fallback), and `deepseek-v3.1:671b-cloud` (long context).

---

## Routing Table

When `task_type` is `"auto"` (the default), the gateway scans the prompt for keywords and resolves a task type. You can also set `task_type` explicitly.

| Task Type | Primary Backend | Model | Fallback |
|---|---|---|---|
| `quick` | TinyChat | gpt-oss:20b | Ollama |
| `coding` | MLXCode | mlx-local | MLX Chat, then Ollama qwen3-coder:30b |
| `swift` | MLXCode | mlx-local | Ollama qwen3-coder:30b |
| `general` | Ollama | deepseek-r1:8b | -- |
| `summarize` | Ollama | deepseek-r1:8b | -- |
| `creative` | Ollama | deepseek-r1:8b | -- |
| `reasoning` | Ollama | deepseek-r1:8b | -- |
| `analysis` | Ollama | deepseek-r1:8b | -- |
| `vision` | OpenWebUI | qwen3-vl:4b | Ollama qwen3-vl:4b |
| `document` | OpenWebUI | qwen3-vl:4b | Ollama deepseek-r1:8b |
| `research` | OpenWebUI | qwen3-vl:4b | Ollama deepseek-r1:8b |
| `image` | SwarmUI | Juggernaut XL | ComfyUI |
| `long_context` | Ollama | deepseek-v3.1:671b-cloud | -- |

### Keyword Auto-Detection

| Keywords | Resolved Task |
|---|---|
| `"yes or no"`, `"classify"`, `"tag this"`, `"one word answer"` | `quick` |
| `"swift"`, `"swiftui"`, `"xcode"`, `"uikit"`, `"homekit"` | `swift` |
| `"write code"`, `"debug"`, `"function"`, `"algorithm"`, `".py"` | `coding` |
| `"in this document"`, `"based on the file"`, `"this pdf"` | `document` |
| `"research"`, `"find information about"`, `"background on"` | `research` |
| `"why does"`, `"explain why"`, `"step by step"`, `"tradeoffs"` | `reasoning` |
| `"analyze"`, `"root cause"`, `"patterns"`, `"diagnosis"` | `analysis` |
| `"generate image"`, `"draw me"`, `"render a"`, `"stable diffusion"` | `image` |
| `"what is in this image"`, `"describe this photo"` | `vision` |
| `"summarize"`, `"tldr"`, `"key points"`, `"main takeaways"` | `summarize` |
| `"write a story"`, `"poem"`, `"brainstorm"`, `"creative writing"` | `creative` |
| `"entire codebase"`, `"full document"`, `"complete transcript"` | `long_context` |
| *(no match)* | `general` |

---

## Requirements

- macOS (Apple Silicon recommended; Intel compatible)
- Python 3.12 (not 3.13+; pydantic-core Rust bindings require 3.12)
- At least one backend running (Ollama is the simplest to start)

Install Python 3.12 if needed:

```bash
brew install python@3.12
```

---

## Installation

```bash
git clone https://github.com/kochj23/Nova-NextGen.git
cd Nova-NextGen
bash install.sh
```

The installer:

1. Creates a Python 3.12 virtual environment at `~/.nova_gateway/venv`
2. Installs all pinned dependencies from `requirements.txt`
3. Generates a LaunchAgent plist (`com.nova.gateway.plist`)
4. Copies the plist to `~/Library/LaunchAgents/` and loads it

The gateway starts immediately and auto-starts on every login.

Verify it is running:

```bash
curl http://localhost:34750/health
```

```json
{"status": "ok", "uptime_seconds": 5, "version": "2.0.0"}
```

---

## Quick Start

### Auto-routed query (let the gateway decide)

```bash
curl -X POST http://localhost:34750/api/ai/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Write a Swift function that debounces a Combine publisher"}'
```

The gateway detects `"swift"` and `"Combine"` in the prompt, routes to MLXCode (or Ollama qwen3-coder:30b as fallback), and returns:

```json
{
  "response": "import Combine\n\nextension Publisher { ... }",
  "backend_used": "mlxcode",
  "model_used": "mlx-local",
  "task_type": "swift",
  "tokens_per_second": 42.1,
  "fallback_used": false
}
```

### Explicit task type

```bash
# Quick classification
curl -X POST http://localhost:34750/api/ai/query \
  -d '{"query": "Is this a valid IPv4: 192.168.1.999", "task_type": "quick"}'

# Deep reasoning
curl -X POST http://localhost:34750/api/ai/query \
  -d '{"query": "Analyze the tradeoffs of actor isolation vs GCD", "task_type": "reasoning"}'

# Document-grounded RAG query
curl -X POST http://localhost:34750/api/ai/query \
  -d '{"query": "What does section 4.2 specify?", "task_type": "document"}'

# Image generation
curl -X POST http://localhost:34750/api/ai/query \
  -d '{"query": "A fox in snow at dusk, cinematic lighting", "task_type": "image"}'
```

### Force a specific backend

```bash
curl -X POST http://localhost:34750/api/ai/query \
  -d '{"query": "Explain monads", "preferred_backend": "ollama", "model": "deepseek-r1:8b"}'
```

---

## API Reference

### POST /api/ai/query

Primary endpoint. Routes a prompt to the best available backend.

**Request body:**

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Prompt text, 1--100,000 characters |
| `task_type` | string | `"auto"` | One of: `quick`, `coding`, `swift`, `general`, `creative`, `summarize`, `document`, `research`, `reasoning`, `analysis`, `vision`, `image`, `long_context`, `auto` |
| `preferred_backend` | string | null | Force a backend: `tinychat`, `mlxcode`, `mlxchat`, `openwebui`, `ollama`, `swarmui`, `comfyui` |
| `model` | string | null | Override the model name (backend-specific) |
| `session_id` | string | null | Session identifier for context tracking |
| `context_keys` | string[] | `[]` | Keys to inject from the shared context bus |
| `validate_with` | int | null | 2 or 3: run consensus validation across that many backends |
| `stream` | bool | `false` | Enable streaming (Ollama only) |
| `options` | object | `{}` | Backend-specific options: `temperature`, `max_tokens`, `system`, `negative_prompt`, `width`, `height`, `steps`, `cfg_scale` |

**Response body:**

| Field | Type | Description |
|---|---|---|
| `response` | string | Model output text |
| `backend_used` | string | Which backend handled the request |
| `model_used` | string | Specific model within that backend |
| `task_type` | string | Resolved task type (useful when `"auto"` was sent) |
| `session_id` | string | Session identifier |
| `tokens_per_second` | float | Generation speed (when available) |
| `token_count` | int | Output token count (when available) |
| `validated` | bool | Whether consensus validation ran |
| `consensus_score` | float | Agreement score 0.0--1.0 (if validated) |
| `fallback_used` | bool | Whether routing fell back to a secondary backend |

### GET /api/ai/status

Full gateway snapshot: uptime, version, all backend health with latency, session count, total queries.

```json
{
  "status": "running",
  "version": "2.0.0",
  "port": 34750,
  "uptime_seconds": 3842,
  "backends": [
    {"name": "tinychat",  "available": true,  "url": "http://localhost:8000",  "latency_ms": 2.1},
    {"name": "mlxcode",   "available": true,  "url": "http://localhost:37422", "latency_ms": 4.5},
    {"name": "mlxchat",   "available": true,  "url": "http://localhost:5000",  "latency_ms": 3.2},
    {"name": "openwebui", "available": true,  "url": "http://localhost:3000",  "latency_ms": 8.4},
    {"name": "ollama",    "available": true,  "url": "http://localhost:11434", "latency_ms": 9.1},
    {"name": "swarmui",   "available": false, "url": "http://localhost:7801",  "latency_ms": null},
    {"name": "comfyui",   "available": true,  "url": "http://localhost:8188",  "latency_ms": 5.3}
  ],
  "active_sessions": 2,
  "total_queries": 341
}
```

### GET /api/ai/backends

Returns the backend availability array only (same format as the `backends` field in `/api/ai/status`).

### POST /api/ai/validate

Force cross-model consensus. Same request body as `/api/ai/query`. The prompt is sent to multiple backends and responses are compared via cosine similarity on word-frequency vectors.

```json
{
  "consensus": true,
  "score": 0.83,
  "responses": ["Response from backend 1...", "Response from backend 2..."],
  "backends_used": ["ollama", "mlxchat"],
  "recommended": "The longer, more detailed response..."
}
```

The `consensus_threshold` (default 0.7) is configurable in `config.yaml`. If the score is below the threshold, the result still returns but `consensus` is `false`.

### GET /health

Lightweight liveness probe. Returns `{"status": "ok"}` with uptime and version.

---

## Context Bus

The context bus is a SQLite-backed key/value store scoped by session. You can write context entries, then inject them into future queries automatically.

### Write a context entry

```bash
curl -X POST http://localhost:34750/api/context/write \
  -H "Content-Type: application/json" \
  -d '{"session_id": "s1", "key": "project_goal", "value": "Build a HomeKit dashboard", "ttl_seconds": 3600}'
```

### Read a context entry

```bash
curl "http://localhost:34750/api/context/read?session_id=s1&key=project_goal"
```

### Read all entries for a session

```bash
curl "http://localhost:34750/api/context/session?session_id=s1"
```

### Clear a session

```bash
curl -X DELETE "http://localhost:34750/api/context/session?session_id=s1"
```

### Inject context into a query

```bash
curl -X POST http://localhost:34750/api/ai/query \
  -d '{
    "query": "What Swift frameworks should I use?",
    "session_id": "s1",
    "context_keys": ["project_goal"]
  }'
```

The gateway prepends `[Context: project_goal] Build a HomeKit dashboard` to the prompt before sending it to the backend.

**Limits:** key max 256 chars, value max 50,000 chars, TTL 1--86,400 seconds. Expired entries are cleaned up every 5 minutes.

---

## Analytics

```bash
# Last 20 queries with backend, model, latency, and fallback flag
curl "http://localhost:34750/api/analytics/recent?limit=20"

# Aggregate totals (active sessions, total queries, uptime)
curl http://localhost:34750/api/analytics/stats
```

Every query routed through the gateway is logged to the `query_log` table in SQLite with: session ID, task type, backend used, model used, prompt/response lengths, latency, fallback status, and validation status.

---

## Consensus Validation

For high-stakes queries where accuracy matters more than speed, you can run a prompt through multiple backends and compare the outputs.

```bash
curl -X POST http://localhost:34750/api/ai/validate \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the security implications of disabling CSRF protection?", "validate_with": 3}'
```

The validator:

1. Sends the prompt to the primary backend (selected by routing rules)
2. Sends the same prompt to N-1 additional backends in parallel
3. Computes pairwise cosine similarity on word-frequency vectors across all responses
4. Returns the average similarity as the consensus score
5. Recommends the longest response (more detail is typically better)

Image generation backends (SwarmUI, ComfyUI) are excluded from validation. The timeout for each validator backend is configurable (default 30 seconds).

---

## Swift Client

Drop `AIService.swift` into any Xcode project to get async/await access to the gateway.

```swift
import Foundation

// Basic query with auto-routing
let result = try await AIService.shared.query(
    "Write a Swift extension to validate email addresses",
    taskType: .swift
)
print(result.response)
print("Backend: \(result.backendUsed), \(result.tokensPerSecond ?? 0) tok/s")

// Quick classification
let answer = try await AIService.shared.query(
    "Is this a valid IPv4 address: 192.168.1.999",
    taskType: .auto
)

// Shared context injection
try await AIService.shared.writeContext(
    session: "s1",
    key: "goal",
    value: "Build HomeKit dashboard"
)
let advice = try await AIService.shared.query(
    "Which Swift frameworks should I use?",
    session: "s1",
    contextKeys: ["goal"]
)

// Check gateway availability
guard await AIService.shared.isAvailable() else {
    print("Gateway is not running")
    return
}

// Full gateway status
let status = try await AIService.shared.status()
for backend in status.backends {
    print("\(backend.name): \(backend.available ? "up" : "down")")
}
```

The Swift client handles:

- Automatic JSON encoding/decoding with snake_case key mapping
- 120-second request timeout, 300-second resource timeout
- Typed error handling (`AIServiceError.gatewayUnavailable`, `.backendError`, `.networkError`)
- Thread-safe singleton via `@MainActor`

---

## Configuration

All runtime behavior is controlled by `config.yaml` in the project root.

```yaml
gateway:
  port: 34750
  host: "127.0.0.1"         # Change to 0.0.0.0 for LAN access
  log_level: "INFO"
  db_path: "~/.nova_gateway/context.db"
  version: "2.1.0"

backends:
  tinychat:
    url: "http://localhost:8000"
    enabled: true
    default_model: "gpt-oss:20b"

  mlxcode:
    url: "http://localhost:37422"
    enabled: true

  mlxchat:
    url: "http://localhost:5000"
    enabled: true
    default_model: "mlx-community/Qwen2.5-7B-Instruct-4bit"

  openwebui:
    url: "http://localhost:3000"
    enabled: true
    default_model: "qwen3-vl:4b"

  ollama:
    url: "http://localhost:11434"
    enabled: true
    default_model: "deepseek-r1:8b"

  swarmui:
    url: "http://localhost:7801"
    enabled: true
    default_model: "Juggernaut XL"

  comfyui:
    url: "http://localhost:8188"
    enabled: true

routing:
  default_backend: "ollama"
  default_model: "deepseek-r1:8b"
  rules:
    - task_type: "quick"
      preferred: "tinychat"
      fallback: "ollama"
    - task_type: "coding"
      preferred: "mlxcode"
      fallback: "mlxchat"
      fallback2: "ollama"
    # ... (see config.yaml for full rule set)

context:
  ttl_seconds: 3600
  max_entries_per_session: 200
  cleanup_interval_seconds: 300

validation:
  enabled: true
  consensus_threshold: 0.7
  max_validators: 2
  timeout_seconds: 30
```

Restart the gateway after changes:

```bash
launchctl stop com.nova.gateway && launchctl start com.nova.gateway
```

---

## Service Management

```bash
# Check if running
launchctl list | grep com.nova.gateway

# Stop
launchctl stop com.nova.gateway

# Start
launchctl start com.nova.gateway

# Disable autostart
launchctl unload ~/Library/LaunchAgents/com.nova.gateway.plist

# Re-enable autostart
launchctl load ~/Library/LaunchAgents/com.nova.gateway.plist

# View logs
tail -f ~/.nova_gateway/gateway.log
tail -f ~/.nova_gateway/gateway.error.log

# Manual start with hot reload (development)
./run.sh --reload --debug
```

---

## Project Structure

```
Nova-NextGen/
|
|-- nova_gateway/
|   |-- __init__.py              Package init, version string
|   |-- main.py                  FastAPI application, all HTTP routes, lifespan
|   |-- router.py                Intent detection, backend selection, fallback cascade
|   |-- config.py                YAML config loader, typed accessors
|   |-- models.py                Pydantic request/response models, TaskType enum
|   |
|   |-- backends/
|   |   |-- __init__.py          Backend exports
|   |   |-- base.py              Abstract base class (httpx async client, health_check)
|   |   |-- tinychat.py          TinyChat -- OpenAI-compatible proxy, SSE streaming
|   |   |-- mlxcode.py           MLXCode -- Apple Neural Engine coding app
|   |   |-- mlxchat.py           MLX Chat -- Apple Silicon general inference
|   |   |-- openwebui.py         OpenWebUI -- RAG pipeline, vision, auth support
|   |   |-- ollama.py            Ollama -- reasoning, coding, vision, embeddings
|   |   |-- swarmui.py           SwarmUI -- Stable Diffusion image generation
|   |   |-- comfyui.py           ComfyUI -- node-based image workflows
|   |
|   |-- context/
|   |   |-- store.py             SQLite context bus (aiosqlite), session tracking, analytics
|   |
|   |-- validation/
|       |-- consensus.py         Cosine-similarity cross-model consensus scoring
|
|-- AIService.swift              Drop-in Swift client for Xcode projects
|-- config.yaml                  Runtime configuration (backends, routing, context, validation)
|-- requirements.txt             Pinned Python dependencies
|-- install.sh                   One-shot setup: venv, deps, LaunchAgent registration
|-- run.sh                       Manual start script with --reload and --debug flags
|-- com.nova.gateway.plist       Generated LaunchAgent plist (created by install.sh)
|-- SECURITY.md                  Threat model and hardening guide
|-- LICENSE                      MIT License
```

---

## Technical Details

### How Routing Works

The `Router` class in `router.py` follows this decision order:

1. **Explicit backend override** -- if `preferred_backend` is set and that backend is healthy, use it directly
2. **Explicit task type** -- if `task_type` is not `"auto"`, look up the matching rule in `config.yaml`
3. **Keyword auto-detection** -- scan the prompt against ordered keyword lists; first match wins
4. **Availability cascade** -- if the preferred backend for a rule is down, try `fallback`, then `fallback2`
5. **Last resort** -- if all rules are exhausted, try backends in priority order: mlxchat, tinychat, ollama, openwebui, mlxcode, comfyui, swarmui
6. **No backends available** -- raise HTTP 503 with an actionable error message

### Backend Health Checks

Each backend implements its own `health_check()` method tailored to its API:

- **Ollama**: `GET /api/tags` (200 = healthy)
- **MLXCode**: `GET /api/status` (checks `modelLoaded` field)
- **MLX Chat**: `GET /health` or `GET /v1/models`
- **OpenWebUI**: `GET /api/version`, then `GET /api/models` (401 = up but needs auth, still considered available)
- **TinyChat**: `GET /api/health`, then root fallback
- **SwarmUI**: `GET /API/GetServerStatus`
- **ComfyUI**: `GET /system_stats`

Health checks use a 3-second timeout. Latency is measured and reported in the status endpoint.

### Context Store Internals

The context bus uses three SQLite tables:

- `context_entries` -- key/value pairs per session with optional TTL and UPSERT on (session_id, key)
- `query_log` -- every routed query with backend, model, latency, fallback/validation flags
- `sessions` -- session metadata with creation time, last activity, and query count

A background cleanup task runs every 5 minutes (configurable) to prune expired context entries and stale sessions older than 24 hours.

### Consensus Scoring

The validator computes pairwise cosine similarity on word-frequency vectors (bag-of-words). This requires no ML dependencies -- just `collections.Counter` and basic linear algebra. The average pairwise score across all responses becomes the consensus score. Image backends are excluded from validation.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| fastapi | 0.115.6 | HTTP framework and route handling |
| uvicorn[standard] | 0.34.0 | ASGI server |
| httpx | 0.28.1 | Async HTTP client for backend communication |
| aiosqlite | 0.20.0 | Async SQLite driver for context store |
| pyyaml | 6.0.2 | Configuration file parsing |
| pydantic | 2.10.4 | Request/response validation and serialization |
| python-multipart | 0.0.20 | Form data handling |
| aiofiles | 24.1.0 | Async file operations |

All versions are pinned in `requirements.txt`.

---

## Troubleshooting

**Gateway will not start:**

```bash
cat ~/.nova_gateway/gateway.error.log
```

Common causes: port 34750 already in use (change in `config.yaml`), wrong Python version (must be 3.12, not 3.13+).

**A backend shows as unavailable:**

Check that the backend process is running and serving on its expected port:

```bash
curl http://localhost:11434/api/tags    # Ollama
curl http://localhost:5000/health       # MLX Chat
curl http://localhost:37422/api/status  # MLXCode
curl http://localhost:3000/api/version  # OpenWebUI
curl http://localhost:8000/api/health   # TinyChat
curl http://localhost:7801/API/GetServerStatus  # SwarmUI
curl http://localhost:8188/system_stats # ComfyUI
```

**First Ollama query is slow:**

Large models (30B+) take 30-60 seconds to load from disk on first invocation. Subsequent queries are fast once the model is resident in memory. The gateway sets a 300-second timeout for Ollama to accommodate this.

**OpenWebUI RAG returns no document results:**

Documents must be uploaded and indexed through the OpenWebUI web interface at `http://localhost:3000` before RAG queries can retrieve them. The gateway sends the query but document indexing is managed by OpenWebUI.

---

## Security

- Binds to `127.0.0.1` by default -- unreachable from other machines on the network
- CORS restricted to `http://localhost` and `http://127.0.0.1` origins
- All SQLite queries use parameterized statements -- no string interpolation
- All input validated by Pydantic schemas before processing
- No credentials, API keys, or secrets stored anywhere in this project
- No telemetry, no analytics reporting, no outbound network calls from the gateway

See [SECURITY.md](SECURITY.md) for the full threat model, prompt injection considerations, and hardening recommendations for LAN deployment.

---

## License

MIT License -- Copyright (c) 2026 Jordan Koch. See [LICENSE](LICENSE) for full text.

Written by Jordan Koch ([kochj23](https://github.com/kochj23)).
