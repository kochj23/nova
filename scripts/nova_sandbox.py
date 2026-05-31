#!/usr/bin/env python3
"""
nova_sandbox.py — Docker-based sandbox for running untrusted code.

Spins up a container, runs code, captures output, auto-cleans.
Constraints: 2GB memory, 2 CPUs, 100 PIDs, read-only FS, 5min timeout, no host mounts.

Usage:
  python3 nova_sandbox.py --run "print(2+2)"
  python3 nova_sandbox.py --rebuild-image

Written by Jordan Koch (via Claude).
"""

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

DOCKER = "/opt/homebrew/bin/docker"
IMAGE = "nova-sandbox:latest"
DOCKERFILE_DIR = Path.home() / ".openclaw/docker/nova-sandbox"
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 300
DEFAULT_MEMORY = "2g"
MAX_PIDS = 100
CPUS = "2"

DB_HOST = "localhost"
DB_NAME = "nova_ops"
DB_USER = "kochj"


def log(msg: str):
    print(f"[sandbox] {msg}", flush=True)


def db_exec(sql: str):
    subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-c", sql],
        capture_output=True, text=True, timeout=10
    )


def ensure_image() -> bool:
    """Check if sandbox image exists, build if not."""
    result = subprocess.run([DOCKER, "images", "-q", IMAGE], capture_output=True, text=True)
    if result.stdout.strip():
        return True
    return rebuild_image()


def rebuild_image() -> bool:
    """Build the sandbox Docker image."""
    DOCKERFILE_DIR.mkdir(parents=True, exist_ok=True)
    dockerfile = DOCKERFILE_DIR / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-slim\n"
        "RUN pip install --no-cache-dir requests numpy pandas beautifulsoup4 httpx pyyaml lxml\n"
        "RUN useradd -m sandbox\n"
        "USER sandbox\n"
        "WORKDIR /home/sandbox\n"
        'ENTRYPOINT ["python3", "-c"]\n'
    )
    log(f"Building image from {DOCKERFILE_DIR}...")
    result = subprocess.run(
        [DOCKER, "build", "-t", IMAGE, str(DOCKERFILE_DIR)],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        log(f"Build failed: {result.stderr[:500]}")
        return False
    log("Image built successfully")
    return True


def run_sandboxed(code: str, language: str = "python", timeout: int = DEFAULT_TIMEOUT,
                  session_id: str = "", trace_id: str = "") -> dict:
    """Run code in a sandboxed container. Returns {exit_code, stdout, stderr, duration_ms, status}."""
    timeout = min(timeout, MAX_TIMEOUT)
    run_id = str(uuid.uuid4())
    container_name = f"nova-sandbox-{run_id[:8]}"

    # Record start
    escaped_code = code.replace("'", "''")[:5000]
    db_exec(f"INSERT INTO sandbox_runs (run_id, session_id, trace_id, code, language, container_id, status) "
            f"VALUES ('{run_id}', '{session_id}', '{trace_id}', '{escaped_code}', '{language}', "
            f"'{container_name}', 'running')")

    if not ensure_image():
        result = {"exit_code": -1, "stdout": "", "stderr": "Failed to build sandbox image",
                  "duration_ms": 0, "status": "failure"}
        db_exec(f"UPDATE sandbox_runs SET status='failure', stderr='Image build failed', "
                f"completed_at=now() WHERE run_id='{run_id}'")
        return result

    start = time.time()
    try:
        proc = subprocess.run(
            [DOCKER, "run", "--rm",
             "--name", container_name,
             "--memory", DEFAULT_MEMORY,
             "--cpus", CPUS,
             "--pids-limit", str(MAX_PIDS),
             "--read-only",
             "--tmpfs", "/tmp:size=100m",
             "--network", "bridge",
             "--security-opt", "no-new-privileges",
             IMAGE, code],
            capture_output=True, text=True, timeout=timeout
        )
        duration_ms = int((time.time() - start) * 1000)
        status = "success" if proc.returncode == 0 else "failure"

        result = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:10000],
            "stderr": proc.stderr[:5000],
            "duration_ms": duration_ms,
            "status": status,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        subprocess.run([DOCKER, "kill", container_name], capture_output=True, timeout=10)
        result = {"exit_code": -1, "stdout": "", "stderr": f"Timeout after {timeout}s",
                  "duration_ms": duration_ms, "status": "timeout"}

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        result = {"exit_code": -1, "stdout": "", "stderr": str(e),
                  "duration_ms": duration_ms, "status": "failure"}

    # Record completion
    escaped_stdout = result["stdout"].replace("'", "''")[:5000]
    escaped_stderr = result["stderr"].replace("'", "''")[:2000]
    db_exec(f"UPDATE sandbox_runs SET exit_code={result['exit_code']}, "
            f"stdout='{escaped_stdout}', stderr='{escaped_stderr}', "
            f"duration_ms={result['duration_ms']}, status='{result['status']}', "
            f"completed_at=now() WHERE run_id='{run_id}'")

    return result


def cleanup_stale():
    """Kill sandbox containers older than MAX_TIMEOUT."""
    result = subprocess.run(
        [DOCKER, "ps", "--filter", "name=nova-sandbox-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10
    )
    for name in result.stdout.strip().split("\n"):
        if name.strip():
            log(f"Killing stale container: {name}")
            subprocess.run([DOCKER, "kill", name], capture_output=True, timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=str, help="Code to run in sandbox")
    parser.add_argument("--rebuild-image", action="store_true", help="Rebuild the Docker image")
    parser.add_argument("--cleanup", action="store_true", help="Kill stale containers")
    args = parser.parse_args()

    if args.rebuild_image:
        rebuild_image()
    elif args.cleanup:
        cleanup_stale()
    elif args.run:
        result = run_sandboxed(args.run, session_id="cli", trace_id="manual")
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
