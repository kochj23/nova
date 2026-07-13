#!/usr/bin/env python3
"""
nova_openwebui.py — Detect and query OpenWebUI for local Ollama inference.

OpenWebUI provides a web interface and API for Ollama models.
This script probes the OpenWebUI endpoint and routes requests.

Usage:
  python3 nova_openwebui.py --detect
  python3 nova_openwebui.py --model "mistral" --prompt "Hello"
  python3 nova_openwebui.py --health-check
  python3 nova_openwebui.py --list-models
"""

import json
import subprocess
import sys
import time
import argparse
import os
from typing import Optional, Dict, List
from urllib.parse import urljoin

# Configuration
OPENWEBUI_ENDPOINT = os.getenv("OPENWEBUI_ENDPOINT", "http://192.168.1.6:3000")
OPENWEBUI_TIMEOUT = int(os.getenv("OPENWEBUI_TIMEOUT", "30"))
DEFAULT_MODEL = os.getenv("OPENWEBUI_DEFAULT_MODEL", "mistral")

class OpenWebUIClient:
    """Client for OpenWebUI (Ollama web interface) inference."""
    
    def __init__(self, endpoint: str = OPENWEBUI_ENDPOINT, timeout: int = OPENWEBUI_TIMEOUT):
        self.endpoint = endpoint
        self.timeout = timeout
        self._health_cache = None
        self._health_cache_time = 0
    
    def detect(self, fast: bool = False) -> Dict:
        """
        Detect if OpenWebUI is running and get basic info.
        
        Args:
            fast: If True, return cached result if <60 seconds old
        
        Returns:
            Dict with running, endpoint, port, status, version (or error)
        """
        now = time.time()
        
        # Use cache if available and fresh
        if fast and self._health_cache and (now - self._health_cache_time) < 60:
            return self._health_cache
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(self.timeout),
                 urljoin(self.endpoint, "/api/version")],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                response = {
                    "running": True,
                    "endpoint": self.endpoint,
                    "port": int(self.endpoint.split(":")[-1]),
                    "status": "online",
                    "version": data.get("version", "unknown"),
                }
                self._health_cache = response
                self._health_cache_time = now
                return response
        except Exception as e:
            pass
        
        return {
            "running": False,
            "endpoint": self.endpoint,
            "status": "offline",
            "error": f"Connection failed (timeout: {self.timeout}s)"
        }
    
    def health_check(self) -> Dict:
        """
        Perform full health check including Ollama backend status.
        
        Returns:
            Dict with openwebui_ready, endpoint, ollama_ready, models_count, response_time_ms
        """
        start = time.time()
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(self.timeout),
                 urljoin(self.endpoint, "/api/tags")],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            elapsed_ms = int((time.time() - start) * 1000)
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                models = data.get("models", [])
                return {
                    "openwebui_ready": True,
                    "endpoint": self.endpoint,
                    "ollama_ready": True,
                    "models_count": len(models),
                    "models_sample": [m.get("name") for m in models[:3]],
                    "response_time_ms": elapsed_ms
                }
        except Exception:
            pass
        
        return {
            "openwebui_ready": False,
            "endpoint": self.endpoint,
            "error": "Health check failed"
        }
    
    def query(self, prompt: str, model: str = DEFAULT_MODEL,
              system: Optional[str] = None, temperature: float = 0.7,
              max_tokens: int = 256, stream: bool = False) -> Optional[str]:
        """
        Query OpenWebUI for inference.
        
        Args:
            prompt: User prompt
            model: Model name (e.g., "mistral")
            system: Optional system prompt
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Max response tokens
            stream: If True, stream response line by line
        
        Returns:
            Response text, or None if request fails
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-m", str(self.timeout),
                 urljoin(self.endpoint, "/api/chat"),
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload)],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            if result.returncode == 0 and result.stdout:
                # OpenWebUI returns JSON lines (one per message chunk) or single JSON
                lines = result.stdout.strip().split('\n')
                response_text = ""
                
                for line in lines:
                    if line.strip():
                        try:
                            data = json.loads(line)
                            if "message" in data and "content" in data["message"]:
                                response_text += data["message"]["content"]
                        except json.JSONDecodeError:
                            # Skip malformed lines
                            pass
                
                if response_text:
                    return response_text.strip()
        except Exception as e:
            print(f"Error querying OpenWebUI: {e}", file=sys.stderr)
        
        return None
    
    def list_models(self) -> List[Dict]:
        """
        Get list of available models.
        
        Returns:
            List of model dicts with name, size, modified time
        """
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(self.timeout),
                 urljoin(self.endpoint, "/api/tags")],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                models = []
                for m in data.get("models", []):
                    models.append({
                        "name": m.get("name"),
                        "size": m.get("size", "unknown"),
                        "modified": m.get("modified_at", "unknown"),
                    })
                return models
        except Exception:
            pass
        
        return []
    
    def get_model_info(self, model_name: str) -> Optional[Dict]:
        """Get detailed info about a specific model."""
        models = self.list_models()
        for m in models:
            if m["name"] == model_name:
                return m
        return None


def main():
    parser = argparse.ArgumentParser(description="OpenWebUI integration for Nova")
    parser.add_argument("--detect", action="store_true", help="Detect if OpenWebUI is running")
    parser.add_argument("--health-check", action="store_true", help="Full health check")
    parser.add_argument("--list-models", action="store_true", help="List available models")
    parser.add_argument("--model", help="Model to use for queries")
    parser.add_argument("--prompt", help="Prompt/input text")
    parser.add_argument("--system", help="System prompt")
    parser.add_argument("--temperature", type=float, default=0.7, help="Temperature (0.0-1.0)")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max response tokens")
    parser.add_argument("--stream", action="store_true", help="Stream response")
    parser.add_argument("--timeout", type=int, help="Request timeout (seconds)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--endpoint", default=OPENWEBUI_ENDPOINT, help="OpenWebUI endpoint URL")
    
    args = parser.parse_args()
    
    # Override endpoint if provided
    endpoint = args.endpoint
    timeout = args.timeout if args.timeout else OPENWEBUI_TIMEOUT
    
    client = OpenWebUIClient(endpoint=endpoint, timeout=timeout)
    
    # Handle commands
    if args.detect:
        result = client.detect()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result["running"]:
                print(f"✓ OpenWebUI online at {result['endpoint']}")
                print(f"  Version: {result['version']}")
            else:
                print(f"✗ OpenWebUI offline: {result['error']}")
        return 0 if result["running"] else 1
    
    elif args.health_check:
        result = client.health_check()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("openwebui_ready"):
                print(f"✓ OpenWebUI healthy")
                print(f"  Models available: {result['models_count']}")
                if result.get("models_sample"):
                    print(f"  Sample: {', '.join(result['models_sample'])}")
                print(f"  Response time: {result['response_time_ms']}ms")
            else:
                print(f"✗ OpenWebUI check failed: {result.get('error', 'unknown error')}")
        return 0 if result.get("openwebui_ready") else 1
    
    elif args.list_models:
        models = client.list_models()
        if args.json:
            print(json.dumps({"models": models}, indent=2))
        else:
            if models:
                print("Available models:")
                for m in models:
                    print(f"  - {m['name']:<20} ({m['size']}, {m['modified']})")
            else:
                print("No models available")
        return 0
    
    elif args.prompt:
        # Query mode
        model = args.model or DEFAULT_MODEL
        response = client.query(
            args.prompt,
            model=model,
            system=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            stream=args.stream
        )
        
        if response:
            if args.json:
                print(json.dumps({
                    "success": True,
                    "model": model,
                    "response": response
                }, indent=2))
            else:
                print(response)
            return 0
        else:
            if args.json:
                print(json.dumps({
                    "success": False,
                    "error": "OpenWebUI query failed"
                }, indent=2))
            else:
                print("Error: OpenWebUI query failed", file=sys.stderr)
            return 1
    
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
