-- Self-check for the claim/lease/lock functions. Runs in a transaction and rolls back.
BEGIN;
INSERT INTO claude_sessions (session_id) VALUES ('t-sess') ON CONFLICT DO NOTHING;
INSERT INTO claude_queue (session_id, description, priority, status)
VALUES ('t-sess', 'coord self-test', -100, 'queued') RETURNING id AS tid \gset
DO $$
DECLARE tid int := (SELECT id FROM claude_queue WHERE description = 'coord self-test' AND priority = -100);
BEGIN
  ASSERT (SELECT count(*) FROM claude_claim('hA/s1', tid)) = 1, 'A claims';
  ASSERT (SELECT count(*) FROM claude_claim('hB/s2', tid)) = 0, 'B cannot steal a live claim';
  ASSERT claude_note(tid, 'hA/s1', 'halfway'), 'holder can note';
  ASSERT NOT claude_note(tid, 'hB/s2', 'nope'), 'non-holder cannot note';
  UPDATE claude_queue SET lease_until = now() - interval '1 minute' WHERE id = tid;
  ASSERT (SELECT count(*) FROM claude_reap() r WHERE r.id = tid) = 1, 'reaper requeues expired';
  ASSERT (SELECT status FROM claude_queue WHERE id = tid) = 'queued', 'status back to queued';
  ASSERT (SELECT count(*) FROM claude_claim('hB/s2', tid)) = 1, 'B claims after reap';
  ASSERT (SELECT progress FROM claude_queue WHERE id = tid) LIKE '%halfway%lease expired%claimed by hB/s2%', 'progress trail kept';
  ASSERT claude_finish(tid, 'hB/s2', 'done', 'ok'), 'B finishes';
  ASSERT claude_lock('repo:t', 'hA/s1'), 'A locks';
  ASSERT claude_lock('repo:t', 'hA/s1'), 'A renews';
  ASSERT NOT claude_lock('repo:t', 'hB/s2'), 'B blocked';
  UPDATE claude_locks SET lease_until = now() - interval '1 second' WHERE resource = 'repo:t';
  ASSERT claude_lock('repo:t', 'hB/s2'), 'B takes expired lock';
  ASSERT NOT claude_unlock('repo:t', 'hA/s1') AND claude_unlock('repo:t', 'hB/s2'), 'only holder unlocks';
  RAISE NOTICE 'coord self-test: all assertions passed';
END $$;
ROLLBACK;
