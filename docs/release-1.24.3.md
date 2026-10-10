# ZenPlus 1.24.3 — accurate storage measurements and protected incident logs

An empty APT package cache no longer prevents system journal and /var/log sizes
from appearing in Settings → General → Storage. Missing optional paths count as
zero. Measurement errors appear as unavailable with a warning, and the unknown
space stays in Other rather than being presented as measured zero.

Ubuntu rsyslog's 500 MiB rotation ceiling is validated through the global
logrotate configuration, including its `su root adm` setting. Rejected edits
still restore the original configuration. The existing hourly timer checks the
ceiling; rotation has hourly overshoot and retained archives, so 500 MiB is not
a hard cap on all of /var/log.

Legacy copies of error, crash, session and backup logs are protected during
emergency purging, as their current tables already were. Routine cleanup still
expires a known-age legacy copy after its retention period; an unknown age does
not prove protected history has expired. Recovery evidence and unknown tables
remain untouched. Protected bytes are excluded when deciding whether deleting
monitoring history could actually meet the space target.

The retention forecast now identifies its historical footprint estimate and
distinguishes it from measured filesystem growth. It reminds operators to allow
space for logs, backups and merges. Independent low-space protection, bounded
ClickHouse logging and telemetry batching from 1.24.2 remain in place.

Appliance-only OTA release; installed Windows agents are preserved. No schema
migration or monitoring-data deletion is part of this change. Regression tests
use temporary filesystem fixtures and mocked databases. Runtime acceptance,
signed-package verification and rollout evidence are recorded separately.
