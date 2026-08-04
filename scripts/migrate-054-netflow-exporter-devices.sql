-- migrate-054-netflow-exporter-devices.sql
--
-- Map a NetFlow exporter IP to the monitored device that owns it.
--
-- Interface names on the NetFlow pages are resolved by matching the flow's
-- exporter_ip against devices.ip_address and then looking up ifIndex in
-- device_interfaces. Routers commonly source flow export from a loopback or
-- WAN interface that is NOT their SNMP management address, so that match
-- fails and the UI falls back to a raw "ifIndex 16" label even though the
-- device is monitored and its interfaces are fully named.
--
-- device_interfaces holds no IP addresses, so there is nothing to auto-match
-- on; this table lets an operator state the relationship explicitly. One
-- exporter IP maps to exactly one device; a device may export from several
-- addresses.

CREATE TABLE IF NOT EXISTS netflow_exporter_devices (
    exporter_ip INET PRIMARY KEY,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    note        TEXT,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_netflow_exporter_devices_device
    ON netflow_exporter_devices (device_id);

COMMENT ON TABLE netflow_exporter_devices IS
    'Maps a NetFlow exporter source IP to the monitored device that owns it, so flow ifIndex values resolve to real interface names when the exporter IP differs from the SNMP management IP.';
