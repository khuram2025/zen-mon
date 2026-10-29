# Analytics durability during whole-cell power loss

The HA profile flushes inserted parts and their directories. That alone does
not flush parts produced by background merges or received from other replicas.
A simultaneous power loss in the VMware lab left empty files in merged parts
on all three replicas. Two tables could not drain their replication queues;
the admission check correctly withheld the controller endpoint. Synchronous
PostgreSQL and the other site's notifications continued to work.

The profile now sets both `min_compressed_bytes_to_fsync_after_merge` and
`min_compressed_bytes_to_fsync_after_fetch` to `1`, alongside
`fsync_after_insert=1` and `fsync_part_directory=1`. A zero threshold disables
the corresponding flush. Both configuration generation and effective settings
on existing tables must be verified during commissioning and upgrade.

This is a storage durability requirement, not a replacement for quorum writes,
independent power/storage failure domains or backup. Flush completion still
depends on the hypervisor and physical storage honoring barriers. Qualify the
exact image and storage configuration under insert, merge and fetch workloads,
including simultaneous loss of every member. Re-measure latency and throughput
with these settings; earlier benchmark numbers do not qualify the new profile.

Do not clear replication queues, discard broken parts or bypass admission to
make the site appear healthy. Preserve the stopped data and Keeper state first.
The lab retained complete constituent parts for the missing merged ranges;
operator reconstruction must validate complete, non-overlapping block ranges,
part checksums and full-row digests before a supported partition replacement.
Include every retained part in the affected partition, not just the missing
merged range. Copying reconstructed parts directly into a replica data directory
does not establish their Keeper ownership and is not a supported repair path.
That forensic repair is not a general automatic recovery contract. Where the
retained data cannot prove completeness, restore a verified backup and report
the actual data loss and recovery point.

Relevant upstream settings are documented in the
[ClickHouse MergeTree settings reference](https://clickhouse.com/docs/reference/settings/merge-tree-settings).
The installed server's `system.merge_tree_settings` descriptions provide the
version-specific semantics and effective values. Whole-cell VM acceptance and
any failed repair attempts belong in the qualification report.
