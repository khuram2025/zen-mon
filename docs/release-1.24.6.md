# Release 1.24.6

Flow-history reconciliation now shares a completion schedule across API workers, preventing repeated simultaneous scans of the same history. A session advisory lock coordinates the census while the existing short per-hour locks continue to protect repairs. Failed passes retry shortly without publishing a successful completion. Startup checks still run after a worker restart or restore; a completion from before that worker started cannot defer them.

An exact census cache reuses unchanged raw-data partitions. Inserts, merges, mutations, partition deletion/replacement, or schema changes invalidate affected results. Changed partitions are queried in bounded batches, and partitions changing during measurement are checked again before their results can be cached. The stored cache is capped at 2 MiB; evicting an entry causes a later rescan. Reuse includes the oldest-record and sampling-repair checks, removing their repeated raw scans as well.

Retention floors, late-flow handling, sampling correction, per-hour revalidation, and protected historical rollups retain their existing behavior. This release does not add a telemetry-table migration or remove monitoring history.

Validation includes disposable database comparisons against the original census, mutation and late-arrival tests, received hours crossing timestamp partitions, the existing full rollup suite, and real PostgreSQL worker coordination and cancellation checks. On a ten-million-record, ten-day fixture, unchanged warm passes read zero raw records; a change to one day rereads only that day. Cold passes still perform a full exact measurement. Appliance-specific acceptance and release evidence are recorded separately.
