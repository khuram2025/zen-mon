# ZenPlus 1.23.18

Cisco discovery, device identity, alert handling, and device-page corrections.

## Discovery and inventory

- Discovery imports apply linked SNMP credentials and profile defaults, including location, port, and monitoring configuration.
- Imported devices are polled consistently with manually added devices; SNMP object identifiers are normalized for identification and template matching.
- ENTITY-MIB chassis inventory supplies device model, serial, and firmware. A serial already held by another device is skipped so duplicate discovery does not block inventory updates.
- Migration 126 adds firmware storage and a last-up baseline for interfaces. Duplicate chassis serials cannot abort its backfill. The migration runner recognizes the earlier local-only checksum of this migration.

## Alerts and dashboard

- Device and service status rules respect their conditions and avoid creating stale "Device Down ... is UP" alerts. Migration 127 resolves the matching existing false up-status alerts.
- Previously unused ports do not immediately generate interface-down alerts; alerts use the recorded operational-up baseline.
- Device diagnostics show SNMP freshness, filtered device counts, clearer status descriptions, fan state, and standalone stack-ring state.
- Credential screens distinguish configured communities without exposing their values.

## Upgrade notes

- Minimum supported appliance version: **1.23.9**.
- Includes migrations **126 and 127**, plus the complete historical migration inventory.
- Bundles the existing signed **Windows Agent 1.14.3** core/full EXE and MSI packages; this release does not change the agent binaries.
- Existing interface-down incidents are not bulk-resolved by this release: historical unused ports cannot reliably be distinguished from genuine failures.
