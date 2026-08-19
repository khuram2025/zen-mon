-- migrate-080: SNMP coverage for current network/security appliance fleet.
--
-- Adds verified Aruba AOS-CX and Dell SmartFabric OS10 profiles, corrects
-- PAN-OS CPU collection to walk HOST-RESOURCES-MIB, and extends Cisco/FortiOS
-- profiles with their current 64-bit/data-plane objects. Vendor-neutral
-- BGP/OSPF/STP/LACP groups are supplied by the poller at a bounded cadence.

-- Aruba AOS-CX (ARUBAWIRED-SYSTEMINFO-MIB, enterprise 47196)
INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Aruba AOS-CX Switch', 'Aruba (HPE)',
  '{"sys_object_id_prefixes":["1.3.6.1.4.1.47196.4.1.1.1"],"sys_descr_regex":"(?i)ArubaOS-CX|AOS-CX","default_vendor":"Aruba","extract_os_version":"FL\\.([0-9A-Za-z.]+)"}'::jsonb,
  $oidg$[
    {
      "key":"system_modules","name":"System Modules","kind":"table",
      "description":"Per-module CPU, memory and storage health from ARUBAWIRED-SYSTEMINFO-MIB.",
      "table":{"label_oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.2"},
      "metrics":[
        {"key":"aruba_cx_module_type","name":"Module Type","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.1","type":"enum"},
        {"key":"aruba_cx_cpu_current","name":"CPU Current","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.3","type":"gauge","unit":"%"},
        {"key":"aruba_cx_memory","name":"Memory Usage","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.4","type":"gauge","unit":"%"},
        {"key":"aruba_cx_storage_nos","name":"NOS Storage Usage","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.5","type":"gauge","unit":"%"},
        {"key":"aruba_cx_storage_log","name":"Log Storage Usage","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.6","type":"gauge","unit":"%"},
        {"key":"aruba_cx_storage_core","name":"Core Storage Usage","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.7","type":"gauge","unit":"%"},
        {"key":"aruba_cx_storage_security","name":"Security Storage Usage","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.8","type":"gauge","unit":"%"},
        {"key":"aruba_cx_selftest","name":"Self Test","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.9","type":"enum"},
        {"key":"aruba_cx_cpu_1min","name":"CPU 1 min","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.10","type":"gauge","unit":"%"},
        {"key":"aruba_cx_cpu_5min","name":"CPU 5 min","oid":"1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.11","type":"gauge","unit":"%"}
      ]
    },
    {
      "key":"vsf_members","name":"VSF Members","kind":"table","interval_seconds":120,
      "description":"VSFv2 member role, status, identity and utilization.",
      "table":{"label_oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.6"},
      "metrics":[
        {"key":"aruba_cx_vsf_role","name":"Role","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.2","type":"enum"},
        {"key":"aruba_cx_vsf_status","name":"Status","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.3","type":"enum"},
        {"key":"aruba_cx_vsf_serial","name":"Serial","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.7","type":"string"},
        {"key":"aruba_cx_vsf_image","name":"Image Version","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.8","type":"string"},
        {"key":"aruba_cx_vsf_cpu","name":"CPU","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.9","type":"gauge","unit":"%"},
        {"key":"aruba_cx_vsf_memory","name":"Memory","oid":"1.3.6.1.4.1.47196.4.1.1.3.15.1.2.1.10","type":"gauge","unit":"%"}
      ]
    }
  ]$oidg$::jsonb,
  1, TRUE,
  'Aruba AOS-CX switch health: per-module CPU/memory/storage/self-test and VSF member state. Source: Aruba AOS-CX 10.12 SNMP/MIB Guide, ARUBAWIRED-SYSTEMINFO-MIB.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor=EXCLUDED.vendor, match_rules=EXCLUDED.match_rules,
  oid_groups=EXCLUDED.oid_groups, builtin=TRUE,
  description=EXCLUDED.description, updated_at=NOW();

