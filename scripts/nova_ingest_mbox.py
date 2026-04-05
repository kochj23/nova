#!/usr/bin/env python3
"""
nova_ingest_mbox.py — Ingest email archives from mbox files into vector memory.

Reads .mbox files, extracts emails, and stores them in vector memory
with metadata (sender, date, subject, folder).

Usage:
  python3 nova_ingest_mbox.py /path/to/mbox/directory
  python3 nova_ingest_mbox.py /Volumes/Data/Nova/Files/Home
"""

import json
import mailbox
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
import urllib.request
import urllib.error

VECTOR_URL = "http://127.0.0.1:18790/remember"

def log(msg: str):
    print(f"[{datetime.now().isoformat()}] {msg}")

def remember(text: str, source: str = "email", metadata: dict = None) -> str:
    """Store memory in vector database."""
    payload = {
        "text": text,
        "source": source,
        "metadata": metadata or {}
    }
    
    try:
        req = urllib.request.Request(
            VECTOR_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read())
            return result.get("id", "unknown")
    except Exception as e:
        log(f"Error storing memory: {e}")
        return None

def parse_email(msg, folder_name: str = "unknown"):
    """Extract key data from email message."""
    try:
        sender = msg.get("From", "unknown")
        subject = msg.get("Subject", "(no subject)")
        date_str = msg.get("Date", "unknown")
        
        # Parse date
        try:
            date_dt = parsedate_to_datetime(date_str)
            date_iso = date_dt.isoformat()
        except:
            date_iso = date_str
        
        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except:
                        pass
        else:
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                body = msg.get_payload()
        
        # Truncate body
        body = (body or "")[:1000]
        
        return {
            "sender": sender,
            "subject": subject,
            "date": date_iso,
            "body": body,
            "folder": folder_name,
        }
    except Exception as e:
        log(f"Error parsing email: {e}")
        return None

def ingest_mbox_file(mbox_path: Path, folder_name: str = None):
    """Ingest all emails from an mbox file."""
    if folder_name is None:
        folder_name = mbox_path.parent.name
    
    log(f"Reading {mbox_path.name} (folder: {folder_name})...")
    
    try:
        mbox = mailbox.mbox(str(mbox_path))
        count = 0
        skipped = 0
        
        for i, msg in enumerate(mbox):
            if i % 100 == 0:
                log(f"  Processing message {i}...")
            
            email_data = parse_email(msg, folder_name)
            if not email_data:
                skipped += 1
                continue
            
            # Create memory entry
            text = f"Email from {email_data['sender']} ({email_data['date']}): {email_data['subject']}\n\n{email_data['body']}"
            
            memory_id = remember(
                text,
                source="email_archive",
                metadata={
                    "sender": email_data["sender"],
                    "subject": email_data["subject"],
                    "date": email_data["date"],
                    "folder": folder_name,
                    "mbox_file": mbox_path.name,
                }
            )
            
            if memory_id:
                count += 1
        
        log(f"✓ Ingested {count} emails from {mbox_path.name} (skipped: {skipped})")
        return count
    
    except Exception as e:
        log(f"✗ Error processing {mbox_path}: {e}")
        return 0

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 nova_ingest_mbox.py /path/to/mbox/directory")
        sys.exit(1)
    
    mbox_dir = Path(sys.argv[1])
    
    if not mbox_dir.exists():
        log(f"Error: {mbox_dir} does not exist")
        sys.exit(1)
    
    log(f"=== MBOX Ingestion Starting ===")
    log(f"Source: {mbox_dir}")
    
    # Find all mbox files
    mbox_files = list(mbox_dir.glob("*/*.mbox/mbox")) + list(mbox_dir.glob("*.mbox/mbox"))
    
    if not mbox_files:
        log(f"No .mbox files found in {mbox_dir}")
        sys.exit(1)
    
    log(f"Found {len(mbox_files)} mbox file(s)")
    
    total_ingested = 0
    for mbox_file in mbox_files:
        folder_name = mbox_file.parent.parent.name or "unknown"
        ingested = ingest_mbox_file(mbox_file, folder_name)
        total_ingested += ingested
    
    log(f"\n✓ COMPLETE: {total_ingested} emails ingested into vector memory")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
