# ZenPlus Agent 1.12.7 update validation

Date: 2026-09-20. Status: signed candidate; **not promoted to the public stable
channel and not yet tested by upgrading an enrolled machine**. Stable remains
1.12.6. Test-machine selection is pending.

## Changes

- Replace the broken PowerShell signature invocation with native Windows
  WinVerifyTrust Authenticode verification, including certificate chain,
  revocation and timestamp validation.
- Add **Updates > Install update** to the dashboard. It downloads and checks
  the size, SHA-256 and signature before opening Setup. Machine scope requests
  elevation before Setup reads the protected existing configuration. User
  scope remains user scope. Download errors and cancellation clean up staging.
- Share the same download/verification implementation between the dashboard
  and service; use a unique staging directory for each download.
- Accept Windows' canonical representations of the read-only dashboard ACL
  while rejecting extra principals, write access, deny entries and missing
  effective access. This fixes a healthy installed service appearing
  disconnected in the unelevated dashboard.

## Completed checks

| Check | Result |
| --- | --- |
| `go test ./...` | All packages passed |
| `go vet ./...` | Passed |
| Actual zentryc.com manifest and package download through agent code | 1.12.6 EXE downloaded; size, SHA-256 and Windows signature passed |
| Candidate EXE and MSI Windows trust | Both passed, including paths with spaces and an apostrophe |
| Tampered signed EXE | Rejected |
| Unsigned and missing package | Rejected |
| Wrong hash, truncated download, HTTP 503 | Rejected; staging cleaned |
| Cancelled download | Cancelled; staging cleaned |
| Complete signed-package download through HTTP test server | Passed |
| Real installed dashboard directory/file ACL validation | Passed without changing permissions |
| Dashboard ACL negative cases | Extra write/inherit-only write, extra principal, deny, missing reader rejected |
| Signed dashboard `-smoke` against installed status | Exit 0 |
| Release build and embedded-payload execution verification | Passed |
| EXE, MSI, four agent executables and telemetry gateway | Seven valid timestamped Zentryc LLC signatures |
| MSI embedded Setup | Exact SHA-256 match with signed standalone EXE |
| Defender custom scans of candidate EXE/MSI | No threats; definitions 1.459.299.0 |

The elevated machine credential/ACL integration test was skipped because this
session is not elevated. WiX ICE validation reported that system policy
prevented it from running. Neither check is represented as passed.

## Candidate artifacts

- `dist/zenplus-agent-1.12.7.exe`: 226084784 bytes;
  SHA-256 `c02e66a68f76869f4857631990049ca8632703105730eb695baf36bf45dfee62`.
- `dist/zenplus-agent-1.12.7.msi`: 227622912 bytes;
  SHA-256 `61d8d059748a0b915c3b529a00efae6661e40735acd94e2158fc26d77fe8a9e1`.

Local evidence: `.tools/update-validation-1.12.7/` contains the before-state,
test logs, signature report, Defender results and extracted MSI. The full build
log is `.tools/build-1.12.7.log`. No credentials are included in these records.

## Remaining live acceptance test

1. Select the enrolled Windows machine. The local machine currently runs
   1.12.6; no local test VM was running during inspection.
2. Record controller, monitoring profile, agent/server IDs, auth state and
   heartbeat before upgrading. Keep the existing signed package available.
3. Upgrade to 1.12.7 and verify Setup succeeds, the service and agent processes
   run the new version, and their installed signatures are valid.
4. Confirm identity and settings are unchanged and a new authenticated
   heartbeat reaches the same appliance. Check APM health when enabled.
5. Exercise the new update dialog, cancellation and a real subsequent-version
   update from a validation feed before claiming the new UI flow is fully
   tested. Download verification alone does not establish installation success.
6. Promote the tested version to Zentryc with files first and manifest last;
   verify its public download and the agent's up-to-date result.

Versions through 1.12.6 need a one-time signed EXE upgrade because their existing
tray button only opens a browser and their service signature invocation is
broken. After that upgrade, future public releases can be installed from the
new dashboard flow. Appliance-managed service updates still prefer the
appliance's manifest, so publishing on Zentryc alone does not trigger a fleet
upgrade or replace an older valid appliance release.
