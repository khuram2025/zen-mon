-- One active network capture per server, enforced by the database.
--
-- Starting a capture checks for an existing queued/running row and then
-- inserts, which is two statements: two operators clicking Start at the same
-- moment both pass the check and both insert. The agent only ever runs one,
-- so the loser becomes an orphan row that is never advanced — and since the
-- same check refuses to start while it exists, that server can never capture
-- again without manual DB surgery.

-- Close out any rows that are already stuck, oldest-wins, so the index can
-- be created on existing data.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY server_id ORDER BY requested_at) AS rn
    FROM network_captures
    WHERE status IN ('queued', 'running')
)
UPDATE network_captures c
SET status = 'failed',
    completed_at = NOW(),
    error_message = COALESCE(NULLIF(c.error_message, ''),
                             'Superseded by another capture on the same server.'),
    updated_at = NOW()
FROM ranked
WHERE c.id = ranked.id AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_network_captures_one_active
    ON network_captures (server_id)
    WHERE status IN ('queued', 'running');
