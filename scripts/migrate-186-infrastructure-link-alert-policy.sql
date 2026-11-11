-- Keep positive evidence by stable interface name so LLDP/FDB loss on failure
-- or a reporter-local ifIndex change cannot hide an infrastructure outage.
CREATE TABLE IF NOT EXISTS interface_alert_infrastructure (
    device_id uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    if_name varchar(255) NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, if_name)
);

-- Only replace the untouched, globally seeded policy. Preserve custom rules.
UPDATE alert_rules
SET target = 'role:infrastructure', min_duration = 180, duration = 180,
    description = 'Infrastructure link down for 3 minutes. Excludes known access ports; includes uplinks, unknown roles and interfaces tagged link-critical.',
    updated_at = now()
WHERE name = 'Interface down' AND metric = 'if_oper_status'
  AND operator = 'eq' AND threshold = 2 AND severity = 'critical'
  AND target IS NULL AND COALESCE(min_duration, 0) = 0
  AND conditions IS NULL AND device_id IS NULL AND group_id IS NULL
  AND server_id IS NULL AND device_type IS NULL AND location IS NULL AND scope_tag IS NULL
  AND created_by IS NULL AND updated_at = created_at;
