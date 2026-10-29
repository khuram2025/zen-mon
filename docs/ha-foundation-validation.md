# HA foundation implementation and validation

Date: 29 October 2026. Baseline: `7b7d64e018d438d3a0eaeedac0efabaf27a4d345` (controller release 1.24.29). This change is the first implementation slice of the approved HA and multisite design. It does **not** enable controller failover, sensor pools, autonomous notification sites or a new customer appliance release.

## Implemented behavior

* Sensor events with an `idempotency_key` reserve the existing ingest ledger. Event history, command completion and ledger completion commit together in PostgreSQL. A repeated completed batch returns the stored acceptance count without inserting history again. Reusing its key with different content returns 409. Empty keyed batches also complete atomically.
* Legacy event clients without a key remain accepted and return `idempotent: false`. Current WAL-based clients already send keys. This is batch-level deduplication within the existing seven-day ledger retention, not permanent event-ID deduplication across differently grouped batches.
* New spool manifests cap events at 200 items and ordinary results at 500. A conservative WAL-frame byte budget reserves 1 KiB for the HTTP envelope below an 8 MiB target. Frames contain the JSON-encoded payload plus metadata, avoiding a payload scan when selecting a batch. Existing in-flight manifests retain their original membership and key across restarts or changed limits: an earlier request may have committed before its ACK was lost.
* A single oversized record still reaches the existing explicit rejection/drop-accounting path. The byte budget does not introduce negotiated limits, compressed-body limits, reserved critical-event capacity or a durable quarantine. Those remain separate work.
* Nine jobs formerly sharing four integer advisory-lock keys now use a named registry with stable signed 64-bit SHA-256-derived keys. Independent jobs no longer suppress each other. Existing transaction/session lock lifetimes are preserved. Dedicated session locks still require a direct or session-pooled PostgreSQL connection.

These changes do not fence a paused controller, guarantee exactly-once external notifications, fix ClickHouse/PostgreSQL atomicity for metric ingestion, or authorize historical results after sensor reassignment.

## Reproducible test tools

`scripts/dev/run-ha-foundation-tests.sh` creates a network namespace with only loopback, then runs as the non-root builder. Dependencies must already be installed/cached. Set `HA_PYTHON` to a dedicated virtualenv and run from the repository root. The script verifies isolation, uses private PostgreSQL fixtures, enables the Go race detector and disables Go module network downloads. Production service URLs point at closed loopback ports. It refuses an ordinary network namespace.

`scripts/dev/run-ha-data-fixtures.py` runs inside the same type of namespace with a checksum-verified ClickHouse binary and PostgreSQL server tools. It creates its own servers and data directories, exposes them only to the isolated loopback network, runs the opt-in NetFlow and Device Tracker tests, and stops/removes its own fixtures. It never selects an installed database. Example invocation inside the namespace:

```sh
python scripts/dev/run-ha-data-fixtures.py --clickhouse /path/to/verified/clickhouse
```

The October run used official ClickHouse 24.12.6.70, downloaded from `https://packages.clickhouse.com/tgz/stable/` and verified against its published SHA-512 file. This tests that explicit version, not every supported ClickHouse release. The NetFlow test fixture now disables TTL in its private fixed-date arithmetic/replay tables; the retention-edge test installs its own five-day TTL and uses current-relative timestamps. No application retention policy changed.

`scripts/dev/ha-sensor-fixture.py` is a loopback-only synthetic HTTPS controller used with the real sensor executable in a disposable VM. It supports durable synthetic receipts, intentionally losing an ACK, checkpointing checksummed WAL payloads and reconciling observation hashes. Its initial identity is preseeded: this test does not qualify real enrollment, authorization, CA lifecycle or the production API. The real event endpoint is independently exercised by private PostgreSQL tests. Never publish this fixture VM, its identity, TLS private key or state as a customer image.

## Evidence recorded in the October lab

