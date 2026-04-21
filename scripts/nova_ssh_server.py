#!/usr/bin/env python3
"""
nova_ssh_server.py — SSH remote access to Nova.

Allows Jordan to chat with Nova from any terminal via SSH.
Messages are sent to the OpenClaw gateway agent session.
Authenticated via SSH keys only (no passwords).

Port: 2222 (loopback by default, can be changed for remote access)
Keys: Uses Jordan's authorized SSH public keys from ~/.ssh/authorized_keys

Usage:
  # Start server
  python3 nova_ssh_server.py

  # Connect from any terminal
  ssh -p 2222 nova@localhost
  ssh -p 2222 nova@192.168.1.6   # from another device on LAN

Written by Jordan Koch.
"""

import asyncio
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import asyncssh
except ImportError:
    print("ERROR: pip install asyncssh")
    sys.exit(1)

HOST = "0.0.0.0"
PORT = 2222
HOST_KEY_PATH = Path.home() / ".openclaw/ssh/nova_host_key"
AUTHORIZED_KEYS = Path.home() / ".ssh/authorized_keys"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "nova:latest"
VECTOR_URL = "http://127.0.0.1:18790"

SYSTEM_PROMPT = """You are Nova, Jordan Koch's AI familiar. He's connected via SSH.
Be concise and direct — this is a terminal, not a chat window.
You have access to 1.27M memories. If he asks about something specific,
check your memories first."""


def log(msg):
    print(f"[nova-ssh {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def recall(query, n=3):
    try:
        q = urllib.parse.quote(query)
        req = urllib.request.Request(f"{VECTOR_URL}/recall?q={q}&n={n}")
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return [m["text"][:300] for m in data.get("memories", [])]
    except Exception:
        return []


def generate(prompt, context=""):
    full_prompt = SYSTEM_PROMPT
    if context:
        full_prompt += f"\n\nRelevant memories:\n{context}\n"
    full_prompt += f"\n\nJordan: {prompt}\nNova:"

    try:
        payload = json.dumps({
            "model": MODEL,
            "prompt": f"/no_think\n\n{full_prompt}",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.6, "num_predict": 500},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("response", "").strip()
    except Exception as e:
        return f"[error: {e}]"


class NovaSSHServer(asyncssh.SSHServer):
    def connection_made(self, conn):
        log(f"Connection from {conn.get_extra_info('peername')[0]}")

    def connection_lost(self, exc):
        log("Connection closed")

    def begin_auth(self, username):
        return username in ("nova", "jordan", "kochj")

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        if not AUTHORIZED_KEYS.exists():
            return False
        try:
            authorized = asyncssh.read_authorized_keys(str(AUTHORIZED_KEYS))
            return authorized.validate(key, username)
        except Exception:
            return False

    def password_auth_supported(self):
        return False


async def handle_session(process):
    process.stdout.write("\r\n\033[1;35m  Nova SSH Terminal\033[0m\r\n")
    process.stdout.write("  Type a message, or 'quit' to disconnect.\r\n\r\n")

    history = []

    while True:
        process.stdout.write("\033[1;36mnova>\033[0m ")
        try:
            line = await asyncio.wait_for(process.stdin.readline(), timeout=300)
        except asyncio.TimeoutError:
            process.stdout.write("\r\n[session timeout]\r\n")
            break

        if not line:
            break

        msg = line.strip()
        if not msg:
            continue
        if msg.lower() in ("quit", "exit", "bye"):
            process.stdout.write("Goodbye.\r\n")
            break

        # Recall relevant memories
        memories = recall(msg)
        context = "\n".join(f"- {m}" for m in memories) if memories else ""

        # Build conversation context from history
        conv = "\n".join(history[-6:])
        if conv:
            context = f"Recent conversation:\n{conv}\n\n{context}"

        process.stdout.write("\033[2m  thinking...\033[0m")
        response = await asyncio.get_event_loop().run_in_executor(
            None, generate, msg, context
        )
        # Clear "thinking..." line
        process.stdout.write("\r\033[K")

        process.stdout.write(f"\033[0;35m{response}\033[0m\r\n\r\n")

        history.append(f"Jordan: {msg}")
        history.append(f"Nova: {response[:200]}")

    process.exit(0)


async def start_server():
    HOST_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not HOST_KEY_PATH.exists():
        log("Generating SSH host key...")
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(str(HOST_KEY_PATH))
        HOST_KEY_PATH.chmod(0o600)
        log(f"Host key saved to {HOST_KEY_PATH}")

    log(f"Starting SSH server on {HOST}:{PORT}")
    log(f"Connect: ssh -p {PORT} nova@localhost")

    await asyncssh.create_server(
        NovaSSHServer,
        host=HOST,
        port=PORT,
        server_host_keys=[str(HOST_KEY_PATH)],
        process_factory=handle_session,
    )


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_server())
        loop.run_forever()
    except KeyboardInterrupt:
        log("Shutting down")
    finally:
        loop.close()


if __name__ == "__main__":
    import urllib.parse
    main()
