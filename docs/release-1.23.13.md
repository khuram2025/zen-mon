# ZenPlus 1.23.13

Published to Zentryc as a signed appliance-only update with full, 100% rollout. Source: merge commit `0d98fdf204cafbf11f16a1f272469ce3a0395fa0`, tag `v1.23.13`. Windows agent 1.12.5 is a separate release and is not bundled. See [publication evidence](../Documentation/Release-1.23.13-Publication.md).

## Changes

- Service detail SLA and its calendar count confirmed incidents, while individual failed probes remain available as diagnostic evidence. Timeout and network failures have readable explanations and expandable technical details.
- Device list and detail pages use consistent availability percentages, including partial failures. Search links accept `search` and legacy `q` parameters.
- Device detail separates overview, metrics, events, and inventory. Widgets can be moved and resized across device tabs and interfaces; saved layouts can target one device or devices sharing vendor/type within the user's browser.
- F5 system memory uses the system used/total OIDs. TMM, non-TMM and swap remain separate. Expanded SNMP coverage includes pool members, nodes, disks, certificates, modules, CPU cores, failure rates and system identity.
- Earlier branch work includes Device Tracker historical evidence, network monitoring and NCM improvements, RUM work, and agent reliability fixes. See the development validation report for their scope and acceptance limits.

## Upgrade and compatibility

The minimum supported upgrade base remains 1.23.9. Ship the complete migration set. Migration 120 adds F5 template coverage idempotently; migrations through 119 and their historical checksums are preserved. The appliance's earlier manual F5 SQL was numbered 117 locally; the release assigns 120 to avoid collision with the existing NCM migration 117.

Widget layouts are saved per user in browser storage, not synchronized between browsers. The new F5 system-memory series begins with corrected collection; old historical readings are preserved. Certificate inventory is not evidence of active TLS certificate bindings. Missing physical sensors remain unavailable.

## Publication verification

The OTA private key matches the repository public trust anchor and portal authentication succeeded. The portal's highest published version at preflight was 1.23.12, so 1.23.13 is the next candidate.

The sole repository owner explicitly authorized removing the independent-approval requirement for the solo workflow. All five required checks passed, and PR #13 merged normally. CI, conversation resolution, force-push/deletion protections and admin enforcement remain enabled. Production Windows signing remains separate; no unsigned Windows installer was published.

The portal independently confirmed publication, exact package digest, minimum version 1.23.9 and full rollout without a target-group restriction. The development appliance receives the release offer, and its authenticated HTTPS download matched the package hash. Automatic updates remain disabled there; this evidence does not claim fleet installation completion.
