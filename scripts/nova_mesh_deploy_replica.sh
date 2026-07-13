#!/bin/bash
# nova_mesh_deploy_replica.sh — Set up PG streaming replica on mac-mini
# Run from mac-studio after SSH key auth is configured to mac-mini.
# Written by Jordan Koch.

set -e

REPLICA_HOST="192.168.1.190"
REPLICA_USER="kochj"
PRIMARY_HOST="192.168.1.6"
PG_DATA="/opt/homebrew/var/postgresql@17"

echo "=== Nova Mesh: Deploy PG Streaming Replica to mac-mini ==="

# Step 1: Install PostgreSQL on mac-mini if not present
echo "[1/5] Ensuring PostgreSQL@17 is installed on mac-mini..."
ssh "$REPLICA_USER@$REPLICA_HOST" "brew list postgresql@17 2>/dev/null || brew install postgresql@17"

# Step 2: Stop any existing PG on mac-mini
echo "[2/5] Stopping PostgreSQL on mac-mini..."
ssh "$REPLICA_USER@$REPLICA_HOST" "brew services stop postgresql@17 2>/dev/null || true; pg_ctl -D $PG_DATA stop 2>/dev/null || true"

# Step 3: Clear data dir and run pg_basebackup
echo "[3/5] Running pg_basebackup from primary..."
ssh "$REPLICA_USER@$REPLICA_HOST" "rm -rf $PG_DATA && pg_basebackup -h $PRIMARY_HOST -U nova_replication -D $PG_DATA -Fp -Xs -P -R"

# Step 4: Configure standby
echo "[4/5] Configuring standby settings..."
ssh "$REPLICA_USER@$REPLICA_HOST" "cat >> $PG_DATA/postgresql.auto.conf << 'EOF'
# Nova Mesh: Streaming Replica Config
hot_standby = on
primary_conninfo = 'host=$PRIMARY_HOST port=5432 user=nova_replication password=nova_replica_2026'
EOF"

# Step 5: Start replica
echo "[5/5] Starting replica..."
ssh "$REPLICA_USER@$REPLICA_HOST" "pg_ctl -D $PG_DATA -l /opt/homebrew/var/log/postgresql@17.log start"

# Verify
sleep 3
echo ""
echo "=== Verifying replica status ==="
psql -h "$REPLICA_HOST" -U kochj -d nova_ops -c "SELECT pg_is_in_recovery();"
echo ""
echo "Done. Replica is streaming from $PRIMARY_HOST."
echo "Read-only queries can now hit $REPLICA_HOST:5432"
