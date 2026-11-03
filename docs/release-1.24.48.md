# ZenPlus 1.24.48

Fix the synthetic scenario editor's notification channel selector when opened
directly. Display loading, empty and retryable error states.

Add Synthetic Scenario Down and SLO Burn to Alert Rules, with All scenarios /
All SLOs and individual scopes. Synthetic notifications follow the configured
retries and include scenario name, failed step and error. Track each entity's
incident independently, deduplicate repeated failures, and support recovery,
schedules, cooldown, priorities and escalation through the common dispatcher.
Matching central rules take precedence over local APM channel settings.

Serialize manual and scheduled synthetic runs across workers. Combine SLO burn
tiers into one central incident; missing traffic cannot count as recovery.
Preserve existing APM RED/RUM thresholds, regional timestamps, restart alert
protection and manual update installation controls.

No schema migration. Appliance-only release requiring the signed 1.24.36 bridge;
existing Windows agent packages are preserved.
