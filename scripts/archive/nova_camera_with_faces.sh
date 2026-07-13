#!/bin/bash
# Run camera monitor + face integration together

export PYTHONPATH="/Volumes/Data/AI/python_packages:$PYTHONPATH"

# Capture camera frames
python3 ~/.openclaw/scripts/nova_camera_monitor.py

# Analyze frames for faces
python3 ~/.openclaw/scripts/nova_face_integration.py
