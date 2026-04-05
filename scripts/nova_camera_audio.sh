#!/bin/bash
# nova_camera_audio.sh — Capture audio from a specific camera
# Usage: nova_camera_audio.sh <camera_name> [duration_seconds]

set -euo pipefail

CAMERA_NAME="${1:-front_door}"
DURATION="${2:-10}"

# Camera RTSP URLs
declare -A cameras=(
    [front_door]="RTSP_URL_REDACTED"
    [front_yard]="RTSP_URL_REDACTED"
    [front_yard_alt]="RTSP_URL_REDACTED"
    [front_door_patio]="RTSP_URL_REDACTED"
    [alley_north]="RTSP_URL_REDACTED"
    [alley_south]="RTSP_URL_REDACTED"
    [garage]="RTSP_URL_REDACTED"
    [carport]="RTSP_URL_REDACTED"
    [side_yard]="RTSP_URL_REDACTED"
    [back_patio]="RTSP_URL_REDACTED"
    [patio_1]="RTSP_URL_REDACTED"
    [patio_2]="RTSP_URL_REDACTED"
    [3d_printers]="RTSP_URL_REDACTED"
    [abundio_boundary]="RTSP_URL_REDACTED"
)

RTSP_URL="${cameras[$CAMERA_NAME]}"
OUTPUT_DIR="$HOME/.openclaw/workspace/camera_audio"
mkdir -p "$OUTPUT_DIR"

OUTPUT_FILE="$OUTPUT_DIR/${CAMERA_NAME}_$(date +%Y%m%d_%H%M%S).wav"

echo "Capturing $DURATION seconds of audio from $CAMERA_NAME..."
ffmpeg -rtsp_transport tcp -i "$RTSP_URL" \
  -t "$DURATION" \
  -acodec pcm_s16le -ar 44100 -ac 2 \
  -f wav -y "$OUTPUT_FILE" 2>&1 | tail -5

echo "✓ Saved to: $OUTPUT_FILE"
ls -lh "$OUTPUT_FILE"