* Go sensor/spool suites passed with `-race`; the complete poller Go suite also passed.
* Targeted Python regression run: 161 passed, 12 skipped. The skipped opt-in data cases were subsequently exercised by the separate private-server run: 46 passed, zero skipped.
* Strict dashboard build passed; route smoke verified 49 required routes; offline-asset validation passed.
* A fresh VMware full clone of the clean test baseline ran with its NAT adapter removed, one host-only interface and no default route. The original primary VM was not used for failure injection.
* With the synthetic controller stopped, monitoring continued from cached configuration and accumulated a WAL backlog. VMware hard power-off/restart preserved all 14 checkpointed observations. The sensor loaded cached configuration while the controller remained unavailable after boot. After restoration, reconciliation found zero missing checkpointed observations and zero reported drops.
* Intentionally dropping a response after the synthetic controller durably committed caused one duplicate request; its retry preserved the batch key and produced no missing checkpointed observations.
* VMware suspend/resume restored sensor operation. This is a sensor process recovery check, **not** a split-brain/fencing test. A later controller outage reconciled all 27 checkpointed observations with zero reported drops.
* A continuously running sensor can report queue depth 0 or 1 while new probes arrive; that snapshot is not a stranded-backlog assertion. Reconciliation uses the recorded observation hashes.

The workstation is a single physical failure domain. These results do not prove independent-host resilience, zero RPO, a commissioned RTO or seven-day load/soak behavior. Broad backend-suite and CI outcomes belong in the attached run evidence/PR checks; do not infer them from the focused counts above.

## Deployment and rollback boundary

This source change is not installed on `.221`. Follow the existing signed release and reference-appliance acceptance workflow. Keep the installed updater stopped and deferred storage repair unchanged until their separate release gates are satisfied.

Changing advisory lock identities requires a coordinated restart of all processes running these jobs. Do not overlap old-key and new-key API workers or advertise a rolling HA upgrade for this slice. The same rule applies to rollback. Existing standalone maintenance procedures are the supported deployment boundary. No schema migration or historical migration checksum changes are required. Event response fields are additive; legacy keyless uploads remain compatible. Spool format and saved in-flight identities are unchanged.

The primary's current download manifest advertises a sensor image at 1.23.3 (update version 1.23.4). Its `ready` status means a bootable image exists; it does not certify the new HA design. New binaries in this lab are clearly labeled `sensor-1.24.29-ha-lab.1` and must not be advertised as signed customer releases.

## Remaining implementation gates

| Work package | Current disposition | Required before release of that capability |
|---|---|---|
| WP0 baseline/lab | Started; isolated fixtures and one fault-test VM established | Full topology, repeatable workload evidence, restore rehearsal and immutable release manifest |
| WP1 worker ownership | Known integer collisions fixed | Discovery owner recovery, complete worker inventory, role readiness and controlled worker shutdown |
| WP2 durable ingestion | Events transaction/replay and initial batch limits implemented | Operational/bulk acceptance contracts, cross-store durable projection payload, producer IDs, priority disk reservations and historical authorization |
| WP3 notifications | Not implemented in this slice | Transactional intents, due-work retry, receipts and explicit uncertain outcomes across every channel |
| WP4–WP8 sensor fabric | Not implemented in this slice | Zone-scoped identity, PKI/manifest, pools, assignment epochs, parity and multi-vantage policy |
| WP9–WP11 controller/site HA | Not implemented in this slice | PostgreSQL/analytics durability, majority authority, external fencing, independent site dependencies and commissioned DR |
| Autonomous critical sites | Not implemented in this slice | Local rules/incidents/escalations, cached policy, local dependencies and isolation/reconciliation tests |
| Admin notification failover | Not implemented in this slice | Per-channel ordered eligible sites, origin-owned grants, durable remote inbox/receipts, local safety route and failback tests |
| WP12 appliances/management | Existing images inventoried | Clean signed rebuild, fresh import/enrollment, upgrade/recovery tests, verified manifest and safe publication |
| WP13 commissioning | Incremental evidence started | Full failure matrix, representative load, seven-day soak, restore, security and primary-site-loss acceptance |

A sensor that can probe and spool is not an autonomous monitoring site. A surviving notification sender is not a replacement incident authority. Keep these capability gates separate in the UI, release notes and customer commissioning record.
