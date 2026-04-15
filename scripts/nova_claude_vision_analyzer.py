#!/usr/bin/env python3
"""
TRACK 2: Claude-powered vision event analysis
Analyzes camera events (people, packages, anomalies) with Claude reasoning.
Generates daily summaries and threat reports.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import subprocess
import sys

MEMORY_URL = "http://127.0.0.1:18790"
WORKSPACE = Path.home() / ".openclaw/workspace"
CLIPS_DIR = Path("/Volumes/Data/motion_clips")

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

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
    """Search memory for related events."""
    try:
        url = f"{MEMORY_URL}/recall?q={urllib.parse.quote(query)}&n={n_results}&source={source_filter}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("results", [])
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []

def call_claude_via_openrouter(prompt, system="You are a home security AI analyzing vision events."):
    """
    Call Claude via OpenRouter API.
    Requires OPENROUTER_API_KEY in environment.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log("⚠️  OPENROUTER_API_KEY not set — cannot call Claude")
        return None
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:18790",
        "X-Title": "Nova Vision Analyzer"
    }
    
    data = {
        "model": "anthropic/claude-3.5-sonnet",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "system": system,
        "temperature": 0.7,
        "max_tokens": 1500
    }
    
    try:
        req = urllib.request.Request(
            "https://openrouter.io/api/v1/chat/completions",
            data=json.dumps(data).encode(),
            headers=headers
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            
            if "choices" in result and len(result["choices"]) > 0:
                return result["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"Claude API error: {e}")
        return None
    
    return None

def analyze_daily_events():
    """
    Analyze all events from today, generate summary report.
    Posts to #general channel.
    """
    log("Analyzing daily vision events...")
    
    # Recall today's events
    today = datetime.now().date()
    query = f"vision events {today.isoformat()}"
    
    events = recall(query, n_results=20, source_filter="vision")
    
    if not events:
        log("No events recorded today")
        return None
    
    # Build prompt for Claude
    event_summary = "\n".join([f"- {e.get('text', '')}" for e in events[:10]])
    
    prompt = f"""
Analyze these home security events from today and create a brief, actionable summary:

{event_summary}

Format your response as:
1. Key Events (2-3 bullet points)
2. Anomalies or Concerns (if any)
3. Threat Level (Low/Medium/High)
4. Recommended Actions

Be concise and practical.
"""
    
    log("Calling Claude for analysis...")
    analysis = call_claude_via_openrouter(prompt)
    
    if analysis:
        log("✓ Claude analysis complete")
        
        # Store the analysis
        remember(
            f"Daily vision report ({today.isoformat()}): {analysis}",
            source="vision"
        )
        
        # Post to #general
        post_to_slack_general(analysis, today)
        
        return analysis
    else:
        log("✗ Claude analysis failed")
        return None

def analyze_threat_profile():
    """
    Weekly threat analysis: review all motion clips and anomalies.
    """
    log("Generating weekly threat profile...")
    
    # Count clips from past 7 days
    if CLIPS_DIR.exists():
        cutoff = datetime.now() - timedelta(days=7)
        clips = [
            f for f in CLIPS_DIR.glob("motion_*.mp4")
            if datetime.fromtimestamp(f.stat().st_mtime) > cutoff
        ]
        clip_count = len(clips)
    else:
        clip_count = 0
    
    # Recall anomalies from memory
    anomalies = recall("anomaly suspicious unusual", n_results=10, source_filter="vision")
    
    prompt = f"""
Generate a threat assessment report based on:
- Motion clips captured this week: {clip_count}
- Anomalies detected: {len(anomalies)}
- Recent suspicious activity: {anomalies[0].get('text', 'none') if anomalies else 'none'}

Provide:
1. Overall Threat Level (Low/Medium/High)
2. Key Concerns
3. Recommendations
4. Next Week's Monitoring Focus

Keep it brief but thorough.
"""
    
    log("Calling Claude for threat analysis...")
    threat_report = call_claude_via_openrouter(prompt)
    
    if threat_report:
        log("✓ Threat analysis complete")
        
        remember(
            f"Weekly threat report: {threat_report}",
            source="vision"
        )
        
        # Post to #general (weekly)
        post_to_slack_general(threat_report, None, is_threat=True)
        
        return threat_report
    
    return None

def post_to_slack_general(message, date=None, is_threat=False):
    """Post analysis to #general channel."""
    try:
        header = "🔒 THREAT REPORT" if is_threat else f"📹 DAILY VISION REPORT ({date})"
        
        slack_msg = f"""{header}

{message}

---
Generated by Nova Vision Analyzer
"""
        
        # Use the message tool
        subprocess.run([
            "python3", "-c",
            f"""
import subprocess
subprocess.run([
    'python3', '-c',
    'from message import message; message("send", channel="slack", target="C049EPC32", message={repr(slack_msg)})'
])
"""
        ], check=False)
        
        log(f"✓ Posted to #general: {header}")
        
    except Exception as e:
        log(f"Slack post failed: {e}")

def anomaly_alert(description, severity="medium"):
    """
    Alert on detected anomalies (in real-time).
    severity: low, medium, high
    """
    log(f"🚨 ANOMALY ALERT ({severity}): {description}")
    
    # Classify with Claude
    prompt = f"""
Quick assessment of this home security anomaly:
"{description}"

Is this concerning? (Yes/No)
If yes, what's the recommended action?
One sentence each.
"""
    
    response = call_claude_via_openrouter(prompt)
    
    if response:
        log(f"Claude assessment: {response}")
        
        # Store alert
        remember(
            f"ANOMALY ALERT ({severity}): {description} — {response}",
            source="vision"
        )
        
        # Send to Slack if high severity
        if severity == "high":
            try:
                subprocess.run([
                    "python3", "-c",
                    f"""
import subprocess
msg = '🚨 HIGH SEVERITY ANOMALY: {description}\\n\\n{response}'
subprocess.run(['python3', '-c', f'message("send", channel="slack", target="C0ATAF7NZG9", message={repr(msg)})'])
"""
                ], check=False)
            except:
                pass

def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "daily":
            analyze_daily_events()
        elif sys.argv[1] == "threat":
            analyze_threat_profile()
        elif sys.argv[1] == "anomaly" and len(sys.argv) > 2:
            severity = sys.argv[3] if len(sys.argv) > 3 else "medium"
            anomaly_alert(" ".join(sys.argv[2:]), severity)
        return
    
    # Default: run daily analysis
    analyze_daily_events()

if __name__ == "__main__":
    main()
