# PostgreSQL Failover Runbook — Nova Fleet

**Purpose:** what to do when the PG **primary on `.6` (mac-studio)** dies, so the
memory DB + nova_ops stay writable. Written 2026-06-21. Free insurance — read it
*before* you need it.

## Topology (as of 2026-06-21)
| Role | Host | PG | How it runs | pgvector | Notes |
|---|---|---|---|---|---|
| **Primary** | `.6` 192.168.1.6 | 17.9 Homebrew | native, `/Volumes/MoreData/postgresql@17` | yes | also the SPOF |
| Replica #1 | `.2` 192.168.1.2 (nova-core) | 17 | **Docker** `pg17-replica` (`postgres:17`) | **NO** ⚠ | stock image — serves `nova_ops` only, **cannot serve `nova_memories` vectors** until image swapped to `pgvector/pgvector:pg17` |
| Replica #2 | `.10` 192.168.1.10 (nuk) | 17.10 brew | native, `postgresql-17.service` | **yes ✓ (verified)** | **preferred failover target for `nova_memories`** |

- Replication user: **`replicator`**, trust auth from `192.168.1.0/24` (no password).
- Slots: `nova_core_slot` (.2), `nuk_replica` (.10). `max_slot_wal_keep_size=100GB`.
- **Pick the failover target by which DB you need:**
  - `nova_ops` (telemetry, queue, actions) → **`.2`** is fine (and beefier).
  - `nova_memories` (1.6M vectors) → **`.10` (nuk)** — it's the only replica with pgvector today.
  - ⚠ Until `.2`'s container is rebuilt on `pgvector/pgvector:pg17`, do **not** promote `.2` for the memory DB.

## 0. Detect
The **nuk watchdog** posts to `#nova-critical`: *"mac-studio (.6) postgres unreachable."*
Confirm `.6` PG is truly down (not just a network blip) before promoting — a
needless promote causes split-brain risk.

```bash
pg_isready -h 192.168.1.6 -p 5432      # CONNECTION FAILED = primary down
```

## 1. Choose target → promote
Pick the most-caught-up replica (lowest lag), respecting the pgvector rule above.

**Promote nuk (.10) — native, pgvector-ready (use for `nova_memories`):**
```bash
PSQL=/home/linuxbrew/.linuxbrew/opt/postgresql@17/bin
ssh kochj@192.168.1.10 "$PSQL/psql -h 127.0.0.1 -d postgres -tAc 'SELECT pg_promote()'"
ssh kochj@192.168.1.10 "$PSQL/psql -h 127.0.0.1 -d postgres -tAc 'SELECT pg_is_in_recovery()'"  # -> f
```

**Promote .2 — Docker `pg17-replica` (`nova_ops` only, no pgvector yet):**
```bash
ssh kochj@192.168.1.2 "docker exec pg17-replica psql -U postgres -tAc 'SELECT pg_promote()'"
ssh kochj@192.168.1.2 "docker exec pg17-replica psql -U postgres -tAc 'SELECT pg_is_in_recovery()'"  # -> f
```

## 2. Re-point applications to the new primary (`.2`)
**This is the painful part** — most Nova scripts hardcode `127.0.0.1`/`192.168.1.6`.
Fastest options, in order of preference:
- **Best (do this preventively):** front PG with a DNS name `pg-primary.lan` or a
  floating VIP, and just flip it to `.2`. (TODO: not yet in place — see #651 follow-up.)
- **Now:** bulk-repoint DSNs. On `.6` scripts dir + `.2`:
  ```bash
  grep -rl '192.168.1.6:5432\|127.0.0.1:5432\|host=127.0.0.1' ~/.openclaw/scripts/
  # sed the DSNs to 192.168.1.2, restart the affected services
  ```
- Secrets on `.2` live in `~/.openclaw/secrets.env` (NOT Keychain).

## 3. Re-home the surviving replica (`.10`) under the new primary
nuk was following `.6`; point it at `.2` so you keep a replica:
```bash
# on .2 (new primary): create a slot for nuk
sudo -u postgres psql -tAc "SELECT pg_create_physical_replication_slot('nuk_replica2');"
# on .10: repoint + restart
sudo systemctl stop postgresql-17.service
PGDATA=/home/linuxbrew/.linuxbrew/var/postgresql@17
# edit primary_conninfo host=192.168.1.6 -> 192.168.1.2, primary_slot_name -> nuk_replica2
sed -i 's/host=192.168.1.6/host=192.168.1.2/' $PGDATA/postgresql.auto.conf
sed -i "s/primary_slot_name = 'nuk_replica'/primary_slot_name = 'nuk_replica2'/" $PGDATA/postgresql.auto.conf
sudo systemctl start postgresql-17.service
# verify: SELECT status FROM pg_stat_wal_receiver;  -> streaming
```
If timelines diverge, rebuild nuk with a fresh `pg_basebackup` from `.2` (see
how it was first built — same steps, host=192.168.1.2).

## 4. When `.6` comes back — DO NOT let it be a second primary (split-brain!)
`.6` must rejoin as a **standby** of the new primary (`.2`):
```bash
# on .6, fastest if WAL aligns:
pg_rewind --target-pgdata=/Volumes/MoreData/postgresql@17 \
          --source-server="host=192.168.1.2 user=replicator dbname=postgres"
# then add standby.signal + primary_conninfo host=192.168.1.2, start.
# if pg_rewind fails: fresh pg_basebackup from .2.
```

## 5. Verify recovery
- Writes succeed on `.2`; `SELECT count(*)` on `nova_memories` returns ~1.6M.
- `vector` queries work (pgvector present on the promoted node).
- nuk replica `streaming`; apps healthy; watchdog quiet.

## Failback (optional, low-priority)
Once stable, you can promote `.6` back during a maintenance window by reversing
the roles. Not urgent — `.2` is a fine primary.

---
**Preventive TODOs (make next failover trivial):**
- [ ] **Rebuild `.2`'s `pg17-replica` container on `pgvector/pgvector:pg17`** so it can
      serve `nova_memories` too — today only nuk can. (Discovered 2026-06-21.)
- [ ] DNS/VIP `pg-primary` so app repoint is one record, not a sed sweep (#651).
- [x] pgvector verified on `.10` (nuk) 2026-06-21; `.2` confirmed WITHOUT (container).
- [ ] Periodic restore-test of a replica (#644).
