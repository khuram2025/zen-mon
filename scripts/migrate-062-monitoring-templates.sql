-- migrate-062: Monitoring Templates (vendor-specific SNMP insight packs)
--
-- Activates the long-reserved device_profiles.oid_groups column: a template
-- is a set of OID groups (scalar batches or SNMP tables) that the poller
-- collects for any device the template is attached to (devices.profile_id).
--
--  * device_template_values — latest-value snapshot written by the poller
--    (numeric AND textual values, plus per-row labels for tables). History
--    for numeric series continues to live in ClickHouse snmp_metrics under
--    series keys prefixed "tpl_".
--  * alert_rules_metric_check — extended so any 'tpl_*' metric key can be
--    alerted on without further migrations.
--  * Seeds five builtin templates (Fortinet FortiGate, Cisco IOS/IOS-XE,
--    Cisco ASA, Palo Alto PAN-OS, F5 BIG-IP) researched from vendor MIBs,
--    Zabbix/LibreNMS templates and vendor KBs (2026-08). Builtins are
--    read-only in the API (clone to customize) and are refreshed by
--    re-running this migration's upsert.

-- ---------------------------------------------------------------- values table

CREATE TABLE IF NOT EXISTS device_template_values (
    device_id  UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    group_key  VARCHAR(64)  NOT NULL,
    metric_key VARCHAR(64)  NOT NULL,
    instance   VARCHAR(128) NOT NULL DEFAULT '',
    series_key VARCHAR(160) NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    unit       VARCHAR(32)  NOT NULL DEFAULT '',
    value_num  DOUBLE PRECISION,
    value_text TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, group_key, metric_key, instance)
);

CREATE INDEX IF NOT EXISTS idx_dtv_device ON device_template_values(device_id);
CREATE INDEX IF NOT EXISTS idx_dtv_updated ON device_template_values(updated_at);

-- ---------------------------------------------------------------- alert metrics

ALTER TABLE alert_rules
  DROP CONSTRAINT IF EXISTS alert_rules_metric_check;

ALTER TABLE alert_rules
  ADD CONSTRAINT alert_rules_metric_check CHECK (
    metric IN (
      'ping_status','rtt','packet_loss','jitter','service_status',
      'cpu','memory','uptime_reset','temperature','fan_state','psu_state',
      'if_in_bps','if_out_bps','if_util_pct','if_errors','if_discards','if_oper_status',
      'session_count','vpn_tunnel_state','ha_state','bgp_neighbor_down',
      'trap',
      -- host (server agent) metrics
      'host_cpu_pct','host_memory_pct','host_filesystem_pct','host_disk_util_pct',
      'host_service_down','host_process_down',
      -- application monitoring (APM) metrics
      'apm_latency_p50','apm_latency_p95','apm_latency_p99',
      'apm_error_rate','apm_throughput','apm_apdex',
      'apm_slo_burn','apm_synthetic_down','apm_anomaly',
      -- user device tracker (UDT) metrics
      'udt_new_endpoint','udt_rogue_endpoint','udt_watch_endpoint','udt_endpoint_moved',
      'udt_port_capacity_pct'
    )
    -- monitoring-template metrics: any series key emitted by a template
    OR metric LIKE 'tpl\_%'
  );

-- ---------------------------------------------------------------- grants

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON device_template_values TO zenplus;
  END IF;
END $$;

