-- migrate-075-netpath.sql
-- NetPath: hop-by-hop WAN/Internet path monitoring (a SolarWinds NetPath
-- competitor). The appliance acts as the probe vantage; a Paris-style
-- multi-flow traceroute discovers every ECMP path to a target service, with
-- per-hop RTT/loss, ASN/ISP enrichment, internal-vs-external classification,
-- managed-device correlation, path-change detection and time-travel history.
--
-- Tables:
--   netpath_probes     probe definitions + denormalized latest state
--   netpath_paths      distinct discovered path topologies per probe (dedup by hash)
--   netpath_snapshots  one row per trace run — full per-hop + per-flow detail
--   netpath_hop_meta   per-IP enrichment cache (rDNS, ASN, geo, device match)
--   netpath_events     activity feed / alert source (path change, unreachable...)
--
-- All new tables; no ALTER of pre-existing objects. The alert_rules metric
-- CHECK is widened separately in migrate-076 (that table is owned by the
-- postgres superuser, this file is safe to apply as the app role).
--
-- Idempotent and safe to re-run.

BEGIN;

-- ------------------------------------------------------------------ probes
CREATE TABLE IF NOT EXISTS netpath_probes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           VARCHAR(120) NOT NULL,
    target_host    VARCHAR(255) NOT NULL,          -- user-entered hostname or IP
    target_ip      INET,                           -- resolved by the poller
    target_port    INT CHECK (target_port BETWEEN 1 AND 65535),
    protocol       VARCHAR(8) NOT NULL DEFAULT 'icmp' CHECK (protocol IN ('icmp','tcp','udp')),
    max_hops       INT NOT NULL DEFAULT 30 CHECK (max_hops BETWEEN 1 AND 64),
    probes_per_hop INT NOT NULL DEFAULT 3  CHECK (probes_per_hop BETWEEN 1 AND 10),
    flows          INT NOT NULL DEFAULT 4  CHECK (flows BETWEEN 1 AND 16),
    interval_s     INT NOT NULL DEFAULT 300 CHECK (interval_s >= 30),
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    run_now        BOOLEAN NOT NULL DEFAULT FALSE, -- API sets, poller clears (on-demand run)
    internal_cidrs TEXT[] NOT NULL DEFAULT '{}',   -- extra "internal" ranges beyond RFC1918
    -- status thresholds (colour the probe independently of alert rules)
    rtt_warn_ms    DOUBLE PRECISION NOT NULL DEFAULT 150,
    rtt_crit_ms    DOUBLE PRECISION NOT NULL DEFAULT 400,
    loss_warn_pct  DOUBLE PRECISION NOT NULL DEFAULT 2,
    loss_crit_pct  DOUBLE PRECISION NOT NULL DEFAULT 10,
    description    TEXT NOT NULL DEFAULT '',
    tags           TEXT[] NOT NULL DEFAULT '{}',
    -- denormalized latest state (poller updates each run)
    last_run_at    TIMESTAMPTZ,
    last_status    VARCHAR(12),                    -- ok|degraded|down|unreached|pending
    last_rtt_ms    DOUBLE PRECISION,
    last_loss_pct  DOUBLE PRECISION,
    last_hop_count INT,
    last_num_paths INT,
    last_path_hash BIGINT,
    last_reached   BOOLEAN,
    last_error     TEXT,
    created_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_netpath_probes_enabled ON netpath_probes (enabled) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_netpath_probes_status  ON netpath_probes (last_status);

-- ------------------------------------------------------------------ paths
-- One row per distinct topology a probe has ever traversed. The poller dedups
-- by path_hash so time-travel and path-change detection reference a stable
-- identity; hop_ips is the ordered primary-flow signature for quick diff.
CREATE TABLE IF NOT EXISTS netpath_paths (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    probe_id    UUID NOT NULL REFERENCES netpath_probes(id) ON DELETE CASCADE,
    path_hash   BIGINT NOT NULL,
    hop_count   INT NOT NULL,
    hop_ips     TEXT[] NOT NULL DEFAULT '{}',
    label       VARCHAR(120),                      -- optional operator label
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count  BIGINT NOT NULL DEFAULT 1,
    UNIQUE (probe_id, path_hash)
);
CREATE INDEX IF NOT EXISTS idx_netpath_paths_probe ON netpath_paths (probe_id, last_seen DESC);

