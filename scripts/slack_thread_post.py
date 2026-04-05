#!/usr/bin/env python3
"""
Slack Thread Reply Handler - Post summaries as threaded replies.
Integrates with email digests, GitHub reports, and git health checks.
"""

import json
import sys
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import re


class SlackThreadPoster:
    def __init__(self, channel: str):
        self.channel = channel
        self.metadata_dir = Path.home() / ".openclaw" / "workspace" / "slack-threads"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def _run_slack_cmd(self, cmd: List[str]) -> Dict[str, Any]:
        """Run OpenClaw message tool."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    return {"status": "ok", "stdout": result.stdout}
            else:
                return {"status": "error", "error": result.stderr}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def post_message(
        self,
        subject: str,
        content: str,
        thread_ts: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Post a message to a thread (or create a new thread if no thread_ts)."""
        
        if thread_ts:
            # Reply to existing thread
            message = content
        else:
            # Create parent message with subject
            message = f"*{subject}*\n{content}"
        
        # Call OpenClaw message tool
        cmd = ["message", "action=send", f"target={self.channel}", f"message={message}"]
        if thread_ts:
            cmd.append(f"threadId={thread_ts}")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                output = json.loads(result.stdout) if result.stdout else {}
                ts = output.get("ts", "")
                
                # Store metadata if provided
                if metadata and ts:
                    self._store_metadata(ts, metadata)
                
                return {"status": "ok", "ts": ts, "thread_ts": thread_ts or ts}
            else:
                return {"status": "error", "error": result.stderr}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _store_metadata(self, ts: str, metadata: Dict[str, str]) -> None:
        """Store metadata about a posted message."""
        safe_ts = ts.replace(".", "-")
        metadata_file = self.metadata_dir / f"{safe_ts}.json"
        
        data = {
            "ts": ts,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            **metadata,
        }
        metadata_file.write_text(json.dumps(data, indent=2))

    def parse_markdown_sections(self, content: str) -> List[Dict[str, str]]:
        """Split markdown content into sections by ## headers."""
        sections = []
        current_section = None
        current_content = []

        lines = content.split("\n")
        for line in lines:
            if line.startswith("## "):
                # Save previous section
                if current_section:
                    sections.append({
                        "title": current_section,
                        "content": "\n".join(current_content).strip(),
                    })
                # Start new section
                current_section = line[3:].strip()
                current_content = []
            else:
                current_content.append(line)

        # Save last section
        if current_section:
            sections.append({
                "title": current_section,
                "content": "\n".join(current_content).strip(),
            })

        return sections

    def post_sectioned(
        self,
        subject: str,
        sections: List[Dict[str, str]],
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Post sections as a threaded conversation."""
        
        results = {
            "subject": subject,
            "posts": [],
            "errors": [],
        }

        # Post parent message with subject and summary
        summary_lines = []
        for section in sections:
            summary_lines.append(f"• {section['title']}")

        summary = "\n".join(summary_lines)
        parent_msg = f"*{subject}*\n\n{summary}"

        parent_result = self._post_via_tool(parent_msg, None)
        if parent_result.get("status") == "error":
            results["errors"].append(parent_result.get("error"))
            return results

        parent_ts = parent_result.get("ts", "")
        results["posts"].append({
            "type": "parent",
            "subject": subject,
            "ts": parent_ts,
        })

        # Post each section as a reply
        for section in sections:
            section_msg = f"*{section['title']}*\n\n{section['content']}"
            reply_result = self._post_via_tool(section_msg, parent_ts)

            if reply_result.get("status") == "ok":
                results["posts"].append({
                    "type": "reply",
                    "title": section["title"],
                    "ts": reply_result.get("ts", ""),
                })
            else:
                results["errors"].append(f"Failed to post section '{section['title']}'")

        return results

    def _post_via_tool(self, message: str, thread_ts: Optional[str] = None) -> Dict[str, Any]:
        """Post via OpenClaw message tool (stub for now)."""
        # This would integrate with the actual message tool
        # For now, return a simulated response
        import time
        ts = f"{int(time.time())}.000001"
        
        return {
            "status": "ok",
            "ts": ts,
        }

    def parse_email_digest(self, digest_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """Convert email digest JSON into sections."""
        sections = []

        # Extract email threads and group by sender
        emails_by_sender: Dict[str, List[Dict]] = {}

        for email in digest_data.get("emails", []):
            sender = email.get("from", "Unknown")
            if sender not in emails_by_sender:
                emails_by_sender[sender] = []
            emails_by_sender[sender].append(email)

        # Create section for each sender
        for sender in sorted(emails_by_sender.keys()):
            emails = emails_by_sender[sender]
            content_lines = [f"*From:* {sender}"]
            content_lines.append("")

            for email in emails:
                subject = email.get("subject", "(no subject)")
                body_preview = email.get("body_preview", "")[:200]
                content_lines.append(f"• *{subject}*")
                if body_preview:
                    content_lines.append(f"  {body_preview}...")
                content_lines.append("")

            sections.append({
                "title": f"From: {sender}",
                "content": "\n".join(content_lines),
            })

        return sections


def main():
    parser = argparse.ArgumentParser(description="Post Slack thread replies")
    parser.add_argument("--channel", required=True, help="Slack channel ID")
    parser.add_argument("--subject", help="Thread subject/title")
    parser.add_argument("--message", help="Message content")
    parser.add_argument("--file", help="Read message from file")
    parser.add_argument("--stdin", action="store_true", help="Read from stdin")
    parser.add_argument("--thread-ts", help="Reply to existing thread (timestamp)")
    parser.add_argument("--parse-sections", action="store_true", help="Split on ## headers")
    parser.add_argument("--from-json", help="Parse email digest JSON")
    parser.add_argument("--metadata", help="Metadata as key=value pairs (comma-separated)")
    parser.add_argument("--get-ts", action="store_true", help="Return thread_ts after posting")
    parser.add_argument("--thread-name", help="Name thread in profile")
    parser.add_argument("--broadcast", action="store_true", help="Also broadcast to channel")

    args = parser.parse_args()

    poster = SlackThreadPoster(args.channel)

    # Read content
    content = None
    if args.message:
        content = args.message
    elif args.file:
        content = Path(args.file).read_text()
    elif args.stdin:
        content = sys.stdin.read()
    elif args.from_json:
        digest_data = json.loads(Path(args.from_json).read_text())
        sections = poster.parse_email_digest(digest_data)
    else:
        print("Error: No content source specified", file=sys.stderr)
        sys.exit(1)

    # Parse metadata
    metadata = {}
    if args.metadata:
        for pair in args.metadata.split(","):
            k, v = pair.split("=", 1)
            metadata[k.strip()] = v.strip()

    # Post message(s)
    result = None

    if args.from_json or args.parse_sections:
        # Handle sectioned posts
        if args.from_json:
            digest_data = json.loads(Path(args.from_json).read_text())
            sections = poster.parse_email_digest(digest_data)
        else:
            sections = poster.parse_markdown_sections(content)

        result = poster.post_sectioned(args.subject or "Report", sections, metadata)
    else:
        # Simple single message
        result = poster.post_message(
            args.subject or "Update",
            content,
            thread_ts=args.thread_ts,
            metadata=metadata,
        )

    # Output result
    if args.get_ts and result:
        if isinstance(result, dict):
            if "posts" in result and result["posts"]:
                ts = result["posts"][0].get("ts", "")
                print(ts)
            elif "ts" in result:
                print(result["ts"])
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
