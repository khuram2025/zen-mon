# ZenPlus 1.24.4 — install the rsyslog size ceiling successfully

Production acceptance of 1.24.3 confirmed that its storage reporting, incident-log
protection, installed signed files and all 51 monitoring tables were healthy.
It also found the rsyslog size ceiling was still rejected: the temporary backup
inside logrotate.d was included as a second configuration, causing duplicate log
declarations. A dot prefix does not exclude a file from logrotate's include.

The installer now keeps its rollback copy in a securely created temporary file
outside the include directory. It validates the complete global configuration,
retains the 500 MiB ceiling on success, and restores the original on failure.
The hourly rotation timer remains in use; this is a rotation threshold with
hourly overshoot and retained archives, not a hard cap on all host logs.

A real-parser regression uses the actual setup stanza and an included directory.
Its negative control reproduces rejection of the old backup location. Existing
rollback and idempotence tests still apply. This appliance-only update carries
the 1.24.3 storage corrections and adds no schema migration.
