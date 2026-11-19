-- migrate-193-alert-rule-packs-overrides.sql
-- Device Templates phase 3: thresholds per class
-- (docs/plans/device-templates-plan.md, docs/alert-rule-overrides.md).
--
-- 1. Templates carry recommended alert rules (rule_pack) and interface roles
--    (interface_roles), both part of the versioned template document. NULL on
--    a customised template means "as the parent". Recommended rules are never
--    created or enabled automatically: an operator accepts them one by one.
-- 2. alert_rules gain:
--      profile_id     scope: devices on this template or on a template
--                     customised from it (RESTRICT: a template with rules
--                     cannot be deleted from under them)
--      pack_key       which recommendation of the template this rule came from
--      pack_accepted  the recommendation as it was when accepted (or when
--                     "keep" was chosen), so a later built-in revision with
--                     another value is shown as "recommendation changed"
--                     instead of changing the customer's rule
--      if_role        interface rules: only interfaces with this role
-- 3. device_interfaces.role: an operator's role for one interface; NULL means
--    the role comes from the device's template.
-- 4. alert_rule_overrides: per group, device or server changes to a rule's
--    threshold, clear level, holds or priority, ordered, with a reason, an
--    author and an optional expiry. Turning a rule off for an asset stays
--    alert_rule_exclusions.
--
-- Idempotent. Built-ins are updated by builtin_key (unique), only where the
-- new columns are still NULL, so a database with two built-in rows of one
-- name (see migrate-189) or customised templates is unaffected.

-- ── 1. Template rule packs and interface roles ────────────────────────────
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS rule_pack JSONB;
ALTER TABLE device_profiles ADD COLUMN IF NOT EXISTS interface_roles JSONB;

COMMENT ON COLUMN device_profiles.rule_pack IS
  'Recommended alert rules beyond those derived from metric thresholds: [{key,name,metric,operator,threshold,severity,min_duration,...}]. NULL on a customised template = the parent''s.';
COMMENT ON COLUMN device_profiles.interface_roles IS
  'Ordered interface role rules [{role, field (if_name|if_alias|if_descr|if_type), pattern}]; the first match gives an interface its role. NULL on a customised template = the parent''s.';

