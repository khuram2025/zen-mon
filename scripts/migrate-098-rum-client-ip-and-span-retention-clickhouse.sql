-- RUM client-IP capture and backend-span retention alignment.
--
-- 1. client_ip lets "Client Analytics" surface the originating address for a
--    session (populated from the X-Forwarded-For / X-Real-IP hop at intake).
--
-- 2. Backend spans were retained for 7 days while RUM raw events are kept for
--    14, so any RUM session/error/resource older than a week linked to a trace
--    that ClickHouse had already dropped -- the "Trace not found" the trace
--    waterfall showed. Extend span retention to 14 days so RUM -> trace
--    correlation resolves for the entire RUM raw-retention window.
--    materialize_ttl_after_modify = 0 changes the retention metadata without
--    forcing a full per-row TTL re-materialisation (which is unnecessary under
--    ttl_only_drop_parts = 1, where whole partitions drop once fully expired).

ALTER TABLE zenplus.apm_rum_events
    ADD COLUMN IF NOT EXISTS client_ip String DEFAULT '' CODEC(ZSTD(1));

ALTER TABLE zenplus.apm_spans
    MODIFY TTL toDateTime(timestamp) + toIntervalDay(14)
    SETTINGS materialize_ttl_after_modify = 0;
