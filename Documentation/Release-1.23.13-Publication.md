# ZenPlus 1.23.13 publication

ZenPlus 1.23.13 is published at https://zentryc.com/ota/releases/ as a signed appliance-only update with full, 100% rollout, without a target-group restriction. The user explicitly requested publication and full rollout. Windows installers remain separate; no unsigned installer was distributed.

## Identity

- Source commit: `0d98fdf204cafbf11f16a1f272469ce3a0395fa0` on main.
- Source tag: `v1.23.13`; PR: https://github.com/khuram2025/zen-mon/pull/13.
- Release ID: `cee344da-d7a6-4328-9fa0-a2e2d7d668e1`.
- Rollout ID: `e5e250c8-9655-4f9d-a212-0bcdaa07e04c`.
- Portal publication timestamp: `2026-09-17T15:16:47.275147+00:00`.
- Portal rollout timestamp: `2026-09-17T15:16:50.732413+00:00`.
- Minimum upgrade base: `1.23.9`; earlier installations follow prerequisites.
- Package: `update-1.23.13.zup`, 28,043,486 bytes.
- SHA-256: `f4765ab8a2c065874e76bf9a4385e4cedcd43e7ec6f71286864e173c35887629`.

## Verification

Built in a clean Linux checkout of the merged main commit. Verified the Ed25519 manifest signature, 876 checksums, 959 archive entries, complete append-only migration inventory and appliance-only scope. No forbidden credential files were present. Independently compared 151 changed source files byte-for-byte with the merged Git commit. The source tree remained clean after building.

All five required GitHub jobs passed on the merged commit: server, dashboard, poller, Linux agent and Windows agent. Prior source validation passed 851 server tests with 45 skips, all poller tests, the strict dashboard build and the UI regression checks. Windows installer signing is a separate workflow and was not required or bypassed for this appliance-only package.

The standard release publisher uploaded over public HTTPS, independently verified the stored version/hash/minimum version after publication, and created the requested full rollout. A separate read verified the rollout. The registered development appliance was offered 1.23.13; an authenticated public download matched its expected size and SHA-256 with TLS verification enabled.

## Operational scope

The owner approved removing only the required independent-review count because this is a solo project. The count changed from one to zero. Required CI checks, admin enforcement, conversation resolution, force-push and deletion protections were verified unchanged.

The development appliance's automatic-update setting remains disabled. Publication activates OTA eligibility; it does not bypass local update policy, maintenance windows, prerequisites, preflight checks or offline status. This record establishes release availability and download integrity, not completed fleet installation or a canary soak.

New F5 history uses the corrected system-memory basis. Historical evidence remains intact. Widget layouts are browser-local. Certificate inventory does not establish active TLS bindings. NCM real-device and broader interoperability acceptance limits remain in the existing development reports.
