-- NSX-ALB-style request breakdown for browser RUM.
--
-- Phase timings come from the Navigation/Resource Timing APIs (network split),
-- server_ms/db_ms from Server-Timing response headers (application execution
-- split), and the client context columns from the browser environment.  All
-- phases are milliseconds.  has_timing marks events whose SDK captured a
-- phase breakdown; has_server_timing marks a server-declared execution split.

ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS redirect_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS dns_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS connect_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS tls_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS wait_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS download_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS blocked_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS processing_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS server_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS db_ms Float64 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_timing UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS has_server_timing UInt8 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS protocol LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS connection_type LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS connection_rtt_ms Float32 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS connection_downlink Float32 DEFAULT 0;
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS language LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS timezone LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS screen_res LowCardinality(String) DEFAULT '';
ALTER TABLE zenplus.apm_rum_events ADD COLUMN IF NOT EXISTS viewport LowCardinality(String) DEFAULT '';
