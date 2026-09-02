-- migrate-106: good / poor sample counters per Core Web Vital in the RUM
-- 5-minute rollup, so the 30- and 90-day ranges can show the same
-- good / needs-improvement / poor distribution as the raw-event ranges.
--
-- Applied through the application ClickHouse client (DDL over HTTP), like
-- migrate-096/102. Idempotent. The columns are appended after load_samples;
-- the intake rollup INSERT relies on that order.
--
-- Rows written before this migration keep zero counters. The overview treats
-- an all-zero counter pair as "no distribution" rather than "0 % good".

ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS lcp_good  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS lcp_poor  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS inp_good  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS inp_poor  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS cls_good  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS cls_poor  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS fcp_good  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS fcp_poor  SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS ttfb_good SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS ttfb_poor SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS load_good SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
ALTER TABLE zenplus.apm_rum_metrics_5m ADD COLUMN IF NOT EXISTS load_poor SimpleAggregateFunction(sum, UInt64) DEFAULT 0;
