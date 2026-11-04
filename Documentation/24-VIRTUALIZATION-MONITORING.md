# Virtualization monitoring: Nutanix, VMware and Hyper-V

## Delivery and validation status

This change adds a native **Servers → Virtualization** module to the ZenPlus source tree. It is not present in the existing SNIH 1.24.48 installation until a release containing the change is built, installed and migrated. No SNIH firewall or hypervisor configuration has been changed by this implementation.

Validation currently covers offline API contracts, authentication/authorization, TLS configuration, pagination, normalization, stale data, credential storage, VMware inventory and Hyper-V JEA invocation. Customer interoperability must be confirmed against the installed Prism/AOS, vSphere and Windows Server builds. Passing fixture tests is not vendor certification.

## Coverage and version requirements

| Connector | API / intended compatibility | Collected data | Limitations |
|---|---|---|---|
| Prism Central | GA v4.0; baseline PC **2024.3 / 2024.3.1**, AOS **7.0 / 7.0.1** | Clusters, hosts, AHV/ESXi VMs, storage containers, performance samples, alerts | The corresponding namespaces and read permissions must exist. No alpha/beta APIs or automatic fallback on authorization errors. |
| Prism Central newer API selection | v4.1 for compatible **PC/AOS 7.3 / 7.3.1**; v4.2/v4.3 selectable on later deployments advertising those namespaces | Same normalized resources and capability reporting | v4.0 is the default. Select a newer GA version only after checking each deployed namespace; test reports unavailable endpoints. This is an API compatibility target, not a claim that every patch combination has been tested. |
| Prism Element | Documented **v2.0**, for AOS deployments retaining that API | Cluster/host/storage capacity and usage from returned statistics, VM configuration/power state, health summaries, alerts | v2 VM configuration does **not** supply full VM performance. Use compatible Central v4 for those counters. This connector does not invoke deprecated v1 statistics or unsupported private endpoints. |
| VMware vCenter / direct ESXi | vSphere Web Services, pyVmomi 8.0.3; **vSphere 8.0** compatibility target | Clusters, hosts, VMs, datastores; QuickStats; available aggregate CPU, memory, disk and network counters; triggered alarms | Direct ESXi has no vCenter cluster inventory. Counters depend on provider support and statistics collection settings. Other versions require interoperability validation. |
| Microsoft Hyper-V | Windows PowerShell 5.1, Hyper-V module, WMI v2 and JEA over PSRP/WinRM HTTPS; **Windows Server 2016, 2019, 2022, 2025** compatibility targets | Local host/VM inventory, CPU, host memory, VM memory demand/assignment, VHD allocation, local volumes, failover cluster identity/health where installed, VM operational warnings and recent VMMS events | Install the collector on **each host**. It does not remotely traverse other cluster nodes. Guest filesystem usage, guest memory usage, application health and SCVMM are not collected. |

