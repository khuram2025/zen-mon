# Receiver hardening and simulated-device validation

**Completed on the development appliance: version 1.23.12.** Receiver lifecycle hardening and a repeatable simulated-device campaign are implemented, tested and installed on `192.168.8.221`. The syslog receiver is enabled only on **127.0.0.1:1514/UDP**, with loopback sources allowed. The package remains unpublished.

This continues the [A–C rollout](Network-ABC-Development-Rollout-2026-09-10.md). No live network equipment was required, and absent development telemetry is not treated as a missing feature. These results establish development behavior, not full SolarWinds NPM parity, vendor interoperability or production capacity.

## Changes delivered

| Area | Result |
|---|---|
| Idle retention | Cleanup runs at startup and periodically, independently of incoming traffic; failures are counted and retried at the next interval |
| Bounded ingestion | Configurable finite queue, processing deadline and shutdown drain deadline; one consumer processes accepted events |
| Operational counters | Separate received, rejected-source, invalid, queue-full, processed, processing-failed, transport-error, retention and shutdown-abandoned counters; queue depth, capacity, high-water and in-flight state |
| Status visibility | Periodic structured journal records and atomically replaced `/run/zenplus-syslog/status.json`, with PID and timestamps; no event messages or credentials in the status file |
| Shutdown/restart | SIGTERM closes the listener, attempts to drain queued work, accounts for abandoned work and exits; tested in a separate process and via the installed systemd unit |
| Configuration | Validated numeric limits and IP/CIDR settings; `/etc/zenplus/syslog.env` can override defaults; systemd creates a private runtime directory |
| Upgrade lifecycle | The package installs the receiver unit without enabling it; later upgrades resume it only when it is enabled. The dev appliance was explicitly enabled on loopback during this rollout |
| Repeatable campaign | Three loopback SNMPv2c discovery/import/sample/alert/recovery cycles; real UDP syslog persistence, acknowledgement/resolution, retention, cooldown and restart checks; repeated PostgreSQL interface reindex checks |

Counters are per process and reset on restart. `shutdown_abandoned` and `processing_failed` describe uncertain outcomes: ingestion can commit before a subsequent operation fails. The service does not claim exactly-once delivery, durable queueing or recovery of UDP packets lost before receipt. The final status is also written to the journal; systemd may remove the runtime status file when the service stops.

## Deployment defect found and repaired

Validation under the non-root service identity exposed a problem missed by the previous rollout's root-level checks: newly installed bcrypt, PySMI, Lark and Jinja2 package trees inherited a private `077` umask. They were readable by root but inaccessible to the API service account.

The repair restored read/traverse permissions only within the eight affected package/metadata trees, changing 313 paths. Application credentials and configuration permissions were not relaxed. The updater's pip step now explicitly uses `022` on POSIX. A subprocess test proves that package files remain readable even when the updater starts with a private backup umask. The local rollout wrapper also separates private backup creation from installation permissions.

After repair and again after installation, checks ran as `zenplus`: bcrypt-sha256 hashing/verification and PySMI/Jinja2/Lark imports succeeded. A fresh login using the existing saved appliance account then succeeded through the running API. Earlier root-level `pip check` and version checks alone were insufficient evidence of service usability; the new checks close that gap.

## Acceptance evidence

| Check | Result |
|---|---|
| Linux regression suite | **793 passed, 43 skipped, 0 failed** |
| Disposable PostgreSQL/ClickHouse integration suite | **22 passed, 0 skipped, 0 failed** |
| Go SNMP, store, pinger, sensor spool and sensor command packages | All five passed |
| PostgreSQL interface reindex/policy test | Passed three repetitions |
| Signed build and verification | Passed; **856 checksums**, **66 changed payload source files**, zero forbidden files |
| Installed source attestation | **65 source files matched**; the version marker is checked separately because installation appends a timestamp |
| Running executable attestation | Poller and NetFlow executable hashes match the signed package; sensor artifact matches |
| Development upgrade | All **30 signed manifest steps** passed; PostgreSQL and ClickHouse schema gates passed |
| Live receiver | Enabled, loopback listener verified; event persisted with socket source `127.0.0.1` and Info severity; processing failures zero in the observed validation interval |
| Fresh account/API check | Login passed; authenticated network-event query returned the exact synthetic event |
| Live restart | systemd restarted the receiver with a new PID; status and listener returned successfully |
| Cleanup | Exact live event deleted; disposable databases and remote staging removed; backups retained |

The regression run used the appliance's repaired existing Linux environment, not another fresh requirements installation. No dependency versions changed in this release. The 43 skips include opt-in integration and legacy tests requiring a live API or separate helpers. Integration was run separately. Legacy smoke tests were directed away from the installed API during regression.

