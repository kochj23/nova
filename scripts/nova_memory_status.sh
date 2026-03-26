#!/usr/bin/env bash
# nova_memory_status.sh — Health check and stats for Nova's vector memory server
# Usage: nova_memory_status.sh

PORT=18790

RESPONSE=$(curl -sf "http://127.0.0.1:${PORT}/stats") || {
    echo "Memory server is DOWN (port ${PORT} not responding)"
    exit 1
}

python3 -c "
import sys, json
d = json.loads(sys.argv[1])
print('Nova Memory Server — ONLINE')
print(f'  Memories stored : {d[\"count\"]}')
print(f'  Embedding dims  : {d[\"dims\"]}')
print(f'  Model           : {d[\"model\"]}')
print(f'  Database        : {d[\"db\"]}')
if d.get('by_source'):
    print('  By source:')
    for src, n in d['by_source'].items():
        print(f'    {src:15s} {n}')
" "$RESPONSE"
