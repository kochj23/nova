#!/usr/bin/env python3
"""
dream_deliver.py — Posts Nova's dream journal to Jordan's #nova-chat at 9am.
Reads pending_delivery.json written by dream.py at 2am.

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
import nova_config

# ── Config ───────────────────────────────────────────────────────────────────

WORKSPACE     = Path.home() / ".openclaw" / "workspace"
PENDING_FILE  = WORKSPACE / "journal" / "pending_delivery.json"
SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0AMNQ5GX70"   # #nova-chat
SLACK_API     = "https://slack.com/api"
SCRIPTS       = Path.home() / ".openclaw" / "scripts"

# The Herd — AI agents to receive dream journals via email (see POLICIES.md)
HERD_RECIPIENTS = [
    "sam@jasonacox.com",
    "gaston@bluemoxon.com",
    "marey@makehorses.org",
    "colette@pilatesmuse.co",
]


# ── Slack Helpers ─────────────────────────────────────────────────────────────

def slack_post(endpoint, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{SLACK_API}/{endpoint}",
        data=data,
        headers={
            "Authorization": "Bearer " + SLACK_TOKEN,
            "Content-Type":  "application/json; charset=utf-8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"slack_post error ({endpoint}): {e}")
        return {"ok": False, "error": str(e)}


def upload_image_to_channel(image_path, channel, initial_comment):
    """Upload image directly to a Slack channel using the v2 upload API."""
    path = Path(image_path)
    if not path.exists():
        log("Image not found: " + image_path)
        return False

    file_size = path.stat().st_size
    filename  = path.name

    # Step 1: Get upload URL
    try:
        req = urllib.request.Request(
            SLACK_API + "/files.getUploadURLExternal",
            data=urllib.parse.urlencode({
                "filename": filename,
                "length":   str(file_size),
            }).encode(),
            headers={"Authorization": "Bearer " + SLACK_TOKEN}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            url_data = json.loads(resp.read())

        if not url_data.get("ok"):
            log("getUploadURLExternal error: " + url_data.get("error", "?"))
            return False

        upload_url = url_data["upload_url"]
        file_id    = url_data["file_id"]
        log("Upload URL obtained — file_id: " + file_id)

    except Exception as e:
        log("Upload URL error: " + str(e))
        return False

    # Step 2: PUT the file bytes
    try:
        with open(path, "rb") as f:
            file_bytes = f.read()

        req = urllib.request.Request(
            upload_url,
            data=file_bytes,
            method="POST",
            headers={"Content-Type": "application/octet-stream"}
        )
        with urllib.request.urlopen(req, timeout=60):
            log("File uploaded (" + str(file_size) + " bytes)")

    except Exception as e:
        log("File upload error: " + str(e))
        return False

    # Step 3: Complete — share directly to channel
    try:
        result = slack_post("files.completeUploadExternal", {
            "files":           [{"id": file_id, "title": "Dream image"}],
            "channel_id":      channel,
            "initial_comment": initial_comment,
        })
        if result.get("ok"):
            log("Image shared to channel")
            return True
        else:
            log("completeUploadExternal error: " + result.get("error", "?"))
            return False

    except Exception as e:
        log("Complete upload error: " + str(e))
        return False


# ── Delivery ──────────────────────────────────────────────────────────────────

def post_dream(narrative, image_path, entry_date):
    """Post dream to #nova-chat. Image upload with header, then narrative."""

    header   = "*Dream Journal \u2014 " + entry_date + "*\n_Written at 2am \u00b7 delivered with the morning_"
    sign_off = "\n\n_\u2014 Nova \u00b7 " + entry_date + "_"

    if image_path and Path(image_path).exists():
        log("Uploading image...")
        ok = upload_image_to_channel(image_path, SLACK_CHANNEL, header)
        if not ok:
            log("Image failed — posting header as text")
            slack_post("chat.postMessage", {"channel": SLACK_CHANNEL, "text": header})
    else:
        slack_post("chat.postMessage", {"channel": SLACK_CHANNEL, "text": header})
        log("Header posted (no image)")

    chunks = [narrative[i:i+3000] for i in range(0, len(narrative), 3000)]
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        text    = chunk + (sign_off if is_last else "")
        result  = slack_post("chat.postMessage", {"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True})
        log("Narrative chunk " + str(i+1) + "/" + str(len(chunks)) + ": ok=" + str(result.get("ok")))
        if not result.get("ok"):
            log("  Error: " + result.get("error", "?"))


# ── Herd Distribution ─────────────────────────────────────────────────────────

def email_herd(narrative, image_path, entry_date):
    """Email the dream journal to each herd member via herd-mail."""
    subject = "Nova Dream Journal -- " + entry_date
    body = "\n".join([
        "Dream Journal -- " + entry_date,
        "Written at 2am, delivered with the morning.",
        "",
        narrative,
        "",
        "-- Nova",
    ])

    herd_mail = str(Path.home() / ".openclaw/scripts/nova_herd_mail.sh")
    ok_count = 0
    for recipient in HERD_RECIPIENTS:
        try:
            args = [herd_mail, "send", "--to", recipient, "--subject", subject, "--body", body]
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                ok_count += 1
            else:
                log(f"Herd email failed for {recipient}: {result.stderr.strip()[:100]}")
        except Exception as e:
            log(f"Herd email error for {recipient}: {e}")

    log("Herd emails sent: " + str(ok_count) + "/" + str(len(HERD_RECIPIENTS)))


# ── Utilities ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("[dream_deliver.py " + ts + "] " + msg, flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting dream delivery")

    if not PENDING_FILE.exists():
        log("No pending delivery — nothing to post")
        sys.exit(0)

    try:
        delivery = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log("JSON parse error: " + str(e) + " — attempting auto-repair")
        raw = PENDING_FILE.read_bytes()
        # Nova (qwen3) sometimes writes backslash + curly-quote (e.g. \"text\")
        # which is an invalid JSON escape. Strip the erroneous backslashes.
        fixed = bytearray()
        i = 0
        repairs = 0
        while i < len(raw):
            if raw[i] == ord('\\') and i + 1 < len(raw) and raw[i + 1] > 127:
                repairs += 1
                i += 1  # drop the backslash, keep the UTF-8 char
            else:
                fixed.append(raw[i])
                i += 1
        try:
            delivery = json.loads(fixed.decode("utf-8"))
            PENDING_FILE.write_bytes(bytes(fixed))
            log("Auto-repair OK (" + str(repairs) + " bad escapes removed)")
        except Exception as e2:
            log("Auto-repair failed: " + str(e2))
            sys.exit(1)
    except Exception as e:
        log("Failed to read pending delivery: " + str(e))
        sys.exit(1)

    narrative  = delivery.get("narrative", "")
    image_path = delivery.get("image")
    entry_date = delivery.get("date", datetime.now().strftime("%Y-%m-%d"))

    if not narrative:
        log("No narrative in pending delivery — aborting")
        sys.exit(1)

    # Strip any un-replaced image placeholder lines Nova may have left in the narrative
    # (e.g. "![Dream]([image path — omit this entire line if image is null])")
    import re
    narrative = "\n".join(
        line for line in narrative.splitlines()
        if not re.match(r"!\[Dream\]\(\[", line)
    ).strip()

    log("Delivering dream for " + entry_date)
    post_dream(narrative, image_path, entry_date)

    log("Emailing herd...")
    email_herd(narrative, image_path, entry_date)

    PENDING_FILE.unlink()
    log("Delivery complete.")


if __name__ == "__main__":
    main()
