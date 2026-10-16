BEGIN;
ALTER TABLE claude_queue
  ADD COLUMN IF NOT EXISTS claimed_by   text,         -- '<host>/<session_id>'
  ADD COLUMN IF NOT EXISTS lease_until  timestamptz,
  ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS progress     text;         -- running notes, newest last
CREATE INDEX IF NOT EXISTS idx_claude_queue_claim ON claude_queue (claimed_by) WHERE claimed_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS claude_locks (
  resource    text PRIMARY KEY,                       -- e.g. 'repo:~/.openclaw'
  holder      text NOT NULL,                          -- '<host>/<session_id>'
  acquired_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz NOT NULL
);

-- Claim a task atomically. p_id NULL = best queued/pending task. Expired leases are claimable;
-- legacy unowned in_progress rows only by explicit id.
-- Kanban WIP limit (2026-10-15, plan #56): a claimant holding p_wip live leases gets nothing back
-- (plus a NOTICE) until it finishes or hands one back. Re-claiming a row you already hold is always allowed.
-- p_wip was added as a 4th defaulted arg, so every existing 1-3 arg call keeps working unchanged.
DROP FUNCTION IF EXISTS claude_claim(text, int, interval);
CREATE OR REPLACE FUNCTION claude_claim(p_who text, p_id int DEFAULT NULL, p_lease interval DEFAULT '20 minutes',
                                        p_wip int DEFAULT 3)
RETURNS SETOF claude_queue LANGUAGE plpgsql AS $$
DECLARE held int;
BEGIN
  SELECT count(*) INTO held FROM claude_queue
   WHERE status = 'in_progress' AND claimed_by = p_who AND lease_until > now() AND id IS DISTINCT FROM p_id;
  IF held >= p_wip THEN
    RAISE NOTICE 'claude_claim: % already holds % in-progress items (WIP limit %) - finish or hand one back first',
                 p_who, held, p_wip;
    RETURN;
  END IF;
  RETURN QUERY
  UPDATE claude_queue q
     SET status = 'in_progress', claimed_by = p_who, lease_until = now() + p_lease,
         heartbeat_at = now(), updated_at = now(),
         progress = concat_ws(E'\n', q.progress,
                    to_char(now(), 'MM-DD HH24:MI') || ' claimed by ' || p_who ||
                    CASE WHEN q.claimed_by IS NOT NULL AND q.claimed_by <> p_who
                         THEN ' (took over from ' || q.claimed_by || ')' ELSE '' END)
   WHERE q.id = (SELECT c.id FROM claude_queue c
                  WHERE (p_id IS NULL OR c.id = p_id)
                    AND (c.status IN ('queued', 'pending')
                         OR (c.status = 'in_progress' AND (c.lease_until < now()
                             OR (p_id IS NOT NULL AND (c.claimed_by IS NULL OR c.claimed_by = p_who)))))
                  ORDER BY c.priority, c.created_at LIMIT 1
                  FOR UPDATE SKIP LOCKED)
  RETURNING q.*;
END $$;

-- Append a progress note (also renews the lease). False if p_who doesn't hold it.
CREATE OR REPLACE FUNCTION claude_note(p_id int, p_who text, p_note text)
RETURNS boolean LANGUAGE sql AS $$
  WITH u AS (UPDATE claude_queue
                SET progress = concat_ws(E'\n', progress, to_char(now(), 'MM-DD HH24:MI') || ' ' || p_note),
                    lease_until = now() + interval '20 minutes', heartbeat_at = now(), updated_at = now()
              WHERE id = p_id AND claimed_by = p_who RETURNING 1)
  SELECT exists(SELECT 1 FROM u);
$$;

-- Finish: p_status done|completed|deferred, or 'queued' to hand it back. Clears the lease.
CREATE OR REPLACE FUNCTION claude_finish(p_id int, p_who text, p_status text, p_outcome text)
RETURNS boolean LANGUAGE sql AS $$
  WITH u AS (UPDATE claude_queue
                SET status = p_status, outcome = p_outcome, lease_until = NULL, updated_at = now(),
                    completed_at = CASE WHEN p_status IN ('done', 'completed') THEN now() END,
                    claimed_by = CASE WHEN p_status = 'queued' THEN NULL ELSE claimed_by END,
                    progress = concat_ws(E'\n', progress, to_char(now(), 'MM-DD HH24:MI') || ' ' || p_status || ' by ' || p_who)
              WHERE id = p_id AND claimed_by = p_who RETURNING 1)
  SELECT exists(SELECT 1 FROM u);
$$;

-- Take or renew a lock. True if p_who now holds it.
CREATE OR REPLACE FUNCTION claude_lock(p_resource text, p_who text, p_ttl interval DEFAULT '15 minutes')
RETURNS boolean LANGUAGE sql AS $$
  WITH u AS (INSERT INTO claude_locks (resource, holder, lease_until)
             VALUES (p_resource, p_who, now() + p_ttl)
             ON CONFLICT (resource) DO UPDATE
               SET holder = excluded.holder, lease_until = excluded.lease_until,
                   acquired_at = CASE WHEN claude_locks.holder = excluded.holder
                                      THEN claude_locks.acquired_at ELSE now() END
             WHERE claude_locks.holder = excluded.holder OR claude_locks.lease_until < now()
             RETURNING 1)
  SELECT exists(SELECT 1 FROM u);
$$;

CREATE OR REPLACE FUNCTION claude_unlock(p_resource text, p_who text)
RETURNS boolean LANGUAGE sql AS $$
  WITH d AS (DELETE FROM claude_locks WHERE resource = p_resource AND holder = p_who RETURNING 1)
  SELECT exists(SELECT 1 FROM d);
$$;

-- Session heartbeat: renew every claim this session holds. Called by session-logger per action.
-- Locks are NOT renewed here: a repo lock lives 15 min past the holder's last git write.
CREATE OR REPLACE FUNCTION claude_heartbeat(p_session text)
RETURNS void LANGUAGE sql AS $$
  UPDATE claude_queue SET lease_until = now() + interval '20 minutes', heartbeat_at = now()
   WHERE status = 'in_progress' AND claimed_by LIKE '%/' || p_session;
$$;

-- Reaper: expired claims go back to the queue with a note; expired locks are dropped.
-- Also unowned zombies (in_progress, no claimant, no lease, untouched for a day: pre-lease legacy rows).
CREATE OR REPLACE FUNCTION claude_reap()
RETURNS TABLE (id int, description text, was text) LANGUAGE sql AS $$
  DELETE FROM claude_locks WHERE lease_until < now();
  WITH old AS (SELECT q.id, q.claimed_by, q.updated_at FROM claude_queue q
                WHERE q.status = 'in_progress'
                  AND ((q.claimed_by IS NOT NULL AND q.lease_until < now())
                       OR (q.claimed_by IS NULL AND q.lease_until IS NULL AND q.updated_at < now() - interval '1 day'))
                FOR UPDATE SKIP LOCKED)
  UPDATE claude_queue q
     SET status = 'queued', claimed_by = NULL, lease_until = NULL, updated_at = now(),
         progress = concat_ws(E'\n', q.progress, to_char(now(), 'MM-DD HH24:MI') ||
                    CASE WHEN old.claimed_by IS NOT NULL
                         THEN ' lease expired, requeued (was ' || old.claimed_by || ')'
                         ELSE ' in_progress with no claimant since ' || to_char(old.updated_at, 'YYYY-MM-DD') || ', requeued' END)
    FROM old WHERE q.id = old.id
  RETURNING q.id, q.description, coalesce(old.claimed_by, '(unowned)');
$$;

-- Kanban (2026-10-15, plan #56): one finished status. 'completed'/'resolved'/'built' from any writer
-- (claude_finish, nova_cve_autopatch, hand UPDATEs) become 'done'; the original word is kept in outcome.
CREATE OR REPLACE FUNCTION claude_queue_normalize_status()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('completed', 'resolved', 'built') THEN
    NEW.outcome := concat_ws(' ', NEW.outcome, '[finished as ' || NEW.status || ']');
    NEW.status := 'done';
    NEW.completed_at := coalesce(NEW.completed_at, NEW.updated_at, now());
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER claude_queue_normalize_status BEFORE INSERT OR UPDATE OF status ON claude_queue
  FOR EACH ROW EXECUTE FUNCTION claude_queue_normalize_status();
UPDATE claude_queue SET status = status WHERE status IN ('completed', 'resolved', 'built');   -- maps history once

-- Work-item age: every open item, oldest first.
CREATE OR REPLACE VIEW claude_queue_wip AS
SELECT q.id, q.status, q.priority, q.claimed_by, left(q.description, 100) AS description,
       round((extract(epoch FROM now() - q.created_at) / 86400)::numeric, 1) AS age_days,
       round((extract(epoch FROM now() - q.updated_at) / 86400)::numeric, 1) AS idle_days
FROM claude_queue q
WHERE q.status IN ('queued', 'pending', 'in_progress', 'deferred')
ORDER BY q.created_at;

-- Flow: lead time (created -> done, last 30 days), throughput vs inflow (7 days), WIP, oldest item.
CREATE OR REPLACE VIEW claude_queue_flow AS
SELECT
  (SELECT count(*) FROM claude_queue WHERE status = 'in_progress') AS wip,
  (SELECT count(*) FROM claude_queue WHERE status IN ('queued', 'pending')) AS queued,
  (SELECT round((extract(epoch FROM now() - min(created_at)) / 86400)::numeric, 1)
     FROM claude_queue WHERE status IN ('queued', 'pending', 'in_progress')) AS oldest_open_days,
  (SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at)) / 86400)::numeric, 2)
     FROM claude_queue WHERE status = 'done' AND completed_at > now() - interval '30 days') AS lead_p50_days,
  (SELECT round((percentile_cont(0.85) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at)) / 86400)::numeric, 2)
     FROM claude_queue WHERE status = 'done' AND completed_at > now() - interval '30 days') AS lead_p85_days,
  (SELECT count(*) FROM claude_queue WHERE status = 'done' AND completed_at > now() - interval '7 days') AS throughput_7d,
  (SELECT count(*) FROM claude_queue WHERE created_at > now() - interval '7 days') AS inflow_7d;

-- Who is doing what: live sessions, their claims and locks.
DROP VIEW IF EXISTS claude_board;
CREATE VIEW claude_board AS
SELECT s.host, left(s.session_id, 8) AS session, s.started_at,
       a.ts AS last_action_at, a.description AS doing,
       (SELECT string_agg('#' || q.id || ' ' || left(q.description, 60), '; ')
          FROM claude_queue q WHERE q.status = 'in_progress' AND q.claimed_by LIKE '%/' || s.session_id) AS claims,
       (SELECT string_agg(l.resource, ', ') FROM claude_locks l
         WHERE l.holder LIKE '%/' || s.session_id AND l.lease_until > now()) AS locks
FROM claude_sessions s
JOIN LATERAL (SELECT ts, description FROM claude_actions
              WHERE session_id = s.session_id ORDER BY ts DESC LIMIT 1) a ON true
WHERE a.ts > now() - interval '30 minutes'
  AND s.session_id ~ '^[0-9a-f]{8}-';
COMMIT;
