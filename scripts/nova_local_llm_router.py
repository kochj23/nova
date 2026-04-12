#!/usr/bin/env python3
"""
nova_local_llm_router.py — Route work to local LLMs to save cloud costs.

Decides which backend to use based on task complexity and availability:
1. MLX Chat (if available) — fastest for M-series Macs
2. OpenWebUI (if available) — alternative Ollama interface
3. TinyChat (Jason's proxy)
4. Direct Ollama
5. Cloud Claude (fallback)

Usage:
  python3 nova_local_llm_router.py --task "summarize_emails" --input "..."
  python3 nova_local_llm_router.py --task "analyze_code" --input "..."
  python3 nova_local_llm_router.py --disable-mlx --task "task" --input "text"
"""

import json
import subprocess
import sys
import time
import os
from enum import Enum
from typing import Optional, Dict
from urllib.parse import urljoin

class TaskComplexity(Enum):
    SIMPLE = "qwen3:4b"        # Fast, low memory (logging, tagging)
    MEDIUM = "qwen3:30b"       # Good balance (summarization, replies)
    HEAVY = "qwen3:72b"        # Complex reasoning (code review, analysis)
    MASSIVE = "qwen3:235b"     # For when you really need it


# Backend endpoints
MLX_ENDPOINT = os.getenv("MLX_CHAT_ENDPOINT", "http://127.0.0.1:5000")
OPENWEBUI_ENDPOINT = os.getenv("OPENWEBUI_ENDPOINT", "http://127.0.0.1:3000")
TINYCHAT_ENDPOINT = os.getenv("TINYCHAT_ENDPOINT", "http://127.0.0.1:8000")
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://127.0.0.1:11434")
BACKEND_TIMEOUT = int(os.getenv("BACKEND_TIMEOUT", "5"))


class BackendAvailability:
    """Check which local backends are available."""
    
    _mlx_available = None
    _openwebui_available = None
    _tinychat_available = None
    _mlx_check_time = 0
    _openwebui_check_time = 0
    _tinychat_check_time = 0
    _check_ttl = 60  # Cache availability for 60 seconds
    
    @staticmethod
    def is_mlx_available(force_check=False) -> bool:
        """Check if MLX Chat is running."""
        now = time.time()
        if (not force_check and BackendAvailability._mlx_available is not None and
            (now - BackendAvailability._mlx_check_time) < BackendAvailability._check_ttl):
            return BackendAvailability._mlx_available
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(BACKEND_TIMEOUT),
                 urljoin(MLX_ENDPOINT, "/health")],
                capture_output=True,
                text=True,
                timeout=BACKEND_TIMEOUT + 1
            )
            available = result.returncode == 0
        except Exception:
            available = False
        
        BackendAvailability._mlx_available = available
        BackendAvailability._mlx_check_time = now
        return available
    
    @staticmethod
    def is_openwebui_available(force_check=False) -> bool:
        """Check if OpenWebUI is running."""
        now = time.time()
        if (not force_check and BackendAvailability._openwebui_available is not None and
            (now - BackendAvailability._openwebui_check_time) < BackendAvailability._check_ttl):
            return BackendAvailability._openwebui_available
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(BACKEND_TIMEOUT),
                 urljoin(OPENWEBUI_ENDPOINT, "/api/version")],
                capture_output=True,
                text=True,
                timeout=BACKEND_TIMEOUT + 1
            )
            available = result.returncode == 0
        except Exception:
            available = False
        
        BackendAvailability._openwebui_available = available
        BackendAvailability._openwebui_check_time = now
        return available
    
    @staticmethod
    def is_tinychat_available(force_check=False) -> bool:
        """Check if TinyChat (Jason's proxy) is running."""
        now = time.time()
        if (not force_check and BackendAvailability._tinychat_available is not None and
            (now - BackendAvailability._tinychat_check_time) < BackendAvailability._check_ttl):
            return BackendAvailability._tinychat_available
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(BACKEND_TIMEOUT),
                 urljoin(TINYCHAT_ENDPOINT, "/health")],
                capture_output=True,
                text=True,
                timeout=BACKEND_TIMEOUT + 1
            )
            available = result.returncode == 0
        except Exception:
            available = False
        
        BackendAvailability._tinychat_available = available
        BackendAvailability._tinychat_check_time = now
        return available

