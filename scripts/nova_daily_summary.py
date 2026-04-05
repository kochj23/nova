#!/usr/bin/env python3
"""
Nova Daily Learning Summary — Posts to Slack at 11:59pm
Reads memory/YYYY-MM-DD.md and summarizes new learnings
"""

import os
import json
from datetime import datetime

def get_todays_memory():
    """Read today's memory file"""
    today = datetime.now().strftime("%Y-%m-%d")
    memory_file = os.path.expanduser(f"~/.openclaw/workspace/memory/{today}.md")
    
    if not os.path.exists(memory_file):
        return None, "No new memories recorded today."
    
    with open(memory_file, "r") as f:
        content = f.read()
    
    return today, content

def post_to_slack(summary):
    """Post summary to Slack #nova-chat"""
    import subprocess
    
    message = f"""```
📖 DAILY LEARNING SUMMARY

{summary}

—N
```"""
    
    # Use message tool via subprocess to post
    subprocess.run([
        "python3", "-c",
        f"from openclaw import message; message.send(action='send', channel='slack', target='C0AMNQ5GX70', message={repr(message)})"
    ], capture_output=True)

def format_summary(memory_content):
    """Format memory file into readable summary"""
    # Parse markdown and extract key sections
    lines = memory_content.split('\n')
    summary_lines = []
    
    current_section = None
    for line in lines:
        if line.startswith('## '):
            current_section = line.replace('## ', '').strip()
            summary_lines.append(f"\n### {current_section}")
        elif line.startswith('- '):
            summary_lines.append(line)
        elif line.startswith('**'):
            summary_lines.append(line)
    
    return '\n'.join(summary_lines[:200]) + "\n\n(Full details in memory system)"

if __name__ == "__main__":
    today, memory_content = get_todays_memory()
    
    if memory_content == "No new memories recorded today.":
        print("No memories recorded today.")
    else:
        summary = format_summary(memory_content)
        print(f"Daily summary for {today}:")
        print(summary)
        
        # Post to Slack
        try:
            post_to_slack(summary)
            print(f"\n✓ Posted to Slack")
        except Exception as e:
            print(f"✗ Error posting to Slack: {e}")
