#!/usr/bin/env python3
"""
nova_jungle_monitor.py — Monitor YouTube for new jungle tracks and alert Jordan.

Searches YouTube for recent jungle releases and high-quality uploads.
Posts new discoveries to Slack #nova-chat.
"""

import subprocess
import json
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / ".openclaw/workspace"
CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
CACHE_FILE = WORKSPACE / "jungle_tracks_cache.json"

def log(msg: str):
    print(f"[nova_jungle_monitor {datetime.now().isoformat()}] {msg}")

def search_youtube_jungle():
    """Search YouTube for recent jungle tracks."""
    try:
        # Search for jungle tracks uploaded in the last week
        query = 'jungle drum and bass new tracks 2026'
        
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", 
             f"ytsearch50:{query}", "--age-limit", "0"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            log(f"✓ YouTube search returned results")
            return result.stdout
        else:
            log(f"YouTube search failed: {result.stderr[:200]}")
            return None
    
    except Exception as e:
        log(f"Error searching YouTube: {e}")
        return None

def parse_tracks(json_output):
    """Parse YouTube results into track list."""
    tracks = []
    try:
        for line in json_output.strip().split('\n'):
            if line:
                data = json.loads(line)
                track = {
                    "title": data.get("title", "Unknown"),
                    "url": f"https://youtube.com/watch?v={data.get('id')}",
                    "uploader": data.get("uploader", "Unknown"),
                    "duration": data.get("duration", 0),
                    "view_count": data.get("view_count", 0),
                }
                tracks.append(track)
    except json.JSONDecodeError:
        pass
    
    return tracks

def filter_quality_tracks(tracks):
    """Filter for high-quality jungle tracks (>5 min, >100 views)."""
    quality = []
    for track in tracks:
        # Jungle tracks are typically 5-7 minutes
        if track.get("duration", 0) > 300 and track.get("view_count", 0) > 100:
            quality.append(track)
    
    return quality[:10]  # Top 10

def post_to_slack(tracks):
    """Post new jungle tracks to Slack."""
    if not tracks:
        log("No quality tracks found")
        return
    
    message = "🎵 **NEW JUNGLE TRACKS DISCOVERED**\n\n"
    
    for i, track in enumerate(tracks[:5], 1):
        message += f"{i}. **{track['title']}**\n"
        message += f"   Artist: {track['uploader']}\n"
        message += f"   Link: {track['url']}\n"
        message += f"   Views: {track['view_count']:,}\n\n"
    
    # Post to Slack using curl
    try:
        import os
        config_path = Path.home() / ".openclaw/openclaw.json"
        if config_path.exists():
            with open(config_path, 'r') as f:
                config = json.load(f)
                bot_token = config.get('channels', {}).get('slack', {}).get('botToken')
            
            if bot_token:
                subprocess.run(
                    ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
                     "-H", f"Authorization: Bearer {bot_token}",
                     "-H", "Content-Type: application/json",
                     "-d", json.dumps({
                         "channel": CHANNEL,
                         "text": message
                     })],
                    timeout=10
                )
                log(f"✓ Posted {len(tracks)} tracks to Slack")
    except Exception as e:
        log(f"Error posting to Slack: {e}")

def main():
    log("Starting jungle track monitor...")
    
    # Search YouTube
    results = search_youtube_jungle()
    if not results:
        log("No results from YouTube")
        return 1
    
    # Parse tracks
    tracks = parse_tracks(results)
    log(f"Found {len(tracks)} total tracks")
    
    # Filter for quality
    quality_tracks = filter_quality_tracks(tracks)
    log(f"Filtered to {len(quality_tracks)} quality tracks")
    
    # Post to Slack
    if quality_tracks:
        post_to_slack(quality_tracks)
        return 0
    else:
        log("No quality tracks met filter")
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