-- Dell SmartFabric OS10. UCD-SNMP total memory is paired with OS10's
-- available-memory object; using free memory alone incorrectly counts cache.
INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'Dell SmartFabric OS10 Switch', 'Dell Technologies',
  '{"sys_object_id_prefixes":["1.3.6.1.4.1.674.11000.5000.100"],"sys_descr_regex":"(?i)SmartFabric OS10|OS10 Enterprise","default_vendor":"Dell Technologies","extract_os_version":"OS10[^0-9]*([0-9.]+)"}'::jsonb,
  $oidg$[
    {
      "key":"processor","name":"Processors","kind":"table",
      "description":"Per-processor load from HOST-RESOURCES-MIB.",
      "table":{"label_oid":"1.3.6.1.2.1.25.3.2.1.3"},
      "metrics":[{"key":"dell_cpu_load","name":"Processor Load","oid":"1.3.6.1.2.1.25.3.3.1.2","type":"gauge","unit":"%"}]
    },
    {
      "key":"memory","name":"Memory","kind":"scalar",
      "description":"Total and truly available RAM. Available memory requires OS10 10.5.3 or later; older agents retain generic HOST-RESOURCES data.",
      "metrics":[
        {"key":"dell_total_mem_kb","name":"Total RAM","oid":"1.3.6.1.4.1.2021.4.5.0","type":"gauge","unit":"KB"},
        {"key":"dell_available_mem_kb","name":"Available RAM","oid":"1.3.6.1.4.1.674.11000.5000.100.4.1.1.3.1.15.1","type":"gauge","unit":"KB"},
        {"key":"dell_free_mem_kb","name":"Free RAM (legacy diagnostic)","oid":"1.3.6.1.4.1.2021.4.6.0","type":"gauge","unit":"KB"}
      ]
    }
  ]$oidg$::jsonb,
  1, TRUE,
  'Dell SmartFabric OS10 switch health: HOST-RESOURCES CPU and cache-aware available-memory calculation. Sources: Dell OS10 10.6 User Guide and Dell KB 000270359.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor=EXCLUDED.vendor, match_rules=EXCLUDED.match_rules,
  oid_groups=EXCLUDED.oid_groups, builtin=TRUE,
  description=EXCLUDED.description, updated_at=NOW();

