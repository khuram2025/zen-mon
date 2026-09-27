# ZenPlus Agent 1.12.8

Built 2026-09-22 for Windows x64. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Published to the stable channel on 2026-09-22. Versioned downloads:
[EXE](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.8.exe)
and [MSI](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.8.msi).
The preceding stable manifest and download-page aliases were backed up before
the new files and final manifest were promoted.

## Fixed

- Settings can read the sanitized machine dashboard with Windows-canonical
  ACLs. It no longer depends on ordinary users reading the protected
  `%ProgramData%\ZenPlus\Agent\config\agent.yaml` file.
- When protected settings cannot be read from the snapshot, Settings opens
  installed Setup with administrator approval and passes no default overrides.
- Controller URL is editable. Save passes the selected address and profile to
  Setup; machine scope requests elevation before Setup loads protected settings.
  Complete Setup to apply the change and restart the agent. Errors retain their
  underlying cause for troubleshooting.
- Bulk-install switches are case-insensitive, accept common silent/scope
  aliases, support an optional log file and reject unknown arguments instead
  of ignoring them. Quiet machine installation requires administrator/SYSTEM.
- New installations default to `https://10.11.40.31`. An existing controller
  is preserved unless explicitly overridden in Setup or by command-line option.
- Includes the signed-update download, native signature validation and
  dashboard permission fixes prepared in 1.12.7.

## Install or upgrade

Run the EXE and choose the all-users Windows service. On an existing installation,
set the controller field to `https://10.11.40.31` if you want to migrate it.
After installation, use Settings to edit the controller; press Save, approve
elevation, then complete Setup. A different appliance may require authorization.

For bulk deployment, run elevated or as SYSTEM:

```cmd
zenplus-agent-1.12.8.exe /machine /quiet /norestart CONTROLLER_URL=https://10.11.40.31 INSTALL_PROFILE=combined /log "%TEMP%\ZenPlus-install.log"
```

```cmd
msiexec /i zenplus-agent-1.12.8.msi /qn /norestart CONTROLLER_URL=https://10.11.40.31 INSTALL_PROFILE=combined /L*v "%TEMP%\ZenPlus-install.log"
```

Use `Start-Process -Wait -PassThru` when invoking the GUI EXE from PowerShell
and checking its exit code. EXE success is 0; failure is 1. Existing enrollment
and settings are preserved by ordinary upgrades. TLS verification remains on;
`CONTROLLER_CA_FILE` supplies a private controller CA when required.

## Validation

- All Go package tests and `go vet ./...` passed.
- Installer tests cover silent aliases, unknown/conflicting arguments, new
  default, preserving a configured controller and saving an explicit new
  controller without changing the monitoring profile or data directory.
- Settings argument tests cover user/machine scope, new controller/profile,
  CA paths with spaces and fallback without unwanted default overrides.
- Visual Settings check: dialog opened; controller field displayed the new
  default and accepted an edited IP. Live elevated Save was not exercised.
- Protected snapshot ACL validation passed against this computer's installed
  snapshot. The signed dashboard's `-smoke` check returned 0.
- Compiled agentctl reported 1.12.8 and the default `https://10.11.40.31`.
- Seven release executables/packages have valid timestamped signatures:
  installer EXE, MSI, service, dashboard, user runner, CLI and APM gateway.
- Native trust tests passed for signed EXE/MSI; altered and unsigned files,
  wrong checksums, truncated/failed/cancelled downloads were rejected.
- Build-time embedded-payload verification passed. MSI extraction confirmed
  that the embedded Setup is byte-identical to the signed standalone EXE.
- Microsoft Defender found no threats in the EXE or MSI.

Post-delivery live check (2026-09-22 14:20 UTC): the user ran the elevated
machine upgrade successfully. The installed service binary matches this release,
has a valid Zentryc LLC signature, and the service is Running/Automatic. The
snapshot reports version 1.12.8, controller `https://10.11.40.31` and profile
`combined`. The pre-upgrade agent/server IDs are retained. The installation log
records successful completion at 17:19:44 local time.

The controller heartbeat is not successful: the snapshot records a connection
timeout to `10.11.40.31:443`, and a direct TCP connection check also timed out.
Do not interpret the snapshot's heartbeat-attempt timestamp or persisted `ok`
authentication state as a successful connection to that address.

Limitations: elevated Settings Save has not been exercised. WiX ICE validation
was unavailable under the workstation's policy.
Automatic approval review blocked an additional direct installer-execution
check with the reason "blocked by policy"; no more specific reason was supplied.
That blocked extra check predates the successful user-performed live upgrade
documented above.

## Files

Copies are in `C:\Users\user\Downloads\ZenPlus-Agent-1.12.8` and the project's
`ZenPlus_Agent\dist` directory.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| zenplus-agent-1.12.8.exe | 226095024 | 3677786c84d90e2fda7ca6f73a2f2f397f94dd226d17bd8c13a5c03a3e0e1a1f |
| zenplus-agent-1.12.8.msi | 227631104 | 9d34c82cd05f46e9808aac1fb9932cf5ab3accc86af769c8ba11d8cbbb72600d |

Local evidence is under `.tools/release-1.12.8-validation`; build and full test
logs are `.tools/build-1.12.8.log` and `.tools/tests-1.12.8.log`.
