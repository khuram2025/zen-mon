-- Per-probe connection metadata; old probes retain an empty value.
ALTER TABLE zenplus.service_metrics ADD COLUMN IF NOT EXISTS network_diagnostics String DEFAULT '';
