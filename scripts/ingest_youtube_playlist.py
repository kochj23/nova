#!/usr/bin/env python3
"""
ingest_youtube_playlist.py — Download YouTube playlist, transcribe with MLX Whisper,
ingest transcriptions into Nova's vector memory.

Usage: python3 ingest_youtube_playlist.py <playlist_url>
"""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790/remember"
WORK_DIR = Path("/Volumes/Data/youtube-ingest")
CHUNK_WORDS = 400

count = 0
failed = 0
total_videos = 0
completed_videos = 0
start_time = time.time()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[yt-ingest {ts}] {msg}", flush=True)

def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)

def remember(text, metadata):
    global count, failed
    payload = json.dumps({"text": text[:2000], "source": "youtube_transcript", "metadata": metadata}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            count += 1
            return True
    except:
        failed += 1
        return False

def chunk_text(text, chunk_size=CHUNK_WORDS):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.strip()) > 50:
            chunks.append(chunk)
    return chunks

def get_playlist_videos(url):
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s", url],
        capture_output=True, text=True, timeout=60
    )
    videos = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            videos.append({"id": parts[0], "title": parts[1]})
    return videos

def download_audio(video_id, output_dir):
    output_path = output_dir / f"{video_id}.wav"
    if output_path.exists():
        return output_path
    
    result = subprocess.run([
        "yt-dlp", "-x", "--audio-format", "wav", "--audio-quality", "0",
        "-o", str(output_dir / f"{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}"
    ], capture_output=True, text=True, timeout=600)
    
    if result.returncode != 0:
        log(f"  Download failed: {result.stderr[:200]}")
        return None
    
    # yt-dlp might output as .wav directly or need conversion
    if output_path.exists():
        return output_path
    # Check for other formats and convert
    for ext in ["m4a", "opus", "webm", "mp3"]:
        alt = output_dir / f"{video_id}.{ext}"
        if alt.exists():
            subprocess.run(["ffmpeg", "-i", str(alt), "-ar", "16000", "-ac", "1", str(output_path), "-y"],
                          capture_output=True, timeout=300)
            alt.unlink()
            if output_path.exists():
                return output_path
    return None

def transcribe(audio_path):
    """Transcribe using MLX Whisper (local, fast on Apple Silicon)."""
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="en",
        )
        return result.get("text", "")
    except Exception as e:
        log(f"  Transcription error: {e}")
        return ""

def post_status():
    elapsed = time.time() - start_time
    rate = count / max(1, elapsed) * 60
    slack_post(
        f":movie_camera: *YouTube Playlist Ingestion*\n"
        f"  Videos: {completed_videos}/{total_videos} complete\n"
        f"  Chunks ingested: {count:,}\n"
        f"  Failed: {failed}\n"
        f"  Rate: {rate:.0f} chunks/min\n"
        f"  Elapsed: {elapsed/60:.1f}m"
    )

def main():
    global total_videos, completed_videos
    
    if len(sys.argv) < 2:
        print("Usage: python3 ingest_youtube_playlist.py <playlist_url>")
        sys.exit(1)
    
    playlist_url = sys.argv[1]
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    
    log("Fetching playlist...")
    videos = get_playlist_videos(playlist_url)
    total_videos = len(videos)
    log(f"Found {total_videos} videos")
    
    slack_post(
        f":movie_camera: *YouTube Playlist Ingestion Started*\n"
        f"  Videos: {total_videos}\n"
        f"  Source: {playlist_url}\n"
        f"  _Posting status every 5 min_"
    )
    
    last_status = time.time()
    
    for i, video in enumerate(videos):
        vid_id = video["id"]
        title = video["title"]
        log(f"[{i+1}/{total_videos}] Processing: {title}")
        
        # Download audio
        log(f"  Downloading audio...")
        audio_path = download_audio(vid_id, WORK_DIR)
        if not audio_path:
            log(f"  SKIPPED (download failed)")
            completed_videos += 1
            continue
        
        # Transcribe
        log(f"  Transcribing with MLX Whisper...")
        transcript = transcribe(audio_path)
        if not transcript:
            log(f"  SKIPPED (transcription failed)")
            completed_videos += 1
            continue
        
        log(f"  Transcript: {len(transcript)} chars, {len(transcript.split())} words")
        
        # Chunk and ingest
        chunks = chunk_text(transcript)
        log(f"  Ingesting {len(chunks)} chunks...")
        
        for j, chunk in enumerate(chunks):
            metadata = {
                "type": "youtube_transcript",
                "video_id": vid_id,
                "title": title,
                "chunk": j + 1,
                "total_chunks": len(chunks),
                "playlist": playlist_url,
            }
            remember(f"[YouTube: {title}] {chunk}", metadata)
        
        completed_videos += 1
        log(f"  Done: {len(chunks)} chunks ingested")
        
        # Clean up audio file
        audio_path.unlink(missing_ok=True)
        
        # Post status every 5 minutes
        if time.time() - last_status > 300:
            post_status()
            last_status = time.time()
    
    # Final status
    elapsed = time.time() - start_time
    slack_post(
        f":white_check_mark: *YouTube Playlist Ingestion Complete*\n"
        f"  Videos: {completed_videos}/{total_videos}\n"
        f"  Total chunks: {count:,}\n"
        f"  Failed: {failed}\n"
        f"  Duration: {elapsed/60:.1f}m"
    )
    log(f"DONE: {count} chunks from {completed_videos} videos in {elapsed/60:.1f}m")

if __name__ == "__main__":
    main()