TASK_ROUTING = {
    # Local LLM (qwen3:30b) — All routine/timed events EXCEPT email & dream journal
    "create_brief": TaskComplexity.MEDIUM,          # Morning brief
    "format_report": TaskComplexity.MEDIUM,         # Nightly report
    "summarize_news": TaskComplexity.MEDIUM,        # Subreddit summaries
    "github_digest": TaskComplexity.MEDIUM,         # GitHub activity digest
    "git_status_report": TaskComplexity.MEDIUM,     # Git repo status
    "metrics_summary": TaskComplexity.MEDIUM,       # Metrics report
    "home_watchdog_alert": TaskComplexity.MEDIUM,   # HomeKit/home alerts
    "memory_consolidation": TaskComplexity.MEDIUM,  # Vector memory consolidation
    "this_day_in_history": TaskComplexity.MEDIUM,   # Historical facts
    "weekly_review": TaskComplexity.MEDIUM,         # Project review
    "game_night": TaskComplexity.MEDIUM,            # Blompie game narrative
    
    # Simple tasks → qwen3:4b (fastest)
    "classify_log": TaskComplexity.SIMPLE,          # Log file analysis
    "extract_keywords": TaskComplexity.SIMPLE,      # Tag extraction
    "format_output": TaskComplexity.SIMPLE,         # Simple formatting
    
    # Local: email memory/recall — personal data must not leave the machine
    "summarize_emails":   TaskComplexity.MEDIUM,   # Email summaries — keep local (privacy)
    "email_recall":       TaskComplexity.MEDIUM,   # Email memory recall — keep local (privacy)
    "memory_recall":      TaskComplexity.MEDIUM,   # Any memory recall — keep local (privacy)
    "memory_query":       TaskComplexity.MEDIUM,   # Any memory query  — keep local (privacy)

    # Cloud Claude (keep reserved for quality-critical work)
    # SLACK, DREAMS, & IMAGE/CAMERA ANALYSIS — These stay on cloud
    "generate_reply": None,         # EMAIL REPLY — Use cloud for quality
    "slack_reply": None,            # SLACK — Use cloud
    "slack_thread_reply": None,     # SLACK — Use cloud
    "dream_journal_generate": None, # DREAM JOURNAL — Use cloud
    "dream_journal_deliver": None,  # DREAM JOURNAL — Use cloud
    "analyze_image": None,          # IMAGE — Use cloud
    "analyze_camera_frame": None,   # CAMERA — Use cloud
    "face_detection_analysis": None,# FACE/CAMERA — Use cloud
    "camera_alert_analysis": None,  # CAMERA ALERT — Use cloud
    
    # Code/architecture stays cloud
    "analyze_code": None,           # Use cloud
    "review_pull_request": None,    # Use cloud
    "debug_complex_issue": None,    # Use cloud
    "architecture_decision": None,  # Use cloud
    "deep_research": None,          # Use cloud
}

def query_mlx(model: str, prompt: str, system: str = None, temperature: float = 0.7) -> Optional[str]:
    """Query MLX Chat."""
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 256,
            "stream": False,
        }
        
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", 
             "-m", str(BACKEND_TIMEOUT + 120),
             urljoin(MLX_ENDPOINT, "/v1/chat/completions"),
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=BACKEND_TIMEOUT + 125
        )
        
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if "choices" in data and data["choices"]:
                return data["choices"][0].get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"[nova_local_llm_router] MLX query error: {e}", file=sys.stderr)
    
    return None


