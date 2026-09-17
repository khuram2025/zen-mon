-- Preserve IPv6 trap sources without rewriting the existing IPv4 sorting key.
ALTER TABLE zenplus.snmp_traps
    ADD COLUMN IF NOT EXISTS source_ip_text String DEFAULT toString(source_ip);
ALTER TABLE zenplus.snmp_traps
    ADD COLUMN IF NOT EXISTS event_id UUID DEFAULT generateUUIDv4();
