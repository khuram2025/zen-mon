# ZenPlus Agent 1.12.10

Windows x64, built 2026-09-27. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Security and efficiency hardening release. Not yet published to the stable
channel or to any appliance.

## Changes

### Security

- **Self-update trusts only Zentryc.** A validly signed package is no longer
  enough: the certificate chain Windows validated must carry the verified
  Zentryc LLC subject, Zentryc's Trusted Signing identity and the Microsoft
  Identity Verification root. Another publisher's signed installer served by a
  compromised or impersonated controller is refused.
- **Bounded update downloads.** The manifest must declare the package size (at
  most 512 MiB) and the download is cut off at that size.
- **Controller config cannot change local security settings.** A config
  response that tries to change the controller URL, TLS verification, proxy,
  CA file, signed-config enforcement, APM, spool or agent identity is rejected
  and the previous config is kept.
- **APM gateway config injection fixed.** Controller-supplied identifiers are
  validated, and `$` is escaped so the gateway can no longer be made to expand
  `${file:...}` or `${env:...}` references and export local file contents.

### Efficiency

- **APM is opt-in for new installs.** A fresh install without a profile uses
  server monitoring only, and Setup recommends that option. Upgrades keep the
  installed profile. A config file written before this release that has no
  `apm` section keeps the previous `combined` behaviour, so hosts already
  sending traces are unaffected.
- **APM no longer delays host monitoring.** APM reconciliation (credential
  enrollment, runtime discovery, gateway health) runs on its own worker instead
  of the collection and heartbeat loop. Credential enrollment failures back off
  exponentially (10 s to 10 min) rather than retrying every 10 seconds, and
  persistent failures are logged once rather than on every pass.

## Installation

Unchanged from 1.12.9. From an administrator CMD:

```cmd
start "" /wait "zenplus-agent-1.12.10.exe" /machine /quiet /norestart /controller 10.11.40.31 /profile infrastructure /log "%TEMP%\ZenPlus-install.log"
msiexec /i "zenplus-agent-1.12.10.msi" /qn /norestart CONTROLLER_URL=https://10.11.40.31 INSTALL_PROFILE=infrastructure /L*v "%TEMP%\ZenPlus-install.log"
```

Use `combined` or `apm` to enable APM. Omitting the profile on a fresh install
now selects `infrastructure`.

## Validation

- All Go package tests and `go vet ./...` passed on the release source.
- New tests: publisher pinning against the installed Zentryc-signed agent and
  third-party signed executables, size-capped downloads, rejection of nine
  kinds of local-setting override, config expansion escaping verified against
  the shipped 0.158 gateway, APM opt-in defaults and upgrade preservation, and
  the non-blocking APM worker and credential backoff.
- Built with `scripts\build.ps1 -RequireSigning -UseExistingResources` through
  Azure Artifact Signing (`zentrycsigning/zentryc-public`). Tests ran separately
  because Smart App Control blocks freshly built test and helper binaries on the
  build workstation. That policy is also why the committed resource objects were
  reused; the icons and manifests are unchanged.
- The EXE, MSI, gateway and all four embedded binaries have valid, timestamped
  Zentryc LLC signatures. `zenplus-agentctl version` reports 1.12.10 and the MSI
  ProductVersion is 1.12.10. The build's embedded-payload verifier passed.
- The new pinned self-update verifier accepts the release EXE and MSI and
  rejects a tampered copy. The generated collector config validates in this
  release's gateway.
- Microsoft Defender scans found no threats in the EXE or MSI.

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `zenplus-agent-1.12.10.exe` | 213747632 | `3c39e9b12a76e6c2ae0c8e836cd937791d2460354930fd229dc5d91ba91c61cd` |
| `zenplus-agent-1.12.10.msi` | 215162880 | `9776a6a59685f08521a312711c0306b57e54b185b1939cd553550d9e49b449a3` |

Limits: no live install or upgrade has been run yet. Installer size is
unchanged (the APM bundle is still embedded); splitting it out is the next
phase.
