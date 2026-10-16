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
  ASSERT (SELECT status FROM claude_queue WHERE id = tid) = 'done', 'finished';
  -- Kanban (2026-10-15): finished statuses collapse to done, original kept in outcome
  UPDATE claude_queue SET status = 'resolved', outcome = 'patched' WHERE id = tid;
  ASSERT (SELECT status = 'done' AND outcome LIKE 'patched%[finished as resolved]' AND completed_at IS NOT NULL
            FROM claude_queue WHERE id = tid), 'resolved -> done';
  -- WIP limit: 3 live claims per claimant, 4th refused, own re-claim still allowed
  INSERT INTO claude_queue (session_id, description, priority)
  SELECT 't-sess', 'wip ' || g, -90 FROM generate_series(1, 4) g;
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9', (SELECT min(id) FROM claude_queue WHERE description = 'wip 1'))) = 1, 'wip 1';
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9', (SELECT min(id) FROM claude_queue WHERE description = 'wip 2'))) = 1, 'wip 2';
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9', (SELECT min(id) FROM claude_queue WHERE description = 'wip 3'))) = 1, 'wip 3';
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9', (SELECT min(id) FROM claude_queue WHERE description = 'wip 4'))) = 0, 'WIP limit blocks a 4th';
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9')) = 0, 'WIP limit blocks next-best too';
  ASSERT (SELECT count(*) FROM claude_claim('hW/s9', (SELECT min(id) FROM claude_queue WHERE description = 'wip 1'))) = 1, 'own re-claim ok';
  ASSERT (SELECT count(*) FROM claude_claim('hX/s8', (SELECT min(id) FROM claude_queue WHERE description = 'wip 4'))) = 1, 'other claimant unaffected';
  -- unowned zombie (no claimant, no lease, idle > 1 day) is requeued by the reaper
  INSERT INTO claude_queue (session_id, description, status, updated_at)
  VALUES ('t-sess', 'zombie', 'in_progress', now() - interval '30 days');
  ASSERT (SELECT count(*) FROM claude_reap() r WHERE r.description = 'zombie' AND r.was = '(unowned)') = 1, 'zombie reaped';
  ASSERT (SELECT progress LIKE '%no claimant since%requeued' FROM claude_queue WHERE description = 'zombie'), 'zombie note';
  ASSERT (SELECT lead_p85_days IS NOT NULL FROM claude_queue_flow), 'flow view has p85 lead time';
  ASSERT (SELECT count(*) FROM claude_queue_wip WHERE description = 'zombie') = 1, 'wip view lists open items';
  RAISE NOTICE 'coord self-test: all assertions passed';
END $$;
ROLLBACK;
