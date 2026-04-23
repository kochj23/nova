#!/bin/zsh
# nova_generate_drag_facts.sh — Generate remaining drag racing facts via Ollama
# and ingest them into Nova's vector memory.
#
# Uses deepseek-r1:8b locally to generate batches of 50 facts each,
# then ingests via the /remember API.
#
# Run: nohup ~/.openclaw/scripts/nova_generate_drag_facts.sh &
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
DATA="$SCRIPTS/data"
VECTOR_URL="http://127.0.0.1:18790/remember"
OLLAMA_URL="http://127.0.0.1:11434/api/generate"
MODEL="deepseek-r1:8b"
TARGET=1000
BATCH=25
LOG="$HOME/.openclaw/logs/drag-facts-gen.log"

mkdir -p "$DATA"

current=$(wc -l < "$DATA/drag_racing_facts.jsonl" 2>/dev/null | tr -d ' ')
echo "[$(date '+%H:%M:%S')] Starting drag racing fact generation. Current: $current, Target: $TARGET" >> "$LOG"

topics=(
    "famous drag racing tracks and their history from 1950 to present"
    "drag racing safety innovations and rule changes from 1960 to present"
    "Pro Mod drag racing class history records and famous cars"
    "import and JDM drag racing milestones and famous builds"
    "drag racing engine building techniques and specifications"
    "famous drag racing rivalries and grudge matches"
    "drag racing tire and suspension technology evolution"
    "no-prep and street racing culture events and personalities"
    "drag racing fuel systems carburetors EFI and tuning"
    "women in drag racing history achievements and milestones"
    "drag racing transmission and drivetrain technology"
    "electric and alternative fuel drag racing developments"
    "bracket racing strategy techniques and championship racing"
    "drag racing regional scenes Texas Oklahoma California Florida"
    "drag racing aerodynamics body design and parachute technology"
    "diesel drag racing trucks and tractor pulling crossover"
    "drag racing sponsorship marketing and media evolution"
    "Top Fuel and Funny Car crew chief strategies and tuning secrets"
    "motorcycle drag racing Pro Stock Bike and Top Fuel Harley"
    "drag racing record progression from 1950s to present decade by decade"
    "famous drag racing accidents crashes and safety improvements"
    "nostalgia drag racing vintage classes and restoration"
    "drag racing pit culture team dynamics and engine rebuilds between rounds"
    "street racing movies documentaries and cultural impact"
    "drag racing weather and track preparation effects on performance"
    "land speed racing crossover with drag racing at Bonneville and El Mirage"
    "drag racing legends who died racing and their legacy"
    "modern drag racing data acquisition and electronic tuning"
    "drag racing chassis design roll cage and SFI specifications"
    "famous drag racing team owner dynasties Force Schumacher Kalitta"
    "international drag racing scenes Australia Europe Middle East"
    "YouTube and social media impact on grassroots drag racing"
    "drag racing weight reduction aerodynamics and power to weight ratio"
    "drag racing clutch management multi-disc centrifugal clutch technology"
    "small tire racing classes X275 Ultra Street Outlaw 10.5 Limited Drag Radial"
)

for topic in "${topics[@]}"; do
    current=$(wc -l < "$DATA/drag_racing_facts.jsonl" 2>/dev/null | tr -d ' ')
    if [ "$current" -ge "$TARGET" ]; then
        echo "[$(date '+%H:%M:%S')] Reached $TARGET facts. Done." >> "$LOG"
        break
    fi

    echo "[$(date '+%H:%M:%S')] Generating $BATCH facts about: $topic (current: $current)" >> "$LOG"

    prompt="/no_think\nGenerate exactly $BATCH unique, specific facts about $topic.\nEach fact must include specific names, dates, speeds, times, or technical specifications.\nOutput ONLY valid JSONL — one JSON object per line, no other text.\nFormat: {\"text\": \"FACT\", \"source\": \"drag_racing\", \"metadata\": {\"category\": \"CATEGORY\", \"subcategory\": \"SUBCATEGORY\"}}\nCategories: history, legends, tracks, vehicles, technology, records, safety, culture, events, organization\nDo NOT repeat facts. Be specific and accurate."

    response=$(curl -s -X POST "$OLLAMA_URL" \
        -H "Content-Type: application/json" \
        -d "{\"model\": \"$MODEL\", \"prompt\": \"$prompt\", \"stream\": false, \"options\": {\"temperature\": 0.5, \"num_predict\": 4096}}" \
        --max-time 300 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)

    if [ -z "$response" ]; then
        echo "[$(date '+%H:%M:%S')] No response from Ollama — skipping topic" >> "$LOG"
        continue
    fi

    # Extract valid JSONL lines and append
    echo "$response" | while IFS= read -r line; do
        # Validate JSON
        echo "$line" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read().strip())
    if 'text' in d and 'source' in d:
        print(json.dumps(d))
except: pass
" >> "$DATA/drag_racing_facts.jsonl" 2>/dev/null
    done

    new_count=$(wc -l < "$DATA/drag_racing_facts.jsonl" | tr -d ' ')
    added=$((new_count - current))
    echo "[$(date '+%H:%M:%S')] Added $added facts (total: $new_count)" >> "$LOG"

    # Ingest new facts
    if [ "$added" -gt 0 ]; then
        tail -n "$added" "$DATA/drag_racing_facts.jsonl" | while IFS= read -r fact; do
            curl -s -X POST "$VECTOR_URL" \
                -H "Content-Type: application/json" \
                -d "$fact" > /dev/null 2>&1
        done
        echo "[$(date '+%H:%M:%S')] Ingested $added new facts into vector memory" >> "$LOG"
    fi

    sleep 5  # Don't hammer Ollama
done

final=$(wc -l < "$DATA/drag_racing_facts.jsonl" | tr -d ' ')
echo "[$(date '+%H:%M:%S')] Generation complete. Total facts: $final" >> "$LOG"

# Slack+Discord notification
bash ~/.openclaw/scripts/nova_slack_post.sh ":racing_car: *Drag Racing Knowledge Generation Complete*
Total facts: $final
All ingested into vector memory (source: drag_racing)" "C0ATAF7NZG9"
