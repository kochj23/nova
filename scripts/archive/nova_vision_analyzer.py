#!/usr/bin/env python3
"""
nova_vision_analyzer.py — Local-only vision event analysis.

Analyzes camera events (people, packages, anomalies) using local LLM.
Generates daily summaries and threat reports. All processing stays on-device.

Usage:
  python3 nova_vision_analyzer.py              # daily analysis (default)
  python3 nova_vision_analyzer.py daily        # daily analysis
  python3 nova_vision_analyzer.py threat       # weekly threat profile
  python3 nova_vision_analyzer.py anomaly "description" [severity]

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3-coder:30b"
VISION_MODEL = "qwen3-vl:4b"
CLIPS_DIR = Path("/Volumes/Data/motion_clips")


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[nova_vision_analyzer {ts}] {msg}", flush=True)


def remember(text, source="vision"):
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("id")
    except Exception as e:
        log(f"Memory store failed: {e}")
        return None


def recall(query, n_results=5, source_filter="vision"):
    try:
        url = f"{MEMORY_URL}/recall?q={urllib.parse.quote(query)}&n={n_results}&source={source_filter}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("results", data.get("memories", []))
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def query_local(prompt, system="You are a home security AI analyzing vision events. Be concise and practical."):
    """Query local Ollama model. All data stays on-device."""
    payload = {
        "model": MODEL,
        "prompt": f"/no_think\n\n{prompt}",
        "system": system,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.4,
            "num_predict": 800,
            "num_ctx": 8192,
        }
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        response = result.get("response", "").strip()
        if "<think>" in response:
            think_end = response.rfind("</think>")
            if think_end > 0:
                response = response[think_end + 8:].strip()
        return response
    except Exception as e:
        log(f"Ollama query failed: {e}")
        return None


def describe_image(image_path, prompt="Describe this security camera image in 1-2 sentences. Focus on people, vehicles, packages, and anything unusual."):
    """Use local vision model to describe a camera frame or snapshot."""
    import base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 200}
        }).encode()

        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read())
        return result.get("response", "").strip()
    except Exception as e:
        log(f"Vision model failed: {e}")
        return None


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def analyze_daily_events():
    """Analyze today's vision events and generate a summary."""
    log("Analyzing daily vision events...")

    today = datetime.now().date()
    events = recall(f"vision events {today.isoformat()}", n_results=20, source_filter="vision")

    if not events:
        log("No events recorded today")
        return None

    event_summary = "\n".join([f"- {e.get('text', '')[:200]}" for e in events[:15]])

    prompt = f"""Analyze these home security events from today and create a brief, actionable summary:

{event_summary}

Format:
1. Key Events (2-3 bullet points)
2. Anomalies or Concerns (if any)
3. Threat Level (Low/Medium/High)
4. Recommended Actions (only if threat is Medium+)

Be concise. If nothing unusual, say so in one line."""

    log("Querying local LLM for analysis...")
    analysis = query_local(prompt)

    if analysis:
        log("Analysis complete")
        remember(f"Daily vision report ({today.isoformat()}): {analysis}", source="vision")

        msg = (
            f":camera: *Daily Vision Report — {today.isoformat()}*\n\n"
            f"{analysis}\n\n"
            f"_— Nova Vision Analyzer (local)_"
        )
        slack_post(msg)
        return analysis

    log("Analysis failed — LLM unavailable")
    return None


def analyze_threat_profile():
    """Weekly threat analysis: review motion clips and anomalies."""
    log("Generating weekly threat profile...")

    clip_count = 0
    if CLIPS_DIR.exists():
        cutoff = datetime.now() - timedelta(days=7)
        clips = [
            f for f in CLIPS_DIR.glob("motion_*.mp4")
            if datetime.fromtimestamp(f.stat().st_mtime) > cutoff
        ]
        clip_count = len(clips)

    anomalies = recall("anomaly suspicious unusual", n_results=10, source_filter="vision")
    anomaly_text = anomalies[0].get('text', 'none')[:200] if anomalies else 'none'

    prompt = f"""Generate a brief home security threat assessment:
- Motion clips this week: {clip_count}
- Anomalies detected: {len(anomalies)}
- Most recent suspicious activity: {anomaly_text}

Provide:
1. Overall Threat Level (Low/Medium/High)
2. Key Concerns (if any)
3. Recommendations (only if Medium+)

If everything looks normal, say so briefly."""

    log("Querying local LLM for threat analysis...")
    report = query_local(prompt)

    if report:
        log("Threat analysis complete")
        remember(f"Weekly threat report: {report}", source="vision")

        msg = (
            f":shield: *Weekly Threat Report*\n\n"
            f"{report}\n\n"
            f"_— Nova Vision Analyzer (local)_"
        )
        slack_post(msg)
        return report

    return None


def anomaly_alert(description, severity="medium"):
    """Real-time anomaly classification and alert."""
    log(f"ANOMALY ALERT ({severity}): {description}")

    prompt = f"""Quick assessment of this home security anomaly:
"{description}"

Is this concerning? (Yes/No)
If yes, what's the recommended action?
One sentence each."""

    response = query_local(prompt)

    if response:
        log(f"Assessment: {response}")
        remember(f"ANOMALY ALERT ({severity}): {description} — {response}", source="vision")

        if severity == "high":
            msg = f":rotating_light: *HIGH SEVERITY ANOMALY*\n{description}\n\n{response}"
            slack_post(msg)

    return response


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "daily":
            analyze_daily_events()
        elif sys.argv[1] == "threat":
            analyze_threat_profile()
        elif sys.argv[1] == "describe" and len(sys.argv) > 2:
            image_path = sys.argv[2]
            prompt = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else None
            result = describe_image(image_path, prompt) if prompt else describe_image(image_path)
            if result:
                print(result)
            else:
                print("Vision model unavailable", file=sys.stderr)
                sys.exit(1)
        elif sys.argv[1] == "anomaly" and len(sys.argv) > 2:
            desc = " ".join(sys.argv[2:])
            severity = "medium"
            if sys.argv[-1] in ("low", "medium", "high"):
                severity = sys.argv[-1]
                desc = " ".join(sys.argv[2:-1])
            anomaly_alert(desc, severity)
        return

    analyze_daily_events()


if __name__ == "__main__":
    main()
