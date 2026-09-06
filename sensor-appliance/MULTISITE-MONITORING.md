# Multi-site monitoring — deployed 6 September 2026

The controller at `192.168.8.221` now supports selecting several monitoring locations for one device or service check.

## Use

- In **Settings → Sensors**, open a sensor and choose **Edit site / location**. Select an existing site or create one, set the location, and save. The deployed `VMware-Probe-01` is assigned to the **VMware Workstation** site.
- On a device detail page, **Site Availability** replaces Recent Alerts beneath Health Score. Choose **Manage sites**, retain Controller (the default), select the available sensors at the required sites, and save.
- Service detail pages have the same Site Availability panel and Manage sites control on the Overview tab.
- New selections require an online, enrolled, authorized probe. A previously selected probe stays visible if it goes offline; losing a site does not silently move its checks elsewhere.
- Devices run their enabled Ping/SNMP checks from the selected probes. Service checks run independently at each selected location. Configuration refresh and the next check interval normally take up to two minutes.

The following examples are configured and were verified with live results from both controller and VMware probe:

| Target | ID | Verified |
| --- | --- | --- |
| cloudflare-dns | 568cd587-6c24-43d6-bdc6-63afb8dbb927 | Ping up at both sites |
| Gateway ping | d677b0cc-d64e-410f-96f4-974ce2dad6d8 | ICMP service up at both sites |
| Test URL | 5b50ba8e-abb8-4c85-ad9a-e906ed49e4a7 | NTLM HTTP service up at both sites; consecutive 60-second probe results |

The Cisco lab device `e531e171-6f71-4978-bee7-d6991276dee6` uses `127.0.0.15`, which refers to the controller's local lab. The user subsequently selected the remote probe too; its SNMP attempts cannot reach that controller-local endpoint through loopback. Use a reachable target address when comparing real remote sites.

## Result semantics

Each location shows current observed state, latest sample time, latency when supplied, and availability across observed Ping/service checks in the last 24 hours. The percentage is sample-based, not a wall-clock SLA. Missing samples are excluded from its denominator and never treated as successful checks. Stale samples show **No recent data**; an offline/disabled/pending probe is identified separately. SNMP shows recent successful collection rather than a percentage because the current sensor protocol reports successful scalar samples but does not emit failed SNMP attempts.

When Controller is selected it remains the source for the existing overall current status. Additional site results do not overwrite it. With only remote sensors selected, devices use a deterministic primary probe for current status; services retain the existing remote-sensor consensus policy. Existing historical charts continue their existing metric aggregation; Site Availability is the explicit per-location comparison.

Saved per-target selections override group/default inheritance. Existing sensor assignment APIs remain supported; the detail page is the recommended place to explicitly choose whether the controller also monitors a target. Authenticated and workflow service checks are supported on sensor 1.23.5 or later. Older probes show Update required until upgraded from the controller. Remote SNMP retains the previously delivered scalar scope (sysUpTime and ifNumber); this change does not add interface/template collectors.

## Implementation and maintenance

- Migration `scripts/migrate-110-multisite-monitoring.sql` introduces `monitoring_policies`, `device_monitoring_vantages`, and `service_monitoring_vantages`, and preserves a primary device owner for global status updates. It is recorded in the PostgreSQL migration ledger and `migrations.lock`.
- `server/app/api/v1/monitoring_sites.py` provides target-scoped GET/PUT monitoring selection and site measurements. Writes require operator access, target/sensor visibility, authorization and availability validation. Results are grouped by their recorded poller ID; only the configured `POLLER_ID` maps to Controller.
- Sensor config and result authorization use multi-sensor membership. The controller poller honors the explicit controller selection for Ping, SNMP and services. The deployed probe now runs `1.23.5`, upgraded using the controller's signed update command. Service checks use the same `internal/checker` implementation as the controller, including NTLM, Basic/Bearer authentication, form workflows, cookies, status/content matching, retries, DNS, TCP, TLS and ICMP.
- Shared dashboard components provide site assignment and availability on both detail pages.

Validation: 29 focused API/security/artifact tests, Go store/pinger/sensor tests, production dashboard build, transactional PostgreSQL tests with controller plus two sensors, live ClickHouse queries, and browser saves. The full TypeScript app check retains pre-existing errors in DeviceDetailPage and other components (including duplicate/missing imports in older device sections). The new MonitoringSites and SensorSiteEditor components have no TypeScript diagnostics; neither do the changed SensorsCard and ServiceCheckDetail files.

Pre-deployment files, controller poller binary, dashboard, and relevant database data/schema are backed up under `/opt/zenplus/backups/multisite-20260906`. Do not blindly restore the database dump over newer configuration. For a rollback, review saved assignments, restore the saved code/binary/dashboard, and restore the old polling-owner view in a controlled migration.

Source is in `C:\Users\user\Documents\ZenPlus-sensor`, branch `codex/sensor-appliance-deployment`, and is deployed on the controller. Include these files and migration in the maintained controller release branch so subsequent vendor/controller updates do not overwrite the deployment changes.

## Service parity update — sensor 1.23.5

The deployed VMware-Probe-01 retains site **VMware Workstation** and location **Riyadh**. Test URL now has both Controller and this probe selected. Its saved LocalAuthTest IIS NTLM credential and configured HTTP security settings are used by the identical shared checker on each host. At 10:09 UTC on 6 September, both locations were up and the remote probe had uploaded consecutive successful checks at its 60-second interval. The service Overview panel shows Site Availability and Manage sites, with location alongside probe name.

Only the service checks assigned to a probe are eligible for its configuration response. The controller decrypts their saved secrets only for that authenticated, authorized probe. Configuration requires HTTPS, carries Cache-Control: no-store, and the VM stores its cached configuration in its service-owned state file with mode 0600. Results contain check outcomes rather than credentials. Decryption failures fail the check closed. Credential rotations and workflow edits participate in the configuration ETag, so the probe refreshes changes. The existing HTTPS certificate verification and signed update verification remain enabled.

Migrations 111 and 112 extend service membership to compatible runtimes and recognize both plain and sensor-prefixed versions. The migration runner was corrected so an already existing view cannot falsely establish that a new replacement definition has been applied. The sensor-assignment API and target-selection API both enforce the minimum runtime for authenticated/workflow checks. Offline health handling now uses the selected sites: a secondary probe going offline cannot invalidate the selected controller's status; losing the only probe makes the service unknown.

Validation included shared-checker Go tests (authenticated cookie-preserving workflow and secret-free telemetry), API compatibility/decryption/HTTPS tests, transactional multi-site SQL tests, offline workflow health integration with rolled-back fixtures, migration-runner tests, the production Vite build, live signed upgrade and browser assignment. One migration-history test was run in the actual local Git worktree because the controller build directory has no .git history. The repository-wide TypeScript build still has the previously identified unrelated baseline errors.

Release digest: `b638533faf901ff3f3e597e01d1157fa1751edcaaaf9ac69c0955d129f25fdea`. The appliance downloads retain their existing base image; update newly enrolled probes through Settings → Sensors before assigning authenticated/workflow services. No VM rebuild was needed for the running probe.

Pre-update source, served dashboard, runtime 1.23.4 and monitoring data are backed up at `/opt/zenplus/backups/service-parity-20260906`. Include migrations 110–112, the migration-runner fix, shared-checker configuration changes, and dashboard/API changes in future maintained releases.
