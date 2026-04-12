# Security Policy

## Scope

Nova-NextGen is a **local-only AI gateway** designed to run on your own machine. It is not a web service and is not intended to be exposed to the public internet. The security model is:

- Binds to `127.0.0.1` (loopback) by default — unreachable from other machines
- No authentication required (loopback + OS process isolation is the boundary)
- All backends it calls are also localhost-only services

---

## Security Design

### Network Exposure
- **Default host: `127.0.0.1`** — loopback only. Any process on the same machine can call it, but no external machine can.
- If you change `host` to `0.0.0.0` in `config.yaml`, the gateway becomes reachable on your LAN. Do this only if you understand the implications and trust your network.
- CORS is restricted to localhost origins (`http://localhost`, `http://127.0.0.1`) to prevent malicious web pages from using the gateway as a proxy.

### Input Validation
- All request bodies are validated by Pydantic before processing.
- `query` field: max 100,000 characters.
- `context.value`: max 50,000 characters.
- `context.key`: max 256 characters.
- `session_id`: max 128 characters.
- `analytics/recent limit`: clamped to 1–100.

### SQL Injection
- All SQLite queries use parameterized statements (`?` placeholders). No string interpolation is used in any query. The SQLite database is local to the user at `~/.nova_gateway/context.db`.

### Credential Handling
- No credentials, API keys, or secrets are stored anywhere in this project.
- All backends are accessed via HTTP to localhost — no authentication tokens are transmitted.
- If your Ollama or other backends require auth, configure that at the backend level.

### Dependency Supply Chain
- Dependencies are pinned to exact versions in `requirements.txt`.
- Dependabot is configured to alert on vulnerability disclosures.
- There are no network calls at install time beyond PyPI package downloads.

### Prompt Injection via Shared Context
- When `context_keys` are used in a query, context values are prepended to the prompt as `[Context: key] value`. A malicious actor with write access to your context store could craft a context value that manipulates model behavior (prompt injection). Since the context store is local to your user account, the threat model is the same as any other file on your machine.

### kwargs Passthrough to Backends
- The `options` field in query requests is passed to backends as kwargs. Each backend only reads specific known keys (`temperature`, `max_tokens`, `system`, `negative_prompt`, `width`, `height`, `steps`, `cfg_scale`). Unrecognized keys are silently ignored. No arbitrary kwargs are forwarded to backend HTTP calls.

---

## What This Gateway Does NOT Do

- No data leaves your machine (all backends are local)
- No telemetry or usage reporting
- No credentials stored in any form
- No internet calls from the gateway itself
- No TLS (loopback connections don't need it; OS guarantees loopback traffic stays on-device)

---

## Reporting a Vulnerability

If you find a security issue in this project, please report it privately rather than opening a public GitHub issue.

**Use GitHub's private vulnerability reporting:**  
Go to **Security → Report a vulnerability** on the [Nova-NextGen repository](https://github.com/kochj23/Nova-NextGen/security/advisories/new).

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested mitigations

I will acknowledge receipt within 48 hours and aim to release a fix within 14 days for confirmed issues.

---

## Hardening Recommendations

If you decide to expose the gateway beyond loopback (`host: "0.0.0.0"`):

1. **Add authentication** — put a reverse proxy (nginx, Caddy) in front with basic auth or mTLS
2. **Firewall** — restrict incoming connections to trusted IP ranges only
3. **Rate limiting** — add nginx `limit_req` or similar to prevent abuse
4. **TLS** — terminate TLS at the proxy; never run HTTP on a non-loopback interface without it
5. **Audit logs** — the built-in query log at `/api/analytics/recent` records all queries; review it periodically

---

## Supported Versions

Only the latest version on the `main` branch is actively maintained.

| Version | Supported |
|---|---|
| Latest `main` | ✅ |
| Older commits | ❌ |
