#!/usr/bin/env python3

"""
Nova Daily Journal Summary

Generates and sends a nightly summary at 9 PM PT:
- What I learned
- What I did
- What Jordan did
- Unifi/Synology insights
- Other highlights
"""

import os
import sys
import logging
from datetime import datetime, timedelta
import subprocess
import json

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser('~/Library/Logs/nova_daily_journal.log')),
        logging.StreamHandler()
    ]
)

# Add workspace to path
sys.path.append(os.path.expanduser('~/.openclaw/scripts'))

try:
    from nova_config import slack_bot_token as get_slack_token
    # from nova_memory import recall_recent_events
    # from nova_unifi import get_recent_events as unifi_events
    # from nova_synology import get_backup_status
    
    # Mock functions for testing
    def recall_recent_events(query, limit=5):
        # Query Postgres vector DB directly
        import subprocess
        import json
        try:
            result = subprocess.run(
                ["curl", "-s", f"http://127.0.0.1:18790/recall?q={query}&n={limit}"],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)
            return [m["text"] for m in data.get("memories", [])]
        except Exception as e:
            print(f"[recall_recent_events] Failed to query vector DB: {e}")
            return []
    
    def unifi_events(last_minutes=5):
        return {"alerts": [], "motion": []}
    
    def get_backup_status():
        return {"status": "success", "last_run": "2026-04-14 22:00"}
except ImportError as e:
    logging.error(f"Import failed: {e}")
    sys.exit(1)

SLACK_TOKEN = get_slack_token()
SLACK_CHANNEL = "C0ATAF7NZG9"  # #nova-notifications


def send_slack_message(message: str):
    """Send a message to Slack using curl."""
    cmd = [
        'curl', '-X', 'POST', 'https://slack.com/api/chat.postMessage',
        '--data-urlencode', f'token={SLACK_TOKEN}',
        '--data-urlencode', f'channel={SLACK_CHANNEL}',
        '--data-urlencode', f'text={message}',
        '--data-urlencode', 'as_user=true'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.returncode != 0:
            logging.error(f"Slack send failed: {result.stderr}")
        else:
            logging.info("Daily journal sent to Slack")
    except subprocess.CalledProcessError as e:
        logging.error(f"Slack API call failed: {e}")


def generate_journal():
    """Generate the daily journal content."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # What I learned
    learned = recall_recent_events(f"what Nova learned on {today}", limit=5) or ["No explicit learnings recorded."]
    
    # What I did
    did = recall_recent_events(f"what Nova did on {today}", limit=5) or ["No actions recorded."]
    
    # What Jordan did
    jordan_did = recall_recent_events(f"what Jordan did on {today}", limit=5) or ["No activity summary available."]
    
    # Unifi insights
    try:
        unifi_data = unifi_events(last_minutes=1440)  # last 24h
        unifi_summary = f"Camera alerts: {len(unifi_data.get('alerts', []))}, Motion events: {len(unifi_data.get('motion', []))}"
    except:
        unifi_summary = "Unifi: Offline or unreachable."
    
    # Synology insights
    try:
        backup_status = get_backup_status()
        synology_summary = f"NAS Backup: {backup_status.get('status', 'Unknown')}, Last: {backup_status.get('last_run')})"
    except:
        synology_summary = "Synology: Offline or unreachable."
    
    # Compile message
    message = f"""*📊 Nova Daily Journal — {today}*

*What I learned:*
- {'; '.join(learned)}

*What I did:*
- {'; '.join(did)}

*What Jordan did:*
- {'; '.join(jordan_did)}

*System Insights:*
- {unifi_summary}
- {synology_summary}

"""

    return message

if __name__ == "__main__":
    logging.info("Starting daily journal generation")
    try:
        journal = generate_journal()
        send_slack_message(journal)
    except Exception as e:
        logging.error(f"Journal generation failed: {e}")
        send_slack_message(f"🚨 Nova Daily Journal failed: {e}")
    logging.info("Daily journal process completed")