The SNMP fixture is a minimal loopback-only GET responder, not a vendor emulator. The campaign runs the real discovery executor and import path with a supplied in-memory fixture credential; operator monitoring settings survive reimport. It obtains synthetic readings through the application's real SNMP GET path, writes them to the disposable history database, and evaluates the real alert worker. This does not prove a complete long-running Go polling daemon against vendor hardware. Counter-reset/rate and concurrent interface reindex handling are covered separately by Go tests.

The syslog campaign exercises real UDP reception and SQL persistence. Notification delivery is replaced with a local sink. Live validation sent no external notification. Audit records of the authorized login remain under normal retention.

Existing TypeScript debt remains outside this change. The established Vite production build passed; this is not a clean TypeScript certification.

## Release and recovery

| Item | Identity |
|---|---|
| Installed version | **1.23.11 → 1.23.12**, 15 September 2026 |
| Candidate commit | `c30e97f8ac240ddb093a1c49d6794d242c58aca1` |
| Branch | `codex/network-receiver-validation` |
| Package SHA-256 | `0ad09db0f4de39db291c26e8e962d68103f992f05966a2f82f8c44649e4df4e1` |
| Appliance backup | `/opt/zenplus/updater/backups/network-dev-20260915/` |
| Publication / other appliances | Not performed |
| Bundled Windows agent | Existing 1.12.4; unrelated local agent work preserved |

The installation used the updater's signature, checksum, version, lock, manifest execution, backup, rollback and schema mechanisms. Code/database, Python-environment and configuration recovery material remains on the appliance under root-protected directories. It is not copied into release artifacts. The installed Git HEAD remains the original OTA checkout baseline; file and process checksums establish the installed release.

For rollback, first stop and disable the optional receiver, preserve current data, and use the retained pre-upgrade package/database/environment backups with a reviewed recovery procedure. The backup is not a full machine snapshot and does not include a full ClickHouse data backup. Do not overwrite shipped migration checksums. Permission repairs to public Python package files should be retained.

## Operator runbook

The current receiver is deliberately useful only for local development traffic. Its settings file contains the loopback bind, port and source allowlist. Changing to real network feeds requires choosing the management interface and permitted source networks first.

```sh
sudo systemctl status zenplus-syslog
sudo cat /run/zenplus-syslog/status.json
sudo journalctl -u zenplus-syslog --since '10 minutes ago'
```

Defaults: queue capacity **4096**, event processing deadline **30 seconds**, drain deadline **20 seconds**, retention **30 days**, retention interval **3600 seconds**, status interval **10 seconds**. systemd supplies a final **150-second** stop limit. The application deadline is a cooperative drain limit; it is not proof that every external dependency will cancel instantly.

Tune `SYSLOG_QUEUE_SIZE`, `SYSLOG_PROCESSING_TIMEOUT`, `SYSLOG_SHUTDOWN_TIMEOUT`, `SYSLOG_RETENTION_DAYS`, `SYSLOG_RETENTION_INTERVAL` and `SYSLOG_STATUS_INTERVAL` only after observing workload and memory use. Stop and restart the service after configuration changes. The Python parser validates configuration before binding.

For campaign reproduction, provision the private fixture databases as described in the [integration report](Network-ABC-Appliance-Integration-2026-09-10.md), stage the candidate source under that fixture root, then run:

```sh
python scripts/validate-network-integration.py \
  --fixture-root /tmp/zenplus-network-integration-<run> --prepare-databases
python scripts/validate-network-integration.py \
  --fixture-root /tmp/zenplus-network-integration-<run> --campaign
```

The campaign is intentionally guarded against the installed database. It uses a private PostgreSQL Unix socket and dedicated fixture user, plus loopback ClickHouse on a separate port. Its fixtures reset disposable tables; never point them at application databases.

## Next plan

1. **D3 — Capacity and soak:** define target device/interface/event rates, then measure sustained ingestion, poll lateness, queue/drop counters, CPU/RAM, database growth and query latency. Include restart and database interruption under load.
2. **Event reliability:** decide the required loss/recovery behavior, then implement durable spooling and TCP/TLS if required. Current UDP best-effort behavior is explicit and observable.
3. **D4 — Vendor lab:** validate representative SNMPv2c/v3 devices or validated vendor simulators, including contexts, IPv6, reboots and high-speed counters. Availability of real hardware is a separate gate.
4. **D5 — Release acceptance:** verify receipt at an authorized notification destination, rehearse complete recovery, review protected-main integration and only then publish a staged production rollout.

Evidence is in `output/network-receiver-validation/`, including the signed package and metadata, candidate Git bundle, source archive/list, JUnit results, build/Go logs, runtime attestation, authenticated smoke result and receiver status snapshots.

- [Runtime attestation](../output/network-receiver-validation/rollout-attestation.json)
- [Installation result](../output/network-receiver-validation/install-result.json)
- [Fresh login and API evidence](../output/network-receiver-validation/authenticated-smoke.json)
- [Receiver before restart](../output/network-receiver-validation/receiver-before-restart.json)
- [Receiver after restart](../output/network-receiver-validation/receiver-after-restart.json)
