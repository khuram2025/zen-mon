-- Migration 131: NCCM network-configuration compliance.
--
-- Versioned policy packs of rules are evaluated against the latest validated
-- captured configuration of each enrolled device. Results keep the latest
-- outcome per device/rule with redacted evidence; daily snapshots give trends.
-- Exceptions have an owner, reason and expiry. Built-in starter packs are
-- upserted by the application (no seed rows here). Additive and idempotent.

CREATE TABLE IF NOT EXISTS ncm_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    builtin_key TEXT,
    name TEXT NOT NULL,
    description TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    platforms TEXT[],
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_policies_builtin ON ncm_policies(builtin_key) WHERE builtin_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS ncm_policy_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id UUID NOT NULL REFERENCES ncm_policies(id) ON DELETE CASCADE,
    rule_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    check_type TEXT NOT NULL CHECK (check_type IN ('must_match', 'must_not_match',
        'section_must_match', 'section_must_not_match', 'section_any_must_match')),
    pattern TEXT NOT NULL,
    section_pattern TEXT,
    platforms TEXT[],
    remediation TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ncm_policy_rules_key ON ncm_policy_rules(policy_id, rule_key);

CREATE TABLE IF NOT EXISTS ncm_compliance_results (
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    rule_id UUID NOT NULL REFERENCES ncm_policy_rules(id) ON DELETE CASCADE,
    policy_id UUID NOT NULL REFERENCES ncm_policies(id) ON DELETE CASCADE,
    version_id UUID REFERENCES device_configs(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'unknown', 'excepted')),
    evidence JSONB NOT NULL DEFAULT '[]',
    detail TEXT,
    exception_id UUID,
    assessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, rule_id)
);
CREATE INDEX IF NOT EXISTS ncm_compliance_results_policy ON ncm_compliance_results(policy_id, status);

CREATE TABLE IF NOT EXISTS ncm_compliance_exceptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID NOT NULL REFERENCES ncm_policy_rules(id) ON DELETE CASCADE,
    device_id UUID REFERENCES devices(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    reason TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS ncm_compliance_exceptions_rule ON ncm_compliance_exceptions(rule_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS ncm_compliance_snapshots (
    day DATE NOT NULL,
    policy_id UUID NOT NULL REFERENCES ncm_policies(id) ON DELETE CASCADE,
    devices INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    unknown INTEGER NOT NULL DEFAULT 0,
    excepted INTEGER NOT NULL DEFAULT 0,
    compliant_devices INTEGER NOT NULL DEFAULT 0,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, policy_id)
);
