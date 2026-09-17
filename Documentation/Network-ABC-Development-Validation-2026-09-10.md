# Network monitoring A/B/C — development implementation and validation

Follow-up: [appliance integration assessment and signed candidate](Network-ABC-Appliance-Integration-2026-09-10.md). That report supersedes the pending integration/installed-identity status below; this document retains the initial development-phase evidence.

Date: 10 September 2026. Baseline: ZenPlus 1.23.10, source HEAD `b4256ff0f0646db137ad2250f8d441b11464e7ee`, plus the local changes described here.

The development implementation for A, B and C is available in this workspace. Tests use synthetic records, explicit timestamps, local notification function sinks, test database adapters, and loopback UDP. No real network device, SNMP traffic generator or production notification destination is needed for these checks. No change has been installed on appliance `192.168.8.221` by this work.

These results establish development behavior. Appliance integration, deployed binary/source identity and vendor certification remain open gates. An empty development inventory, disabled notification channel or idle sensor is not classified as a missing feature or production failure.

## A — Development validation and findings

| Finding | Development outcome | Evidence |
|---|---|---|
| Discovery range expansion | Reproduced bounded-range failures; corrected IPv6 ranges, full exclusions and /31-/127 endpoint handling | `server/tests/test_discovery_contracts.py` |
| Interface status and counter calculations | Corrected IF-MIB down code, exact 32-bit wrap boundary, elapsed-time guards and counter-width changes | `poller/internal/checker/snmp/polling_contract_test.go` |
| Compound conditions and hold timing | Worker now uses all conditions. Explicit timestamps verify AND/OR, first-breach timing, gaps, unknown samples and recovery | `test_network_condition_contract.py`, `test_network_worker_contract.py` |
| Component identity | Separate template table instances maintain separate alert identities; healthy tunnel values cannot clear another tunnel's breach | Worker contract fixture |
| Supported canonical states | `fan_state`, `psu_state`, `vpn_tunnel_state`, `ha_state` and `bgp_neighbor_down` lacked a canonical evaluator. New rules reject these keys with an actionable error; collected `tpl_*` state metrics remain usable | `test_network_rule_validation.py` |
| Performance vs component health | Device card now labels the CPU/memory/loss calculation “Performance Score,” explains its scope and displays “Insufficient data” when an input is absent | Device detail UI change; source review |
| NetPath freshness | Reproduced stale stored “ok” status using a fixed clock. Current list/detail/summary distinguish disabled, pending and stale probes while retaining the last observed status | `test_network_development_validation.py` |
| Availability and remote sensors | Existing coverage/unknown-data and sensor contracts were exercised as regression tests. No physical remote sensor was required | `test_service_availability.py`, `test_sensor_snmp.py`, `test_sensor_fabric_contract.py`, Go sensor spool tests |
| Installed build identity | Added a read-only source inventory/comparison tool. Local identity is recorded; the appliance's installed inventory has not been collected | `scripts/network-build-identity.py` |

Freshness policy: network metrics become unknown after the greater of 180 seconds or three configured collection intervals. Template group cadence is considered separately for each metric. NetPath uses the greater of 180 seconds or three probe intervals. Missing data does not mean zero throughput, success or recovery. Legacy stored zero-rate baselines are treated as unknown by alert evaluation when counter movement contradicts a zero reading.

## B — Discovery and polling contracts

