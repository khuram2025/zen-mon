-- migrate-171: Controller-derived readings for managed FortiSwitches/FortiAPs
--
-- Children promoted from a FortiGate (migrate-069) had blank CPU, memory and
-- uptime because nothing carried the controller's per-switch/per-AP readings
-- to the child. The readings were already collected on the FortiGate's own
-- template rows; this migration adds what the child pages need on top:
--
--  * device_template_values.instance_oid — the raw OID index suffix of a
--    table row. The poller hashes composite indexes into `instance`, which
--    loses the AP serial inside a radio row's index ({vdom, wtpId, radioId})
--    and the switch inside a port row's index. The raw suffix keeps rows
--    relatable and lets the API decode serials and client MACs.
--  * access_points / fortiswitch groups gain boot uptime, IP, MAC, serial,
--    firmware and traffic columns, and their children mapping names which
--    columns are the child's CPU, memory, uptime, clients and serial.
--  * Five controller-source groups (display = "children"): FortiSwitch ports
--    (fgSwPortTable), FortiAP radios (fgWcWtpSessionRadioTable), SSIDs per
--    radio (fgWcWtpSessionVapTable), radio profile band/width
--    (fgWcWtpProfileRadioTable) and wireless clients (fgWcStaTable). They are
--    shown on the child devices, never on the FortiGate's own page.
--    Client identity columns are strings (Postgres only) so per-client rows
--    never become ClickHouse series.
--
-- Everything is read-only FORTINET-FORTIGATE-MIB on the existing controller
-- poll: no child is pinged or polled directly and no license seat is used.
-- Guarded per metric/group key, so operator edits and replays are kept.

ALTER TABLE device_template_values ADD COLUMN IF NOT EXISTS instance_oid TEXT;

-- ─── existing child groups: extra columns + children mapping keys ───

UPDATE device_profiles p
SET oid_groups = (
        SELECT jsonb_agg(
            CASE
                WHEN g.value->>'key' IN ('access_points', 'fortiswitch') THEN
                    jsonb_set(
                        CASE WHEN g.value ? 'children' THEN
                            jsonb_set(g.value, '{children}',
                                (CASE g.value->>'key'
                                    WHEN 'access_points' THEN
                                        '{"os_version_key":"fgt_ap_sw_version","ip_key":"fgt_ap_ip",
                                          "mac_key":"fgt_ap_mac",
                                          "serial_from_index":{"layout":["int","str"],"part":1},
                                          "cpu_key":"fgt_ap_cpu","memory_key":"fgt_ap_mem",
                                          "uptime_key":"fgt_ap_uptime","clients_key":"fgt_ap_clients",
                                          "state_key":"fgt_ap_status"}'::jsonb
                                    ELSE
                                        '{"serial_key":"fgt_sw_serial","ip_key":"fgt_sw_ip",
                                          "cpu_key":"fgt_sw_cpu","memory_key":"fgt_sw_mem",
                                          "state_key":"fgt_sw_status","auth_key":"fgt_sw_auth"}'::jsonb
                                 END) || (g.value->'children'))
                        ELSE g.value END,
                        '{metrics}',
                        (g.value->'metrics') || COALESCE((
                            SELECT jsonb_agg(m.value ORDER BY m.ordinality)
                            FROM jsonb_array_elements(
                                CASE g.value->>'key'
                                    WHEN 'access_points' THEN $ap$ [
                                        {"key":"fgt_ap_uptime","name":"Uptime (since boot)","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.8","type":"gauge","unit":"s","scale":0.01},
                                        {"key":"fgt_ap_session_uptime","name":"CAPWAP Session Uptime","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.10","type":"gauge","unit":"s","scale":0.01},
                                        {"key":"fgt_ap_sw_version","name":"Firmware","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.14","type":"string"},
                                        {"key":"fgt_ap_profile","name":"Profile","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.11","type":"string"},
                                        {"key":"fgt_ap_ip","name":"IP Address","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.3","type":"string","format":"hex"},
                                        {"key":"fgt_ap_mac","name":"Base MAC","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.6","type":"string","format":"hex"},
                                        {"key":"fgt_ap_rx_bps","name":"Traffic In","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.18","type":"counter","unit":"bps","scale":8},
                                        {"key":"fgt_ap_tx_bps","name":"Traffic Out","oid":"1.3.6.1.4.1.12356.101.14.4.4.1.19","type":"counter","unit":"bps","scale":8}
                                    ] $ap$::jsonb
                                    ELSE $sw$ [
                                        {"key":"fgt_sw_serial","name":"Serial","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.3","type":"string"},
                                        {"key":"fgt_sw_ip","name":"IP Address","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.9","type":"string"},
                                        {"key":"fgt_sw_join_time","name":"Joined FortiLink","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.8","type":"string"},
                                        {"key":"fgt_sw_flag","name":"Transaction Flags","oid":"1.3.6.1.4.1.12356.101.24.1.1.1.10","type":"string"}
                                    ] $sw$::jsonb
                                END) WITH ORDINALITY AS m(value, ordinality)
                            WHERE NOT (g.value->'metrics') @> jsonb_build_array(jsonb_build_object('key', m.value->>'key'))
                        ), '[]'::jsonb))
                ELSE g.value
            END
            ORDER BY g.ordinality)
        FROM jsonb_array_elements(p.oid_groups) WITH ORDINALITY AS g(value, ordinality)
    ),
    updated_at = NOW()
