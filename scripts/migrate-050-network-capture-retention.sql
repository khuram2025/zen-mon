-- Configurable retention and operator archives for on-demand captures.
--
-- New captures default to one hour of retention after they become terminal.
-- Existing terminal rows receive a fresh one-hour window at migration time so
-- an upgrade never immediately destroys historical capture data. Archived
-- rows have no purge deadline and remain until explicitly unarchived/purged.

ALTER TABLE network_captures
    ADD COLUMN IF NOT EXISTS retention_s INTEGER NOT NULL DEFAULT 3600,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_by UUID,
    ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ;

ALTER TABLE network_captures
    DROP CONSTRAINT IF EXISTS network_captures_retention_s_check;
ALTER TABLE network_captures ADD CONSTRAINT network_captures_retention_s_check
    CHECK (retention_s BETWEEN 900 AND 604800);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'network_captures_archived_by_fkey'
          AND conrelid = 'network_captures'::regclass
    ) THEN
        ALTER TABLE network_captures
            ADD CONSTRAINT network_captures_archived_by_fkey
            FOREIGN KEY (archived_by) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION set_network_capture_retention()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.archived_at IS NOT NULL THEN
        NEW.purge_after := NULL;
    ELSIF NEW.status IN ('completed', 'failed', 'expired', 'cancelled') THEN
        -- Set the deadline once, when a run first becomes terminal. A repeated
        -- final upload must not keep extending retention indefinitely.
        IF TG_OP = 'INSERT' THEN
            NEW.purge_after := COALESCE(NEW.completed_at, NOW())
                               + make_interval(secs => NEW.retention_s);
        ELSIF OLD.status NOT IN ('completed', 'failed', 'expired', 'cancelled')
              OR NEW.purge_after IS NULL THEN
            NEW.purge_after := COALESCE(NEW.completed_at, NOW())
                               + make_interval(secs => NEW.retention_s);
        END IF;
    ELSE
        -- Retention starts after completion, never at request/start time.
        NEW.purge_after := NULL;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_network_capture_retention ON network_captures;
CREATE TRIGGER trg_network_capture_retention
    BEFORE INSERT OR UPDATE OF status, completed_at, archived_at
    ON network_captures
    FOR EACH ROW
    EXECUTE FUNCTION set_network_capture_retention();

-- Preserve pre-upgrade terminal data for a full default window rather than
-- making old rows immediately eligible when the retention worker starts.
UPDATE network_captures
SET purge_after = NOW() + make_interval(secs => retention_s)
WHERE status IN ('completed', 'failed', 'expired', 'cancelled')
  AND archived_at IS NULL
  AND purge_after IS NULL;

CREATE INDEX IF NOT EXISTS idx_network_captures_retention_due
    ON network_captures (purge_after)
    WHERE archived_at IS NULL
      AND status IN ('completed', 'failed', 'expired', 'cancelled')
      AND purge_after IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_network_captures_archived
    ON network_captures (archived_at DESC)
    WHERE archived_at IS NOT NULL;

COMMENT ON COLUMN network_captures.retention_s IS
    'Seconds to retain flow data after the capture becomes terminal (15m to 7d).';
COMMENT ON COLUMN network_captures.archived_at IS
    'Non-null suppresses automatic purge until explicit unarchive/purge.';
COMMENT ON COLUMN network_captures.purge_after IS
    'Automatic purge deadline; null while active or archived.';
