#!/usr/bin/env python3
"""
nova_agent_sentinel.py — Security Sentinel background agent.

Monitors UniFi, camera feeds, nmap results, and privacy/PII leaks. Combines:
  - Vision analysis (qwen3-vl:4b) for camera anomalies
  - Reasoning (deepseek-r1:8b) for threat assessment
  - Privacy monitor: detects model routing drift and unintended cloud traffic

Only alerts on genuine anomalies. Posts to #nova-notifications for
routine events, flags critical threats to Jordan via Slack #nova-chat.

Runs as a persistent daemon subscribed to security channels.

Written by Jordan Koch.
"""

import glob
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

# Models that should NEVER appear in gateway config (anything other than these
# local identifiers is a potential PII leak vector).
ALLOWED_MODELS = {
    "ollama/qwen3-next:80b",
    "ollama/nova:latest",
    "ollama/qwen3-coder:30b",
    "ollama/deepseek-r1:8b",
    "ollama/qwen3-vl:4b",
    "mlx:qwen2.5-32b",
    # Research agent only — intentional cloud use for vague/non-private queries
    "openrouter/qwen/qwen3-235b-a22b-2507",
}

# Channels/agents that are explicitly allowed to use the research (OpenRouter) model
OPENROUTER_ALLOWED_AGENTS = {"research"}

# Outbound hosts that are acceptable for the openclaw process
ALLOWED_OUTBOUND_HOSTS = {
    "openrouter.ai",        # research agent only
    "slack.com",            # Slack Socket Mode
    "wss-primary.slack.com",
    "discord.com",          # Discord gateway
    "gateway.discord.gg",
    "signal.org",           # Signal registration (rare)
    "cdnjs.cloudflare.com", # Discord CDN
    "localhost",
    "127.0.0.1",
    "192.168.1.",           # LAN only
}

NMAP_RESULTS = Path.home() / "Library/Containers/com.digitalnoise.nmapscanner.macos"
NOVACONTROL_API = "http://127.0.0.1:37400"


