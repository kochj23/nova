#!/usr/bin/env bash
# nova_camera_audio.sh — Capture audio from a specific camera
# Usage: nova_camera_audio.sh <camera_name> [duration_seconds]

set -eo pipefail

# Usage function
usage() {
    echo "Usage: $0 <camera_name> [duration_seconds]" >&2
    echo "Available cameras: front_door, front_yard, front_yard_alt, front_door_patio, alley_north, alley_south, garage, carport, side_yard, back_patio, patio_1, patio_2, 3d_printers, abundio_boundary" >&2
    exit 1
}

# Validate arguments
if [ $# -eq 0 ]; then
    usage
fi

# Map camera names to RTSP URLs
get_camera_url() {
    local camera_name="$1"
    case "$camera_name" in
        "front_door") echo "RTSP_URL_REDACTED" ;;
        "front_yard") echo "RTSP_URL_REDACTED" ;;
        "front_yard_alt") echo "RTSP_URL_REDACTED" ;;
        "front_door_patio") echo "RTSP_URL_REDACTED" ;;
        "alley_north") echo "RTSP_URL_REDACTED" ;;
        "alley_south") echo "RTSP_URL_REDACTED" ;;
        "garage") echo "RTSP_URL_REDACTED" ;;
        "carport") echo "RTSP_URL_REDACTED" ;;
        "side_yard") echo "RTSP_URL_REDACTED" ;;
        "back_patio") echo "RTSP_URL_REDACTED" ;;
        "patio_1") echo "RTSP_URL_REDACTED" ;;
        "patio_2") echo "RTSP_URL_REDACTED" ;;
        "3d_printers") echo "RTSP_URL_REDACTED" ;;
        "abundio_boundary") echo "RTSP_URL_REDACTED" ;;
        *) return 1 ;;
    esac
}

# Get parameters
CAMERA_NAME="${1:-front_door}"
DURATION="${2:-10}"

# Validate camera name
if ! CAMERA_URL=$(get_camera_url "$CAMERA_NAME"); then
    echo "Error: Camera '$CAMERA_NAME' not found in camera list" >&2
    echo "Available cameras: front_door, front_yard, front_yard_alt, front_door_patio, alley_north, alley_south, garage, carport, side_yard, back_patio, patio_1, patio_2, 3d_printers, abundio_boundary" >&2
    exit 1
fi

# Set up output
OUTPUT_DIR="$HOME/.openclaw/workspace/camera_audio"
mkdir -p "$OUTPUT_DIR"

OUTPUT_FILE="$OUTPUT_DIR/${CAMERA_NAME}_$(date +%Y%m%d_%H%M%S).wav"

# Capture audio
echo "Capturing $DURATION seconds of audio from $CAMERA_NAME..."
ffmpeg -rtsp_transport tcp -i "$CAMERA_URL" \
  -t "$DURATION" \
  -acodec pcm_s16le -ar 44100 -ac 2 \
  -f wav -y "$OUTPUT_FILE" 2>&1 | tail -5

echo "✓ Saved to: $OUTPUT_FILE"
ls -lh "$OUTPUT_FILE"