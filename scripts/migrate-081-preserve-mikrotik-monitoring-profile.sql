-- migrate-081: Repair the MikroTik monitoring pack after legacy bootstrap.
--
-- Appliances installed before the database-backed monitoring packs retain
-- /opt/zenplus/data/profiles/mikrotik.json. That classification-only JSON has
-- no oid_groups and older pollers upserted it on every startup, replacing the
-- monitoring groups introduced by migrate-080 with an empty array. The poller
-- now treats an existing database row as authoritative; this replay-safe
-- update repairs appliances on which the old startup behavior already ran.

UPDATE device_profiles
SET vendor = 'MikroTik',
    match_rules = '{"sys_object_id_prefixes":["1.3.6.1.4.1.14988"],"sys_descr_regex":"(?i)RouterOS|MikroTik","default_vendor":"MikroTik","extract_os_version":"RouterOS\\s+([0-9A-Za-z.]+)"}'::jsonb,
    oid_groups = $oidg$[
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
    builtin = TRUE,
    description = 'RouterOS health: standard HOST-RESOURCES CPU/memory plus RouterBOARD health, PoE, optical diagnostics and IPsec IKE SAs. Source: official MikroTik RouterOS 7.21.5 MIKROTIK-MIB.',
    updated_at = NOW()
WHERE name = 'MikroTik RouterOS' AND version = 1;
