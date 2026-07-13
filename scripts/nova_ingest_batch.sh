#!/bin/bash
# nova_ingest_batch.sh — fire the queued Wikipedia-BFS ingests, max 2 concurrent so we
# don't hammer the embedding pipeline / memory server. nohup this so it survives the session.
# Each job: "mode|query|source|queue_id". Marks each queue row in_progress as it launches.
set -uo pipefail
PY=/opt/homebrew/bin/python3
ING="$HOME/.openclaw/scripts/nova_ingest.py"
LOGD="$HOME/.openclaw/logs"
MAXCONC=2

JOBS=(
  "wikipedia|Zigbee|iot_core|640"
  "wikipedia|Z-Wave|iot_core|641"
  "wikipedia|Strawberry|gardening|642"
  "wikipedia|Klingon language|linguistics|643"
  "wikipedia|Chevrolet Corvette (C6)|automotive|807"
  "wikipedia|Paxton Automotive|automotive|808"
  "wikipedia|Chaminade College Preparatory School (California)|education|809"
  "wikipedia|Heraclius|history|810"
  "wikipedia|Carvel (franchise)|cooking|811"
  "wikipedia|Big League Chew|cooking|813"
  "wikipedia|Fun Dip|cooking|814"
)

echo "[batch] $(date) starting ${#JOBS[@]} ingests, max ${MAXCONC} concurrent"
for job in "${JOBS[@]}"; do
  IFS='|' read -r mode query src qid <<< "$job"
  # gate: wait while this script already has MAXCONC children running
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$MAXCONC" ]; do sleep 20; done
  log="$LOGD/ingest_q${qid}_${src}.log"
  nohup "$PY" "$ING" "$mode" "$query" --source "$src" --target 10000 --yes > "$log" 2>&1 &
  pid=$!
  echo "[batch] launched q$qid pid=$pid: '$query' -> $src (log: $log)"
  psql -h localhost -U kochj -d nova_ops -tAc \
    "UPDATE claude_queue SET status='in_progress', context=coalesce(context,'')||' [batch pid $pid $(date +%F)]', updated_at=now() WHERE id=$qid;" >/dev/null 2>&1
  sleep 8
done
wait
echo "[batch] $(date) all ingests finished"
