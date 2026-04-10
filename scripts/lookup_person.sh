#!/bin/bash
# lookup_person.sh — Search the SRE directory in Nova's memory
# Usage: lookup_person.sh "Name" [source]
# Example: lookup_person.sh "CONTACT_NAME_REDACTED" disney
QUERY="${1:?Usage: lookup_person.sh \"Name\" [source]}"
SOURCE="${2:-disney}"
ENCODED=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$QUERY'))")
curl -s "http://127.0.0.1:18790/search?q=${ENCODED}&source=${SOURCE}&n=5"
