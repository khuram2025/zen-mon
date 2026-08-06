-- migrate-063: Monitoring templates — Juniper JunOS + Aruba Wireless Controller
--
-- Extends the migrate-062 builtin template set with two more vendor packs.
-- OIDs researched 2026-08 from the vendor MIBs (JUNIPER-MIB/JUNIPER-JS-SMI,
-- ArubaOS 8.8 WLSX-* MIBs), LibreNMS discovery definitions, the official
-- Zabbix Juniper template and community templates; core objects verified
-- against >=2 independent sources.

-- =============================== Juniper JunOS ================================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Juniper JunOS', 'Juniper',
  $mr$ {
    "sys_object_id_prefixes": ["1.3.6.1.4.1.2636.1.1.1.2"],
    "default_vendor": "Juniper",
    "extract_model": "Juniper Networks, Inc\\. (\\S+)",
    "extract_os_version": "JUNOS ([0-9][^\\s,]*)"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "health", "name": "Chassis Health", "kind": "table",
      "description": "Per-component state, temperature, CPU and memory from jnxOperatingTable — one row per Routing Engine, FPC, power supply and fan tray.",
      "table": {"label_oid": "1.3.6.1.4.1.2636.3.1.13.1.5"},
      "metrics": [
        {"key":"jnx_state","name":"State","oid":"1.3.6.1.4.1.2636.3.1.13.1.6","type":"enum",
         "labels":{"1":{"text":"Unknown","sev":"warn"},"2":{"text":"Running","sev":"ok"},"3":{"text":"Ready","sev":"ok"},"4":{"text":"Reset","sev":"warn"},"5":{"text":"Running (Full Speed)","sev":"ok"},"6":{"text":"Down","sev":"crit"},"7":{"text":"Standby","sev":"info"}}},
        {"key":"jnx_temp","name":"Temperature","oid":"1.3.6.1.4.1.2636.3.1.13.1.7","type":"gauge","unit":"°C","thresholds":{"warn":50,"crit":60}},
        {"key":"jnx_cpu","name":"CPU","oid":"1.3.6.1.4.1.2636.3.1.13.1.8","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"jnx_mem_util","name":"Memory","oid":"1.3.6.1.4.1.2636.3.1.13.1.11","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}}
      ]
    },
    {
      "key": "alarms", "name": "Chassis Alarms", "kind": "scalar",
      "description": "System yellow/red alarm LEDs (JUNIPER-ALARM-MIB).",
      "metrics": [
        {"key":"jnx_alarm_yellow","name":"Yellow Alarm","oid":"1.3.6.1.4.1.2636.3.4.2.2.1.0","type":"enum",
         "labels":{"1":{"text":"Other","sev":"info"},"2":{"text":"Off","sev":"ok"},"3":{"text":"On","sev":"warn"}}},
        {"key":"jnx_alarm_red","name":"Red Alarm","oid":"1.3.6.1.4.1.2636.3.4.2.3.1.0","type":"enum",
         "labels":{"1":{"text":"Other","sev":"info"},"2":{"text":"Off","sev":"ok"},"3":{"text":"On","sev":"crit"}}}
      ]
    },
    {
      "key": "redundancy", "name": "RE Redundancy", "kind": "table",
      "description": "Routing-engine mastership and switchover count (dual-RE / clustered systems only).",
      "table": {"label_oid": "1.3.6.1.4.1.2636.3.1.14.1.5"},
      "metrics": [
        {"key":"jnx_red_state","name":"Role","oid":"1.3.6.1.4.1.2636.3.1.14.1.7","type":"enum",
         "labels":{"1":{"text":"Unknown","sev":"warn"},"2":{"text":"Master","sev":"ok"},"3":{"text":"Backup","sev":"ok"},"4":{"text":"Disabled","sev":"warn"}}},
        {"key":"jnx_red_switchovers","name":"Switchovers","oid":"1.3.6.1.4.1.2636.3.1.14.1.8","type":"gauge","unit":"count"}
      ]
    },
    {
      "key": "srx_flow", "name": "SRX Flow Sessions", "kind": "scalar",
      "description": "Box-wide flow session load vs capacity (flow-mode SRX / vSRX only; empty on EX/MX).",
      "metrics": [
        {"key":"jnx_sessions","name":"Active Sessions","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.2.0","type":"gauge","unit":"sessions"},
        {"key":"jnx_sessions_max","name":"Session Capacity","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.3.0","type":"gauge","unit":"sessions"}
      ]
    },
    {
      "key": "srx_spus", "name": "SRX SPUs", "kind": "table",
      "description": "Per-SPU CPU, memory and sessions (branch SRX shows one row; chassis clusters one set per node).",
      "table": {"label_oid": "1.3.6.1.4.1.2636.3.39.1.12.1.1.1.11"},
      "metrics": [
        {"key":"jnx_spu_cpu","name":"CPU","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.1.1.4","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"jnx_spu_mem","name":"Memory","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.1.1.5","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"jnx_spu_sessions","name":"Sessions","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.1.1.6","type":"gauge","unit":"sessions"},
        {"key":"jnx_spu_sessions_max","name":"Capacity","oid":"1.3.6.1.4.1.2636.3.39.1.12.1.1.1.7","type":"gauge","unit":"sessions"}
      ]
    },
    {
      "key": "bgp_peers", "name": "BGP Peers", "kind": "table",
      "description": "BGP4-MIB peer states (IPv4 peers). Anything other than Established deserves a look.",
      "table": {"label_oid": "1.3.6.1.2.1.15.3.1.7"},
      "metrics": [
        {"key":"jnx_bgp_state","name":"State","oid":"1.3.6.1.2.1.15.3.1.2","type":"enum",
         "labels":{"1":{"text":"Idle","sev":"warn"},"2":{"text":"Connect","sev":"warn"},"3":{"text":"Active","sev":"warn"},"4":{"text":"OpenSent","sev":"warn"},"5":{"text":"OpenConfirm","sev":"warn"},"6":{"text":"Established","sev":"ok"}}}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'JunOS monitoring for SRX/EX/MX: per-component chassis health (RE/FPC/PSU/fan state, temperature, CPU, memory), yellow/red chassis alarms, RE redundancy, SRX flow-session load with per-SPU detail, BGP peer states. Sources: JUNIPER-MIB + JUNIPER-JS-SMI, LibreNMS, official Zabbix template.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();

-- ========================= Aruba Wireless Controller ==========================

INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Aruba Wireless Controller', 'Aruba (HPE)',
  $mr$ {
    "sys_object_id_prefixes": ["1.3.6.1.4.1.14823.1.1"],
    "default_vendor": "Aruba",
    "extract_model": "MODEL: ([^)]+)",
    "extract_os_version": "Version ([0-9][^\\s,]*)"
  } $mr$::jsonb,
  $oidg$ [
    {
      "key": "system", "name": "Controller Health", "kind": "scalar",
      "description": "Controller CPU/memory and role (ArubaOS 8.x; on a Mobility Conductor the wireless tables are served by the managed devices, not the MM).",
      "metrics": [
        {"key":"aruba_cpu","name":"CPU Usage","oid":"1.3.6.1.4.1.14823.2.2.1.2.1.30.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"aruba_mem","name":"Memory Usage","oid":"1.3.6.1.4.1.14823.2.2.1.2.1.31.0","type":"gauge","unit":"%","thresholds":{"warn":80,"crit":90}},
        {"key":"aruba_role","name":"Role","oid":"1.3.6.1.4.1.14823.2.2.1.1.1.4.0","type":"enum",
         "labels":{"1":{"text":"Conductor","sev":"ok"},"2":{"text":"Local","sev":"ok"},"3":{"text":"Standby Conductor","sev":"ok"},"4":{"text":"Branch","sev":"ok"},"5":{"text":"Managed Device","sev":"ok"}}}
      ]
    },
    {
      "key": "wireless", "name": "Wireless Overview", "kind": "scalar",
      "metrics": [
        {"key":"aruba_aps","name":"Access Points","oid":"1.3.6.1.4.1.14823.2.2.1.1.3.1.0","type":"gauge","unit":"APs"},
        {"key":"aruba_clients","name":"Connected Clients","oid":"1.3.6.1.4.1.14823.2.2.1.1.3.2.0","type":"gauge","unit":"clients"}
      ]
    },
    {
      "key": "access_points", "name": "Access Points", "kind": "table",
      "description": "Per-AP status and identity (wlsxWlanAPTable, indexed by AP MAC).",
      "table": {"label_oid": "1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.3"},
      "metrics": [
        {"key":"aruba_ap_status","name":"Status","oid":"1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.19","type":"enum",
         "labels":{"1":{"text":"Up","sev":"ok"},"2":{"text":"Down","sev":"crit"}}},
        {"key":"aruba_ap_group","name":"Group","oid":"1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.4","type":"string"},
        {"key":"aruba_ap_model","name":"Model","oid":"1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.13","type":"string"},
        {"key":"aruba_ap_ip","name":"IP Address","oid":"1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.2","type":"string"}
      ]
    },
    {
      "key": "fans", "name": "Fans", "kind": "table",
      "description": "Chassis fan status (hardware controllers only; empty on VM controllers).",
      "table": {},
      "metrics": [
        {"key":"aruba_fan_status","name":"Status","oid":"1.3.6.1.4.1.14823.2.2.1.2.1.17.1.2","type":"enum",
         "labels":{"1":{"text":"Active","sev":"ok"},"2":{"text":"Inactive","sev":"crit"}}}
      ]
    },
    {
      "key": "psus", "name": "Power Supplies", "kind": "table",
      "table": {},
      "metrics": [
        {"key":"aruba_psu_status","name":"Status","oid":"1.3.6.1.4.1.14823.2.2.1.2.1.18.1.2","type":"enum",
         "labels":{"1":{"text":"Active","sev":"ok"},"2":{"text":"Inactive","sev":"crit"}}}
      ]
    }
  ] $oidg$::jsonb,
  1, TRUE,
  'ArubaOS Mobility Controller monitoring: controller CPU/memory/role, AP and client totals, per-AP status by name with group/model/IP, chassis fan and PSU health. Sources: ArubaOS 8.8 WLSX MIBs, LibreNMS, Zabbix community template, Airheads.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor = EXCLUDED.vendor, match_rules = EXCLUDED.match_rules,
  oid_groups = EXCLUDED.oid_groups, builtin = TRUE,
  description = EXCLUDED.description, updated_at = NOW();