-- ---------------------------------------------------------------- builtin seeds
--
-- oid_groups JSON shape (shared with server/app/schemas/snmp.py and the
-- poller's oidgroups.go):
--   [{ key, name, kind: scalar|table, description?,
--      table?: { label_oid },
--      metrics: [{ key, name, oid, type: gauge|counter|enum|string,
--                  unit?, scale?, value_map?,            -- poller-side
--                  labels?: {code:{text,sev}}, thresholds?: {warn,crit,op} }] }]
-- The poller emits ClickHouse series "tpl_<metric key>[_<row instance>]" and
-- keeps the latest values (incl. strings + row labels) in
-- device_template_values. "labels"/"thresholds" drive UI status colors and
-- are ignored by the poller.

-- =============================== Fortinet FortiGate ===========================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Fortinet FortiGate', 'Fortinet',
  $mr$ {
    "sys_object_id_prefixes": ["1.3.6.1.4.1.12356.101.1", "1.3.6.1.4.1.12356.15"],
    "default_vendor": "Fortinet",
    "extract_model": "(Forti[A-Za-z]+-[0-9A-Za-z]+)",
    "extract_os_version": "v([0-9][0-9.,a-z]*)"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "system", "name": "System Health", "kind": "scalar",
      "description": "FortiGate global CPU, memory, disk, session load and firmware/signature versions (FORTINET-FORTIGATE-MIB fgSystemInfo).",
      "metrics": [
        {"key":"fgt_cpu","name":"CPU Usage","oid":"1.3.6.1.4.1.12356.101.4.1.3.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_mem","name":"Memory Usage","oid":"1.3.6.1.4.1.12356.101.4.1.4.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":88}},
        {"key":"fgt_sessions","name":"Active Sessions","oid":"1.3.6.1.4.1.12356.101.4.1.8.0","type":"gauge","unit":"sessions"},
        {"key":"fgt_ses_rate","name":"Session Setup Rate (1m)","oid":"1.3.6.1.4.1.12356.101.4.1.11.0","type":"gauge","unit":"sessions/s"},
        {"key":"fgt_sessions6","name":"Active IPv6 Sessions","oid":"1.3.6.1.4.1.12356.101.4.1.15.0","type":"gauge","unit":"sessions"},
        {"key":"fgt_disk_used","name":"Disk Used","oid":"1.3.6.1.4.1.12356.101.4.1.6.0","type":"gauge","unit":"MB"},
        {"key":"fgt_disk_cap","name":"Disk Capacity","oid":"1.3.6.1.4.1.12356.101.4.1.7.0","type":"gauge","unit":"MB"},
        {"key":"fgt_fw_version","name":"Firmware","oid":"1.3.6.1.4.1.12356.101.4.1.1.0","type":"string"},
        {"key":"fgt_av_version","name":"AV Signatures","oid":"1.3.6.1.4.1.12356.101.4.2.1.0","type":"string"},
        {"key":"fgt_ips_version","name":"IPS Signatures","oid":"1.3.6.1.4.1.12356.101.4.2.2.0","type":"string"}
      ]
    },
    {
      "key": "cpu_cores", "name": "Processor Cores", "kind": "table",
      "description": "Per-core / per-NPU utilization (fgProcessorTable). Hotspot detection the global CPU average hides.",
      "table": {},
      "metrics": [
        {"key":"fgt_core_usage","name":"Core Usage (1m)","oid":"1.3.6.1.4.1.12356.101.4.4.2.1.2","type":"gauge","unit":"%","thresholds":{"warn":85,"crit":95}}
      ]
    },
    {
      "key": "vdoms", "name": "Virtual Domains", "kind": "table",
      "description": "Per-VDOM CPU, memory, sessions and HA role (fgVdTable).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.3.2.1.1.2"},
      "metrics": [
        {"key":"fgt_vd_cpu","name":"CPU","oid":"1.3.6.1.4.1.12356.101.3.2.1.1.5","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_vd_mem","name":"Memory","oid":"1.3.6.1.4.1.12356.101.3.2.1.1.6","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":88}},
        {"key":"fgt_vd_sessions","name":"Sessions","oid":"1.3.6.1.4.1.12356.101.3.2.1.1.7","type":"gauge","unit":"sessions"},
        {"key":"fgt_vd_ha_state","name":"HA Role","oid":"1.3.6.1.4.1.12356.101.3.2.1.1.4","type":"enum",
         "labels":{"1":{"text":"Primary","sev":"ok"},"2":{"text":"Secondary","sev":"ok"},"3":{"text":"Standalone","sev":"info"}}}
      ]
    },
    {
      "key": "ha", "name": "High Availability", "kind": "scalar",
      "description": "Cluster mode and group (fgHaInfo).",
      "metrics": [
        {"key":"fgt_ha_mode","name":"HA Mode","oid":"1.3.6.1.4.1.12356.101.13.1.1.0","type":"enum",
         "labels":{"1":{"text":"Standalone","sev":"info"},"2":{"text":"Active-Active","sev":"ok"},"3":{"text":"Active-Passive","sev":"ok"}}},
        {"key":"fgt_ha_group","name":"HA Group","oid":"1.3.6.1.4.1.12356.101.13.1.7.0","type":"string"}
      ]
    },
    {
      "key": "ha_members", "name": "HA Members", "kind": "table",
      "description": "Per-member health and config-sync state (fgHaStatsTable). Alert on sync loss; a member row disappearing means a unit left the cluster.",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.13.2.1.1.11"},
      "metrics": [
        {"key":"fgt_ha_serial","name":"Serial","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.2","type":"string"},
        {"key":"fgt_ha_cpu","name":"CPU","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.3","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_ha_mem","name":"Memory","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.4","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":88}},
        {"key":"fgt_ha_net","name":"Network","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.5","type":"gauge","unit":"kbps"},
        {"key":"fgt_ha_sessions","name":"Sessions","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.6","type":"gauge","unit":"sessions"},
        {"key":"fgt_ha_sync","name":"Sync Status","oid":"1.3.6.1.4.1.12356.101.13.2.1.1.12","type":"enum",
         "labels":{"0":{"text":"Not Synchronized","sev":"crit"},"1":{"text":"Synchronized","sev":"ok"}}}
      ]
    },
    {
      "key": "vpn", "name": "VPN Overview", "kind": "scalar",
      "metrics": [
        {"key":"fgt_tunnels_up","name":"IPsec Tunnels Up","oid":"1.3.6.1.4.1.12356.101.12.1.1.0","type":"gauge","unit":"tunnels"}
      ]
    },
    {
      "key": "sslvpn", "name": "SSL-VPN (per VDOM)", "kind": "table",
      "description": "Logged-in users / web sessions / tunnel sessions per VDOM (fgVpnSslStatsTable).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.3.2.1.1.2"},
      "metrics": [
        {"key":"fgt_sslvpn_users","name":"Logged-in Users","oid":"1.3.6.1.4.1.12356.101.12.2.3.1.2","type":"gauge","unit":"users"},
        {"key":"fgt_sslvpn_web","name":"Web Sessions","oid":"1.3.6.1.4.1.12356.101.12.2.3.1.4","type":"gauge","unit":"sessions"},
        {"key":"fgt_sslvpn_tunnels","name":"Tunnel Sessions","oid":"1.3.6.1.4.1.12356.101.12.2.3.1.6","type":"gauge","unit":"tunnels"}
      ]
    },
    {
      "key": "ipsec", "name": "IPsec Tunnels", "kind": "table",
      "description": "Per-tunnel state and throughput (fgVpn2TunTable, FortiOS 6.4.2+; IPv4+IPv6).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.12.4.2.1.2"},
      "metrics": [
        {"key":"fgt_tun_status","name":"Status","oid":"1.3.6.1.4.1.12356.101.12.4.2.1.26","type":"enum",
         "labels":{"1":{"text":"Down","sev":"crit"},"2":{"text":"Up","sev":"ok"}}},
        {"key":"fgt_tun_p2","name":"Phase 2","oid":"1.3.6.1.4.1.12356.101.12.4.2.1.3","type":"string"},
        {"key":"fgt_tun_in_bps","name":"Traffic In","oid":"1.3.6.1.4.1.12356.101.12.4.2.1.24","type":"counter","unit":"bps","scale":8},
        {"key":"fgt_tun_out_bps","name":"Traffic Out","oid":"1.3.6.1.4.1.12356.101.12.4.2.1.25","type":"counter","unit":"bps","scale":8}
      ]
    },
    {
      "key": "sdwan", "name": "SD-WAN Health", "kind": "table",
      "description": "Health-check state, latency, jitter and packet loss per SD-WAN member link (fgVWLHealthCheckLinkTable, FortiOS 6.0+).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.4.9.2.1.14"},
      "metrics": [
        {"key":"fgt_sdwan_check","name":"Health Check","oid":"1.3.6.1.4.1.12356.101.4.9.2.1.2","type":"string"},
        {"key":"fgt_sdwan_state","name":"State","oid":"1.3.6.1.4.1.12356.101.4.9.2.1.4","type":"enum",
         "labels":{"0":{"text":"Alive","sev":"ok"},"1":{"text":"Dead","sev":"crit"}}},
        {"key":"fgt_sdwan_latency","name":"Latency","oid":"1.3.6.1.4.1.12356.101.4.9.2.1.5","type":"gauge","unit":"ms","thresholds":{"warn":150,"crit":300}},
        {"key":"fgt_sdwan_jitter","name":"Jitter","oid":"1.3.6.1.4.1.12356.101.4.9.2.1.6","type":"gauge","unit":"ms","thresholds":{"warn":30,"crit":50}},
        {"key":"fgt_sdwan_loss","name":"Packet Loss","oid":"1.3.6.1.4.1.12356.101.4.9.2.1.9","type":"gauge","unit":"%","thresholds":{"warn":5,"crit":20}}
      ]
    },
    {
      "key": "wifi", "name": "WiFi Controller", "kind": "scalar",
      "description": "Managed FortiAP fleet and client totals (fgWcInfo).",
      "metrics": [
        {"key":"fgt_aps_managed","name":"APs Managed","oid":"1.3.6.1.4.1.12356.101.14.2.4.0","type":"gauge","unit":"APs"},
        {"key":"fgt_ap_sessions","name":"APs Connected","oid":"1.3.6.1.4.1.12356.101.14.2.5.0","type":"gauge","unit":"APs"},
        {"key":"fgt_wifi_clients","name":"WiFi Clients","oid":"1.3.6.1.4.1.12356.101.14.2.7.0","type":"gauge","unit":"clients"},
        {"key":"fgt_wifi_capacity","name":"Client Capacity","oid":"1.3.6.1.4.1.12356.101.14.2.6.0","type":"gauge","unit":"clients"}
      ]
    },
    {
      "key": "access_points", "name": "FortiAP Access Points", "kind": "table",
      "description": "Per-AP CAPWAP state, client count and resource usage (fgWcWtpSessionTable + names from fgWcWtpConfigTable).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.14.4.3.1.3"},
      "metrics": [
        {"key":"fgt_ap_status","name":"Status","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.7","type":"enum",
         "labels":{"0":{"text":"Other","sev":"warn"},"1":{"text":"Offline","sev":"crit"},"2":{"text":"Online","sev":"ok"},"3":{"text":"Downloading Image","sev":"warn"},"4":{"text":"Connected (Image)","sev":"warn"},"5":{"text":"Standby","sev":"info"}}},
        {"key":"fgt_ap_clients","name":"Clients","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.17","type":"gauge","unit":"clients"},
        {"key":"fgt_ap_cpu","name":"CPU","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.20","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_ap_mem","name":"Memory","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.21","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_ap_model","name":"Model","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.12","type":"string"}
      ]
    },
    {
      "key": "fortiswitch", "name": "Managed FortiSwitch", "kind": "table",
      "description": "FortiLink-managed switch status and resources (fgSwDeviceTable, FortiOS 6.4.2+; CPU/memory need 7.0.1+).",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.24.1.1.1.4"},
      "metrics": [
        {"key":"fgt_sw_status","name":"Status","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.7","type":"enum",
         "labels":{"0":{"text":"Down","sev":"crit"},"1":{"text":"Up","sev":"ok"}}},
        {"key":"fgt_sw_auth","name":"Authorization","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.6","type":"enum",
         "labels":{"0":{"text":"Discovered","sev":"warn"},"1":{"text":"Disabled","sev":"info"},"2":{"text":"Authorized","sev":"ok"}}},
        {"key":"fgt_sw_cpu","name":"CPU","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.11","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_sw_mem","name":"Memory","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.12","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"fgt_sw_version","name":"OS Version","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.5","type":"string"}
      ]
    },
    {
      "key": "sensors", "name": "Hardware Sensors", "kind": "table",
      "description": "Temperatures, fans, voltages and PSU alarm bits (fgHwSensorTable). Empty on VMs.",
      "table": {"label_oid": "1.3.6.1.4.1.12356.101.4.3.2.1.2"},
      "metrics": [
        {"key":"fgt_sensor_value","name":"Value","oid":"1.3.6.1.4.1.12356.101.4.3.2.1.3","type":"gauge"},
        {"key":"fgt_sensor_alarm","name":"Alarm","oid":"1.3.6.1.4.1.12356.101.4.3.2.1.4","type":"enum",
         "labels":{"0":{"text":"OK","sev":"ok"},"1":{"text":"ALARM","sev":"crit"}}}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'Deep FortiGate monitoring: system health, per-core CPU, VDOMs, HA cluster sync, IPsec/SSL-VPN, SD-WAN link quality, managed FortiAPs and FortiSwitches, hardware sensors. Sources: FORTINET-FORTIGATE-MIB (2025-04), Zabbix official template, LibreNMS, Fortinet KBs.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();

-- =============================== Cisco IOS / IOS-XE ===========================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Cisco IOS/IOS-XE Switch & Router', 'Cisco',
  $mr$ {
    "sys_descr_regex": "(?i)(IOS Software|IOS-XE Software|IOS \\(tm\\))",
    "default_vendor": "Cisco",
    "extract_os_version": "Version ([^,\\s]+)"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "cpu", "name": "CPU", "kind": "table",
      "description": "Per-CPU/RP load from CISCO-PROCESS-MIB cpmCPUTotalTable (1-min and 5-min revised averages).",
      "table": {},
      "metrics": [
        {"key":"cisco_cpu_5min","name":"CPU 5 min","oid":"1.3.6.1.4.1.9.9.109.1.1.1.1.8","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"cisco_cpu_1min","name":"CPU 1 min","oid":"1.3.6.1.4.1.9.9.109.1.1.1.1.7","type":"gauge","unit":"%"}
      ]
    },
    {
      "key": "memory", "name": "Memory Pools", "kind": "table",
      "description": "CISCO-MEMORY-POOL-MIB used/free per pool. Note: 32-bit values — pools over 4 GB misreport (IOS-XE largely keeps pools under this). The lsmpi_io pool is always ~100% used by design.",
      "table": {"label_oid": "1.3.6.1.4.1.9.9.48.1.1.1.2"},
      "metrics": [
        {"key":"cisco_mem_used","name":"Used","oid":"1.3.6.1.4.1.9.9.48.1.1.1.5","type":"gauge","unit":"bytes"},
        {"key":"cisco_mem_free","name":"Free","oid":"1.3.6.1.4.1.9.9.48.1.1.1.6","type":"gauge","unit":"bytes"}
      ]
    },
    {
      "key": "temperature", "name": "Temperature", "kind": "table",
      "description": "CISCO-ENVMON-MIB temperature sensors with the device's own state assessment.",
      "table": {"label_oid": "1.3.6.1.4.1.9.9.13.1.3.1.2"},
      "metrics": [
        {"key":"cisco_temp","name":"Temperature","oid":"1.3.6.1.4.1.9.9.13.1.3.1.3","type":"gauge","unit":"°C","thresholds":{"warn":50,"crit":60}},
        {"key":"cisco_temp_state","name":"State","oid":"1.3.6.1.4.1.9.9.13.1.3.1.6","type":"enum",
         "labels":{"1":{"text":"Normal","sev":"ok"},"2":{"text":"Warning","sev":"warn"},"3":{"text":"Critical","sev":"crit"},"4":{"text":"Shutdown","sev":"crit"},"5":{"text":"Not Present","sev":"info"},"6":{"text":"Not Functioning","sev":"crit"}}}
      ]
    },
    {
      "key": "fans", "name": "Fans", "kind": "table",
      "table": {"label_oid": "1.3.6.1.4.1.9.9.13.1.4.1.2"},
      "metrics": [
        {"key":"cisco_fan_state","name":"State","oid":"1.3.6.1.4.1.9.9.13.1.4.1.3","type":"enum",
         "labels":{"1":{"text":"Normal","sev":"ok"},"2":{"text":"Warning","sev":"warn"},"3":{"text":"Critical","sev":"crit"},"4":{"text":"Shutdown","sev":"crit"},"5":{"text":"Not Present","sev":"info"},"6":{"text":"Not Functioning","sev":"crit"}}}
      ]
    },
    {
      "key": "psu", "name": "Power Supplies", "kind": "table",
      "table": {"label_oid": "1.3.6.1.4.1.9.9.13.1.5.1.2"},
      "metrics": [
        {"key":"cisco_psu_state","name":"State","oid":"1.3.6.1.4.1.9.9.13.1.5.1.3","type":"enum",
         "labels":{"1":{"text":"Normal","sev":"ok"},"2":{"text":"Warning","sev":"warn"},"3":{"text":"Critical","sev":"crit"},"4":{"text":"Shutdown","sev":"crit"},"5":{"text":"Not Present","sev":"info"},"6":{"text":"Not Functioning","sev":"crit"}}},
        {"key":"cisco_psu_source","name":"Source","oid":"1.3.6.1.4.1.9.9.13.1.5.1.4","type":"enum",
         "labels":{"1":{"text":"Unknown","sev":"info"},"2":{"text":"AC","sev":"info"},"3":{"text":"DC","sev":"info"},"4":{"text":"External","sev":"info"},"5":{"text":"Internal Redundant","sev":"info"}}}
      ]
    },
    {
      "key": "stack", "name": "Stack Ring", "kind": "scalar",
      "description": "StackWise ring redundancy (empty on non-stackable platforms).",
      "metrics": [
        {"key":"cisco_stack_ring","name":"Ring Redundant","oid":"1.3.6.1.4.1.9.9.500.1.1.3.0","type":"enum",
         "labels":{"1":{"text":"Redundant","sev":"ok"},"2":{"text":"Open Ring","sev":"warn"}}}
      ]
    },
    {
      "key": "stack_members", "name": "Stack Members", "kind": "table",
      "description": "CISCO-STACKWISE-MIB member role and state; any state other than Ready deserves attention.",
      "table": {},
      "metrics": [
        {"key":"cisco_stack_num","name":"Switch #","oid":"1.3.6.1.4.1.9.9.500.1.2.1.1.1","type":"gauge"},
        {"key":"cisco_stack_role","name":"Role","oid":"1.3.6.1.4.1.9.9.500.1.2.1.1.3","type":"enum",
         "labels":{"1":{"text":"Master","sev":"ok"},"2":{"text":"Member","sev":"ok"},"3":{"text":"Not Member","sev":"warn"},"4":{"text":"Standby","sev":"ok"}}},
        {"key":"cisco_stack_state","name":"State","oid":"1.3.6.1.4.1.9.9.500.1.2.1.1.6","type":"enum",
         "labels":{"1":{"text":"Waiting","sev":"warn"},"2":{"text":"Progressing","sev":"warn"},"3":{"text":"Added","sev":"warn"},"4":{"text":"Ready","sev":"ok"},"5":{"text":"SDM Mismatch","sev":"crit"},"6":{"text":"Version Mismatch","sev":"crit"},"7":{"text":"Feature Mismatch","sev":"crit"},"8":{"text":"New Master Init","sev":"crit"},"9":{"text":"Provisioned","sev":"warn"},"10":{"text":"Invalid","sev":"crit"},"11":{"text":"Removed","sev":"warn"}}}
      ]
    },
    {
      "key": "poe", "name": "PoE Budget", "kind": "table",
      "description": "POWER-ETHERNET-MIB per-PSE-group power budget vs consumption (row per stack member / slot).",
      "table": {},
      "metrics": [
        {"key":"cisco_poe_power","name":"Budget","oid":"1.3.6.1.2.1.105.1.3.1.1.2","type":"gauge","unit":"W"},
        {"key":"cisco_poe_consumed","name":"Consumed","oid":"1.3.6.1.2.1.105.1.3.1.1.4","type":"gauge","unit":"W"},
        {"key":"cisco_poe_status","name":"PSE Status","oid":"1.3.6.1.2.1.105.1.3.1.1.3","type":"enum",
         "labels":{"1":{"text":"On","sev":"ok"},"2":{"text":"Off","sev":"warn"},"3":{"text":"Faulty","sev":"crit"}}}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'Catalyst / ISR monitoring: per-CPU load, memory pools, ENVMON temperature/fan/PSU states, StackWise ring & members, PoE budget. Sources: Cisco MIBs, Zabbix official template, LibreNMS severity maps.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();

-- =============================== Cisco ASA ====================================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Cisco ASA Firewall', 'Cisco',
  $mr$ {
    "sys_descr_regex": "(?i)Adaptive Security Appliance",
    "default_vendor": "Cisco",
    "extract_os_version": "Version ([^,\\s]+)"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "connections", "name": "Firewall Connections", "kind": "scalar",
      "description": "Firewall-wide connection count and boot-time peak (CISCO-FIREWALL-MIB cfwConnectionStat protoIp).",
      "metrics": [
        {"key":"asa_conns","name":"Current Connections","oid":"1.3.6.1.4.1.9.9.147.1.2.2.2.1.5.40.6","type":"gauge","unit":"conns"},
        {"key":"asa_conns_peak","name":"Peak Connections","oid":"1.3.6.1.4.1.9.9.147.1.2.2.2.1.5.40.7","type":"gauge","unit":"conns"}
      ]
    },
    {
      "key": "cpu", "name": "CPU", "kind": "table",
      "table": {},
      "metrics": [
        {"key":"asa_cpu_5min","name":"CPU 5 min","oid":"1.3.6.1.4.1.9.9.109.1.1.1.1.8","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"asa_cpu_1min","name":"CPU 1 min","oid":"1.3.6.1.4.1.9.9.109.1.1.1.1.7","type":"gauge","unit":"%"}
      ]
    },
    {
      "key": "memory", "name": "Memory", "kind": "table",
      "description": "32-bit memory pools; ASA >4 GB RAM under-reports (known Gauge32 limit).",
      "table": {"label_oid": "1.3.6.1.4.1.9.9.48.1.1.1.2"},
      "metrics": [
        {"key":"asa_mem_used","name":"Used","oid":"1.3.6.1.4.1.9.9.48.1.1.1.5","type":"gauge","unit":"bytes"},
        {"key":"asa_mem_free","name":"Free","oid":"1.3.6.1.4.1.9.9.48.1.1.1.6","type":"gauge","unit":"bytes"}
      ]
    },
    {
      "key": "failover", "name": "Failover", "kind": "scalar",
      "description": "Primary/secondary unit failover state (cfwHardwareStatusTable). Healthy pair = Active + Standby; alert on Down/Error or any state change.",
      "metrics": [
        {"key":"asa_fo_primary","name":"Primary Unit","oid":"1.3.6.1.4.1.9.9.147.1.2.1.1.1.3.6","type":"enum",
         "labels":{"1":{"text":"Other","sev":"info"},"2":{"text":"Up","sev":"ok"},"3":{"text":"Down","sev":"crit"},"4":{"text":"Error","sev":"crit"},"9":{"text":"Active","sev":"ok"},"10":{"text":"Standby","sev":"ok"}}},
        {"key":"asa_fo_secondary","name":"Secondary Unit","oid":"1.3.6.1.4.1.9.9.147.1.2.1.1.1.3.7","type":"enum",
         "labels":{"1":{"text":"Other","sev":"info"},"2":{"text":"Up","sev":"ok"},"3":{"text":"Down","sev":"crit"},"4":{"text":"Error","sev":"crit"},"9":{"text":"Active","sev":"ok"},"10":{"text":"Standby","sev":"ok"}}},
        {"key":"asa_fo_primary_txt","name":"Primary Detail","oid":"1.3.6.1.4.1.9.9.147.1.2.1.1.1.4.6","type":"string"},
        {"key":"asa_fo_secondary_txt","name":"Secondary Detail","oid":"1.3.6.1.4.1.9.9.147.1.2.1.1.1.4.7","type":"string"}
      ]
    },
    {
      "key": "vpn", "name": "Remote Access VPN", "kind": "scalar",
      "description": "Session counts by type (CISCO-REMOTE-ACCESS-MONITOR-MIB).",
      "metrics": [
        {"key":"asa_vpn_sessions","name":"Active Sessions","oid":"1.3.6.1.4.1.9.9.392.1.3.1.0","type":"gauge","unit":"sessions"},
        {"key":"asa_vpn_users","name":"Active Users","oid":"1.3.6.1.4.1.9.9.392.1.3.3.0","type":"gauge","unit":"users"},
        {"key":"asa_anyconnect","name":"AnyConnect (SVC)","oid":"1.3.6.1.4.1.9.9.392.1.3.35.0","type":"gauge","unit":"sessions"},
        {"key":"asa_ipsec_ra","name":"IPsec RA","oid":"1.3.6.1.4.1.9.9.392.1.3.26.0","type":"gauge","unit":"sessions"},
        {"key":"asa_l2l","name":"Site-to-Site","oid":"1.3.6.1.4.1.9.9.392.1.3.29.0","type":"gauge","unit":"tunnels"},
        {"key":"asa_webvpn","name":"Clientless WebVPN","oid":"1.3.6.1.4.1.9.9.392.1.3.38.0","type":"gauge","unit":"sessions"},
        {"key":"asa_vpn_capacity","name":"Session Capacity","oid":"1.3.6.1.4.1.9.9.392.1.1.1.0","type":"gauge","unit":"sessions"}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'ASA firewall monitoring: connection load vs peak, CPU/memory, failover pair state, remote-access VPN sessions by type. Sources: CISCO-FIREWALL-MIB, CISCO-REMOTE-ACCESS-MONITOR-MIB, community-verified enums.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();

-- =============================== Palo Alto PAN-OS =============================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Palo Alto PAN-OS Firewall', 'Palo Alto Networks',
  $mr$ {
    "sys_object_id_prefixes": ["1.3.6.1.4.1.25461.2.3"],
    "default_vendor": "Palo Alto Networks"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "sessions", "name": "Session Table", "kind": "scalar",
      "description": "Session utilization vs platform capacity, by protocol, plus SSL-decrypt load (PAN-COMMON-MIB panSession).",
      "metrics": [
        {"key":"pan_sess_util","name":"Session Utilization","oid":"1.3.6.1.4.1.25461.2.1.2.3.1.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"pan_sess_active","name":"Active Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.3.0","type":"gauge","unit":"sessions"},
        {"key":"pan_sess_max","name":"Session Capacity","oid":"1.3.6.1.4.1.25461.2.1.2.3.2.0","type":"gauge","unit":"sessions"},
        {"key":"pan_sess_tcp","name":"TCP Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.4.0","type":"gauge","unit":"sessions"},
        {"key":"pan_sess_udp","name":"UDP Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.5.0","type":"gauge","unit":"sessions"},
        {"key":"pan_sess_icmp","name":"ICMP Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.6.0","type":"gauge","unit":"sessions"},
        {"key":"pan_sess_ssl","name":"SSL-Proxy Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.7.0","type":"gauge","unit":"sessions"},
        {"key":"pan_ssl_util","name":"SSL-Proxy Utilization","oid":"1.3.6.1.4.1.25461.2.1.2.3.8.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}}
      ]
    },
    {
      "key": "cpu", "name": "CPU Planes", "kind": "scalar",
      "description": "Management-plane vs dataplane CPU (HOST-RESOURCES hrProcessorLoad; PAN KB: index 1 = management, index 2 = dataplane). Never average the two.",
      "metrics": [
        {"key":"pan_cpu_mgmt","name":"Management CPU","oid":"1.3.6.1.2.1.25.3.3.1.2.1","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"pan_cpu_data","name":"Dataplane CPU","oid":"1.3.6.1.2.1.25.3.3.1.2.2","type":"gauge","unit":"%","thresholds":{"warn":90,"crit":95}}
      ]
    },
    {
      "key": "ha", "name": "High Availability", "kind": "scalar",
      "description": "HA mode and local/peer state (PAN returns literal strings; the most valuable alert is any state change).",
      "metrics": [
        {"key":"pan_ha_mode","name":"HA Mode","oid":"1.3.6.1.4.1.25461.2.1.2.1.13.0","type":"enum",
         "value_map":{"disabled":0,"active-passive":1,"active-active":2},
         "labels":{"0":{"text":"Disabled","sev":"info"},"1":{"text":"Active-Passive","sev":"ok"},"2":{"text":"Active-Active","sev":"ok"}}},
        {"key":"pan_ha_state","name":"Local State","oid":"1.3.6.1.4.1.25461.2.1.2.1.11.0","type":"enum",
         "value_map":{"disabled":0,"active":1,"passive":2,"active-primary":3,"active-secondary":4,"initial":5,"tentative":6,"unknown":7,"non-functional":8,"suspended":9},
         "labels":{"0":{"text":"Disabled","sev":"info"},"1":{"text":"Active","sev":"ok"},"2":{"text":"Passive","sev":"ok"},"3":{"text":"Active-Primary","sev":"ok"},"4":{"text":"Active-Secondary","sev":"ok"},"5":{"text":"Initial","sev":"warn"},"6":{"text":"Tentative","sev":"warn"},"7":{"text":"Unknown","sev":"warn"},"8":{"text":"Non-Functional","sev":"crit"},"9":{"text":"Suspended","sev":"crit"}}},
        {"key":"pan_ha_peer","name":"Peer State","oid":"1.3.6.1.4.1.25461.2.1.2.1.12.0","type":"enum",
         "value_map":{"disabled":0,"active":1,"passive":2,"active-primary":3,"active-secondary":4,"initial":5,"tentative":6,"unknown":7,"non-functional":8,"suspended":9},
         "labels":{"0":{"text":"Disabled","sev":"info"},"1":{"text":"Active","sev":"ok"},"2":{"text":"Passive","sev":"ok"},"3":{"text":"Active-Primary","sev":"ok"},"4":{"text":"Active-Secondary","sev":"ok"},"5":{"text":"Initial","sev":"warn"},"6":{"text":"Tentative","sev":"warn"},"7":{"text":"Unknown","sev":"warn"},"8":{"text":"Non-Functional","sev":"crit"},"9":{"text":"Suspended","sev":"crit"}}}
      ]
    },
    {
      "key": "globalprotect", "name": "GlobalProtect", "kind": "scalar",
      "description": "GP gateway tunnel utilization (PAN-OS 9.x+; zeros when no GP gateway configured).",
      "metrics": [
        {"key":"pan_gp_util","name":"Gateway Utilization","oid":"1.3.6.1.4.1.25461.2.1.2.5.1.1.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"pan_gp_active","name":"Active Tunnels","oid":"1.3.6.1.4.1.25461.2.1.2.5.1.3.0","type":"gauge","unit":"tunnels"},
        {"key":"pan_gp_max","name":"Tunnel Capacity","oid":"1.3.6.1.4.1.25461.2.1.2.5.1.2.0","type":"gauge","unit":"tunnels"}
      ]
    },
    {
      "key": "vsys", "name": "Virtual Systems", "kind": "table",
      "description": "Per-vsys session load (panVsysTable; single row on non-multi-vsys platforms).",
      "table": {"label_oid": "1.3.6.1.4.1.25461.2.1.2.3.9.1.2"},
      "metrics": [
        {"key":"pan_vsys_util","name":"Session Utilization","oid":"1.3.6.1.4.1.25461.2.1.2.3.9.1.3","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"pan_vsys_sessions","name":"Active Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.9.1.4","type":"gauge","unit":"sessions"},
        {"key":"pan_vsys_max","name":"Max Sessions","oid":"1.3.6.1.4.1.25461.2.1.2.3.9.1.5","type":"gauge","unit":"sessions"}
      ]
    },
    {
      "key": "content", "name": "Software & Content", "kind": "scalar",
      "description": "PAN-OS version and security-content versions — stale content is a compliance signal. Panorama connectivity included.",
      "metrics": [
        {"key":"pan_sw_version","name":"PAN-OS Version","oid":"1.3.6.1.4.1.25461.2.1.2.1.1.0","type":"string"},
        {"key":"pan_app_version","name":"App-ID Content","oid":"1.3.6.1.4.1.25461.2.1.2.1.7.0","type":"string"},
        {"key":"pan_av_version","name":"Antivirus","oid":"1.3.6.1.4.1.25461.2.1.2.1.8.0","type":"string"},
        {"key":"pan_threat_version","name":"Threat Content","oid":"1.3.6.1.4.1.25461.2.1.2.1.9.0","type":"string"},
        {"key":"pan_wildfire_version","name":"WildFire","oid":"1.3.6.1.4.1.25461.2.1.2.1.17.0","type":"string"},
        {"key":"pan_panorama_conn","name":"Panorama","oid":"1.3.6.1.4.1.25461.2.1.2.4.1.0","type":"enum",
         "value_map":{"connected":1,"not-connected":0},
         "labels":{"0":{"text":"Not Connected","sev":"warn"},"1":{"text":"Connected","sev":"ok"}}}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'PAN-OS firewall monitoring: session-table utilization by protocol, mgmt vs dataplane CPU, HA states with failover detection, GlobalProtect tunnels, per-vsys load, content-version compliance. Sources: PAN-COMMON-MIB, Palo Alto KBs, LibreNMS, Zabbix community template.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();

-- =============================== F5 BIG-IP ====================================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'F5 BIG-IP', 'F5 Networks',
  $mr$ {
    "sys_object_id_prefixes": ["1.3.6.1.4.1.3375.2.1"],
    "default_vendor": "F5 Networks"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "cpu", "name": "CPU (TMM vs Host)", "kind": "scalar",
      "description": "Data-plane (TMM) vs control-plane CPU, 1-min and 5-min windows (F5 official thresholds: TMM ≥85% critical).",
      "metrics": [
        {"key":"f5_tmm_cpu","name":"TMM CPU (1m)","oid":"1.3.6.1.4.1.3375.2.1.1.2.21.35.0","type":"gauge","unit":"%","thresholds":{"warn":75,"crit":85}},
        {"key":"f5_tmm_cpu_5m","name":"TMM CPU (5m)","oid":"1.3.6.1.4.1.3375.2.1.1.2.21.36.0","type":"gauge","unit":"%"},
        {"key":"f5_host_cpu","name":"Host CPU (1m)","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.29.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"f5_host_cpu_5m","name":"Host CPU (5m)","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.37.0","type":"gauge","unit":"%"}
      ]
    },
    {
      "key": "memory", "name": "Memory", "kind": "scalar",
      "description": "TMM (data plane) and Other (Linux) memory tracked separately per F5 guidance; swap presence is normal — watch growth.",
      "metrics": [
        {"key":"f5_tmm_mem_used","name":"TMM Memory Used","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.45.0","type":"gauge","unit":"bytes"},
        {"key":"f5_tmm_mem_total","name":"TMM Memory Total","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.44.0","type":"gauge","unit":"bytes"},
        {"key":"f5_other_mem_used","name":"Host Memory Used","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.45.0","type":"gauge","unit":"bytes"},
        {"key":"f5_other_mem_total","name":"Host Memory Total","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.44.0","type":"gauge","unit":"bytes"},
        {"key":"f5_swap_used","name":"Swap Used","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.47.0","type":"gauge","unit":"bytes"},
        {"key":"f5_swap_total","name":"Swap Total","oid":"1.3.6.1.4.1.3375.2.1.1.2.20.46.0","type":"gauge","unit":"bytes"}
      ]
    },
    {
      "key": "connections", "name": "Connections & SSL", "kind": "scalar",
      "description": "Client/server concurrency, new-connection rate and SSL TPS (licensed metric = native + compat handshake rate).",
      "metrics": [
        {"key":"f5_client_conns","name":"Client Connections","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.8.0","type":"gauge","unit":"conns"},
        {"key":"f5_server_conns","name":"Server Connections","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.15.0","type":"gauge","unit":"conns"},
        {"key":"f5_client_cps","name":"New Connections","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.7.0","type":"counter","unit":"conns/s"},
        {"key":"f5_ssl_conns","name":"SSL Connections","oid":"1.3.6.1.4.1.3375.2.1.1.2.9.2.0","type":"gauge","unit":"conns"},
        {"key":"f5_ssl_tps_native","name":"SSL TPS (native)","oid":"1.3.6.1.4.1.3375.2.1.1.2.9.6.0","type":"counter","unit":"tps"},
        {"key":"f5_ssl_tps_compat","name":"SSL TPS (compat)","oid":"1.3.6.1.4.1.3375.2.1.1.2.9.9.0","type":"counter","unit":"tps"}
      ]
    },
    {
      "key": "throughput", "name": "Throughput", "kind": "scalar",
      "description": "Client-side (frontend) and server-side (pool-facing) traffic.",
      "metrics": [
        {"key":"f5_client_in_bps","name":"Client In","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.3.0","type":"counter","unit":"bps","scale":8},
        {"key":"f5_client_out_bps","name":"Client Out","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.5.0","type":"counter","unit":"bps","scale":8},
        {"key":"f5_server_in_bps","name":"Server In","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.10.0","type":"counter","unit":"bps","scale":8},
        {"key":"f5_server_out_bps","name":"Server Out","oid":"1.3.6.1.4.1.3375.2.1.1.2.1.12.0","type":"counter","unit":"bps","scale":8}
      ]
    },
    {
      "key": "virtual_servers", "name": "Virtual Servers", "kind": "table",
      "description": "Availability, enabled state, connections and traffic per virtual server (ltmVsStatusTable + ltmVirtualServStatTable; red while enabled = outage).",
      "table": {"label_oid": "1.3.6.1.4.1.3375.2.2.10.13.2.1.1"},
      "metrics": [
        {"key":"f5_vs_avail","name":"Availability","oid":"1.3.6.1.4.1.3375.2.2.10.13.2.1.2","type":"enum",
         "labels":{"0":{"text":"Unknown","sev":"info"},"1":{"text":"Available","sev":"ok"},"2":{"text":"Degraded","sev":"warn"},"3":{"text":"Offline","sev":"crit"},"4":{"text":"No Monitor","sev":"info"},"5":{"text":"Unlicensed","sev":"info"}}},
        {"key":"f5_vs_enabled","name":"Enabled","oid":"1.3.6.1.4.1.3375.2.2.10.13.2.1.3","type":"enum",
         "labels":{"0":{"text":"None","sev":"info"},"1":{"text":"Enabled","sev":"ok"},"2":{"text":"Disabled","sev":"info"},"3":{"text":"Disabled (parent)","sev":"info"}}},
        {"key":"f5_vs_conns","name":"Connections","oid":"1.3.6.1.4.1.3375.2.2.10.2.3.1.12","type":"gauge","unit":"conns"},
        {"key":"f5_vs_in_bps","name":"Traffic In","oid":"1.3.6.1.4.1.3375.2.2.10.2.3.1.7","type":"counter","unit":"bps","scale":8},
        {"key":"f5_vs_out_bps","name":"Traffic Out","oid":"1.3.6.1.4.1.3375.2.2.10.2.3.1.9","type":"counter","unit":"bps","scale":8},
        {"key":"f5_vs_reason","name":"Detail","oid":"1.3.6.1.4.1.3375.2.2.10.13.2.1.5","type":"string"}
      ]
    },
    {
      "key": "pools", "name": "Pools", "kind": "table",
      "description": "Active vs configured members and pool availability (ltmPoolTable + ltmPoolStatusTable). Active < configured = degraded; 0 = down.",
      "table": {"label_oid": "1.3.6.1.4.1.3375.2.2.5.1.2.1.1"},
      "metrics": [
        {"key":"f5_pool_active","name":"Active Members","oid":"1.3.6.1.4.1.3375.2.2.5.1.2.1.8","type":"gauge","unit":"members"},
        {"key":"f5_pool_members","name":"Configured Members","oid":"1.3.6.1.4.1.3375.2.2.5.1.2.1.23","type":"gauge","unit":"members"},
        {"key":"f5_pool_avail","name":"Availability","oid":"1.3.6.1.4.1.3375.2.2.5.5.2.1.2","type":"enum",
         "labels":{"0":{"text":"Unknown","sev":"info"},"1":{"text":"Available","sev":"ok"},"2":{"text":"Degraded","sev":"warn"},"3":{"text":"Offline","sev":"crit"},"4":{"text":"No Monitor","sev":"info"},"5":{"text":"Unlicensed","sev":"info"}}},
        {"key":"f5_pool_reason","name":"Detail","oid":"1.3.6.1.4.1.3375.2.2.5.5.2.1.5","type":"string"}
      ]
    },
    {
      "key": "ha", "name": "HA & Config Sync", "kind": "scalar",
      "description": "Device failover role and config-sync health (sysCmFailoverStatus / sysCmSyncStatus).",
      "metrics": [
        {"key":"f5_failover","name":"Failover State","oid":"1.3.6.1.4.1.3375.2.1.14.3.1.0","type":"enum",
         "labels":{"0":{"text":"Unknown","sev":"warn"},"1":{"text":"Offline","sev":"crit"},"2":{"text":"Forced Offline","sev":"crit"},"3":{"text":"Standby","sev":"ok"},"4":{"text":"Active","sev":"ok"}}},
        {"key":"f5_sync","name":"Sync Status","oid":"1.3.6.1.4.1.3375.2.1.14.1.1.0","type":"enum",
         "labels":{"0":{"text":"Unknown","sev":"warn"},"1":{"text":"Syncing","sev":"warn"},"2":{"text":"Manual Sync Needed","sev":"warn"},"3":{"text":"In Sync","sev":"ok"},"4":{"text":"Sync Failed","sev":"crit"},"5":{"text":"Disconnected","sev":"crit"},"6":{"text":"Standalone","sev":"ok"},"7":{"text":"Awaiting Initial Sync","sev":"warn"},"8":{"text":"Incompatible Version","sev":"crit"},"9":{"text":"Partial Sync","sev":"warn"}}},
        {"key":"f5_failover_txt","name":"Failover Detail","oid":"1.3.6.1.4.1.3375.2.1.14.3.2.0","type":"string"}
      ]
    },
    {
      "key": "fans", "name": "Chassis Fans", "kind": "table",
      "table": {},
      "metrics": [
        {"key":"f5_fan_status","name":"Status","oid":"1.3.6.1.4.1.3375.2.1.3.2.1.2.1.2","type":"enum",
         "labels":{"0":{"text":"Bad","sev":"crit"},"1":{"text":"Good","sev":"ok"},"2":{"text":"Not Present","sev":"info"}}},
        {"key":"f5_fan_speed","name":"Speed","oid":"1.3.6.1.4.1.3375.2.1.3.2.1.2.1.3","type":"gauge","unit":"RPM"}
      ]
    },
    {
      "key": "psus", "name": "Power Supplies", "kind": "table",
      "table": {},
      "metrics": [
        {"key":"f5_psu_status","name":"Status","oid":"1.3.6.1.4.1.3375.2.1.3.2.2.2.1.2","type":"enum",
         "labels":{"0":{"text":"Bad","sev":"crit"},"1":{"text":"Good","sev":"ok"},"2":{"text":"Not Present","sev":"info"}}}
      ]
    },
    {
      "key": "temperature", "name": "Chassis Temperature", "kind": "table",
      "table": {},
      "metrics": [
        {"key":"f5_temp","name":"Temperature","oid":"1.3.6.1.4.1.3375.2.1.3.2.3.2.1.2","type":"gauge","unit":"°C","thresholds":{"warn":45,"crit":50}}
      ]
    },
    {
      "key": "system", "name": "System", "kind": "scalar",
      "metrics": [
        {"key":"f5_version","name":"TMOS Version","oid":"1.3.6.1.4.1.3375.2.1.4.2.0","type":"string"},
        {"key":"f5_serial","name":"Chassis Serial","oid":"1.3.6.1.4.1.3375.2.1.3.3.3.0","type":"string"}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'BIG-IP LTM monitoring: TMM vs host CPU/memory, connection & SSL TPS load, client/server throughput, per-virtual-server and per-pool health with detail reasons, failover & config-sync state, chassis hardware. Sources: F5-BIGIP-SYSTEM/LOCAL MIBs, F5 K-articles (K44014003/K15468/K000152888), Zabbix official template, LibreNMS.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();