- Discovery V2 imports are serialized across runs. Rediscovery can update discovered device identity without creating another device or replacing operator monitoring/credential choices. New imports inherit the credential that succeeded during discovery. Savepoints isolate device/server creation failures; invalid result IDs are reported without inserting invalid foreign keys.
- Interface monitoring selection and configured speed follow an unambiguous logical interface name across ifIndex changes. Reused indices do not inherit another port's policy. Successive reindexing selects the latest matching inventory record; ambiguous names fall back conservatively.
- Polling recognizes high-capacity counter presence even at zero; counter width, logical name, speed, discontinuity or uptime reset establish a fresh baseline. Invalid elapsed time and implausible rates are guarded. Existing historical series still use ifIndex; this change does not merge historical series after a rename/reindex.
- SNMPv3 sessions reject incomplete authentication/privacy combinations instead of silently downgrading. IPv6 address/context configuration is covered by fixtures. Real-device polling interoperability is pending.
- MIB uploads can be compiled offline into a JSON symbol index using pinned `pysmi==2.0.0`. Compilation runs in a child process with a 30-second deadline, 4 MB per-file and 32 MB total input limits. The index includes source hashes; missing dependencies, parse failures, stale indexes and ambiguous symbols are reported. Vendor dependencies must be uploaded; there is no automatic external MIB download.
- Monitoring templates have versioned JSON export/import APIs and UI controls. Export includes profile definitions, scalar/table metrics, scales, labels and matching rules; device assignments and credentials are excluded. Import creates a custom profile through the existing profile schema. Numeric scalar instance suffixes and table indices remain explicit.

Counter fixtures establish numerical contract behavior, including 1 and 100 Gbit/s examples and wrap/reset boundaries. They are not traffic-generator accuracy or sustained throughput certification. Discovery V2 duplicate protection does not establish global inventory uniqueness across every legacy/manual creation path.

## C — Alerts and network events

- A shared timestamped condition evaluator implements three-valued AND/OR, continuous trigger holds, optional reset thresholds and metric freshness. Active incidents remain open when evidence is unknown. The editor retains reset values even for a single condition; partial API edits validate the resulting complete condition set.
- Device dependencies and maintenance suppress downstream network incidents. Existing dependency metadata is cleared when suppression ends. Template component alerts have distinct entity keys; pre-upgrade aggregate template incidents retain their identity until observed recovery.
- Network rules honor channel selection, custom message templates, quiet hours, entity cooldown and snoozes. Recovery notifications follow the recorded trigger-dispatch state. Acknowledged incidents do not receive a deferred pending trigger. Escalation continues through the existing shared escalation worker.
- Syslog has an optional bounded UDP receiver, RFC 5424/legacy header decoding, source-IP identity, PostgreSQL event storage, retention, scoped event browsing, message/severity filters, and rule authoring/editing. Event UUID replay is idempotent. Maintenance, dependencies, snoozes, cooldown and schedules gate event alerts. Syslog incidents require manual resolution.
- Trap normalization maps SNMPv1 generic/enterprise traps to the expected OIDs. Wire fixtures verify SNMPv2 inform acknowledgements and configured-source SNMPv3 authPriv traps, invalid credentials, downgrade rejection and credential refresh. Trap alert dispatch supports replay IDs, dependency suppression and source-specific cooldown. IPv6 source text is preserved alongside the legacy ClickHouse IPv4 key.

Event replay means replaying the same decoded UUID. Two separately received UDP datagrams have separate identities. UDP delivery is best-effort; no durable syslog spool, TCP/TLS syslog listener or SNMPv3 inform authoritative-engine service is included. SNMPv3 trap credentials currently follow the monitored device's polling credentials. Full USM timeliness/replay and vendor interoperability certification remain open. Notification counters indicate dispatcher success/attempts, not independently confirmed receipt at external destinations. Quiet-hour syslog/trap events remain recorded; delayed event delivery is not promised.

## Validation record

The repeatable runner writes `results.json`, per-stage logs and Python JUnit XML to `output/network-development-validation/`.

| Check | Result |
|---|---|
| A/B/C targeted Python suite | 150 passed in the final targeted run; see generated XML/log for the authoritative count |
| Go SNMP, store, pinger and sensor spool packages | Passed, including actual loopback UDP trap tests |
| Linux amd64 poller and sensor builds | Cross-compiled successfully; binaries are in the validation output directory and have not been deployed |
| Alert editor serialization | Passed: single-condition hysteresis and syslog scope/filter/channel round-trips |
| Dashboard Vite bundle | Passed; existing large-chunk warning remains |
| TypeScript baseline comparison | Baseline 541 diagnostics; current 539; no new normalized diagnostics. Full `npm run build` remains blocked by existing type errors |
| Full Python regression, before the last additional baseline-zero fixture | 765 passed, 44 skipped, 4 failed |
| Browser UI fixtures | Verified syslog rule creation, MIB compile results, syslog filter editing, reset-value retention and template import/export controls |