Nutanix publishes its [API generation compatibility table](https://github.com/nutanix/nutanix.ansible#api-sdk-generation-compatibility) and [API version guidance](https://www.nutanix.dev/api-versions/). PC namespaces are versioned independently; selecting a version is not proof that every capability is present. Its [legacy API notice](https://www.nutanix.dev/nutanix-api-user-guide/) currently schedules v0.8/v1/v2/v3 deprecation with the AOS/PC upgrade release planned for Q4 2027. Confirm lifecycle support for the exact installed patch versions with Nutanix.

## Required access

### Nutanix

Use a dedicated monitoring identity, never the Prism administrator credential.

- **Element:** assign the built-in **Viewer** role.
- **Central:** use **Prism Viewer**, or the equivalent read-only IAM role for that release, with an authorization policy covering the intended clusters, hosts, VMs, storage containers and alerts. Legacy role names can differ after an upgrade.
- **Central service accounts:** API-key authentication requires PC 2024.3+ and AOS 7.0+ with a service account and appropriate IAM authorization policy. Enter the API key as the credential; the connector sends `X-Ntnx-Api-Key`. Do not paste a Basic-auth string or bearer token into that field. See [Nutanix API-key requirements](https://www.nutanix.dev/2025/02/21/nutanix-v4-apis-using-api-key-authentication-part-2/).
- Basic account authentication uses HTTPS Basic authentication. Interactive SSO/MFA sessions are not collected or replayed.
- Required operations are **list/get cluster, list/get host, list/get VM, list/get storage container, read statistics for these resources, and list/get alerts**. No creation, configuration, power operation, guest command, snapshot operation, alert acknowledgment or alert resolution permission is needed.
- Scope all required resource types consistently. An account that sees only some VMs will produce inventory for only those visible VMs. The connector cannot discover resources the server silently excludes through RBAC.

Read-only role descriptions are in [Nutanix's permission guide](https://next.nutanix.com/intelligent-operations-26/prism-user-permissions-what-are-the-options-37266); use the deployed release's IAM operation catalogue for exact custom-role permission names. A 403 during any attempted read fails the collection and identifies the permission problem; it does not cause a downgrade to an older API.

### VMware

Create a dedicated account with the built-in **Read-only** role at the intended inventory root and propagate it to children. Include hosts, VMs, datastores and clusters in that scope. The collector uses inventory properties, QuickStats, triggered alarm state and `PerformanceManager.QueryPerf`; the latter requires `System.View` according to the [vSphere API reference](https://developer.broadcom.com/xapis/vsphere-web-services-api/latest/vim.PerformanceManager.html). No guest operations or VM power privileges are required. Session login/logout and temporary server-side inventory views are cleaned up after collection.

### Hyper-V

Use a dedicated domain or local monitoring principal that is **not** a member of Administrators or Hyper-V Administrators. Grant it access only to the `ZenPlusMonitoring` JEA endpoint. The endpoint's temporary virtual account executes the installed parameterless `Get-ZenPlusHypervisorSnapshot` function; the caller cannot supply scripts, paths, VM names, or commands. This avoids granting a monitoring identity general management access.

1. Copy `scripts/hyperv/Install-ZenPlusMonitoring.ps1` and `ZenPlusMonitoring.psm1` to each host through your normal administration process. Review the scripts first.
2. Run locally in an elevated Windows PowerShell session:

   ```powershell
   .\Install-ZenPlusMonitoring.ps1 -MonitoringPrincipal 'DOMAIN\ZenPlusHypervisorReaders'
   ```

3. Configure a WinRM **HTTPS** listener on TCP 5986 with a trusted certificate whose SAN matches the host's DNS name. Allow only the ZenPlus polling address through the Windows/network firewall. The installer does not open firewall rules or create listeners.
4. Ensure only administrators can modify the module, role-capability file and endpoint configuration. Do not install this privileged virtual-account endpoint on a domain controller; the installer refuses it.
5. Test that the monitoring account can invoke `Get-ZenPlusHypervisorSnapshot`, and cannot invoke `Stop-VM`, `Set-VM`, `Invoke-Expression` or an arbitrary script in this endpoint. Do not grant it access to the default unrestricted remoting endpoint.
6. Add one connector per Hyper-V host in ZenPlus. Shared cluster identity can appear in several host connectors; capacities are not summed across connectors.

The implementation follows Microsoft's [JEA role-capability model](https://learn.microsoft.com/en-us/powershell/scripting/security/remoting/jea/role-capabilities) and [JEA security guidance](https://learn.microsoft.com/en-us/powershell/scripting/security/remoting/jea/security-considerations). Hyper-V health is read from WMI/provider operational state, not inferred from a successful TCP connection.

## TLS and credentials

- Certificate chain and hostname validation are mandatory; there is no insecure/skip-verification switch.
- For a private PKI, paste the issuing CA chain in PEM format into the connector. This supplements trust for Prism/VMware; Hyper-V uses the supplied PEM as its CA bundle. Do not paste a private key.
- If using `https://10.23.2.31:9440`, the Prism certificate must include **IP SAN 10.23.2.31**. Otherwise use the certificate's DNS name, resolving to that address, and keep the same scoped firewall destination.
- Expired, untrusted and hostname-mismatched certificates fail. Do not work around the failure by disabling TLS verification.
- Credentials are encrypted using ZenPlus's AES-GCM integration encryption key (`SNMP_ENC_KEY`). The key must already be configured and backed up through the appliance's normal protected configuration backup. API responses and audit records do not return the credential.
- Administrators create, test, edit, pause and delete connectors. Authenticated readers see only connectors allowed by existing visibility tags. Editing a credential requires re-entry and a new connection test.
- Tests return a signed, user-bound receipt valid for ten minutes. Saving different connection details or an expired test is rejected by the backend.
- To change the endpoint or platform, create a new connector so resource history remains attached to the correct provider identity. Editing credentials clears the current snapshot until the new account completes discovery.

## SNIH firewall change request

| Field | Required value |
|---|---|
| Source | ZenPlus server **10.21.0.13/32** |
| Destination | Prism **10.23.2.31/32** |
| Service | **TCP destination 9440** |
| Direction | ZenPlus initiates; allow stateful return traffic |
| Action / logging | Accept this exact flow; log sessions |
| NAT | Normally disabled for internally routed management traffic; confirm SNIH routing policy |
| Rule placement | Before the FortiGate policy currently denying this flow |

Use the reviewed [FortiGate template](../scripts/virtualization/snih-prism-fortigate.conf.example). The correct VDOM, interfaces and policy position must be supplied by the SNIH firewall administrator. Do not widen this rule to arbitrary source hosts, whole destination subnets or all services. If the actual poller moves to another controller address during HA failover, separately review that source address.

A ping or TCP check verifies only routing/reachability. Acceptance requires authenticated cluster/host/VM discovery, usable metrics and alert reads through **Test connection**.

## Configure and accept the connector

1. Record whether `10.23.2.31` is Prism Central or Prism Element and its exact PC/AOS build. Record the certificate SAN and issuing CA.
2. Install a ZenPlus release containing this feature. Apply PostgreSQL migration **171** and ClickHouse migration **172** through the tracked migration runners. Install the pinned Python requirements; restart the API and serve the new dashboard bundle. Normal release packaging includes the Hyper-V setup scripts under `scripts/hyperv`.
3. Open **Servers → Virtualization → Add connector**, select the platform, enter its HTTPS endpoint and dedicated read credential. For Central select the matching GA API version, default v4.0.
4. Add CA PEM if needed, visibility tags such as `SNIH`, and a polling interval (default 300 seconds; supported range 60–3600).
5. Click **Test connection**. This runs real discovery/statistics/alert reads with a 90-second bound, not a ping. Review resource counts and coverage notes before saving. On large installations, reduce the monitoring identity's resource scope if a complete test cannot finish within the bound.
6. Save. The controller-side virtualization scheduler begins collecting automatically; **Poll now** advances the next poll. The Go network poller is not the owner of these API sessions. Collection runs from the controller, not a remote sensor.
7. Compare discovered counts against the resources visible to the same account in Prism. Check host and cluster CPU, memory and storage units against Prism. For Central, check at least one powered-on VM's metrics and one known alert's resource link.
8. Open resource details to see individual capacity, utilization, I/O, health-score and network counters where available, plus 24-hour charts. Unknown counters must stay unknown; powered-off VM state must not be reported as a network outage.
9. Confirm an invalid password, wrong CA or blocked TCP 9440 makes the connector fail and marks the previous data stale. Restore the valid configuration and confirm collection recovers.
10. Record the exact version combination, tested identity scope, TLS issuer, counts and representative metric values as the SNIH acceptance evidence. Do not record passwords or API keys.

## Metric and alert semantics

- All resource identities are scoped to the connector and keyed by provider IDs. A completed inventory replaces the prior snapshot. A failed collection retains the last successful snapshot and timestamp. Staleness is shown after failure, pause, or two polling intervals plus 30 seconds without a success.
- CPU/memory PPM from Nutanix are converted to percent by division by 10,000. Byte capacities remain bytes; the dashboard formats binary units. Unavailable/negative sentinel values are omitted, not converted to zero.
- Prism v4 uses cluster/host `TimeValuePair` arrays and VM `stats` tuples; VM statistics are fetched in paginated batches. PE VM lists use `offset/length`, while PE clusters/hosts/alerts use `count/page`.
- Health states come from provider health/operational fields and active alert severity. Where only a numeric health score exists it is displayed as `health_score`; absent categorical health remains unknown. An empty alert list is not proof of overall cluster health.
- VMware VM storage usage is committed allocation, and Hyper-V VM storage usage is VHD file allocation; neither is guest filesystem utilization. Hyper-V VM memory demand and assigned memory are separately named metrics, not presented as guest memory usage. Install an OS agent for guest/application monitoring.
- Imported Prism alerts preserve provider ID, severity, acknowledgment and resolution state. Links point to the affected resource and alert's authenticated provider API URL; Prism UI hash routes differ by release and are not guessed. VMware links open the vSphere client search for the resource. Hyper-V links open the local ZenPlus resource view; it has no assumed web console.
- Provider alerts are shown in the Virtualization module. This change does not automatically create notification rules or acknowledge/resolve anything in the provider. Hyper-V VMMS events are bounded to 24 hours / 500 records, and are events rather than a provider acknowledgment workflow.
- Metrics are retained in ClickHouse for 30 days. History reads aggregate into five-minute buckets, or hourly for API requests exceeding 72 hours. No cross-provider capacity totals are calculated, preventing double counting when Central and vCenter expose the same VMs/storage.
- Pagination and request durations are bounded; transient HTTP 429/502/503/504 responses get bounded retries. Unsupported optional endpoints produce coverage notes. TLS/authentication/permission errors fail the poll. There is no implicit v3 or insecure API downgrade.
- Multiple API workers coordinate polling with PostgreSQL row locks (`FOR UPDATE SKIP LOCKED`). Synchronous VMware/Hyper-V SDK collectors run in a subprocess terminated at the collection timeout or API shutdown; credentials are supplied through stdin, not command arguments or files.

## Developer verification

Run from the repository with the server dependencies installed:

```powershell
$env:PYTHONPATH = 'server'
python -m pytest --noconftest server/tests/test_virtualization.py -q
npx --prefix dashboard tsc --project dashboard/tsconfig.virtualization-check.json --noEmit
```

The focused suite intentionally isolates the virtualization router from unrelated application test fixtures. Run the standard dashboard bundle build and migration lint as well. Release builds must pass the repository-wide strict TypeScript check and route smoke tests.

Validation on 4 November 2026:

- 32 virtualization tests passed, including real local HTTPS handshakes for trusted CA, untrusted CA, hostname mismatch and expired certificates.
- 41 existing migration lint/runner tests passed; the migration lock verifies both new migrations. PostgreSQL/ClickHouse migrations have not been applied to SNIH.
- Focused TypeScript check and Vite production bundle build passed. Browser smoke tests passed for resource inventory/history, test-before-save, invalidating a test after connection changes, and save; no browser errors were observed.
- Both Hyper-V PowerShell files passed syntax parsing. Hyper-V and vSphere collector behavior is covered with SDK contract mocks. The Windows development machine's Application Control policy blocks a native SSPI dependency, so no real Hyper-V authentication was validated there. Do not weaken that policy; validate PSRP on the intended Linux ZenPlus controller against a configured JEA host.
- SNIH connectivity, actual provider versions, permissions, certificate trust and end-to-end polling remain deployment acceptance checks. Screenshots under `output/virtualization-2026-11-04` use explicitly labelled synthetic fixtures.
