-- Migration 129: per-service availability objectives and business mapping.
--
-- Availability reports judged every service against a hardcoded 99.9%
-- target and could not tell a critical customer-facing service from a lab
-- probe. These columns hold the configured target, business criticality and
-- owner. NULL means "not configured": reports fall back to the global policy
-- default (system_settings key 'availability.default_target_pct', written by
-- PUT /reports/availability-policy) and then to 99.9, and treat the service
-- as unmapped for customer-impact purposes. Idempotent.

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS sla_target_pct NUMERIC(6,3)
    CONSTRAINT service_checks_sla_target_pct_check
    CHECK (sla_target_pct IS NULL OR (sla_target_pct > 0 AND sla_target_pct <= 100));

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS criticality VARCHAR(16)
    CONSTRAINT service_checks_criticality_check
    CHECK (criticality IS NULL OR criticality IN ('critical', 'high', 'medium', 'low'));

ALTER TABLE service_checks ADD COLUMN IF NOT EXISTS business_owner VARCHAR(255);
