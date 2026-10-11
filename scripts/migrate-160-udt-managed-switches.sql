-- UDT controller observations are separate from opt-in device promotion.
-- No monitored devices, permissions or historical observations are removed.
BEGIN;
ALTER TABLE udt_device_settings ADD COLUMN IF NOT EXISTS ncm_credential_id uuid REFERENCES ncm_credentials(id) ON DELETE SET NULL;
CREATE TABLE IF NOT EXISTS udt_managed_switches (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 controller_id uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
 serial varchar(128) NOT NULL,
 hostname varchar(255) NOT NULL,
 instance varchar(160) NOT NULL,
 status varchar(20) NOT NULL DEFAULT 'unknown',
 last_seen timestamptz NOT NULL DEFAULT NOW(),
 UNIQUE(controller_id,serial)
);
CREATE TABLE IF NOT EXISTS udt_managed_ports (
 switch_id uuid NOT NULL REFERENCES udt_managed_switches(id) ON DELETE CASCADE,
 port_number integer NOT NULL CHECK(port_number>0),
 if_name varchar(255) NOT NULL,
 oper_status varchar(20) NOT NULL DEFAULT 'unknown',
 pvid integer,
 last_seen timestamptz NOT NULL DEFAULT NOW(),
 PRIMARY KEY(switch_id,port_number)
);
CREATE TABLE IF NOT EXISTS udt_managed_locations (
 id bigserial PRIMARY KEY,
 endpoint_id uuid NOT NULL REFERENCES udt_endpoints(id) ON DELETE CASCADE,
 switch_id uuid NOT NULL REFERENCES udt_managed_switches(id) ON DELETE CASCADE,
 port_number integer NOT NULL,
 vlan_id integer,
 is_direct boolean NOT NULL DEFAULT false,
 active boolean NOT NULL DEFAULT true,
 first_seen timestamptz NOT NULL DEFAULT NOW(),
 last_seen timestamptz NOT NULL DEFAULT NOW(),
 closed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS udt_managed_locations_current ON udt_managed_locations(endpoint_id,switch_id,port_number,COALESCE(vlan_id,-1)) WHERE active;
CREATE INDEX IF NOT EXISTS udt_managed_locations_endpoint ON udt_managed_locations(endpoint_id,last_seen DESC);
CREATE INDEX IF NOT EXISTS udt_managed_locations_port ON udt_managed_locations(switch_id,port_number) WHERE active;
CREATE INDEX IF NOT EXISTS udt_managed_locations_stale ON udt_managed_locations(last_seen) WHERE active;
CREATE TABLE IF NOT EXISTS udt_gateway_collection (
 device_id uuid PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
 last_poll_at timestamptz,
 last_success_at timestamptz,
 status varchar(20) NOT NULL DEFAULT 'unknown',
 message varchar(500),
 switch_count integer NOT NULL DEFAULT 0,
 port_count integer NOT NULL DEFAULT 0,
 mac_count integer NOT NULL DEFAULT 0,
 arp_count integer NOT NULL DEFAULT 0
);
CREATE OR REPLACE VIEW udt_switch_inventory AS
 SELECT id,hostname,location,false AS managed,id AS controller_id FROM devices
 UNION ALL
 SELECT s.id,s.hostname,d.location,true,s.controller_id FROM udt_managed_switches s JOIN devices d ON d.id=s.controller_id;
CREATE OR REPLACE VIEW udt_observed_locations AS
 SELECT id,endpoint_id,device_id,if_index,vlan_id,is_direct,active,first_seen,last_seen,closed_at,false AS managed FROM udt_endpoint_locations
 UNION ALL
 SELECT -id,endpoint_id,switch_id,port_number,vlan_id,is_direct,active,first_seen,last_seen,closed_at,true AS managed FROM udt_managed_locations;
CREATE OR REPLACE VIEW udt_switch_ports AS
 SELECT di.device_id,di.if_index,di.if_name,di.if_descr,di.if_alias,di.if_type,di.admin_status,di.oper_status,di.if_speed,
        ps.is_uplink,ps.uplink_reason,ps.uplink_override,ps.monitored,ps.mac_count,ps.vlan_ids,ps.pvid,ps.last_endpoint_seen,false AS managed,ps.updated_at AS observed_at
 FROM device_interfaces di LEFT JOIN udt_port_state ps ON ps.device_id=di.device_id AND ps.if_index=di.if_index
 WHERE EXISTS(SELECT 1 FROM udt_port_state p WHERE p.device_id=di.device_id)
 UNION ALL
 SELECT p.switch_id,p.port_number,p.if_name,NULL::varchar,NULL::varchar,6,NULL::varchar,
        CASE WHEN p.last_seen < NOW()-INTERVAL '15 minutes' THEN 'unknown' ELSE p.oper_status END,NULL::bigint,
        NULL::boolean,NULL::varchar,NULL::varchar,true,
        (SELECT count(DISTINCT l.endpoint_id)::integer FROM udt_managed_locations l WHERE l.switch_id=p.switch_id AND l.port_number=p.port_number AND l.active AND l.last_seen>NOW()-INTERVAL '15 minutes'),
        CASE WHEN p.pvid IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(p.pvid) END,p.pvid,
        (SELECT max(l.last_seen) FROM udt_managed_locations l WHERE l.switch_id=p.switch_id AND l.port_number=p.port_number),true,p.last_seen
 FROM udt_managed_ports p;
GRANT SELECT,INSERT,UPDATE,DELETE ON udt_managed_switches,udt_managed_ports,udt_managed_locations,udt_gateway_collection TO zenplus;
GRANT USAGE,SELECT ON SEQUENCE udt_managed_locations_id_seq TO zenplus;
GRANT SELECT ON udt_switch_inventory,udt_switch_ports,udt_observed_locations TO zenplus;
COMMIT;