WHERE p.builtin = TRUE
  AND p.name = 'Fortinet FortiGate'
  AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(p.oid_groups) AS g(value)
        WHERE (g.value->>'key' = 'access_points'
               AND NOT (g.value->'metrics') @> '[{"key":"fgt_ap_uptime"}]'::jsonb)
           OR (g.value->>'key' = 'fortiswitch'
               AND NOT (g.value->'metrics') @> '[{"key":"fgt_sw_serial"}]'::jsonb)
  );

-- ─── controller-source groups for the child pages ───

UPDATE device_profiles p
SET oid_groups = p.oid_groups || COALESCE((
        SELECT jsonb_agg(ng.value ORDER BY ng.ordinality)
        FROM jsonb_array_elements($groups$ [
          {
            "key": "fsw_ports", "name": "FortiSwitch Ports", "kind": "table", "display": "children",
            "interval_seconds": 300,
            "description": "Per-port link, speed, VLAN and PoE of FortiLink-managed switches (fgSwPortTable, FortiOS 6.4.2+). Traffic, errors, LAG and LLDP are not in this MIB.",
            "table": {"label_oid": "1.3.6.1.4.1.12356.101.24.2.1.1.5", "max_rows": 4000},
            "belongs_to": {"group": "fortiswitch", "by": "serial", "metric": "fgt_swp_serial"},
            "metrics": [
              {"key":"fgt_swp_serial","name":"Switch Serial","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.4","type":"string"},
              {"key":"fgt_swp_status","name":"Link","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.6","type":"enum",
               "labels":{"0":{"text":"Down","sev":"info"},"1":{"text":"Up","sev":"ok"}}},
              {"key":"fgt_swp_speed","name":"Speed / Duplex","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.7","type":"string"},
              {"key":"fgt_swp_native_vlan","name":"Native VLAN","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.8","type":"string"},
              {"key":"fgt_swp_allowed_vlans","name":"Allowed VLANs","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.9","type":"string"},
              {"key":"fgt_swp_untagged_vlans","name":"Untagged VLANs","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.10","type":"string"},
              {"key":"fgt_swp_poe_capable","name":"PoE Capable","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.11","type":"string"},
              {"key":"fgt_swp_poe_status","name":"PoE Enabled","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.12","type":"string"},
              {"key":"fgt_swp_poe_state","name":"PoE State","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.13","type":"string"},
              {"key":"fgt_swp_poe_power","name":"PoE Draw","oid":"1.3.6.1.4.1.12356.101.24.2.1.1.14","type":"gauge","unit":"W","scale":0.001}
            ]
          },
          {
            "key": "fap_radios", "name": "FortiAP Radios", "kind": "table", "display": "children",
            "interval_seconds": 120,
            "description": "Per-radio mode, channel, transmit power and clients (fgWcWtpSessionRadioTable).",
            "table": {"max_rows": 1000},
            "belongs_to": {"group": "access_points", "by": "index_prefix", "layout": ["int","str","int"]},
            "metrics": [
              {"key":"fgt_apr_mode","name":"Mode","oid":"1.3.6.1.4.1.12356.101.14.4.5.1.3","type":"enum",
               "labels":{"0":{"text":"Other","sev":"info"},"1":{"text":"Not present","sev":"info"},"2":{"text":"Disabled","sev":"info"},"3":{"text":"Access point","sev":"ok"},"4":{"text":"Monitor","sev":"info"},"5":{"text":"Sniffer","sev":"info"}}},
              {"key":"fgt_apr_bssid","name":"BSSID","oid":"1.3.6.1.4.1.12356.101.14.4.5.1.4","type":"string","format":"hex"},
              {"key":"fgt_apr_channel","name":"Channel","oid":"1.3.6.1.4.1.12356.101.14.4.5.1.7","type":"gauge"},
              {"key":"fgt_apr_power","name":"Transmit Power","oid":"1.3.6.1.4.1.12356.101.14.4.5.1.8","type":"gauge","unit":"dBm"},
              {"key":"fgt_apr_clients","name":"Clients","oid":"1.3.6.1.4.1.12356.101.14.4.5.1.9","type":"gauge","unit":"clients"}
            ]
          },
          {
            "key": "fap_ssids", "name": "FortiAP SSIDs", "kind": "table", "display": "children",
            "interval_seconds": 120,
            "description": "Clients and traffic per AP, radio and SSID (fgWcWtpSessionVapTable).",
            "table": {"label_oid": "1.3.6.1.4.1.12356.101.14.4.6.1.3", "max_rows": 3000},
            "belongs_to": {"group": "access_points", "by": "index_prefix", "layout": ["int","str","int","int"]},
            "metrics": [
              {"key":"fgt_vap_clients","name":"Clients","oid":"1.3.6.1.4.1.12356.101.14.4.6.1.4","type":"gauge","unit":"clients"},
              {"key":"fgt_vap_rx_bps","name":"Traffic In","oid":"1.3.6.1.4.1.12356.101.14.4.6.1.5","type":"counter","unit":"bps","scale":8},
              {"key":"fgt_vap_tx_bps","name":"Traffic Out","oid":"1.3.6.1.4.1.12356.101.14.4.6.1.6","type":"counter","unit":"bps","scale":8}
            ]
          },
          {
            "key": "fap_radio_profiles", "name": "FortiAP Radio Profiles", "kind": "table", "display": "children",
            "interval_seconds": 900,
            "description": "Configured band, channel width and channels per AP profile radio (fgWcWtpProfileRadioTable).",
            "table": {"max_rows": 500},
            "belongs_to": {"group": "access_points", "by": "profile", "metric": "fgt_ap_profile", "layout": ["int","str","int"]},
            "metrics": [
              {"key":"fgt_apf_band","name":"Band","oid":"1.3.6.1.4.1.12356.101.14.4.2.1.11","type":"string"},
              {"key":"fgt_apf_width","name":"Channel Width","oid":"1.3.6.1.4.1.12356.101.14.4.2.1.20","type":"string"},
              {"key":"fgt_apf_channels","name":"Channels","oid":"1.3.6.1.4.1.12356.101.14.4.2.1.13","type":"string"}
            ]
          },
          {
            "key": "fap_clients", "name": "Wireless Clients", "kind": "table", "display": "children",
            "interval_seconds": 300,
            "description": "Associated wireless stations (fgWcStaTable). Identity columns need Device Tracker permissions to view.",
            "table": {"max_rows": 2000},
            "belongs_to": {"group": "access_points", "by": "serial", "metric": "fgt_sta_wtp", "layout": ["int","int","mac"]},
            "metrics": [
              {"key":"fgt_sta_wtp","name":"AP","oid":"1.3.6.1.4.1.12356.101.14.5.1.3","type":"string"},
              {"key":"fgt_sta_radio","name":"Radio","oid":"1.3.6.1.4.1.12356.101.14.5.1.4","type":"string"},
              {"key":"fgt_sta_vlan","name":"VLAN","oid":"1.3.6.1.4.1.12356.101.14.5.1.5","type":"string"},
              {"key":"fgt_sta_ip","name":"IP Address","oid":"1.3.6.1.4.1.12356.101.14.5.1.7","type":"string","format":"hex"},
              {"key":"fgt_sta_host","name":"Host","oid":"1.3.6.1.4.1.12356.101.14.5.1.9","type":"string"},
              {"key":"fgt_sta_user","name":"User","oid":"1.3.6.1.4.1.12356.101.14.5.1.10","type":"string"},
              {"key":"fgt_sta_signal","name":"Signal","oid":"1.3.6.1.4.1.12356.101.14.5.1.12","type":"string"},
              {"key":"fgt_sta_noise","name":"Noise","oid":"1.3.6.1.4.1.12356.101.14.5.1.13","type":"string"},
              {"key":"fgt_sta_bw_tx","name":"TX Rate","oid":"1.3.6.1.4.1.12356.101.14.5.1.15","type":"string"},
              {"key":"fgt_sta_bw_rx","name":"RX Rate","oid":"1.3.6.1.4.1.12356.101.14.5.1.16","type":"string"},
              {"key":"fgt_sta_channel","name":"Channel","oid":"1.3.6.1.4.1.12356.101.14.5.1.17","type":"string"}
            ]
          }
        ] $groups$::jsonb) WITH ORDINALITY AS ng(value, ordinality)
        WHERE NOT p.oid_groups @> jsonb_build_array(jsonb_build_object('key', ng.value->>'key'))
    ), '[]'::jsonb),
    updated_at = NOW()
WHERE p.builtin = TRUE
  AND p.name = 'Fortinet FortiGate'
  AND jsonb_typeof(p.oid_groups) = 'array'
  AND NOT p.oid_groups @> '[{"key":"fap_clients"}]'::jsonb;
