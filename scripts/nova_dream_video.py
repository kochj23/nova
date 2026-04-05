#!/usr/bin/env python3
"""
nova_dream_video.py — Generate ~10 second dream video from narrative using SwarmUI.

Creates video clip from dream journal text (via Flux or other video model in ComfyUI).
"""

import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

SWARMUI_URL = "http://localhost:7801"
WORKSPACE = Path.home() / ".openclaw/workspace"
DREAM_DIR = WORKSPACE / "dream_videos"
DREAM_DIR.mkdir(exist_ok=True)

def log(msg: str):
    print(f"[nova_dream_video {datetime.now().isoformat()}] {msg}")

def generate_dream_video(prompt: str, num_frames: int = 5) -> str:
    """Generate dream video by creating frame sequence + combining with ffmpeg.
    
    Creates 5 variations of the dream image, then compiles into ~10sec video.
    
    Args:
        prompt: Dream narrative text
        num_frames: Number of image variations to generate
    
    Returns:
        Path to generated video file
    """
    
    try:
        log(f"Generating {num_frames} dream frames from narrative...")
        
        frames = []
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        frame_dir = DREAM_DIR / timestamp
        frame_dir.mkdir(exist_ok=True)
        
        # Generate variations of the dream image
        for i in range(num_frames):
            # Use generate_image.sh to create frame
            result = subprocess.run(
                [str(Path.home() / ".openclaw/scripts/generate_image.sh"), 
                 f"{prompt} (frame {i+1})", "1024", "576", "15"],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                # Extract path from output
                for line in result.stdout.split('\n'):
                    if 'Workspace copy:' in line:
                        frame_path = line.split('Workspace copy:')[1].strip()
                        frames.append(frame_path)
                        log(f"  Frame {i+1}: {Path(frame_path).name}")
                        break
        
        if not frames:
            log("No frames generated")
            return None
        
        # Compile frames into video using ffmpeg
        # Each frame displayed for 2 seconds (10 sec total / 5 frames)
        output_video = DREAM_DIR / f"dream_{timestamp}.mp4"
        
        # Create concat file for ffmpeg
        concat_file = frame_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for frame in frames:
                f.write(f"file '{frame}'\nduration 2\n")
        
        log(f"Compiling {len(frames)} frames into video...")
        
        # Use ffmpeg to create video
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-vf", "fps=2",  # 2 fps = 2 seconds per frame
            str(output_video)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0 and output_video.exists():
            log(f"✓ Dream video created: {output_video}")
            return str(output_video)
        else:
            log(f"ffmpeg error: {result.stderr[:200]}")
    
    except subprocess.TimeoutExpired:
        log("Video compilation timeout")
    except Exception as e:
        log(f"Error: {e}")
    
    return None

def main():
    if len(sys.argv) < 2:
        log("Usage: nova_dream_video.py '<dream narrative>'")
        return 1
    
    dream_text = sys.argv[1]
    
    if not dream_text or len(dream_text) < 10:
        log("Dream text too short")
        return 1
    
    log(f"Generating dream visualization from narrative...")
    
    # For now: generate a single image (video generation needs more setup)
    # This will create the visual representation of the dream
    result = subprocess.run(
        [str(Path.home() / ".openclaw/scripts/generate_image.sh"), 
         dream_text, "1024", "576", "20"],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    if result.returncode == 0:
        for line in result.stdout.split('\n'):
            if 'Workspace copy:' in line:
                image_path = line.split('Workspace copy:')[1].strip()
                log(f"✓ Dream image ready: {image_path}")
                print(image_path)
                return 0
    
    log("Dream visualization failed")
    return 1

if __name__ == "__main__":
    sys.exit(main())
