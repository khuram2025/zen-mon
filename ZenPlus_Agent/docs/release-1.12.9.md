# ZenPlus Agent 1.12.9

Windows x64, built 2026-09-22. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Published to the stable channel on 2026-09-22 at 14:33:53 UTC. The previous
manifest, download page and latest-file aliases were backed up before promotion.

## Changes

- Settings now saves the controller and monitoring profile directly. It no
  longer opens the installation wizard. Machine settings request administrator
  approval, preserve unrelated configuration and enrollment files, and are
  picked up by the running agent without reinstalling or restarting the service.
- If the machine dashboard snapshot is unavailable, Settings opens a standalone
  elevated Settings dialog rather than Setup.
- Fixed overlapping text in the MSI wizard header: the banner no longer contains
  a second product title beneath the native Windows Installer page title.
- Silent and bulk installation switches remain supported. New installations
  default to `https://10.11.40.31`; ordinary upgrades preserve the saved controller
  unless explicitly overridden.

## Installation

Run this command from an **administrator CMD** in the package directory:

```cmd
start "" /wait "zenplus-agent-1.12.9.exe" /machine /quiet /norestart /controller 10.11.40.31 /profile combined /log "%TEMP%\ZenPlus-install.log"
echo Exit code: %ERRORLEVEL%
```

An address without a scheme is normalized to HTTPS. EXE exit code 0 means success;
1 means failure. The log directory must exist. `Install-Silent.cmd` in the local
Downloads folder runs the same deployment with a versioned log filename.

Supported EXE options include `/machine` or `/user`, `/quiet` (aliases `/silent`,
`/verysilent`, `/S`, `/qn`), `/norestart`, `/controller ADDRESS`,
`/profile combined|infrastructure|apm`, `/log PATH`, `/no-start-menu`,
`CONTROLLER_URL=...`, `INSTALL_PROFILE=...`, `CONTROLLER_CA_FILE=...`,
`APM_ENABLED=0|1`, `/uninstall`, and `/purge`. Machine deployment must run as
administrator or SYSTEM; a silent command does not display an elevation prompt.

Standard MSI deployment is also supported:

```cmd
msiexec /i "zenplus-agent-1.12.9.msi" /qn /norestart CONTROLLER_URL=https://10.11.40.31 INSTALL_PROFILE=combined /L*v "%TEMP%\ZenPlus-install.log"
```

After upgrading, open Settings, edit the controller, and press Save. Approve the
Windows administrator prompt for an all-users installation. A different
controller may require approval in its Agent Fleet. TLS verification stays on.

## Validation

- All Go package tests and `go vet ./...` passed.
- Settings tests exercised real temporary configuration writes, preservation of
  unrelated fields and stored state, and rejection of invalid input without
  changing the original file.
- Runtime integration tests verified controller and profile changes are picked
  up without a service restart, including combined-to-APM profile changes.
- Installer tests cover silent switches, validation, and controller overrides.
- Seven release binaries/packages have valid timestamped Microsoft-issued public
  signatures. Native signature tests accept the signed EXE/MSI and reject
  tampered/unsigned packages and invalid downloads.
- Build-time embedded-payload verification passed. MSI extraction confirmed its
  embedded Setup exactly matches the signed standalone EXE. The extracted wizard
  banner was visually checked and has no baked-in product title.
- Microsoft Defender scans found no threats in the EXE or MSI.
- Downloaded both packages from the public Zentryc URLs after publication:
  their SHA-256 hashes match the release manifest and both have valid Zentryc LLC
  signatures with Microsoft timestamps. The public stable manifest reports 1.12.9.

Limits: live elevated Settings Save and a live 1.12.9 installation/update have
not been exercised. The user successfully installed 1.12.8 on this workstation;
that is not a substitute for a live 1.12.9 test. The controller at 10.11.40.31:443
was unreachable from this workstation during the preceding live check. WiX ICE
validation was unavailable under workstation policy. Public signing and a clean
Defender scan do not guarantee that every antivirus or SmartScreen policy will
accept a new release without a warning.

## Files

[EXE](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.9.exe)
and [MSI](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.9.msi).
Local copies: `C:\Users\user\Downloads\ZenPlus-Agent-1.12.9`.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| zenplus-agent-1.12.9.exe | 226111912 | 2e8da47c67e2adc2b2a8ad206385703201c0d6d66e26fc2d65ef9d596acc8fe6 |
| zenplus-agent-1.12.9.msi | 227540992 | e7b7515f5b24efef07045fa7c54b4e3569a6e75d8adda375932e198d1c153845 |

Evidence: `.tools/release-1.12.9-validation`, `.tools/tests-1.12.9.log`,
and `.tools/build-1.12.9.log`.
