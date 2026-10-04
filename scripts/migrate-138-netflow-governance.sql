-- Migration 138: NetFlow governance (NetFlow development review, CR-04..CR-13).
--
-- * netflow_saved_views: private/shared views owned by a user, typed by page,
--   with a query-schema version so v1 forensics blobs are upgraded on read.
-- * netflow_ip_groups: managed IP groups (replaces the appliance-local
--   netflow-ip-groups.json, which the application imports once).
-- * netflow_app_rules: operator application rules for the classifier
--   (port rules, protocol rules, address rules).
-- * netflow_exporter_settings / netflow_interface_overrides: exporter role and
--   counting direction (edge counting), expected export interval, and
--   per-interface names, speeds and roles for capacity.
-- * netflow_findings / netflow_detector_exceptions: persistent detection
--   findings with a lifecycle, and approved exceptions.
-- * netflow_ti_feeds / netflow_ti_entries: threat-intelligence prefix lists.
-- * netflow_export_jobs: complete raw-flow exports produced in the background.
-- * Permissions netflow.manage and netflow.export for the operator and editor
--   roles (admins hold them through system.admin).
--
-- Idempotent: every statement is guarded, so a replay is a no-op.

BEGIN;

-- ── Saved views ────────────────────────────────────────────────────────────
-- Private/shared is the is_shared column shipped by migrate-150, which runs
-- before this file (migrations.lock order); existing views stay shared.
ALTER TABLE netflow_saved_views ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'forensics'
    CHECK (kind IN ('forensics', 'overview', 'section'));
ALTER TABLE netflow_saved_views ADD COLUMN IF NOT EXISTS query_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE netflow_saved_views ADD COLUMN IF NOT EXISTS updated_by UUID NULL
    REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_netflow_saved_views_owner ON netflow_saved_views (owner_id);

-- ── IP groups ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_ip_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(120) NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    description TEXT,
    cidrs       TEXT[] NOT NULL,
    color       VARCHAR(16),
    tags        JSONB NOT NULL DEFAULT '[]',
    parent_id   UUID NULL REFERENCES netflow_ip_groups(id) ON DELETE SET NULL
                CHECK (parent_id IS NULL OR parent_id <> id),
    created_by  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_netflow_ip_groups_parent ON netflow_ip_groups (parent_id);

-- ── Application rules ──────────────────────────────────────────────────────
-- A port rule has port_lo (and optionally port_hi, protocol, cidrs = the
-- service side's address); a protocol/address rule has no ports and a
-- protocol and/or cidrs (either endpoint). Lower priority wins; built-in
-- rules sit at 1000.
CREATE TABLE IF NOT EXISTS netflow_app_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application VARCHAR(80) NOT NULL CHECK (btrim(application) <> ''),
    service     VARCHAR(80),
    category    VARCHAR(64),
    protocol    SMALLINT NULL CHECK (protocol BETWEEN 0 AND 255),
    port_lo     INT NULL CHECK (port_lo BETWEEN 1 AND 65535),
    port_hi     INT NULL CHECK (port_hi BETWEEN 1 AND 65535),
    cidrs       TEXT[] NULL,
    priority    INT NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 100000),
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (port_hi IS NULL OR (port_lo IS NOT NULL AND port_hi >= port_lo)),
    CHECK (port_lo IS NOT NULL OR protocol IS NOT NULL OR COALESCE(cardinality(cidrs), 0) > 0)
);

CREATE INDEX IF NOT EXISTS idx_netflow_app_rules_enabled
    ON netflow_app_rules (priority) WHERE enabled;

-- ── Exporter settings ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_exporter_settings (
    exporter_ip         INET PRIMARY KEY,
    display_name        VARCHAR(120),
    role                VARCHAR(16) NOT NULL DEFAULT 'edge'
                        CHECK (role IN ('edge', 'core', 'internal', 'excluded')),
    count_direction     VARCHAR(16) NOT NULL DEFAULT 'auto'
                        CHECK (count_direction IN ('auto', 'ingress', 'egress', 'both')),
    expected_interval_s INT NULL CHECK (expected_interval_s IS NULL OR expected_interval_s BETWEEN 1 AND 86400),
    note                TEXT,
    created_by          UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Interface overrides ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_interface_overrides (
    exporter_ip   INET NOT NULL,
    ifindex       BIGINT NOT NULL CHECK (ifindex BETWEEN 0 AND 4294967295),
    display_name  VARCHAR(120),
    speed_in_bps  BIGINT NULL CHECK (speed_in_bps IS NULL OR speed_in_bps > 0),
    speed_out_bps BIGINT NULL CHECK (speed_out_bps IS NULL OR speed_out_bps > 0),
    role          VARCHAR(16) NULL CHECK (role IN ('wan', 'lan', 'uplink', 'internal', 'ignore')),
    note          TEXT,
    created_by    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exporter_ip, ifindex)
);

