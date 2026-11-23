-- migrate-211-condition-tree.sql
-- Multi-condition alert rules, phase 1 (docs/multi-condition-rules.md).
--
-- condition_tree: a rule's conditions as a tree, {"logic": "AND"|"OR",
-- "items": [condition | {"logic", "items": [condition, ...]}]}, one level of
-- groups under the root. NULL for every existing rule: those keep the flat
-- metric/operator/threshold columns and the `conditions` array, unchanged.
-- Phase 1 stores trees on server (host_*) rules only; the flat columns mirror
-- the first condition so lists, exports and older pages keep working.
--
-- subject_type: what the rule watches ('server' in phase 1). NULL means the
-- subject follows from the metric, as before.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS condition_tree JSONB;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS subject_type VARCHAR(32);

UPDATE alert_rules SET subject_type = 'server'
 WHERE subject_type IS NULL AND metric LIKE 'host\_%';