-- MikroTik RouterOS (official MIKROTIK-MIB release 2025-05-19). CPU and
-- memory remain in HOST-RESOURCES-MIB; this pack adds hardware/security data.
INSERT INTO device_profiles (name, vendor, match_rules, oid_groups, version, builtin, description)
VALUES (
  'MikroTik RouterOS', 'MikroTik',
  '{"sys_object_id_prefixes":["1.3.6.1.4.1.14988"],"sys_descr_regex":"(?i)RouterOS","default_vendor":"MikroTik","extract_os_version":"RouterOS ([0-9.]+)"}'::jsonb,
  $oidg$[
    {
      "key":"health","name":"Hardware Health","kind":"scalar",
      "description":"RouterBOARD temperatures, voltage/current/power, PSU and fan health. Temperature/voltage/power scalars use tenths per MIKROTIK-MIB textual conventions.",
      "metrics":[
        {"key":"mt_cpu_temp","name":"CPU Temperature","oid":"1.3.6.1.4.1.14988.1.1.3.6.0","type":"gauge","unit":"C","scale":0.1},
        {"key":"mt_board_temp","name":"Board Temperature","oid":"1.3.6.1.4.1.14988.1.1.3.7.0","type":"gauge","unit":"C","scale":0.1},
        {"key":"mt_voltage","name":"Input Voltage","oid":"1.3.6.1.4.1.14988.1.1.3.8.0","type":"gauge","unit":"V","scale":0.1},
        {"key":"mt_power","name":"Power","oid":"1.3.6.1.4.1.14988.1.1.3.12.0","type":"gauge","unit":"W","scale":0.1},
        {"key":"mt_current","name":"Current","oid":"1.3.6.1.4.1.14988.1.1.3.13.0","type":"gauge","unit":"mA"},
        {"key":"mt_psu","name":"Power Supply","oid":"1.3.6.1.4.1.14988.1.1.3.15.0","type":"enum"},
        {"key":"mt_backup_psu","name":"Backup Power Supply","oid":"1.3.6.1.4.1.14988.1.1.3.16.0","type":"enum"},
        {"key":"mt_fan1","name":"Fan 1","oid":"1.3.6.1.4.1.14988.1.1.3.17.0","type":"gauge","unit":"rpm"},
        {"key":"mt_fan2","name":"Fan 2","oid":"1.3.6.1.4.1.14988.1.1.3.18.0","type":"gauge","unit":"rpm"}
      ]
    },
    {
      "key":"poe","name":"PoE Ports","kind":"table","interval_seconds":120,
      "table":{"label_oid":"1.3.6.1.4.1.14988.1.1.15.1.1.2"},
      "metrics":[
        {"key":"mt_poe_status","name":"Status","oid":"1.3.6.1.4.1.14988.1.1.15.1.1.3","type":"enum"},
        {"key":"mt_poe_voltage","name":"Voltage","oid":"1.3.6.1.4.1.14988.1.1.15.1.1.4","type":"gauge","unit":"V","scale":0.1},
        {"key":"mt_poe_current","name":"Current","oid":"1.3.6.1.4.1.14988.1.1.15.1.1.5","type":"gauge","unit":"mA"},
        {"key":"mt_poe_power","name":"Power","oid":"1.3.6.1.4.1.14988.1.1.15.1.1.6","type":"gauge","unit":"W","scale":0.1}
      ]
    },
    {
      "key":"optics","name":"Optical Modules","kind":"table","interval_seconds":120,
      "table":{"label_oid":"1.3.6.1.4.1.14988.1.1.19.1.1.2"},
      "metrics":[
        {"key":"mt_opt_rx_loss","name":"RX Loss","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.3","type":"enum"},
        {"key":"mt_opt_tx_fault","name":"TX Fault","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.4","type":"enum"},
        {"key":"mt_opt_temp","name":"Temperature","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.6","type":"gauge","unit":"C"},
        {"key":"mt_opt_voltage","name":"Supply Voltage","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.7","type":"gauge","unit":"V","scale":0.001},
        {"key":"mt_opt_tx_dbm","name":"TX Power","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.9","type":"gauge","unit":"dBm","scale":0.001},
        {"key":"mt_opt_rx_dbm","name":"RX Power","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.10","type":"gauge","unit":"dBm","scale":0.001},
        {"key":"mt_opt_vendor","name":"Vendor","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.11","type":"string"},
        {"key":"mt_opt_serial","name":"Serial","oid":"1.3.6.1.4.1.14988.1.1.19.1.1.12","type":"string"}
      ]
    },
    {
      "key":"ipsec","name":"IPsec IKE Security Associations","kind":"table","interval_seconds":120,
      "table":{"label_oid":"1.3.6.1.4.1.14988.1.1.20.2.1.10"},
      "metrics":[
        {"key":"mt_ike_state","name":"State","oid":"1.3.6.1.4.1.14988.1.1.20.2.1.7","type":"enum"},
        {"key":"mt_ike_uptime","name":"Uptime","oid":"1.3.6.1.4.1.14988.1.1.20.2.1.8","type":"gauge","unit":"centiseconds"},
        {"key":"mt_ike_child_sa","name":"Child SAs","oid":"1.3.6.1.4.1.14988.1.1.20.2.1.11","type":"gauge","unit":"SAs"}
      ]
    }
  ]$oidg$::jsonb,
  1, TRUE,
  'RouterOS health: standard HOST-RESOURCES CPU/memory plus RouterBOARD health, PoE, optical diagnostics and IPsec IKE SAs. Source: official MikroTik RouterOS 7.21.5 MIKROTIK-MIB.'
)
ON CONFLICT (name, version) DO UPDATE SET
  vendor=EXCLUDED.vendor, match_rules=EXCLUDED.match_rules,
  oid_groups=EXCLUDED.oid_groups, builtin=TRUE,
  description=EXCLUDED.description, updated_at=NOW();