-- ── Detection findings ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_findings (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint      VARCHAR(80) NOT NULL UNIQUE,
    kind             VARCHAR(48),
    severity         VARCHAR(16),
    confidence       VARCHAR(8),
    status           VARCHAR(16) NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'acknowledged', 'suppressed', 'resolved')),
    title            TEXT,
    description      TEXT,
    src_ip           INET NULL,
    dst_ip           INET NULL,
    exporter_ip      INET NULL,
    first_seen       TIMESTAMPTZ NOT NULL,
    last_seen        TIMESTAMPTZ NOT NULL,
    occurrences      INT NOT NULL DEFAULT 1,
    evidence         JSONB NOT NULL DEFAULT '{}',
    metrics          JSONB NOT NULL DEFAULT '{}',
    alert_id         UUID NULL,
    assigned_to      UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_by  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at  TIMESTAMPTZ NULL,
    suppressed_until TIMESTAMPTZ NULL,
    suppressed_by    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    suppress_reason  TEXT,
    resolved_at      TIMESTAMPTZ NULL,
    notes            JSONB NOT NULL DEFAULT '[]',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_netflow_findings_status_seen
    ON netflow_findings (status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_netflow_findings_kind ON netflow_findings (kind);
CREATE INDEX IF NOT EXISTS idx_netflow_findings_src ON netflow_findings (src_ip);
CREATE INDEX IF NOT EXISTS idx_netflow_findings_exporter ON netflow_findings (exporter_ip);

CREATE TABLE IF NOT EXISTS netflow_detector_exceptions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       VARCHAR(48) NULL,
    src_cidr   CIDR NULL,
    dst_cidr   CIDR NULL,
    dst_port   INT NULL CHECK (dst_port IS NULL OR dst_port BETWEEN 0 AND 65535),
    reason     TEXT NOT NULL CHECK (btrim(reason) <> ''),
    expires_at TIMESTAMPTZ NULL,
    created_by UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_netflow_detector_exceptions_kind
    ON netflow_detector_exceptions (kind);

-- ── Threat intelligence ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_ti_feeds (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              VARCHAR(120) NOT NULL UNIQUE,
    source            VARCHAR(16) NOT NULL CHECK (source IN ('url', 'upload', 'builtin')),
    url               TEXT NULL,
    category          VARCHAR(48) NOT NULL DEFAULT 'malicious',
    enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    refresh_hours     INT NOT NULL DEFAULT 24 CHECK (refresh_hours BETWEEN 1 AND 8760),
    last_refreshed_at TIMESTAMPTZ NULL,
    last_status       TEXT,
    entry_count       INT NOT NULL DEFAULT 0,
    created_by        UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by        UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (source <> 'url' OR url IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS netflow_ti_entries (
    feed_id UUID NOT NULL REFERENCES netflow_ti_feeds(id) ON DELETE CASCADE,
    prefix  CIDR NOT NULL,
    PRIMARY KEY (feed_id, prefix)
);

-- Reputation lookups go prefix -> feed; GiST answers "which prefixes contain
-- this address" with the inet containment operators.
CREATE INDEX IF NOT EXISTS idx_netflow_ti_entries_prefix
    ON netflow_ti_entries USING gist (prefix inet_ops);

-- ── Raw-flow export jobs ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS netflow_export_jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_by UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    status       VARCHAR(16) NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'expired')),
    query        JSONB NOT NULL,
    row_count    BIGINT,
    byte_size    BIGINT,
    file_path    TEXT,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ NULL,
    finished_at  TIMESTAMPTZ NULL,
    expires_at   TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_netflow_export_jobs_status
    ON netflow_export_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_netflow_export_jobs_requester
    ON netflow_export_jobs (requested_by, created_at DESC);

-- ── Permissions ────────────────────────────────────────────────────────────
-- Appended to the built-in roles that manage monitoring. Custom roles are
-- left to their administrators; admins hold both through system.admin.
UPDATE roles
   SET permissions = permissions || '["netflow.manage"]'::jsonb, updated_at = NOW()
 WHERE name IN ('operator', 'editor')
   AND NOT permissions ? 'netflow.manage';

UPDATE roles
   SET permissions = permissions || '["netflow.export"]'::jsonb, updated_at = NOW()
 WHERE name IN ('operator', 'editor')
   AND NOT permissions ? 'netflow.export';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_ip_groups TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_app_rules TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_exporter_settings TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_interface_overrides TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_findings TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_detector_exceptions TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_ti_feeds TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_ti_entries TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_export_jobs TO zenplus;
        GRANT SELECT, INSERT, UPDATE, DELETE ON netflow_saved_views TO zenplus;
    END IF;
END $$;

COMMENT ON TABLE netflow_ip_groups IS
    'Managed NetFlow IP groups (name -> CIDRs); imported once from netflow-ip-groups.json.';
COMMENT ON TABLE netflow_app_rules IS
    'Operator application rules for the NetFlow classifier (port, protocol and address rules).';
COMMENT ON TABLE netflow_exporter_settings IS
    'Per-exporter role, counting direction and expected export interval.';
COMMENT ON TABLE netflow_interface_overrides IS
    'Per-exporter-interface display name, capacity speeds and role for NetFlow capacity.';
COMMENT ON TABLE netflow_findings IS
    'Persistent NetFlow detection findings with acknowledgement/suppression lifecycle.';
COMMENT ON TABLE netflow_export_jobs IS
    'Background jobs producing complete raw-flow exports (netflow.export).';

COMMIT;
