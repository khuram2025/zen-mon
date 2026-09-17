# ZenPlus 1.23.13 release candidate

This candidate combines the existing development branch with the latest monitoring corrections. It is not yet a published OTA release.

## Changes

- Service detail SLA and its calendar count confirmed incidents, while individual failed probes remain available as diagnostic evidence. Timeout and network failures have readable explanations and expandable technical details.
- Device list and detail pages use consistent availability percentages, including partial failures. Search links accept `search` and legacy `q` parameters.
- Device detail separates overview, metrics, events, and inventory. Widgets can be moved and resized across device tabs and interfaces; saved layouts can target one device or devices sharing vendor/type within the user's browser.
- F5 system memory uses the system used/total OIDs. TMM, non-TMM and swap remain separate. Expanded SNMP coverage includes pool members, nodes, disks, certificates, modules, CPU cores, failure rates and system identity.
- Earlier branch work includes Device Tracker historical evidence, network monitoring and NCM improvements, RUM work, and agent reliability fixes. See the development validation report for their scope and acceptance limits.

## Upgrade and compatibility

The minimum supported upgrade base remains 1.23.9. Ship the complete migration set. Migration 120 adds F5 template coverage idempotently; migrations through 119 and their historical checksums are preserved. The appliance's earlier manual F5 SQL was numbered 117 locally; the release assigns 120 to avoid collision with the existing NCM migration 117.

Widget layouts are saved per user in browser storage, not synchronized between browsers. The new F5 system-memory series begins with corrected collection; old historical readings are preserved. Certificate inventory is not evidence of active TLS certificate bindings. Missing physical sensors remain unavailable.

## Publication gates

The OTA private key matches the repository public trust anchor and portal authentication succeeded. The portal's highest published version at preflight was 1.23.12, so 1.23.13 is the next candidate.

GitHub requires all five development checks and one independent approval on PR #13. Production Windows signing still needs `ZENPLUS_CODE_SIGNING_PFX_BASE64` and `ZENPLUS_CODE_SIGNING_PFX_PASSWORD` as repository secrets. These values must never be stored in source.

The user requested full rollout. That authorization is recorded, but does not replace the protected-main review or artifact-signing gates. Build and verify the final signed package from the approved main commit before publishing and enabling rollout. Do not mark this candidate as released until the portal confirms publication and rollout state.