class SecuritySentinel(SubAgent):
    name = "sentinel"
    model = "deepseek-r1:8b"
    backend = "ollama"
    channels = ["security", "nmap", "unifi", "camera_alert"]
    description = "Security monitoring: UniFi, cameras, nmap. Vision + reasoning for threat assessment."
    temperature = 0.1

    async def handle(self, task: dict) -> dict:
        task_type = task.get("type", "")

        if task_type == "nmap_scan":
            return await self._analyze_nmap(task)
        elif task_type == "camera_alert":
            return await self._analyze_camera(task)
        elif task_type == "unifi_event":
            return await self._analyze_unifi(task)
        elif task_type == "threat_assessment":
            return await self._threat_assessment(task)
        elif task_type == "privacy_monitor":
            return await self._privacy_monitor(task)
        else:
            return await self._generic_security(task)

    async def _privacy_monitor(self, task: dict) -> dict:
        """
        Check for model routing drift and unintended cloud traffic.
        Runs every 5 minutes via scheduler. Alerts Jordan immediately on any violation.
        """
        violations = []
        warnings = []

        # 1. Check gateway config for unexpected cloud models
        try:
            import psycopg2
            _conn = psycopg2.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
            _cur = _conn.cursor()
            _cur.execute("SELECT content FROM nova_documents WHERE category='nova_config' AND name='openclaw.json'")
            _row = _cur.fetchone()
            _conn.close()
            if not _row:
                warnings.append("openclaw.json not found in nova_ops")
                raise RuntimeError("not in PG")
            config = json.loads(_row[0])

            agents_conf = config.get("agents", {})
            defaults_model = agents_conf.get("defaults", {}).get("model", {})
            primary = defaults_model.get("primary", "") if isinstance(defaults_model, dict) else str(defaults_model)
            if primary and primary not in ALLOWED_MODELS:
                violations.append(f"agents.defaults.model = `{primary}` — NOT in allowed list")

            for agent in agents_conf.get("list", []):
                aid = agent.get("id", "?")
                m = agent.get("model", "")
                if m and m not in ALLOWED_MODELS:
                    violations.append(f"agent[{aid}].model = `{m}` — NOT in allowed list")
                # Research is the only agent allowed to use OpenRouter
                if m and "openrouter" in m and aid not in OPENROUTER_ALLOWED_AGENTS:
                    violations.append(f"agent[{aid}] using OpenRouter — only `research` agent is permitted")

            mbc = config.get("channels", {}).get("modelByChannel", {})
            for channel, entries in mbc.items():
                for key, model in entries.items():
                    if model not in ALLOWED_MODELS:
                        violations.append(f"channels.modelByChannel.{channel}.{key} = `{model}` — NOT allowed")
                    if "openrouter" in model:
                        violations.append(f"channels.modelByChannel.{channel}.{key} routes to OpenRouter — conversations may leak")

        except Exception as e:
            warnings.append(f"Could not read openclaw.json: {e}")

        # 2. Check gateway runtime log for unexpected outbound model calls
        try:
            log_date = datetime.now().strftime("%Y-%m-%d")
            runtime_log = Path(f"/tmp/openclaw/openclaw-{log_date}.log")
            if runtime_log.exists():
                recent_lines = runtime_log.read_text(errors="replace").splitlines()[-200:]
                for line in recent_lines:
                    try:
                        entry = json.loads(line)
                        msg = entry.get("message", "")
                        # Look for model invocations that reference cloud providers
                        if re.search(r"openrouter|anthropic\.com|api\.openai|bedrock", msg, re.I):
                            agent_ctx = entry.get("agent", entry.get("session", "unknown"))
                            if "research" not in str(agent_ctx).lower():
                                warnings.append(f"Outbound model call from non-research context: {msg[:120]}")
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as e:
            warnings.append(f"Could not read gateway runtime log: {e}")

        # 3. Check active network connections from openclaw process for unexpected outbound hosts
        try:
            result = subprocess.run(
                ["lsof", "-i", "-n", "-P", "-a", "-p",
                 subprocess.run(["pgrep", "-f", "^openclaw$"], capture_output=True, text=True).stdout.strip()],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if "ESTABLISHED" not in line:
                    continue
                # Extract remote host from lsof output
                match = re.search(r"->(\S+?):\d+\s", line)
                if not match:
                    continue
                remote = match.group(1)
                # Resolve to hostname if it's an IP
                is_allowed = any(
                    remote.startswith(h) or remote.endswith(h)
                    for h in ALLOWED_OUTBOUND_HOSTS
                )
                if not is_allowed and not remote.startswith("192.168.") and remote not in ("127.0.0.1", "::1"):
                    warnings.append(f"Unexpected outbound connection: {remote}")
        except Exception as e:
            warnings.append(f"Could not check network connections: {e}")

        # 4. Check that Signal is still locked down (reuse config from step 1)
        try:
            signal_conf = config.get("channels", {}).get("signal", {})
            if signal_conf.get("dmPolicy") != "allowlist":
                violations.append(f"Signal dmPolicy = `{signal_conf.get('dmPolicy')}` — should be allowlist")
            if signal_conf.get("groupPolicy") != "allowlist":
                violations.append(f"Signal groupPolicy = `{signal_conf.get('groupPolicy')}` — should be allowlist")
        except Exception:
            pass

        # Build result
        if violations:
            risk = "critical"
            summary = f":rotating_light: *Privacy/PII Leak Risk Detected*\n"
            summary += "\n".join(f"  :x: {v}" for v in violations)
            if warnings:
                summary += "\n" + "\n".join(f"  :warning: {w}" for w in warnings)
            await self.report_to_jordan(summary)
            log(LOG_ERROR, f"[privacy_monitor] VIOLATIONS: {violations}")
        elif warnings:
            risk = "medium"
            summary = f":warning: *Privacy Monitor — Warnings*\n"
            summary += "\n".join(f"  :warning: {w}" for w in warnings[:5])
            await self.notify(summary)
            log(LOG_WARN, f"[privacy_monitor] warnings: {warnings}")
        else:
            risk = "none"
            log(LOG_INFO, "[privacy_monitor] OK — no routing drift or PII leak vectors detected")

        return {"risk_level": risk, "violations": violations, "warnings": warnings}

    async def _analyze_nmap(self, task: dict) -> dict:
        """Analyze nmap scan results for new/unexpected devices or open ports."""
        devices = task.get("devices", [])
        threats = task.get("threats", [])

        if not devices and not threats:
            # Try fetching from NovaControl API
            try:
                resp = urllib.request.urlopen(f"{NOVACONTROL_API}/api/nmap/devices", timeout=10)
                data = json.loads(resp.read())
                devices = data.get("devices", [])
            except Exception:
                pass
            try:
                resp = urllib.request.urlopen(f"{NOVACONTROL_API}/api/nmap/threats", timeout=10)
                data = json.loads(resp.read())
                threats = data.get("threats", [])
            except Exception:
                pass

        if not devices:
            return None

        # Format for analysis
        device_summary = "\n".join(
            f"- {d.get('hostname', d.get('ip', '?'))}: {d.get('type', '?')} "
            f"(ports: {d.get('open_ports', [])})"
            for d in devices[:30]
        )
        threat_summary = "\n".join(
            f"- {t.get('description', t.get('type', '?'))}: severity={t.get('severity', '?')}"
            for t in threats[:10]
        ) if threats else "None reported"

        prompt = (
            f"Analyze this network scan:\n\n"
            f"DEVICES ({len(devices)}):\n{device_summary}\n\n"
            f"THREATS ({len(threats)}):\n{threat_summary}\n\n"
            f"Identify: unknown devices, suspicious open ports, potential intrusions."
        )

        system = (
            "You are Security Sentinel. Analyze network scans for threats.\n"
            "Return JSON: {\"risk_level\": \"critical|high|medium|low|none\", "
            "\"findings\": [{\"type\": \"unknown_device|open_port|intrusion|misconfiguration\", "
            "\"description\": \"...\", \"severity\": \"...\", \"ip\": \"...\"}], "
            "\"summary\": \"one paragraph assessment\", \"flag_jordan\": true/false}"
        )

        try:
            response = await self.infer(prompt, system=system)
        except Exception as e:
            log(f"Nmap analysis failed: {e}", level=LOG_ERROR, source="subagent.sentinel")
            return None

        result = self._parse_response(response)
        await self._report_security(result, "Network Scan")
        return result

    async def _analyze_camera(self, task: dict) -> dict:
        """Dispatch camera event to Lookout for vision, then assess threat."""
        smart_types = task.get("smart_types", [])
        if smart_types and all(t in ("vehicle", "licensePlate") for t in smart_types):
            return None

        description = task.get("description", task.get("text", ""))
        camera = task.get("camera", "unknown")

        prompt = (
            f"Security assessment for camera event:\n"
            f"Camera: {camera}\n"
            f"Event: {description}\n\n"
            f"Is this a genuine security concern or normal activity?"
        )

        system = (
            "You are Security Sentinel. Assess camera events.\n"
            "Return JSON: {\"threat\": true/false, \"risk_level\": \"critical|high|medium|low|none\", "
            "\"assessment\": \"explanation\", \"flag_jordan\": true/false}"
        )

        try:
            response = await self.infer(prompt, system=system)
        except Exception:
            return None

        result = self._parse_response(response)
        if result.get("threat"):
            await self._report_security(result, f"Camera: {camera}")
        return result

    async def _analyze_unifi(self, task: dict) -> dict:
        """Analyze UniFi network events (client connects/disconnects, AP issues)."""
        event = task.get("event", "")
        details = task.get("details", "")

        prompt = (
            f"Analyze this UniFi network event:\n"
            f"Event: {event}\n"
            f"Details: {details}\n\n"
            f"Is this normal network behavior or a security concern?"
        )

        system = (
            "You are Security Sentinel. Assess network events.\n"
            "Return JSON: {\"risk_level\": \"critical|high|medium|low|none\", "
            "\"assessment\": \"explanation\", \"action_needed\": true/false, "
            "\"flag_jordan\": true/false}"
        )

        try:
            response = await self.infer(prompt, system=system)
        except Exception:
            return None

        result = self._parse_response(response)
        if result.get("action_needed") or result.get("risk_level") in ("critical", "high"):
            await self._report_security(result, "UniFi Event")
        return result

    async def _threat_assessment(self, task: dict) -> dict:
        """Cross-reference multiple signals for composite threat assessment."""
        signals = task.get("signals", [])
        if not signals:
            return None

        prompt = (
            f"Perform a composite threat assessment from these signals:\n\n" +
            "\n".join(f"- [{s.get('source')}] {s.get('description')}" for s in signals[:10])
        )

        system = (
            "You are Security Sentinel. Correlate multiple security signals.\n"
            "Return JSON: {\"overall_risk\": \"critical|high|medium|low\", "
            "\"correlated_threats\": [{\"description\": \"...\", \"confidence\": 0.0-1.0}], "
            "\"recommendation\": \"what to do\", \"flag_jordan\": true/false}"
        )

        try:
            response = await self.infer(prompt, system=system)
        except Exception:
            return None

        result = self._parse_response(response)
        await self._report_security(result, "Threat Assessment")
        return result

    async def _generic_security(self, task: dict) -> dict:
        """Handle generic security events."""
        text = task.get("text", task.get("content", ""))
        if not text:
            return None

        prompt = f"Security assessment:\n{text[:3000]}"
        try:
            response = await self.infer(prompt)
        except Exception:
            return None

        return self._parse_response(response)

    def _parse_response(self, response: str) -> dict:
        try:
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()

            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end])
        except json.JSONDecodeError:
            pass
        return {"summary": response[:500], "risk_level": "unknown", "flag_jordan": False}

    async def _report_security(self, result: dict, context: str):
        risk = result.get("risk_level", result.get("overall_risk", "unknown"))
        emoji = {"critical": ":rotating_light:", "high": ":shield:", "medium": ":lock:",
                 "low": ":information_source:", "none": ":white_check_mark:"}.get(risk, ":mag:")

        msg = f"{emoji} *Sentinel — {context}* ({risk.upper()})\n"
        summary = result.get("summary", result.get("assessment", ""))
        if summary:
            msg += f"{summary[:300]}\n"
        findings = result.get("findings", [])
        if findings:
            for f in findings[:3]:
                msg += f"  • {f.get('description', '')[:100]}\n"

        if result.get("flag_jordan") or risk in ("critical", "high"):
            await self.report_to_jordan(msg)
        else:
            await self.notify(msg)


if __name__ == "__main__":
    SecuritySentinel().run()
