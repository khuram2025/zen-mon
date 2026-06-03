-- migrate-021-alert-multi-condition.sql
-- E1 (slice 1): Multi-condition alert rules. Additive + backward compatible.
--
-- A rule whose `conditions` array is non-empty uses COMPOUND evaluation: each
-- element is {metric, operator, threshold}, combined by `condition_logic`
-- (AND / OR). When `conditions` is NULL or empty, the legacy flat
-- metric/operator/threshold columns act as the single condition, so existing
-- rules keep working unchanged.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS conditions JSONB DEFAULT NULL;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS condition_logic VARCHAR(4) NOT NULL DEFAULT 'AND';