-- Correct PAN-OS CPU modeling: hrProcessorLoad is a table whose rows vary by
-- platform/dataplane count. Do not assume indexes 1 and 2 are fixed planes.
UPDATE device_profiles p
SET oid_groups = (
  SELECT jsonb_agg(
    CASE WHEN elem->>'key' = 'cpu' THEN
      '{"key":"cpu","name":"Processors","kind":"table","description":"All PAN-OS processor rows from HOST-RESOURCES-MIB; the canonical headline uses the busiest row.","table":{"label_oid":"1.3.6.1.2.1.25.3.2.1.3"},"metrics":[{"key":"pan_cpu_load","name":"Processor Load","oid":"1.3.6.1.2.1.25.3.3.1.2","type":"gauge","unit":"%"}]}'::jsonb
    ELSE elem END ORDER BY ord
  )
  FROM jsonb_array_elements(p.oid_groups) WITH ORDINALITY AS groups(elem, ord)
), updated_at = NOW()
WHERE p.name = 'Palo Alto PAN-OS Firewall' AND p.version = 1;

-- Current Cisco 64-bit memory pools (especially required by modern ASA).
UPDATE device_profiles
SET oid_groups = oid_groups || '[{"key":"memory64","name":"Enhanced 64-bit Memory Pools","kind":"table","description":"CISCO-ENHANCED-MEMPOOL-MIB 64-bit used/free values for large-memory systems.","table":{"label_oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.2"},"metrics":[{"key":"cisco_enh_mem_used","name":"Used","oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.18","type":"gauge","unit":"bytes"},{"key":"cisco_enh_mem_free","name":"Free","oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.20","type":"gauge","unit":"bytes"}]}]'::jsonb,
    updated_at = NOW()
WHERE name = 'Cisco IOS/IOS-XE Switch & Router' AND version = 1
  AND NOT oid_groups @> '[{"key":"memory64"}]'::jsonb;

UPDATE device_profiles
SET oid_groups = oid_groups || '[{"key":"memory64","name":"Enhanced 64-bit Memory Pools","kind":"table","description":"CISCO-ENHANCED-MEMPOOL-MIB; replaces deprecated 32-bit ASA memory counters.","table":{"label_oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.2"},"metrics":[{"key":"asa_enh_mem_used","name":"Used","oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.18","type":"gauge","unit":"bytes"},{"key":"asa_enh_mem_free","name":"Free","oid":"1.3.6.1.4.1.9.9.221.1.1.1.1.20","type":"gauge","unit":"bytes"}]}]'::jsonb,
    updated_at = NOW()
WHERE name = 'Cisco ASA Firewall' AND version = 1
  AND NOT oid_groups @> '[{"key":"memory64"}]'::jsonb;

-- FortiOS 8 exposes separate data-plane pressure alongside global values.
UPDATE device_profiles
SET oid_groups = oid_groups || '[{"key":"dataplane","name":"Data Plane","kind":"scalar","description":"FortiOS 8 data-plane CPU and memory pressure.","metrics":[{"key":"fgt_data_cpu","name":"Data CPU Usage","oid":"1.3.6.1.4.1.12356.101.4.1.34.0","type":"gauge","unit":"%"},{"key":"fgt_data_mem","name":"Data Memory Usage","oid":"1.3.6.1.4.1.12356.101.4.1.35.0","type":"gauge","unit":"%"},{"key":"fgt_mem_free","name":"Free Memory","oid":"1.3.6.1.4.1.12356.101.4.1.36.0","type":"gauge","unit":"KB"},{"key":"fgt_mem_freeable","name":"Freeable Memory","oid":"1.3.6.1.4.1.12356.101.4.1.37.0","type":"gauge","unit":"KB"}]}]'::jsonb,
    updated_at = NOW()
WHERE name = 'Fortinet FortiGate' AND version = 1
  AND NOT oid_groups @> '[{"key":"dataplane"}]'::jsonb;

-- Attach new profiles only where no operator-selected profile exists.
UPDATE devices d SET profile_id = p.id
FROM device_profiles p
WHERE d.profile_id IS NULL AND p.name = 'Aruba AOS-CX Switch' AND p.version = 1
  AND ltrim(COALESCE(d.sys_object_id, ''), '.') LIKE '1.3.6.1.4.1.47196.4.1.1.1%';

UPDATE devices d SET profile_id = p.id
FROM device_profiles p
WHERE d.profile_id IS NULL AND p.name = 'Dell SmartFabric OS10 Switch' AND p.version = 1
  AND ltrim(COALESCE(d.sys_object_id, ''), '.') LIKE '1.3.6.1.4.1.674.11000.5000.100%';
