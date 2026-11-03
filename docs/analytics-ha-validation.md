# Replicated analytics commissioning and validation

This is a lab-qualified commissioning foundation, not a signed customer HA
release. It adds cell-local ClickHouse data replication and controller admission.
Standalone deployments retain their current store. Site A's three disposable
VMware nodes use this profile; site B has not yet been converted. One Workstation
host does not establish physical failure isolation.

## Storage and transport

The analytics_profile.py renderer uses the PostgreSQL profile's three-member
inventory. Three Keeper voters use authenticated TLS for clients and Raft peers.
A strict cell has three data replicas; an economy cell has two data replicas
and one Keeper-only witness. Analytics always requires two data acknowledgements,
including economy mode. Losing either economy data member stops protected
ingestion; analytics durability is never silently lowered to match PostgreSQL.

The tested runtime is ClickHouse 24.12.6.70. Server native/HTTP interfaces bind
loopback. Replication uses HTTPS on the inventory address, mutual certificate
validation and a distinct replication password. Keeper has a separate digest
identity, TLS client certificates, durable Raft logs and quorum reads.
Application inserts require quorum 2, synchronous insertion, fsync of parts and
directory entries, and dependent-view deduplication. Client credentials are read
from private files, never passed as command-line values.

The renderer creates a new private output directory and refuses replacement.
Credential JSON keys are application, administration, replication and keeper.
Each value must be distinct, 32–128 characters from the URL-safe token alphabet.
Provision node SAN certificates and a dedicated analytics CA in
/etc/zenplus-ha/analytics-tls. No CA key belongs on a generic appliance image.

    python3 appliance/ha/analytics_profile.py --inventory /secure/inventory.json \
      --node a1 --credentials /secure/analytics-credentials.json \
      --output /secure/new-analytics-a1

Install keeper.xml, clickhouse.xml, client.xml and database.sql under
/etc/zenplus-ha/analytics, root-owned, group zenplus, mode 0640. Directories are
root-owned 0750. Data subdirectories under /var/lib/zenplus-ha/analytics belong
to zenplus; parent ownership remains root. Install both supplied systemd units,
the pinned ClickHouse binary and controller modules including analytics_health.py.
The units do not convert an existing store or replace a standalone service.

The default 1 GiB server limit, 256 MiB query limit and two query threads are
small-lab settings, not enterprise sizing. Qualify throughput, replication
backlog, disk latency and memory before choosing production capacity.

## Data conversion

A Replicated database replicates DDL; each stored table must also use a
Replicated MergeTree-family engine. The schema planner accepts a reviewed
system.tables inventory, preserves engine-specific arguments and converts only
the top-level engine. Default replica paths include a cell, shard and table UUID.

The lab migration paused site A's consumers, exported every base table in Native
format, recorded counts and SHA-256 hashes, and retained all original directories.
It imported 20,841 exported rows from three previously independent stores into
67 schema objects. Historical aggregate tables were imported before creating
materialized views to avoid regenerating old rollups. Replacing/aggregate engines
can merge rows; exported counts are not identical physical-row-count promises.

The converter rejects unsupported engines and internally stored materialized
views. Customer conversion additionally needs overlap analysis, schema drift
handling, migration checkpoints and restore qualification. Do not run the lab
helper against a customer database. Historical SQL migrations remain unchanged;
future migrations need an HA-aware execution path before upgrades are enabled.

## Controller admission

Declare zenplus-ha-clickhouse.service in both commissioned dependency lists.
Keeper and ClickHouse are local infrastructure, not active-only workers. Before
activation and again after predecessor fencing, the controller synchronizes
database metadata and checks every local replicated table for:

* a live session, writable replica, at least two active copies and no lost parts;
* an empty local replication queue and a log pointer beyond the current log;
* a matching count of stored MergeTree tables, rejecting unreplicated tables.

Index zero can mean either an empty log or its first real entry. Admission checks
Keeper to verify emptiness. During normal operation, ordinary queue activity
does not stop a writer, but loss of replica/quorum health withdraws admission.
A shared analytics quorum failure does not permanently quarantine all PostgreSQL
members. Existing local-process failure and external-fencing rules still apply.

## VMware evidence and discovered failure

The lab passed mutual TLS membership and rejection of clients without
certificates; replication of converted schema/history; continued two-copy
ingestion with one analytics process stopped; returned-replica catch-up;
rejection of a stale candidate with fetches stopped; and refusal to acknowledge
a write with one data copy, followed by consumer withdrawal and automatic recovery.

The combined power-loss test preserved 200 acknowledged synthetic rows and the
existing login session. It exposed an **open product defect**: an earlier
timed-out insert eventually existed in the raw table but its materialized-view
result was missing. Replaying the identical block with the original deduplication
token completed the view without duplicating the raw row. This was a test-harness
retry, not yet a durable application retry service.

After that replay, a fresh combined VM power-loss run recovered through the fixed
HTTPS address in 52.17 seconds, retained the 200 acknowledged markers, matched
raw/derived totals and reused the login session. The former node rejoined. This
does not close the durable-replay defect. The focused source suite passed 60 tests.

A raw-table presence check is therefore insufficient proof that ingestion and
dependent views completed. Persist pending batches and stable tokens across
controller crashes, preserve identity across regrouping, and qualify the finite
deduplication window. Existing in-memory buffers and raw-presence verification
are release gates. An uncertain insert must not be reported as definitively absent.

The host telemetry batcher now refuses presence-based acknowledgement for
replicated tables and tables with dependent views. Unknown metadata also refuses
that shortcut. A lost-response injection against the replicated lab store
verified that an existing raw row did not cause a false acknowledgement.
The guard prevents premature success; it does not persist grouped batch identity
across crashes or close the durable-replay gate. The expanded focused suite
passed 78 tests.

Runtime commissioning also caught and corrected missing Keeper storage roots,
a required default ClickHouse settings profile, explicit-engine-argument
restrictions and the HTTPS-specific advertised host setting. These findings
were observed on the pinned runtime, not inferred from configuration tests.

The primary was never a fault target or modified by this work. A separately
observed primary reboot at 15:32 UTC means this report does not claim uninterrupted
primary process uptime.

## Remaining release gates

Durable producer replay, HA-aware migrations, site B analytics commissioning,
artifact replication, backup/restore, sustained mixed workloads, certificate
rotation, clean-image commissioning, two independent imports and exact signed
artifact acceptance remain required. Cluster OTA stays disabled. Retain all
five CI checks and existing release-signature verification.

## Component references

* [ClickHouse replication and quorum](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/replication)
* [Replicated database metadata](https://clickhouse.com/docs/reference/engines/database-engines/replicated)
* [Keeper consensus and quorum reads](https://clickhouse.com/docs/guides/oss/deployment-and-scaling/keeper)
* [Pinned HTTPS host selection](https://github.com/ClickHouse/ClickHouse/blob/v24.12.6.70-stable/programs/server/Server.cpp)

Component documentation does not replace implementation evidence or establish
an SLA. Retain failed attempts alongside corrected acceptance results.
