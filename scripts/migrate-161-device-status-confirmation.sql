-- Migration 161: confirmed device status, hysteresis and flap detection.
--
-- devices.status stays the HARD (confirmed) status. A single slow or lossy
-- ping no longer flips it: the central poller and the remote-sensor ingest run
-- the same state machine (docs/device-status-policy.md) and persist its
-- counters here, so a restart or an upgrade neither resets confirmation nor
-- re-notifies. Flapping is a flag beside the status, never a status value.
--
--  * devices                 is_flapping / flapping_since / flap_kind mirror
--                            the state row so the device list filters without
--                            a join; status_changed_at is the time of the last
--                            hard change (NULL until the first one after this
--                            upgrade: no backfill, ClickHouse is not reachable
--                            from here).
--  * device_status_state     one row per polled device, owned by the writer
--                            that polls it ('central' or the sensor uuid). A
--                            missing row, or a row of another owner, is seeded
--                            silently from devices.status by that writer, so
--                            this migration inserts nothing.
--  * device_flap_episodes    flapping history plus the durable start/stop
--                            notice state the server's notice loop delivers.
--  * alert_rules             notify_while_flapping: per-rule escape from the
--                            flap hold (notify at once, e.g. core routers).
--
-- device_status_log (ClickHouse) is unchanged and keeps hard transitions only.
-- Additive and idempotent; one transaction, so the grants can never be left
-- behind by a partial run that the runner would later baseline.

BEGIN;

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS is_flapping BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS flapping_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS flap_kind VARCHAR(16),
    ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ;

-- Flapping devices are few; the summary count and the list filter read this.
CREATE INDEX IF NOT EXISTS idx_devices_is_flapping
    ON devices(is_flapping)
    WHERE is_flapping;

-- Updated on most poll cycles: leave page space so updates stay HOT.
CREATE TABLE IF NOT EXISTS device_status_state (
    device_id UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    hard_status VARCHAR(20) NOT NULL DEFAULT 'unknown'
        CONSTRAINT device_status_state_hard_status_check
        CHECK (hard_status IN ('up', 'degraded', 'down', 'unknown')),
    in_maintenance BOOLEAN NOT NULL DEFAULT false,
    status_since TIMESTAMPTZ,
    fail_streak SMALLINT NOT NULL DEFAULT 0,
    ok_streak SMALLINT NOT NULL DEFAULT 0,
    clear_streak SMALLINT NOT NULL DEFAULT 0,
    window_bits INTEGER NOT NULL DEFAULT 0,
    window_len SMALLINT NOT NULL DEFAULT 0,
    reach_changes TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    perf_changes TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    reach_flapping BOOLEAN NOT NULL DEFAULT false,
    perf_flapping BOOLEAN NOT NULL DEFAULT false,
    flapping_since TIMESTAMPTZ,
    last_sample_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) WITH (fillfactor = 80);

COMMENT ON TABLE device_status_state IS
    'Persisted device status state machine (docs/device-status-policy.md). owner is ''central'' or the owning sensor uuid; a row of another owner is reseeded, never trusted.';
COMMENT ON COLUMN device_status_state.hard_status IS
    'Confirmed status; maintenance is the in_maintenance overlay, never a hard status.';

CREATE TABLE IF NOT EXISTS device_flap_episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    kind VARCHAR(16) NOT NULL,
    start_changes INTEGER,
    window_minutes INTEGER,
    status_at_start VARCHAR(20),
    status_at_end VARCHAR(20),
    end_reason VARCHAR(20),
    start_notice VARCHAR(12) NOT NULL DEFAULT 'pending',
    stop_notice VARCHAR(12) NOT NULL DEFAULT 'pending',
    notice_channels UUID[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE device_flap_episodes IS
    'Device flapping episodes; at most one open (ended_at IS NULL) per device. Start/stop notices are delivered from here by the server.';
COMMENT ON COLUMN device_flap_episodes.kind IS
    'reachability | performance | both: the union over the episode.';
COMMENT ON COLUMN device_flap_episodes.end_reason IS
    'stable | disabled | maintenance | owner_change | reseed | not_polled';
COMMENT ON COLUMN device_flap_episodes.start_notice IS
    'pending | sent | skipped';
COMMENT ON COLUMN device_flap_episodes.stop_notice IS
    'pending | sent | skipped; only sent to notice_channels, the channels the start notice reached.';

CREATE INDEX IF NOT EXISTS idx_device_flap_episodes_device_started
    ON device_flap_episodes(device_id, started_at DESC);
-- Writers open an episode with INSERT ... ON CONFLICT (device_id)
-- WHERE ended_at IS NULL DO NOTHING, so a lost or repeated write converges.
CREATE UNIQUE INDEX IF NOT EXISTS uq_device_flap_episodes_open
    ON device_flap_episodes(device_id)
    WHERE ended_at IS NULL;
-- Notice loop work queue, in started_at order. Broad enough that a query for
-- pending start notices, pending stop notices of ended episodes, or both is
-- covered by the predicate.
CREATE INDEX IF NOT EXISTS idx_device_flap_episodes_pending_notice
    ON device_flap_episodes(started_at)
    WHERE start_notice = 'pending' OR stop_notice = 'pending';

ALTER TABLE alert_rules
    ADD COLUMN IF NOT EXISTS notify_while_flapping BOOLEAN NOT NULL DEFAULT false;

-- UUID primary keys: no sequences to grant.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_status_state TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_flap_episodes TO zenplus;
    END IF;
END $$;

COMMIT;
