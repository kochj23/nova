# Setting Up PostgreSQL for Nova's Memory System

## Problem

Nova currently uses a SQLite-based memory system (`main.sqlite`), but my policy requires all new memories and vector indexes to be stored in a PostgreSQL vector database running locally.

Currently, `postgresql@16` and `postgresql@17` are installed via Homebrew, and `psql` is available at `/opt/homebrew/bin/psql`. PostgreSQL is accessible via port 5432.

However, the application's memory system still points to SQLite (`http://127.0.0.1:18790`).

## Solution

1. **Initialize Database** (if not already done):

   ```bash
   # As your user (jordankoch), PostgreSQL is already running
   # Connect to the server
   psql -h localhost -U $(whoami) -d postgres
   ```

   ```sql
   -- Create the database
   CREATE DATABASE nova_mem;

   -- Create user with secure password
   CREATE USER nova_user WITH PASSWORD 'secure_password_here';
   GRANT CONNECT ON DATABASE nova_mem TO nova_user;
   ```

2. **Update Memory App Configuration**

   Edit the memory server config (e.g., `.openclaw/config.json`) to use PostgreSQL:

   ```json
   {
     "memoryBackend": "postgres",
     "postgresHost": "127.0.0.1",
     "postgresPort": 5432,
     "postgresDatabase": "nova_mem",
     "postgresUser": "nova_user",
     "postgresPassword": "secure_password_here" // or use env var / keychain
   }
   ```

3. **Migrate Existing Data**
   Run the migration script (which should handle schema and content):

   ```bash
   python3 ~/.openclaw/scripts/migrate_sqlite_to_postgres.py
   ```

4. **Update Application**
   After migration, the memory server should now respond with the correct stats from PostgreSQL:

   ```bash
   curl -s 'http://127.0.0.1:18790/stats'
   ```
   Expected: `{'source': 'memory', 'count': 1200, 'status': 'indexed'}`

---
**✅ Readiness**
- [x] PostgreSQL is running
- [x] `psql` client available
- [ ] Database `nova_mem` created
- [ ] User `nova_user` created and granted access
- [ ] Application config updated to point to Postgres
- [ ] Data migration completed

**🔐 Security Reminder**:
- PostgreSQL password must be stored in keychain (`nova-postgres-password`), not in config files.
- The memory server should read it via `vault` API or `cluster_config.py`.