#!/usr/bin/env python3
"""
nova_video_ingest.py — Extract knowledge from videos for Nova.

Pipeline:
  1. ffprobe → metadata (duration, resolution, codec, creation date)
  2. ffmpeg → keyframes (1 per N seconds) → qwen3-vl → scene descriptions
  3. ffmpeg → audio track → MLX Whisper → transcript
  4. Combine all → vector memory (source: "video")

Everything runs locally — no cloud APIs. Vision via Ollama qwen3-vl,
transcription via MLX Whisper on Apple Silicon.

Usage:
  python3 nova_video_ingest.py /path/to/video.mp4
  python3 nova_video_ingest.py /path/to/video.mp4 --frames-only     # Skip transcription
  python3 nova_video_ingest.py /path/to/video.mp4 --transcript-only  # Skip vision
  python3 nova_video_ingest.py /path/to/video.mp4 --interval 30      # 1 frame per 30 sec
  python3 nova_video_ingest.py /path/to/folder/                       # Process all videos

Written by Jordan Koch.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
OLLAMA_URL = "http://127.0.0.1:11434"
VISION_MODEL = "qwen3-vl:4b"
DEFAULT_INTERVAL = 10  # 1 keyframe every N seconds
MAX_FRAMES = 60  # Cap to avoid overwhelming on long videos

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".mpg", ".mpeg"}


def log(msg):
    print(f"[nova_video {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_dm(text):
    nova_config.post_both(text, slack_channel=nova_config.JORDAN_DM)


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "video",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}?async=1", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── Metadata ─────────────────────────────────────────────────────────────────

def get_metadata(video_path):
    """Extract video metadata with ffprobe."""
    try:
        result = subprocess.run([
            FFPROBE, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(video_path)
        ], capture_output=True, text=True, timeout=30)

        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

        duration = float(fmt.get("duration", 0))
        minutes = int(duration // 60)
        seconds = int(duration % 60)

        return {
            "filename": Path(video_path).name,
            "duration": duration,
            "duration_str": f"{minutes}m {seconds}s",
            "size_mb": round(int(fmt.get("size", 0)) / 1024 / 1024, 1),
            "width": video_stream.get("width", 0),
            "height": video_stream.get("height", 0),
            "codec": video_stream.get("codec_name", "?"),
            "fps": (lambda r: int(r.split("/")[0]) / int(r.split("/")[1]) if "/" in r and r.split("/")[1] != "0" else 0)(str(video_stream.get("r_frame_rate", "0/1"))),
            "audio_codec": audio_stream.get("codec_name", "none"),
            "creation_time": fmt.get("tags", {}).get("creation_time", ""),
        }
    except Exception as e:
        log(f"Metadata error: {e}")
        return {"filename": Path(video_path).name, "duration": 0}


# ── Frame extraction + vision ────────────────────────────────────────────────

def extract_frames(video_path, interval, tmpdir):
    """Extract keyframes at regular intervals using ffmpeg."""
    metadata = get_metadata(video_path)
    duration = metadata.get("duration", 0)

    if duration == 0:
        log("Could not determine video duration")
        return []

    num_frames = min(int(duration / interval) + 1, MAX_FRAMES)
    frames = []

    for i in range(num_frames):
        timestamp = i * interval
        if timestamp > duration:
            break

        output = Path(tmpdir) / f"frame_{i:04d}.jpg"
        try:
            subprocess.run([
                FFMPEG, "-ss", str(timestamp), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", "-y", str(output)
            ], capture_output=True, timeout=15)

            if output.exists() and output.stat().st_size > 1000:
                minutes = int(timestamp // 60)
                seconds = int(timestamp % 60)
                frames.append({
                    "path": str(output),
                    "timestamp": timestamp,
                    "time_str": f"{minutes:02d}:{seconds:02d}",
                })
        except Exception as e:
            log(f"Frame extraction error at {timestamp}s: {e}")

    log(f"Extracted {len(frames)} frames from {metadata.get('duration_str', '?')}")
    return frames


def describe_frame(frame_path):
    """Send a frame to qwen3-vl for visual description."""
    try:
        with open(frame_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": "Describe this video frame in 1-2 sentences. What's happening? Who/what is visible? What's the setting?",
            "images": [img_b64],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 150}
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            result = json.loads(r.read())
            response = result.get("response", "").strip()
            if "</think>" in response:
                response = response.split("</think>", 1)[-1].strip()
            return response
    except Exception as e:
        log(f"Vision error: {e}")
        return ""


# ── Audio transcription ──────────────────────────────────────────────────────

def extract_audio(video_path, tmpdir):
    """Extract audio track as WAV for Whisper."""
    audio_path = Path(tmpdir) / "audio.wav"
    try:
        subprocess.run([
            FFMPEG, "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            "-y", str(audio_path)
        ], capture_output=True, timeout=600)  # 10 min for large videos

        if audio_path.exists() and audio_path.stat().st_size > 1000:
            return str(audio_path)
    except Exception as e:
        log(f"Audio extraction error: {e}")
    return None


def transcribe_audio(audio_path):
    """Transcribe audio using MLX Whisper (local, Apple Silicon)."""
    try:
        import mlx_whisper
        log("Transcribing with MLX Whisper...")
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        )
        text = result.get("text", "").strip()
        log(f"Transcribed {len(text)} characters")
        return text
    except ImportError:
        log("mlx-whisper not installed — skipping transcription")
        return ""
    except Exception as e:
        log(f"Transcription error: {e}")
        return ""


# ── Main pipeline ────────────────────────────────────────────────────────────

def ingest_video(video_path, interval=DEFAULT_INTERVAL, frames_only=False, transcript_only=False):
    """Full video ingestion pipeline."""
    video_path = Path(video_path)
    if not video_path.exists():
        log(f"File not found: {video_path}")
        return

    log(f"Processing: {video_path.name}")
    start_time = time.time()

    # Metadata
    metadata = get_metadata(video_path)
    log(f"  {metadata.get('duration_str', '?')}, {metadata.get('width', '?')}x{metadata.get('height', '?')}, "
        f"{metadata.get('size_mb', '?')} MB, {metadata.get('codec', '?')}")

    with tempfile.TemporaryDirectory() as tmpdir:
        scene_descriptions = []
        transcript = ""

        # Frame analysis
        if not transcript_only:
            frames = extract_frames(str(video_path), interval, tmpdir)
            for i, frame in enumerate(frames):
                desc = describe_frame(frame["path"])
                if desc:
                    scene_descriptions.append(f"[{frame['time_str']}] {desc}")
                    log(f"  Frame {i+1}/{len(frames)} ({frame['time_str']}): {desc[:80]}...")

        # Audio transcription
        if not frames_only:
            audio_path = extract_audio(str(video_path), tmpdir)
            if audio_path:
                transcript = transcribe_audio(audio_path)

    # Store in vector memory
    elapsed = time.time() - start_time

    # 1. Metadata memory
    meta_text = (
        f"Video: {metadata['filename']}. "
        f"Duration: {metadata.get('duration_str', '?')}. "
        f"Resolution: {metadata.get('width', '?')}x{metadata.get('height', '?')}. "
        f"Size: {metadata.get('size_mb', '?')} MB. "
        f"Codec: {metadata.get('codec', '?')}."
    )
    if metadata.get("creation_time"):
        meta_text += f" Created: {metadata['creation_time']}."
    vector_remember(meta_text, {
        "type": "video_metadata",
        "filename": metadata["filename"],
        "duration": metadata.get("duration", 0),
    })

    # 2. Scene descriptions (chunked if long)
    if scene_descriptions:
        # Store as chunks of 5 scenes each
        for i in range(0, len(scene_descriptions), 5):
            chunk = scene_descriptions[i:i+5]
            chunk_text = f"Video '{metadata['filename']}' visual content:\n" + "\n".join(chunk)
            vector_remember(chunk_text, {
                "type": "video_scenes",
                "filename": metadata["filename"],
                "frame_range": f"{i+1}-{min(i+5, len(scene_descriptions))}",
            })

    # 3. Transcript (chunked if long)
    if transcript:
        # Chunk transcript at ~500 chars
        words = transcript.split()
        chunks = []
        current = []
        current_len = 0
        for word in words:
            current.append(word)
            current_len += len(word) + 1
            if current_len >= 500:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
        if current:
            chunks.append(" ".join(current))

        for i, chunk in enumerate(chunks):
            chunk_text = f"Video '{metadata['filename']}' transcript (part {i+1}/{len(chunks)}):\n{chunk}"
            vector_remember(chunk_text, {
                "type": "video_transcript",
                "filename": metadata["filename"],
                "part": i + 1,
                "total_parts": len(chunks),
            })

    # Summary
    summary_parts = [f"Processed video '{metadata['filename']}'"]
    if scene_descriptions:
        summary_parts.append(f"{len(scene_descriptions)} scene descriptions")
    if transcript:
        summary_parts.append(f"{len(transcript)} char transcript")
    summary_parts.append(f"in {elapsed:.0f}s")
    summary = ". ".join(summary_parts)

    log(summary)
    return {
        "filename": metadata["filename"],
        "metadata": metadata,
        "scenes": len(scene_descriptions),
        "transcript_length": len(transcript),
        "elapsed": elapsed,
    }


def ingest_folder(folder_path, **kwargs):
    """Process all videos in a folder."""
    folder = Path(folder_path)
    videos = [f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS]

    if not videos:
        log(f"No video files found in {folder}")
        return

    log(f"Found {len(videos)} video(s) in {folder}")
    results = []

    for video in sorted(videos):
        result = ingest_video(video, **kwargs)
        if result:
            results.append(result)

    # Post summary to Slack
    if results:
        lines = [f"*Video Ingest Complete — {len(results)} video(s)*"]
        for r in results:
            lines.append(f"  {r['filename']}: {r['scenes']} scenes, {r['transcript_length']} chars transcript ({r['elapsed']:.0f}s)")
        slack_dm("\n".join(lines))

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Video Ingest")
    parser.add_argument("path", help="Video file or folder path")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Seconds between keyframes (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--frames-only", action="store_true", help="Skip audio transcription")
    parser.add_argument("--transcript-only", action="store_true", help="Skip frame analysis")
    parser.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                        help=f"Max frames to analyze (default: {MAX_FRAMES})")
    args = parser.parse_args()

    MAX_FRAMES = args.max_frames
    path = Path(args.path)

    if path.is_dir():
        ingest_folder(path, interval=args.interval,
                      frames_only=args.frames_only, transcript_only=args.transcript_only)
    elif path.is_file():
        ingest_video(path, interval=args.interval,
                     frames_only=args.frames_only, transcript_only=args.transcript_only)
    else:
        print(f"Not found: {path}")
        sys.exit(1)
