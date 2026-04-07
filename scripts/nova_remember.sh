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
curl -X POST http://127.0.0.1:37400/api/workflows/action-item-to-slack/run \
  -H "Content-Type: application/json" \
  -d "{ \"summary\": \"$summary\" }"

elif [ "$type_tag" = "action-item" ] || [ "$type_tag" = "task" ]; then
  # Assume action-item/task storage logic here
echo "$json_payload" | /opt/homebrew/bin/bun run "$HOME/.openclaw/scripts/ingest_action_item_to_graph.ts"
echo "Action item stored: $summary"
  
else
  # Default storage in vector memory via the gateway
  # Make a POST request to store the summary
  http_response=$(curl -s -w "%{http_code}" -X POST http://127.0.0.1:18790/context/write \
    -H "Content-Type: application/json" \
    --data "{\"key\": \"$type_tag\", \"value\": \"$summary\"}")

  # Extract the HTTP status code from the response
  status_code="${http_response: -3}"

  # Check if the request was successful
  if [ "$status_code" -eq 200 ]; then
    echo "Summary stored successfully in vector memory: $summary"
  else
    echo "Failed to store summary in vector memory. HTTP Status: $status_code. Full response: $http_response"
    exit 1
  fi

fi
