#!/usr/bin/env python3
"""
nova_writeas.py — Publish posts to Nova's Write.as journal.

Usage:
  nova_writeas.py post --title "Title" --body "Markdown body" [--tags "dream,surreal"]
  nova_writeas.py post --title "Title" --file /path/to/post.md [--tags "essay,security"]
  nova_writeas.py list [--limit 10]
  nova_writeas.py test

The blog collection is 'novakoch' at https://novakoch.writeas.com/
Custom domain: nova.digitalnoise.net (once configured)

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://write.as/api"
COLLECTION = "novakoch"
LOG_FILE = Path.home() / ".openclaw/logs/nova_writeas.log"
TOKEN_CACHE = Path.home() / ".openclaw/workspace/state/.writeas_token"
TOKEN_TTL = 86400 * 7  # 7 days


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "nova-writeas-password", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-writeas-password not in Keychain")


def get_token() -> str:
    """Get a cached token or login for a fresh one."""
    if TOKEN_CACHE.exists():
        age = time.time() - TOKEN_CACHE.stat().st_mtime
        if age < TOKEN_TTL:
            return TOKEN_CACHE.read_text().strip()

    password = get_password()
    payload = json.dumps({"alias": "NovaKoch", "pass": password}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    token = data["data"]["access_token"]

    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(token)
    TOKEN_CACHE.chmod(0o600)
    return token


def api_request(method: str, endpoint: str, body: dict | None = None) -> dict:
    """Make an authenticated API request."""
    token = get_token()
    url = f"{API_BASE}{endpoint}"

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Token {token}")
    req.add_header("Content-Type", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        log(f"API error {e.code}: {error_body[:300]}")
        raise


def publish_post(title: str, body: str, tags: list[str] | None = None, created: str | None = None) -> dict:
    """Publish a post to the novakoch collection."""
    post_data = {
        "title": title,
        "body": body,
    }
    if tags:
        post_data["tags"] = tags
    if created:
        post_data["created"] = created

    result = api_request("POST", f"/collections/{COLLECTION}/posts", post_data)
    post = result.get("data", {})
    slug = post.get("slug", "?")
    url = f"https://novakoch.writeas.com/{slug}"
    log(f"Published: {title} → {url}")
    return post


def list_posts(limit: int = 10) -> list:
    """List recent posts."""
    result = api_request("GET", f"/collections/{COLLECTION}/posts")
    posts = result.get("data", {}).get("posts", [])
    return posts[:limit]


def test_connection():
    """Test API access."""
    token = get_token()
    log(f"Auth OK (token: {token[:10]}...)")
    result = api_request("GET", f"/collections/{COLLECTION}")
    data = result.get("data", {})
    log(f"Blog: {data.get('title')} ({data.get('url')})")
    log(f"Public: {data.get('public')}")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "test":
        test_connection()

    elif cmd == "post":
        title = None
        body = None
        tags = []
        created = None
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--title" and i + 1 < len(sys.argv):
                title = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--body" and i + 1 < len(sys.argv):
                body = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--file" and i + 1 < len(sys.argv):
                body = Path(sys.argv[i + 1]).read_text()
                i += 2
            elif sys.argv[i] == "--tags" and i + 1 < len(sys.argv):
                tags = [t.strip() for t in sys.argv[i + 1].split(",")]
                i += 2
            elif sys.argv[i] == "--created" and i + 1 < len(sys.argv):
                created = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not title or not body:
            print("ERROR: --title and (--body or --file) required")
            sys.exit(1)

        publish_post(title, body, tags, created)

    elif cmd == "list":
        limit = 10
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            if idx + 1 < len(sys.argv):
                limit = int(sys.argv[idx + 1])
        posts = list_posts(limit)
        for p in posts:
            print(f"  {p.get('created', '?')[:10]}  {p.get('title', '(untitled)')}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
