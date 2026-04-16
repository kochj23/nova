#!/bin/zsh

# Validate inputs
if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: $0 \"<summary text>\" \"<type>\""
  exit 1
fi

# Extract and sanitize variables
summary="$1"
type_tag="$2"

# Construct the JSON payload
json_payload="{
  \"summary\": \"$summary\"\n}"  

# Determine the appropriate curl command based on the type tag
if [ "$type_tag" = "meeting" ]; then
  # Use the NovaControl workflow for meeting summaries
curl -s -X POST http://127.0.0.1:37400/api/workflows/action-item-to-slack/run \
  -H "Content-Type: application/json" \
  -d "{ \"title\": \"$summary\", \"assignee\": \"Jordan\", \"trigger\": \"meeting\" }"

elif [ "$type_tag" = "action-item" ] || [ "$type_tag" = "task" ]; then
  # Assume action-item/task storage logic here
echo "$json_payload" | /opt/homebrew/bin/bun run "$HOME/.openclaw/scripts/ingest_action_item_to_graph.ts"
echo "Action item stored: $summary"
  
else
  # Default storage in vector memory via the /remember endpoint
  http_response=$(curl -s -w "\n%{http_code}" -X POST http://127.0.0.1:18790/remember \
    -H "Content-Type: application/json" \
    --data "{\"text\": \"$summary\", \"source\": \"$type_tag\", \"metadata\": {}}")

  # Extract the HTTP status code (last line)
  status_code=$(echo "$http_response" | tail -1)
  body=$(echo "$http_response" | sed '$d')

  if [ "$status_code" -eq 200 ]; then
    echo "Stored in vector memory (source=$type_tag): ${summary:0:80}..."
  else
    echo "Failed to store in vector memory. HTTP $status_code: $body"
    exit 1
  fi

fi
