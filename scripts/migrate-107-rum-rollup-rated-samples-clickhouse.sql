-- migrate-107: per-vital "rated" sample counters in the RUM 5-minute rollup.
-- Companion to migrate-106. good/poor shares divide by <vital>_rated (samples
-- written together with band counters), so buckets from before migrate-106
-- show p75 alone instead of a false 0 % good. Applied through the app
-- ClickHouse client; idempotent; columns append after load_poor.
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS lcp_rated  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS inp_rated  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS cls_rated  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS fcp_rated  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS ttfb_rated SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS load_rated SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
