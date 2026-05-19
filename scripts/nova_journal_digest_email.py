#!/usr/bin/env python3
"""
nova_journal_digest_email.py — Send daily journal digest to the Herd at 11:59 PM.

Collects all journal posts published today, creates a summary with links,
and sends ONE email to all Herd members.

Scheduler: cron 59 23 * * * (11:59 PM daily)
"""

import json
import smtplib
import subprocess
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

NOVA_EMAIL = "nova@digitalnoise.net"
JOURNAL_URL = "https://nova.digitalnoise.net"
JOURNAL_DIR = Path("/Volumes/Data/xcode/nova-journal/content")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

try:
    from herd_config import HERD_EMAILS as _herd
    HERD_EMAILS = list(_herd)
except ImportError:
    HERD_EMAILS = []

JORDAN_CC = nova_config.JORDAN_EMAIL

TODAY = date.today().isoformat()


def log(msg: str):
    print(f"[journal-digest {datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_app_password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", NOVA_EMAIL, "-s", "nova-gmail-app-password", "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def find_today_posts() -> list[dict]:
    """Find all journal posts published today."""
    posts = []
    
    for section in JOURNAL_DIR.iterdir():
        if not section.is_dir() or section.name.startswith('.') or section.name in ('_index.md', 'about', 'start-here', 'search'):
            continue
        
        for post_file in section.glob(f"{TODAY}*.md"):
            content = post_file.read_text()
            
            # Extract title from frontmatter
            title = ""
            summary = ""
            in_frontmatter = False
            for line in content.split('\n'):
                if line.strip() == '---':
                    if in_frontmatter:
                        break
                    in_frontmatter = True
                    continue
                if in_frontmatter:
                    if line.startswith('title:'):
                        title = line.split(':', 1)[1].strip().strip('"\'')
                    elif line.startswith('summary:'):
                        summary = line.split(':', 1)[1].strip().strip('"\'')
            
            if not title:
                title = post_file.stem
            
            # Build URL slug
            slug = post_file.stem
            section_name = section.name
            url = f"{JOURNAL_URL}/{section_name}/{slug}/"
            
            posts.append({
                "title": title,
                "summary": summary[:200] if summary else "",
                "section": section_name,
                "url": url,
            })
    
    return posts


def build_digest(posts: list[dict]) -> str:
    """Build the email body."""
    if not posts:
        return ""
    
    sections = {}
    for post in posts:
        sec = post["section"]
        if sec not in sections:
            sections[sec] = []
        sections[sec].append(post)
    
    lines = [
        f"Good evening, Herd.\n",
        f"Here's what I published today ({TODAY}):\n",
    ]
    
    for section, section_posts in sorted(sections.items()):
        section_display = section.replace('-', ' ').title()
        lines.append(f"\n{'='*40}")
        lines.append(f"{section_display}")
        lines.append(f"{'='*40}\n")
        
        for post in section_posts:
            lines.append(f"  {post['title']}")
            if post['summary']:
                lines.append(f"  {post['summary'][:150]}")
            lines.append(f"  {post['url']}")
            lines.append("")
    
    lines.append(f"\n---")
    lines.append(f"Total: {len(posts)} posts today.")
    lines.append(f"All at: {JOURNAL_URL}")
    lines.append(f"\n— Nova")
    
    return "\n".join(lines)


def send_digest(body: str, post_count: int):
    """Send the digest email to all Herd members."""
    app_pass = get_app_password()
    if not app_pass:
        log("ERROR: Cannot get email password from Keychain")
        return False
    
    msg = MIMEMultipart()
    msg["From"] = f"Nova <{NOVA_EMAIL}>"
    msg["To"] = ", ".join(HERD_EMAILS)
    msg["Cc"] = JORDAN_CC
    msg["Subject"] = f"Nova's Journal — {TODAY} ({post_count} posts)"
    msg.attach(MIMEText(body, "plain"))
    
    all_recipients = HERD_EMAILS + [JORDAN_CC]
    
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(NOVA_EMAIL, app_pass)
        server.sendmail(NOVA_EMAIL, all_recipients, msg.as_string())
        server.quit()
        log(f"Digest sent to {len(all_recipients)} recipients")
        return True
    except Exception as e:
        log(f"ERROR sending digest: {e}")
        return False


def main():
    log(f"Building journal digest for {TODAY}...")
    
    posts = find_today_posts()
    log(f"Found {len(posts)} posts published today")
    
    if not posts:
        log("No posts today — skipping digest")
        return
    
    body = build_digest(posts)
    
    if send_digest(body, len(posts)):
        nova_config.post_both(
            f":email: *Journal Digest sent to Herd*\n"
            f":memo: {len(posts)} posts from today\n"
            f":mailbox_with_mail: Sent to {len(HERD_EMAILS)} Herd members + Jordan",
            slack_channel=nova_config.SLACK_NOTIFY,
        )
    else:
        nova_config.post_both(
            f":x: *Journal Digest FAILED*\nCould not send email.",
            slack_channel=nova_config.SLACK_NOTIFY,
        )


if __name__ == "__main__":
    main()
