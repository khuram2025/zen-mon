# ZenPlus Agent 1.12.6

Status: signed, published, and verified on September 20, 2026.

Public downloads: [guided EXE](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.6.exe)
and [enterprise MSI](https://zentryc.com/downloads/zenplus-agent/stable/zenplus-agent-1.12.6.msi).

## Verified publisher

- Microsoft Azure Artifact Signing Public Trust profile:
  `zentrycsigning/zentryc-public`, East US.
- Publisher: `CN=Zentryc LLC, O=Zentryc LLC, L=Sheridan, S=Wyoming, C=US`.
- Issuer: `Microsoft ID Verified CS AOC CA 03`.
- Timestamp chain: `Microsoft Public RSA Timestamping CA 2020`.
- SHA-256 Authenticode signatures and RFC 3161 timestamps verified on the
  service, CLI, tray application, user collector, telemetry gateway, setup EXE,
  and MSI. Executables are signed before they are embedded in setup.
- The release manifest is generated after signing and continues to require
  Authenticode validation for automatic updates.

The agent carries forward the 1.12.5 functionality. This release increments the
agent version and adds the Microsoft signing build path. The download page is
updated to use the separate MSI URL and checksum now that the guided EXE is the
canonical update package.

## Validation

- Go package tests passed. Windows Application Control initially blocked four
  unsigned test executables; those were compiled, Microsoft-signed, and rerun
  successfully. The installer tests were run from their package directory.
- `go vet ./...`: passed.
- Release-artifact regression tests: 4 passed (`pytest --noconftest`).
- Required-signing argument rejection and PowerShell syntax checks: passed.
- Full offline APM bundle component, version, and configuration checks: passed.
- Signed setup `/verify-payload /quiet`: exit 0 during the build.
- MSI ProductVersion: `1.12.6`; summary template: `x64;1033`.
- Extracted MSI setup binary: exact SHA-256 match to the standalone signed EXE;
  its embedded Authenticode signature is valid.
- SignTool `/pa /all` and Windows Authenticode checks: valid and timestamped.
- Microsoft Defender custom scan of both installers and all five shipped
  Zentryc executables: no threats found, exit 0. Definition version
  `1.459.299.0`, updated September 20, 2026.
- Download-page rendering with the new manifest: correct EXE and MSI links,
  MSI checksum, and verified-publisher status.
- Fresh HTTPS downloads of both published installers: byte counts and SHA-256
  match the locally validated release; Windows reports valid timestamped
  signatures for `Zentryc LLC`. Chrome shows version `1.12.6` and
  `Verified Zentryc publisher` on the live download page.

The previous manifest, page, and latest-alias targets are retained on the web
server at `/home/net/agent-release-backups/1.12.6-20260920T062220Z`.

WiX reported that ICE validation could not run under the workstation's system
policy. MSI metadata, installer unit tests, payload verification, and signature
validation passed. A clean-machine installation and third-party antivirus
testing were not performed in this release session.

## Release artifacts

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `zenplus-agent-1.12.6.exe` | 225,981,872 | `ea74ed484ae01b8cabf6eeead0fe4f12dc1364e2c9bb2a19453b5e662c733850` |
| `zenplus-agent-1.12.6.msi` | 227,520,512 | `251b70f975b2d80c83a4ad93bbcd2b38f932a770f933bade2a9563bc50c13b5e` |

Signing verifies the publisher and package integrity. SmartScreen can still
warn about a new download until reputation develops; signing is not a guarantee
against antivirus detections. See [Microsoft's code-signing guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options).
