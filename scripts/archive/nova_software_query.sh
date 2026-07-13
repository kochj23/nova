#!/bin/bash
# nova_software_query.sh — Quick software inventory queries

INVENTORY="$HOME/.openclaw/workspace/software-inventory/inventory-latest.json"

if [ ! -f "$INVENTORY" ]; then
  echo "❌ No inventory found. Run: python3 ~/.openclaw/scripts/nova_software_inventory.py"
  exit 1
fi

case "$1" in
  "list-brew")
    echo "📦 Homebrew Packages:"
    jq -r '.homebrew.formulae[] | "\(.name) (\(.version))"' "$INVENTORY" | sort
    ;;
  
  "list-apps")
    echo "🖥️  Applications:"
    jq -r '.applications[] | "\(.name) (\(.version))"' "$INVENTORY" | sort
    ;;
  
  "list-npm")
    echo "📦 Global npm Packages:"
    jq -r '.npm[] | "\(.name) (\(.version))"' "$INVENTORY" | sort
    ;;
  
  "list-python")
    echo "🐍 Python Packages:"
    jq -r '.python[] | "\(.name) (\(.version))"' "$INVENTORY" | sort
    ;;
  
  "list-cli")
    echo "⚙️  CLI Tools:"
    jq -r '.cli_tools[] | "\(.name): \(.version)"' "$INVENTORY"
    ;;
  
  "search")
    if [ -z "$2" ]; then
      echo "Usage: $0 search <term>"
      exit 1
    fi
    TERM="$2"
    echo "🔍 Searching for '$TERM'..."
    echo ""
    
    BREW_MATCHES=$(jq -r ".homebrew.formulae[] | select(.name | contains(\"$TERM\")) | .name" "$INVENTORY")
    if [ -n "$BREW_MATCHES" ]; then
      echo "Homebrew matches:"
      echo "$BREW_MATCHES" | sed 's/^/  • /'
    fi
    
    APP_MATCHES=$(jq -r ".applications[] | select(.name | contains(\"$TERM\")) | .name" "$INVENTORY")
    if [ -n "$APP_MATCHES" ]; then
      echo "Application matches:"
      echo "$APP_MATCHES" | sed 's/^/  • /'
    fi
    
    NPM_MATCHES=$(jq -r ".npm[] | select(.name | contains(\"$TERM\")) | .name" "$INVENTORY")
    if [ -n "$NPM_MATCHES" ]; then
      echo "npm matches:"
      echo "$NPM_MATCHES" | sed 's/^/  • /'
    fi
    ;;
  
  "summary")
    echo "📊 Software Inventory Summary:"
    jq '{
      timestamp: .timestamp,
      homebrew_packages: (.homebrew.formulae | length),
      homebrew_casks: (.homebrew.casks | length),
      applications: (.applications | length),
      npm_global: (.npm | length),
      python_packages: (.python | length),
      cli_tools: (.cli_tools | length)
    }' "$INVENTORY" | jq .
    ;;
  
  *)
    cat << 'EOF'
Usage: nova_software_query.sh <command>

Commands:
  list-brew       List all Homebrew packages
  list-apps       List all Applications
  list-npm        List global npm packages
  list-python     List Python packages
  list-cli        List CLI tools
  search <term>   Search across all software
  summary         Show inventory summary

Examples:
  nova_software_query.sh list-brew
  nova_software_query.sh search node
  nova_software_query.sh summary
EOF
    ;;
esac
