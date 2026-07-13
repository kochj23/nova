#!/usr/bin/env python3
"""
nova_mlx_chat.py — Detect and query MLX Chat for local LLM inference.

MLX Chat runs on M-series Macs and provides efficient GPU acceleration.
This script probes the MLX endpoint and routes requests.

Usage:
  python3 nova_mlx_chat.py --detect
  python3 nova_mlx_chat.py --model "mlx-community/Mistral-7B-Instruct-v0.1" --prompt "Hello"
  python3 nova_mlx_chat.py --health-check
  python3 nova_mlx_chat.py --list-models
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
MLX_ENDPOINT = os.getenv("MLX_CHAT_ENDPOINT", "http://127.0.0.1:5000")
MLX_TIMEOUT = int(os.getenv("MLX_CHAT_TIMEOUT", "30"))
DEFAULT_MODEL = os.getenv("MLX_CHAT_DEFAULT_MODEL", "mlx-community/Mistral-7B-Instruct-v0.1")

class MLXChatClient:
    """Client for MLX Chat inference."""
    
    def __init__(self, endpoint: str = MLX_ENDPOINT, timeout: int = MLX_TIMEOUT):
        self.endpoint = endpoint
        self.timeout = timeout
        self._health_cache = None
        self._health_cache_time = 0
    
    def detect(self, fast: bool = False) -> Dict:
        """
        Detect if MLX Chat is running and get basic info.
        
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
                 urljoin(self.endpoint, "/health")],
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
        Perform full health check including model status.
        
        Returns:
            Dict with mlx_ready, endpoint, models_loaded, response_time_ms
        """
        start = time.time()
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(self.timeout),
                 urljoin(self.endpoint, "/v1/models")],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            elapsed_ms = int((time.time() - start) * 1000)
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return {
                    "mlx_ready": True,
                    "endpoint": self.endpoint,
                    "models_loaded": [m.get("id") for m in data.get("data", [])],
                    "response_time_ms": elapsed_ms
                }
        except Exception:
            pass
        
        return {
            "mlx_ready": False,
            "endpoint": self.endpoint,
            "error": "Health check failed"
        }
    
    def query(self, prompt: str, model: str = DEFAULT_MODEL, 
              system: Optional[str] = None, temperature: float = 0.7,
              max_tokens: int = 256) -> Optional[str]:
        """
        Query MLX Chat for inference.
        
        Args:
            prompt: User prompt
            model: Model name (e.g., "mlx-community/Mistral-7B-Instruct-v0.1")
            system: Optional system prompt
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Max response tokens
        
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
            "max_tokens": max_tokens,
            "stream": False,
        }
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "-m", str(self.timeout),
                 urljoin(self.endpoint, "/v1/chat/completions"),
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload)],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                if "choices" in data and data["choices"]:
                    return data["choices"][0].get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"Error querying MLX: {e}", file=sys.stderr)
        
        return None
    
    def list_models(self) -> List[str]:
        """
        Get list of available models.
        
        Returns:
            List of model IDs
        """
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(self.timeout),
                 urljoin(self.endpoint, "/v1/models")],
                capture_output=True,
                text=True,
                timeout=self.timeout + 2
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return [m.get("id") for m in data.get("data", [])]
        except Exception:
            pass
        
        return []
    
    def current_model(self) -> Optional[str]:
        """Get currently loaded model."""
        models = self.list_models()
        return models[0] if models else None


def main():
    parser = argparse.ArgumentParser(description="MLX Chat integration for Nova")
    parser.add_argument("--detect", action="store_true", help="Detect if MLX is running")
    parser.add_argument("--health-check", action="store_true", help="Full health check")
    parser.add_argument("--list-models", action="store_true", help="List available models")
    parser.add_argument("--current-model", action="store_true", help="Get currently loaded model")
    parser.add_argument("--model", help="Model to use for queries")
    parser.add_argument("--prompt", help="Prompt/input text")
    parser.add_argument("--system", help="System prompt")
    parser.add_argument("--temperature", type=float, default=0.7, help="Temperature (0.0-1.0)")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max response tokens")
    parser.add_argument("--timeout", type=int, help="Request timeout (seconds)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--endpoint", default=MLX_ENDPOINT, help="MLX endpoint URL")
    
    args = parser.parse_args()
    
    # Override endpoint if provided
    endpoint = args.endpoint
    timeout = args.timeout if args.timeout else MLX_TIMEOUT
    
    client = MLXChatClient(endpoint=endpoint, timeout=timeout)
    
    # Handle commands
    if args.detect:
        result = client.detect()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result["running"]:
                print(f"✓ MLX Chat online at {result['endpoint']}")
                print(f"  Version: {result['version']}")
            else:
                print(f"✗ MLX Chat offline: {result['error']}")
        return 0 if result["running"] else 1
    
    elif args.health_check:
        result = client.health_check()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result["mlx_ready"]:
                print(f"✓ MLX Chat healthy")
                print(f"  Models: {', '.join(result['models_loaded'])}")
                print(f"  Response time: {result['response_time_ms']}ms")
            else:
                print(f"✗ MLX Chat check failed: {result.get('error', 'unknown error')}")
        return 0 if result["mlx_ready"] else 1
    
    elif args.list_models:
        models = client.list_models()
        if args.json:
            print(json.dumps({"models": models}, indent=2))
        else:
            if models:
                print("Available models:")
                for m in models:
                    print(f"  - {m}")
            else:
                print("No models available")
        return 0
    
    elif args.current_model:
        model = client.current_model()
        if args.json:
            print(json.dumps({"current_model": model}))
        else:
            print(model if model else "No model loaded")
        return 0
    
    elif args.prompt:
        # Query mode
        model = args.model or DEFAULT_MODEL
        response = client.query(
            args.prompt,
            model=model,
            system=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens
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
                    "error": "MLX query failed"
                }, indent=2))
            else:
                print("Error: MLX query failed", file=sys.stderr)
            return 1
    
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
