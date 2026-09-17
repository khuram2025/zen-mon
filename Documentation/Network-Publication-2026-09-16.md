# ZenPlus NPM publication — 16 September 2026

ZenPlus **1.23.12** is published at https://zentryc.com/ota/releases/ with an active **full, 100% rollout**, without a target-group restriction. The user explicitly requested publication and full rollout. This promotes the existing development-validated artifact; it does not establish completion of fleet installations or the remaining NPM parity work.

## Release identity

- Product: `ota` (ZenPlus).
- Release ID: `4822db79-cc93-4689-84de-454c5ef9bed7`.
- Rollout ID: `bbce6b4b-4627-4911-b00a-048919a052ac`.
- Published: 2026-09-16 12:13:19 UTC.
- Full rollout started: 2026-09-16 12:13:34 UTC.
- Minimum direct upgrade version: **1.23.9**; older versions follow the existing prerequisite chain.
- Package: `update-1.23.12.zup`, **119,231,549 bytes**.
- SHA-256: `0ad09db0f4de39db291c26e8e962d68103f992f05966a2f82f8c44649e4df4e1`.
- Source: `c30e97f8ac240ddb093a1c49d6794d242c58aca1`, branch `codex/network-receiver-validation`.
- Existing bundled Windows agent: **1.12.4**. This release does not publish the separate 1.12.5 agent build.

## Verification and publication

Reverified the Ed25519 signature, all **856 checksums**, package scope, **66 authoritative changed source files**, and absence of forbidden files. The package is byte-identical to the 1.23.12 build installed and validated on the development appliance. The signed manifest retains its original development-candidate description; current publication state is recorded in the authenticated release catalog and this report. No artifact was rebuilt or overwritten.

The public upload returned HTTP 413. The package was transferred over authenticated SSH to the Zentryc origin, then registered through its normal authenticated release-create API over the server's loopback interface. No upload-size configuration or application security controls were changed. Publication and full rollout were performed through the normal public admin API using the immutable release ID. The server's prerequisite signature/hash/version/rollout checks passed.

An independent authenticated catalog read confirmed the product, release, minimum version, digest, publication and full rollout. A complete download through the public HTTPS update endpoint using the existing development appliance identity matched the expected size and SHA-256; TLS verification remained enabled.

## Fleet snapshot

Read-only execution of the installed offer-selection function against active appliance records at 12:13:49 UTC found:

| Result | Count |
|---|---:|
| Direct offer of 1.23.12 | 3 |
| Prerequisite update offered first | 15 |
| Already running 1.23.12 | 1 |
| Other ZenPlus records receiving no offer | 2 |
| Other-product records confirmed excluded from this release | 12 |

Direct offers were confirmed for `zen`, `oly-scan-app-ub`, and `DJSPR-ZNPLSCor1`, all reporting 1.23.10. Many historical records have stale or absent check-ins. One record has an invalid `unknown` version and receives no offer. These are offer-selection results, not evidence that an appliance has downloaded or installed the release. No new release installation reports had arrived during the final check.

## Remaining boundaries

Full rollout activates normal OTA eligibility. Installation still depends on appliance connectivity, local update policy, maintenance windows, prerequisite completion and preflight checks. It does not force an offline appliance online or bypass failed prerequisites.

The previous development validation remains the evidence base: **793 passing regression tests, 43 skips**, **22 passing isolated integration tests**, successful Go checks, signed installation and runtime attestation. Capacity/soak certification, representative vendor interoperability, external notification receipt, complete recovery rehearsal and remaining TypeScript debt remain open. Syslog remains optional, best-effort UDP; the development listener is loopback-only. Protected-main source integration was not performed as part of this artifact promotion.

See [receiver validation](Network-Receiver-Validation-2026-09-15.md) for the detailed implementation, acceptance evidence and recovery limitations.

Publication evidence: [upload](../output/network-publication-2026-09-16/upload.json), [publication](../output/network-publication-2026-09-16/publish.json), [rollout](../output/network-publication-2026-09-16/full.json), [fleet offers](../output/network-publication-2026-09-16/fleet.json), [public download](../output/network-publication-2026-09-16/download.json), [final catalog check](../output/network-publication-2026-09-16/final.json).
