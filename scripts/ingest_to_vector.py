#!/usr/bin/env python3

"""
Ingest a markdown file into the PostgreSQL vector database via HTTP API.
"""

import sys
import requests
import os

VECTOR_API = "http://127.0.0.1:18790/ingest"

if len(sys.argv) != 3:
    print("Usage: ingest_to_vector.py <file.md> <source>")
    sys.exit(1)

file_path = sys.argv[1]
source = sys.argv[2]

title = os.path.basename(file_path).replace('.md', '').replace('_', ' ')

with open(file_path, 'r') as f:
    content = f.read()

payload = {
    "text": content,
    "title": title,
    "source": source
}

try:
    response = requests.post(VECTOR_API, json=payload)
    if response.status_code == 200:
        print(f"Successfully ingested {file_path} into '{source}'")
    else:
        print(f"Failed to ingest: {response.status_code} {response.text}")
except Exception as e:
    print(f"Request failed: {e}")