The four full-suite failures are the existing long-password test with the installed passlib/bcrypt combination, the POSIX file-mode assertion on Windows, and two RADIUS tests requiring `select.poll`, which Windows lacks. These checks remain release gates in the supported Linux environment. Windows application control prevented execution of the sensor command test executable; the policy was not bypassed. Focused sensor spool/pinger tests passed. PostgreSQL/ClickHouse operations in the targeted suite use test doubles; migrations were linted but not applied to a database here.

Run from the repository root with Python dependencies installed from `server/requirements.txt`, Go available, and dashboard dependencies installed:

```text
python scripts/validate-network-development.py --go /path/to/go --node /path/to/node
python scripts/network-build-identity.py --output output/network-development-validation/source-identity.json
```

Run the inventory script on an installed source tree with `--root /opt/zenplus`, then use `--compare` against the reviewed inventory. Hashes normalize text line endings and exclude credentials/configuration. Matching source inventories alone do not attest the running binary; verify release package signatures, binary checksums and service restart/load state separately.

## Remaining delivery plan and acceptance gates

1. **Appliance integration — backend/QA:** collect the installed source and binary identity; review the local change set; create the normal signed release candidate. Use an isolated PostgreSQL/ClickHouse environment to apply migration 115 (event storage and rule support) and 116 (trap source/identity columns). Exercise real transactions, concurrent imports/evaluators, RBAC, retention and API-to-storage-to-dashboard reads. Require no duplicate device identities, preserved interface policy and correct unknown/recovery behavior.
2. **Development appliance rollout — release/SRE:** deploy the reviewed backend, dashboard and poller together with the new dependency and migrations through the established updater. The optional service template is `scripts/systemd/zenplus-syslog.service`; it starts on loopback UDP/1514. Set an explicit bind address and allowed source CIDRs for the intended fixture network before enabling it. Opening device-facing ingress is a separate deployment configuration step.
3. **Integrated synthetic exercise — QA:** discover the same fixture subnet twice, swap interface indices, replay counter/uptime/discontinuity fixtures, alternate two tunnel states, fail/recover a parent, cross quiet hours with the test clock, and replay the same trap/syslog event IDs. Use local notification destinations and capture expected/actual events, active incidents and recoveries. Include custom scalar/table metrics in charts and alerts. Test failure/restart behavior and legacy aggregate alerts.
4. **Hardware certification — network engineering:** schedule representative Cisco, Juniper, Fortinet, Palo Alto and relevant controller/device families according to actual product scope. Record model, firmware, OIDs, v2c/v3 security level, context, IPv4/IPv6 and collection interval. Compare 1/10/100-Gbit/s rates against a known traffic reference, exercise wraps/reboots/reindexing and verify vendor trap behavior. Missing equipment remains a validation dependency, not a missing-feature verdict.
5. **Release sign-off:** resolve or formally disposition the existing Linux/security and dashboard type-check failures; measure sustained collection/event rates, query cost and queue loss. External SMTP/SMS/webhook delivery, v3 informs, full USM timeliness, durable event delivery and broader NPM feature parity must receive their own acceptance results before claiming coverage.

Rollback: stop the optional syslog receiver and disable its new rules, restore the prior application/dashboard/poller package using the normal rollback process, and retain the additive tables/columns. Do not delete event data or rewrite shipped migration checksums to roll back. Migrations 115/116 in this workspace are new, unshipped migrations.

This delivery closes confirmed development defects and adds the MIB/syslog foundations. It does not certify full SolarWinds NPM parity or complete the remaining topology, wireless, cloud, forecasting and shared-investigation workstreams from the assessment.
