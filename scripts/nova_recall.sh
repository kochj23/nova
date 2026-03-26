#!/usr/bin/env bash
# nova_recall.sh — Retrieve semantically similar memories from Nova's vector DB
# Usage: nova_recall.sh "query text" [n_results] [source_filter] [min_score]
# Returns JSON array of matching memories with similarity scores.

set -euo pipefail

QUERY="${1:?Usage: nova_recall.sh \"query\" [n_results] [source_filter] [min_score]}"
N="${2:-5}"
SOURCE="${3:-}"
MIN_SCORE="${4:-0.0}"
PORT=18790

URL="http://127.0.0.1:${PORT}/recall?n=${N}&min_score=${MIN_SCORE}"

# URL-encode the query
ENCODED_QUERY=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$QUERY")
URL="${URL}&q=${ENCODED_QUERY}"

if [[ -n "$SOURCE" ]]; then
    URL="${URL}&source=${SOURCE}"
fi

RESPONSE=$(curl -sf "$URL") || {
    echo "ERROR: Memory server not reachable at port ${PORT}. Is it running?" >&2
    exit 1
}

# Pretty-print results
python3 -c "
import sys, json

data = json.loads(sys.argv[1])
memories = data.get('memories', [])
count = data.get('count', 0)

if count == 0:
    print('No memories found for: ' + data.get('query', ''))
    sys.exit(0)

print(f'Found {count} memory/memories for: ' + data.get('query', ''))
print()
for i, m in enumerate(memories, 1):
    score_pct = int(m['score'] * 100)
    print(f'[{i}] ({score_pct}% match) [{m[\"source\"]}] {m[\"created_at\"][:10]}')
    print(f'    {m[\"text\"]}')
    if m.get('metadata'):
        print(f'    meta: {json.dumps(m[\"metadata\"])}')
    print()
" "$RESPONSE"