-- ------------------------------------------------------------------ snapshots
-- The time series of path health: one row per trace run. hops holds the
-- per-TTL responder set with metrics; flows holds the per-flow ordered paths
-- used to draw ECMP edges. Both are enriched at read time against
-- netpath_hop_meta so we never rewrite the JSONB.
CREATE TABLE IF NOT EXISTS netpath_snapshots (
    id                 BIGSERIAL PRIMARY KEY,
    probe_id           UUID NOT NULL REFERENCES netpath_probes(id) ON DELETE CASCADE,
    path_id            UUID REFERENCES netpath_paths(id) ON DELETE SET NULL,
    run_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vantage            VARCHAR(64) NOT NULL DEFAULT 'appliance',
    protocol           VARCHAR(8) NOT NULL,
    reached            BOOLEAN NOT NULL DEFAULT FALSE,
    path_changed       BOOLEAN NOT NULL DEFAULT FALSE,
    hop_count          INT NOT NULL DEFAULT 0,
    num_paths          INT NOT NULL DEFAULT 1,
    rtt_ms             DOUBLE PRECISION,           -- end-to-end (to destination / last responder)
    loss_pct           DOUBLE PRECISION,           -- end-to-end
    worst_hop_loss_pct DOUBLE PRECISION,
    jitter_ms          DOUBLE PRECISION,
    duration_ms        INT,
    path_hash          BIGINT,
    status             VARCHAR(12) NOT NULL DEFAULT 'ok',
    error              TEXT,
    hops               JSONB NOT NULL DEFAULT '[]',
    flows              JSONB NOT NULL DEFAULT '[]',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_netpath_snapshots_probe    ON netpath_snapshots (probe_id, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_netpath_snapshots_changed  ON netpath_snapshots (probe_id, run_at DESC) WHERE path_changed;

-- ------------------------------------------------------------------ hop meta
-- Per-IP enrichment cache, maintained by the API enrichment sweeper. Keyed by
-- IP so rDNS/ASN lookups happen once per address, not once per run.
CREATE TABLE IF NOT EXISTS netpath_hop_meta (
    ip           INET PRIMARY KEY,
    hostname     VARCHAR(255),
    asn          BIGINT,
    as_name      VARCHAR(255),
    country      VARCHAR(2),
    device_id    UUID REFERENCES devices(id) ON DELETE SET NULL,
    is_internal  BOOLEAN NOT NULL DEFAULT FALSE,
    enriched_at  TIMESTAMPTZ,                       -- NULL = never enriched (pending)
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_netpath_hop_meta_pending ON netpath_hop_meta (enriched_at) WHERE enriched_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_netpath_hop_meta_device  ON netpath_hop_meta (device_id) WHERE device_id IS NOT NULL;

-- ------------------------------------------------------------------ events
CREATE TABLE IF NOT EXISTS netpath_events (
    id          BIGSERIAL PRIMARY KEY,
    probe_id    UUID NOT NULL REFERENCES netpath_probes(id) ON DELETE CASCADE,
    event_type  VARCHAR(24) NOT NULL,              -- path_change|unreachable|reachable|latency|loss
    snapshot_id BIGINT,
    severity    VARCHAR(12) NOT NULL DEFAULT 'info',
    details     JSONB NOT NULL DEFAULT '{}',
    alerted     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_netpath_events_unalerted ON netpath_events (id) WHERE NOT alerted;
CREATE INDEX IF NOT EXISTS idx_netpath_events_probe     ON netpath_events (probe_id, created_at DESC);

-- The app role owns these tables when this file is applied as `zenplus`; the
-- GRANT is a no-op there and covers the case where the postgres superuser
-- applies it on a fresh install (pg_default_acl already grants DML, but be
-- explicit and harmless).
DO $$
BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON
        netpath_probes, netpath_paths, netpath_snapshots, netpath_hop_meta, netpath_events
        TO zenplus;
    GRANT USAGE, SELECT ON SEQUENCE
        netpath_snapshots_id_seq, netpath_events_id_seq TO zenplus;
EXCEPTION WHEN OTHERS THEN
    NULL;  -- role may not exist / already owner
END $$;

COMMIT;