-- ── 2. Alert rule scope and pack origin ────────────────────────────────────
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS profile_id UUID
    REFERENCES device_profiles(id) ON DELETE RESTRICT;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS pack_key VARCHAR(120);
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS pack_accepted JSONB;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS if_role VARCHAR(20);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_if_role_values') THEN
        ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_if_role_values
            CHECK (if_role IS NULL OR if_role IN
                   ('uplink', 'access', 'wan', 'server', 'tunnel', 'vlan', 'loopback', 'management'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'alert_rules_pack_needs_template') THEN
        ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_pack_needs_template
            CHECK (pack_key IS NULL OR profile_id IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS alert_rules_profile ON alert_rules (profile_id) WHERE profile_id IS NOT NULL;
-- One accepted rule per recommendation and template.
CREATE UNIQUE INDEX IF NOT EXISTS alert_rules_pack_unique
    ON alert_rules (profile_id, pack_key) WHERE pack_key IS NOT NULL;

COMMENT ON COLUMN alert_rules.profile_id IS
  'Scope: devices whose template is this one or customised from it (with the other scope filters).';
COMMENT ON COLUMN alert_rules.pack_accepted IS
  'The template recommendation (threshold, reset, holds, severity, priority) when accepted or last kept; compared with the current recommendation.';

-- ── 3. Manual interface role ───────────────────────────────────────────────
ALTER TABLE device_interfaces ADD COLUMN IF NOT EXISTS role VARCHAR(20);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_interfaces_role_values') THEN
        ALTER TABLE device_interfaces ADD CONSTRAINT device_interfaces_role_values
            CHECK (role IS NULL OR role IN
                   ('uplink', 'access', 'wan', 'server', 'tunnel', 'vlan', 'loopback', 'management'));
    END IF;
END $$;
COMMENT ON COLUMN device_interfaces.role IS
  'Role set by an operator; NULL = the role from the device template''s interface_roles. Never changes "monitored".';

-- ── 4. Overrides ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alert_rule_overrides (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id          UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL DEFAULT 100,
    device_id        UUID REFERENCES devices(id) ON DELETE CASCADE,
    server_id        UUID REFERENCES servers(id) ON DELETE CASCADE,
    group_id         UUID REFERENCES device_groups(id) ON DELETE CASCADE,
    instance_pattern VARCHAR(200),
    metric           VARCHAR(80),
    threshold        DOUBLE PRECISION,
    reset_threshold  DOUBLE PRECISION,
    min_duration     INTEGER,
    clear_duration   INTEGER,
    priority         VARCHAR(2),
    reason           TEXT NOT NULL,
    expires_at       TIMESTAMPTZ,
    created_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    created_by_name  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT alert_rule_overrides_one_target CHECK (
        (device_id IS NOT NULL)::int + (server_id IS NOT NULL)::int + (group_id IS NOT NULL)::int = 1),
    CONSTRAINT alert_rule_overrides_changes_something CHECK (
        threshold IS NOT NULL OR reset_threshold IS NOT NULL OR min_duration IS NOT NULL
        OR clear_duration IS NOT NULL OR priority IS NOT NULL),
    CONSTRAINT alert_rule_overrides_priority CHECK (priority IS NULL OR priority ~ '^P[1-4]$'),
    CONSTRAINT alert_rule_overrides_durations CHECK (
        (min_duration IS NULL OR min_duration BETWEEN 0 AND 86400)
        AND (clear_duration IS NULL OR clear_duration BETWEEN 0 AND 86400)),
    CONSTRAINT alert_rule_overrides_reason_len CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    CONSTRAINT alert_rule_overrides_pattern_len CHECK (instance_pattern IS NULL OR length(instance_pattern) <= 200)
);
CREATE INDEX IF NOT EXISTS alert_rule_overrides_rule ON alert_rule_overrides (rule_id, position);
CREATE INDEX IF NOT EXISTS alert_rule_overrides_device ON alert_rule_overrides (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS alert_rule_overrides_server ON alert_rule_overrides (server_id) WHERE server_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS alert_rule_overrides_group ON alert_rule_overrides (group_id) WHERE group_id IS NOT NULL;

COMMENT ON TABLE alert_rule_overrides IS
  'Per-asset changes to a rule (docs/alert-rule-overrides.md). Device/server rows are tried before group rows, then by position; the first matching, unexpired row applies.';

-- ── 5. Built-in interface roles and recommended interface rules ────────────
-- Only what an interface says about itself: its IF-MIB type, a description an
-- operator wrote ("uplink", "WAN") and FortiOS's default WAN port names. Ports
-- are never guessed to be uplinks from their position or speed.
UPDATE device_profiles p
   SET interface_roles = r.roles
  FROM (VALUES
        ('cisco_ios'), ('cisco_asa'), ('paloalto_panos'), ('f5_bigip'), ('juniper_junos'),
        ('mikrotik_routeros'), ('aruba_wlc'), ('fortinet_fortigate'), ('aruba_aoscx'), ('dell_os10')
       ) AS k(key)
 CROSS JOIN LATERAL (SELECT
        jsonb_build_array(
            jsonb_build_object('role', 'tunnel',     'field', 'if_type',  'pattern', '131'),
            jsonb_build_object('role', 'loopback',   'field', 'if_type',  'pattern', '24'),
            jsonb_build_object('role', 'vlan',       'field', 'if_type',  'pattern', '135,136'),
            jsonb_build_object('role', 'management', 'field', 'if_name',  'pattern', '(?i)^(mgmt\d*|management\d*|me0|fxp0|ma1)$'),
            jsonb_build_object('role', 'uplink',     'field', 'if_alias', 'pattern', '(?i)\b(uplink|upstream)\b'),
            jsonb_build_object('role', 'wan',        'field', 'if_alias', 'pattern', '(?i)\b(wan|isp|internet)\b'))
        || CASE WHEN k.key = 'fortinet_fortigate'
                THEN jsonb_build_array(jsonb_build_object('role', 'wan', 'field', 'if_name', 'pattern', '^wan\d*$'))
                ELSE '[]'::jsonb END
        || CASE WHEN k.key IN ('aruba_aoscx', 'dell_os10')
                THEN jsonb_build_array(jsonb_build_object('role', 'access', 'field', 'if_type', 'pattern', '6'))
                ELSE '[]'::jsonb END AS roles) r
 WHERE p.builtin_key = k.key AND p.interface_roles IS NULL;

UPDATE device_profiles p
   SET rule_pack = jsonb_build_array(
        jsonb_build_object('key', 'uplink_down', 'name', 'Uplink down',
            'description', 'An interface with the uplink role is operationally down.',
            'metric', 'if_oper_status', 'operator', '==', 'threshold', 2,
            'if_role', 'uplink', 'severity', 'critical', 'min_duration', 60),
        jsonb_build_object('key', 'wan_down', 'name', 'WAN link down',
            'description', 'An interface with the WAN role is operationally down.',
            'metric', 'if_oper_status', 'operator', '==', 'threshold', 2,
            'if_role', 'wan', 'severity', 'critical', 'min_duration', 60),
        jsonb_build_object('key', 'uplink_util', 'name', 'Uplink utilisation high',
            'description', 'An uplink carries more than 80 % of its speed for 10 minutes.',
            'metric', 'if_util_pct', 'operator', '>', 'threshold', 80,
            'if_role', 'uplink', 'severity', 'warning', 'min_duration', 600))
 WHERE p.builtin_key IN ('cisco_ios', 'cisco_asa', 'paloalto_panos', 'f5_bigip', 'juniper_junos',
                         'mikrotik_routeros', 'aruba_wlc', 'fortinet_fortigate', 'aruba_aoscx', 'dell_os10')
   AND p.rule_pack IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON alert_rule_overrides TO zenplus;
    END IF;
END $$;