def query_openwebui(model: str, prompt: str, system: str = None, temperature: float = 0.7) -> Optional[str]:
    """Query OpenWebUI."""
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-m", str(BACKEND_TIMEOUT + 120),
             urljoin(OPENWEBUI_ENDPOINT, "/api/chat"),
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=BACKEND_TIMEOUT + 125
        )
        
        if result.returncode == 0 and result.stdout:
            lines = result.stdout.strip().split('\n')
            response_text = ""
            for line in lines:
                if line.strip():
                    try:
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            response_text += data["message"]["content"]
                    except json.JSONDecodeError:
                        pass
            if response_text:
                return response_text.strip()
    except Exception as e:
        print(f"[nova_local_llm_router] OpenWebUI query error: {e}", file=sys.stderr)
    
    return None


def query_tinychat(model: str, prompt: str, system: str = None, temperature: float = 0.7) -> Optional[str]:
    """Query TinyChat (OpenAI-compatible proxy to Ollama)."""
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", 
             "-m", str(BACKEND_TIMEOUT + 120),
             urljoin(TINYCHAT_ENDPOINT, "/api/chat/stream"),
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=BACKEND_TIMEOUT + 125
        )
        
        if result.returncode == 0:
            # TinyChat returns SSE stream, parse response
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if line.startswith('data:'):
                    try:
                        data = json.loads(line[5:].strip())
                        if 'choices' in data and data['choices']:
                            return data['choices'][0].get('message', {}).get('content', '').strip()
                    except:
                        pass
    except Exception as e:
        print(f"[nova_local_llm_router] TinyChat query error: {e}", file=sys.stderr)
    
    return None

def query_ollama(model: str, prompt: str, system: str = None, temperature: float = 0.7) -> Optional[str]:
    """Query local Ollama instance directly."""
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "temperature": temperature,
        }
        
        if system:
            payload["system"] = system
        
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-m", str(BACKEND_TIMEOUT + 120),
             urljoin(OLLAMA_ENDPOINT, "/api/generate"),
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=BACKEND_TIMEOUT + 125
        )
        
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("response", "").strip()
    except Exception as e:
        print(f"[nova_local_llm_router] Ollama query error: {e}", file=sys.stderr)
    
    return None

def route_task(task: str, input_text: str, system_prompt: str = None) -> dict:
    """Route task to appropriate local model."""
    
    # Determine complexity
    complexity = TASK_ROUTING.get(task)
    
    # If None (not in simple list), return "use cloud"
    if complexity is None:
        return {
            "success": False,
            "task": task,
            "error": "Task not enabled for local routing",
            "fallback": "Use cloud Claude",
            "source": "cloud",
        }
    
    model = complexity.value
    
    print(f"[nova_local_llm_router] Task: {task} → Model: {model} (via TinyChat)")
    
    # Try TinyChat first (Jason's proxy), fall back to direct Ollama
    response = query_tinychat(model, input_text, system_prompt)
    
    if not response:
        print(f"[nova_local_llm_router] TinyChat failed, trying direct Ollama...")
        response = query_ollama(model, input_text, system_prompt)
    
    if response:
        return {
            "success": True,
            "model": model,
            "task": task,
            "response": response,
            "source": "local",
        }
    else:
        return {
            "success": False,
            "model": model,
            "task": task,
            "error": "Ollama query failed",
            "fallback": "Use cloud Claude",
        }

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Route work to local LLMs")
    parser.add_argument("--task", required=True, help="Task name (e.g., summarize_emails)")
    parser.add_argument("--input", required=True, help="Input text/prompt")
    parser.add_argument("--system", help="System prompt")
    parser.add_argument("--temperature", type=float, default=0.7, help="Temperature")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    args = parser.parse_args()
    
    result = route_task(args.task, args.input, args.system)
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["success"]:
            print(result["response"])
        else:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
