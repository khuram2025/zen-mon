# ZenPlus NCM: feature assessment and implementation plan

Assessment date: 10 September 2026 | Appliance: https://192.168.8.221/ncm | Observed release: 1.23.10

Prepared for the ZenPlus product and network operations teams. Internal assessment.

## Executive assessment

**ZenPlus currently provides an early configuration archive and comparison module. It is not yet a functional replacement for the full SolarWinds Network Configuration Manager module.** The existing implementation is a useful foundation: SSH collection, connection profiles, per-device and bulk settings, scheduled collection, stored versions, downloads, normalized text comparisons, and local change alerts. The main missing layers are reliable recovery, approved configuration standards, governed changes, compliance, lifecycle intelligence, and operational assurance.

**The immediate operational finding is zero backup coverage on the inspected appliance.** The live page displayed 40 inventory entries, 0 enrolled devices, 0 devices with backups, 0% coverage, and no NCM connection profiles. This establishes the state of this appliance at inspection; it does not prove that backups do not exist in another product or location. The inventory includes servers, printers, public DNS targets, and lab/loopback entries, so 40 must not be treated as the number of production network devices requiring configuration backup.

**Recommendation: harden collection and change detection first, then deliver recoverability, then controlled change and compliance.** Do not prioritize a larger dashboard, firmware deployment, or AI-generated changes ahead of trustworthy backups and restore drills. If enterprise recovery and compliance are needed immediately, retain or procure a proven NCCM product during development and use ZenPlus for a restricted pilot.

The decision is based on live UI inspection, local implementation review, a synthetic comparison check, and current official competitor documentation. No production backup, configuration push, restore, firmware operation, enrollment, credential change, or schedule change was performed. The report and plan are the deliverables; appliance implementation is a separate project.

### Management priorities

| Priority | Outcome | Why it comes first |
|---|---|---|
| P0 | Establish the real device scope and achieve verified initial backups | The inspected appliance currently has no configuration recovery evidence. |
| P0 | Correct hidden security changes, incomplete capture acceptance, and access enforcement | A successful-looking backup or diff must be trustworthy. |
| P1 | Protected baselines, running/startup coverage, external alerts, durable jobs, restore workflow | These close the most consequential daily operational gaps. |
| P1 | Approvals, change audit, templates, and configuration compliance | These move ZenPlus from archiving into configuration management. |
| P2 | Certified device expansion, CVE/EOL, firmware workflows, distributed NCM, ITSM and assurance | These approach the breadth of full enterprise NCCM. |
| P3 | Specialized ACL analytics, broad intent assurance, legacy niche functions | Build according to customer demand after foundational gates pass. |

### Planning envelope

Assume two backend/automation engineers, one frontend engineer, one QA/network engineer, and part-time security/SRE support, with access to a representative device lab. A bounded backup-and-recovery release is a **10-14 week planning target**. A broader core NCCM release with change governance and compliance is a **20-26 week target**. Broad SolarWinds-class coverage is a **9-12+ month program**, especially when firmware, device certification, distributed collection, and specialized policy analysis are included. These are engineering estimates, not commitments or measured productivity. Re-estimate after the first two weeks and the supported-device scope is agreed.

## Evidence, scope, and confidence

The benchmark is the full documented functional scope of standalone SolarWinds NCM, with shared SolarWinds Platform services identified separately. This is a product-level comparison, not a claim that every command, integration, or device model has been exhaustively tested. SolarWinds Observability SaaS features and Advanced-only additions are not silently counted as standard NCM. The official NCM administrator guide defines the scope, including backup/transfer, comparison, policy, jobs/templates, approvals, inventory, vulnerabilities, firmware, ACL/policy views, reports, and platform functions. [S1]

Other reference products were selected to cover recognizable categories: ManageEngine NCM for enterprise NCCM; BackBox for recovery assurance; ScienceLogic Restorepoint, now described in current documentation as Skylar Compliance, for backup/compliance; Oxidized for the open-source archive baseline; and NetBrain for governed network change and validation. They are functional benchmarks, not a market-share ranking. Competitor capabilities are vendor-documented, not independently executed in this assessment. Device/plugin/edition support must be confirmed for any purchase.

### Evidence register

| ID | Evidence | What it establishes and what it does not |
|---|---|---|
| E1 | Live /ncm, inspected 10 Sep 2026 | Release 1.23.10; 40 entries; no profiles; no enrollment or backups. Bulk actions visible but disabled without profiles/enrollment. |
| E2 | Live connection-profile dialog | Name, username, SSH port, password, optional enable secret, default profile. No profile was created. |
| E3 | Live core-router-01 detail and platform selector | Backup now (SSH), profile/platform, scheduling toggle, retention default 5, change-alert toggle, zero versions. Nine named platform choices plus Auto-detect. |
| E4 | Local server/app/api/v1/ncm.py, lines 31-318 | Collection commands, SSH verification, pager handling, normalization, version persistence, retention, and local change-alert insertion. |
| E5 | Same file, lines 397-630 | Enrollment and bulk settings, collection execution, scheduling logic, and run-scheduled endpoint. |
| E6 | Same file, lines 636-762 | Manual/API capture, version list, raw content read, same-device diff, and fleet coverage calculation. |
| E7 | Local dashboard/src/pages/NcmDevicePage.tsx and NcmPage.tsx | Scheduling editor, inline diff UI, adjacent comparisons, download, bulk workflows, and positional Baseline label. |
| E8 | scripts/systemd/zenplus-ncm-backup.timer and .service | Hourly timer with up to 120 seconds randomized delay; synchronous HTTP caller has a 600-second timeout. Installation/runtime status not verified. |
| E9 | core/security.py, core/permissions.py, core/crypto.py; migrate-023-ncm.sql | General roles and NCM permission names exist; NCM routes use broader dependencies; credentials use AES-GCM; config body stored as TEXT. Infrastructure encryption not assessed. |
| E10 | Synthetic local execution of the actual normalization function | Password-only and certificate-only changes normalized to identical text; a description change remained detectable. No real secrets or devices used. |
| E11 | Local repository routing and references | NCM router mounted directly; no NCM calls found in the Go poller; no NCM report implementation found in inspected report modules. Absence is limited to reviewed code and UI. |

The local checkout reports version 1.23.10 and commit `b4256ff0f0646db137ad2250f8d441b11464e7ee`. Its version label matches the UI, but deployed backend hashes were not compared. Therefore, implementation findings are strongly relevant local-code evidence, **not proof of exact deployed binaries**. Unrelated working-tree edits were left untouched. An older NCM roadmap exists, but its planned features were not credited as implemented.

Live collection, restore success, active scheduler installation, offsite appliance recovery, disk encryption, key escrow, SSH trust provisioning, lower-privilege access, and load capacity remain unverified. Empty live history prevents validating real historical diffs and alert behavior. Missing below means no corresponding capability found in the reviewed NCM implementation and UI, not a universal claim about all uninspected ZenPlus components.

### Status key

**Present** = a bounded feature exists in UI and/or implementation; not automatically production-tested. **Partial** = related functionality exists but falls short of the stated feature. **Missing** = no implementation found in the reviewed NCM scope. **Unverified** = deployment or behavior needs direct validation. Priority is the recommended implementation order; P0 is urgent correctness/operational work, P1 core NCCM, P2 enterprise expansion, P3 specialized demand-led work.

\pagebreak

## Feature-by-feature comparison: collection and archive

Each row states the SolarWinds reference behavior, the observed ZenPlus position, and the work needed. Source IDs link to official documentation in the source register. Evidence IDs refer to the appliance/local evidence above.

| ID / feature | SolarWinds NCM reference | ZenPlus assessment and required work | Priority |
|---|---|---|---|
| 01. NCM enrollment | Managed NCM nodes and connection information. [S1] | Partial. Shared inventory and enrollment exist, but 0 enrolled live. Define eligible production devices, owners and exclusions. E1/E5. | P0 |
| 02. Manual SSH backup | On-demand configuration download. [S2] | Present in UI/code; unverified on actual devices. Create profiles through authorized operations and certify first captures. E2-E5. | P0 |
| 03. Scheduled backup | Scheduled configuration download jobs. [S3] | Partial. Interval/daily/weekly options; hourly timer can delay requested times. Runtime timer installation unverified. E5/E8. | P0 |
| 04. Bulk operations | Jobs target multiple nodes. [S3] | Present for profile assignment, settings and manual collection. Local bulk request limit 500; not a certified fleet-size limit. E5/E7. | P1 |
| 05. Running config | Download supported configuration types. [S2] | Present. SSH path saves running configuration only. Device-family correctness not proven. E4/E5. | P0 |
| 06. Startup config | Running/startup and other configured types. [S2/S4] | Partial. Manual API accepts startup; SSH does not collect it. Implement device-aware startup retrieval. E5/E6. | P1 |
| 07. Additional config artifacts | Custom config types; template-based transfer. [S2/S5] | Missing. No native certificates, binary bundles, ancillary files or full multi-file recovery package. E4-E6. | P1 |
| 08. Multi-vendor coverage | Default and customizable device templates. [S5] | Partial. Nine explicit UI platform choices plus autodetect. Cisco IOS/XE, NX-OS, ASA, Arista, Junos, PAN-OS, FortiOS, Comware, Huawei. E3/E4. | P1 |
| 09. Auto-identification | Templates can be automatically assigned. [S5] | Partial. Netmiko autodetect falls back to Cisco IOS when no result; unknown commands also fall back to show running-config. Fail explicitly on unsupported devices. E4. | P0 |
| 10. Editable device drivers | Wizard/XML template editing and testing. [S5] | Missing. Command mapping is hardcoded; no supported administrator template editor or certification package. E4/E7. | P2 |
| 11. Connection profiles | Credentials and transfer/command protocols. [S5] | Present for SSH password and enable secret; reusable/default profiles. Live profile list empty. E2/E4. | P0 |
| 12. SSH identity and trust | Device connection configuration/template controls. [S5] | Partial. Strict host-key checking and known_hosts path exist; no key-based login/profile field or visible host-key onboarding workflow. E2/E4. | P1 |
| 13. Transfer protocols | CLI plus documented SFTP/SCP/TFTP transfer arrangements. [S6] | Partial. SSH CLI collection only. Add secure API/SFTP/SCP paths where required; legacy insecure transports should be optional exceptions. E4. | P2 |
| 14. Historical versions | Config archive, imports and maintenance. [S2/S3] | Present. Every successful capture stores a snapshot, including unchanged content. No current dedup despite older comments. E4/E6. | P1 |
| 15. Retention and export | Archive maintenance and export jobs. [S3] | Partial. Keep latest 1-100 snapshots per type; default 5; no time-based policy, pin or legal hold. Download is per version. E4/E6/E7. | P1 |
| 16. Capture validation | Download/transfer status available. [S2] | Partial. Status exists, but no explicit completeness/CLI-error gate before success. Match BackBox's recovery assurance benchmark, not merely transfer status. E4/E5; [S20]. | P0 |
| 17. Imports | Import existing config files. [S7] | Partial. Manual/API text capture endpoint exists; inspected UI has no import workflow or historical migration wizard. E6/E7. | P2 |
| 18. Coverage and freshness | Backup/configuration reporting. [S3/S15] | Partial. Coverage counts any stored version across all inventory; no age/validity/SLA filter. Add eligible, fresh, validated and recoverable coverage separately. E1/E6. | P0 |

## Feature-by-feature comparison: changes and differences

| ID / feature | SolarWinds NCM reference | ZenPlus assessment and required work | Priority |
|---|---|---|---|
| 19. Historical diff | Compare versions of a node. [S8] | Present in code; no real history live. Unified colored diff, latest pair and previous-version comparison. E6/E7. | P1 |
| 20. Arbitrary pair selection | Compare chosen configuration files. [S8] | Partial. API accepts two same-device IDs; UI offers adjacent/latest comparisons, not a general pair picker. E6/E7. | P1 |
| 21. Cross-device compare | Compare configurations on different nodes. [S8] | Missing. API query restricts both versions to one device. Add pair selection with authorization for both devices. E6. | P1 |
| 22. Side-by-side diff | Graphical comparison, including baseline view. [S9] | Partial. Unified inline display only in inspected UI. Add side-by-side, line numbers, navigation and export. E7. | P1 |
| 23. Running/startup mismatch | Config-type comparisons and baseline application. [S4/S8] | Partial. API can compare manually supplied types, but no automatic startup capture or dedicated unsaved-change indicator. E5-E7. | P1 |
| 24. Noise suppression | Config comparison exclusions. [S4/S10] | Partial, correctness concern. Hardcoded global normalization masks secrets/certificates; no raw diff mode. Add scoped exclusions and sensitive-change detection. E4/E10. | P0 |
| 25. Approved golden baseline | Named full or snippet baselines assigned to nodes. [S4] | Missing. Oldest displayed version is labeled Baseline automatically; no approval, pin, ownership or assignment. E7. | P1 |
| 26. Baseline drift tracking | Downloaded config checked against assigned baselines. [S9] | Missing. Previous-snapshot differences are not drift from an approved standard. Add status/history against protected baselines. E4/E6/E7. | P1 |
| 27. Event-triggered capture | Syslog/trap-triggered change detection, with device-specific rules. [S11] | Missing. No NCM event-to-collection path found. Existing trap page is not proof of NCM integration. E5/E11. | P1 |
| 28. External change notification | Change email configuration and notification options. [S11] | Partial. NCM inserts local alerts only; no channel dispatch. Add durable email/webhook/ITSM delivery and retries. E4. | P0 |
| 29. Who/what/when | Config-change reporting and event context. [S3/S11] | Partial. Timestamp/content/source exist; captured_by is ssh/manual/api, not authenticated initiator or device-side editor. Add distinct identities and provenance. E4/E6. | P1 |
| 30. Authorized vs unexpected change | Approval workflow governs NCM-initiated changes. [S12] | Missing. No change request/window correlation. For out-of-band changes, add event/AAA attribution; do not infer editor from backup account. E5/E6. | P1 |
| 31. Fleet configuration search | Search configurations and node properties. [S13] | Missing for config content. Current search filters inventory attributes. Add permission-filtered latest/historical search. E1/E6/E7. | P1 |
| 32. Change history reports | Ad hoc and recurring change reports. [S3/S6] | Missing as a dedicated NCM report. Build date/device/actor filters, change evidence and scheduled delivery. E11. | P1 |

\pagebreak

## Feature-by-feature comparison: restore and controlled changes

| ID / feature | SolarWinds NCM reference | ZenPlus assessment and required work | Priority |
|---|---|---|---|
| 33. Restore/upload to device | Upload stored/edited configuration. [S2] | Missing. Downloading a .cfg file is not restoring a device. Implement driver-specific merge/replace/commit workflow. E4-E7. | P1 |
| 34. Config editing | Edit configuration before upload. [S14] | Missing. Raw view/download only. Add drafts, validation and preview with immutable source version. E7. | P1 |
| 35. Bulk command scripts | Manual/scheduled command jobs. [S3] | Missing. Collection commands are not a script orchestration system. Add signed/versioned job definitions and results. E4/E5. | P1 |
| 36. Parameterized templates | Conditional logic, loops and inventory-backed templates. [S14] | Missing. Add typed inputs, target preview, variable validation and per-vendor generation. E4-E7. | P1 |
| 37. Template sharing | Import/export templates. [S16] | Missing. Add versioned, reviewed template packages and provenance; no arbitrary untrusted template execution. E4-E7. | P2 |
| 38. Approval and separation of duties | Request/approve/decline workflow can be enabled. [S12] | Missing. No draft/approval/execution state machine. Implement maker/checker, scope, expiry and immutable approval binding. E5/E9. | P1 |
| 39. Job history/status | Transfer and script status plus job types. [S2/S3] | Partial. Last backup status/error and synchronous result only. Add durable jobs, per-device steps, retries and cancellation. E5. | P1 |
| 40. Scheduled change/reboot/save | Upload, command and reboot jobs; optional save to NVRAM. [S3/S14] | Missing. Current schedules are backup-only. Add maintenance windows and device-specific persistence semantics. E5. | P2 |
| 41. Failure rollback and postchecks | Recovery primitives; broader network assurance is a separate benchmark. [S2/S23] | Missing. Build pre-change backup, health gates, abort/rollback triggers and post-change comparison before broad push access. E4-E7. | P1 |
| 42. Bare-device recovery readiness | Compare against BackBox's recovery workflow, beyond simple text upload. [S20] | Missing. Need bootstrap prerequisites, full artifact manifest, replacement hardware rules and timed restore drills. E4-E7. | P1 |

## Feature-by-feature comparison: compliance, inventory and lifecycle

| ID / feature | SolarWinds NCM reference | ZenPlus assessment and required work | Priority |
|---|---|---|---|
| 43. Policy/rule engine | Required/prohibited strings, regex, block matching and conditions. [S17] | Missing. Add rule/policy assignment, versioning and pass/fail/unknown results tied to config versions. E4-E7. | P1 |
| 44. Packaged security policies | Compliance capabilities and reusable policy content. [S1/S17] | Missing. Develop reviewed platform-specific CIS/security packs and applicability rules. Passing config checks alone is not regulatory certification. E11. | P1 |
| 45. Compliance remediation | Rule-associated remediation script/template. [S17] | Missing. Begin with guidance/drafts; execute only through the approved change pipeline. E4-E7. | P1 |
| 46. Compliance reports/evidence | Policy reports and NCM report library. [S3/S15] | Missing. Add findings, matched evidence, exceptions/expiry, history and scheduled audit exports. E11. | P1 |
| 47. Detailed device inventory | Inventory scans and hardware/platform reports. [S18] | Partial at platform level. Shared vendor/type/location inventory exists; NCM-specific hardware, serial, image and component collection not established. E1/E11. | P2 |
| 48. Firmware vulnerability matching | Standard NCM: Cisco IOS, IOS XE, IOS XR; NIST-based matching. [S19] | Missing. Add normalized OS inventory, feed freshness, match confidence, triage and vendor scope. Broader vendor matching is a separate expansion. E11. | P2 |
| 49. Offline vulnerability feed | Manual import for closed networks. [S19] | Missing. Plan authenticated feed bundles, provenance and visible stale-feed state. E11. | P2 |
| 50. Firmware repository | Image storage/repository and upgrade templates. [S21] | Missing for network devices. Appliance OTA is a different function. Add checksum, compatibility and access controls. E11. | P2 |
| 51. Firmware deployment | Ordered device upgrades using templates. [S21] | Missing. Add preflight/free-space checks, image validation, reboot/reconnect, postchecks and vendor-specific recovery. E11. | P2 |
| 52. EOL/EOS/EoS | Automatic Cisco/Palo Alto lifecycle data; manual dates for others. [S22] | Missing in reviewed NCM. Add model/part mapping, source dates, confidence, overrides and lifecycle reports. E11. | P2 |
| 53. Cisco ACL analysis | ASA/Nexus ACL management, shadow/redundancy analysis. [S24] | Missing. Text backups do not constitute parsed ACL analysis. Build only for prioritized platforms. E4/E6. | P3 |
| 54. Palo Alto policy views | Policy information for supported PAN-OS devices. [S24] | Missing. PAN-OS show config running is a backup command, not a policy analytics view. E4. | P3 |

\pagebreak

## Feature-by-feature comparison: platform and enterprise operation

These rows include shared-platform capabilities and additional assurance requirements. They are not all standalone NCM license entitlements.

| ID / feature | Benchmark and boundary | ZenPlus assessment and required work | Priority |
|---|---|---|---|
| 55. NCM-specific permissions | NCM roles and shared-platform access controls. [S1/S4] | Partial, enforcement gap. ncm.view/manage names exist, but reads use general login and writes use broad operator/devices.manage checks. Add scoped permissions for content, credentials, restore and approval. E9. | P0 |
| 56. Audit and identity | NCM audit reports and change approvals. [S12/S15] | Partial platform foundation. No NCM audit writes found; capture source is not actor identity. Record read/export/change/credential events and protect audit retention. E6/E9/E11. | P1 |
| 57. Protected config repository | Enterprise security requirement; ME advertises encrypted storage. [S25] | Partial. CLI credentials encrypted; config content stored as plain TEXT at application layer. Disk/database encryption unverified. Add raw-config encryption and redacted views. E4/E9. | P0 |
| 58. Protected scheduler invocation | Operational/security requirement, not a special competitor feature. | Missing at route layer. run-scheduled has no auth dependency or local-source check; reviewed proxy template forwards /api. Live exposure not probed. E5/E9/E11. | P0 |
| 59. Scalable collection | Job-based operation; polling scale and HA are platform/deployment features. [S3/S26] | Partial. Scheduler awaits each device serially; no NCM durable queue, device lock or collector routing found. E5/E8/E11. | P1 |
| 60. Remote-site NCM | Platform deployment; ME Enterprise central/probe is explicit. [S26/S27] | Missing in reviewed NCM. API host directly runs SSH; existing ZenPlus sensors do not prove remote NCM execution. E5/E11. | P2 |
| 61. HA and appliance disaster recovery | SolarWinds Platform HA/deployment dependencies. [S26] | Unverified. No NCM failover, offsite config/key backup, or full appliance recovery drill demonstrated. Add independent archives and recovery evidence. E8/E9. | P1 |
| 62. API and integration | SWIS supports NCM inventory/templates; integrations vary. [S14] | Partial. REST routes exist for core archive operations; no NCM webhook event contract, ITSM lifecycle or Git export found. E5/E6/E11. | P2 |
| 63. Operational reporting | Predefined NCM reports and shared report platform. [S15] | Partial. Basic fleet totals and network/security dashboard integration; no NCM job, stale-backup, recovery or compliance reports found. E1/E11. | P1 |
| 64. Safe-change assurance | NetBrain pre/during/post-change validation; beyond basic NCM diff. [S23] | Missing for NCM. Monitoring/maps can be reused, but are not yet linked to change intent, blast radius or acceptance gates. E11. | P2 |
| 65. EnergyWise and legacy niche actions | EnergyWise appears in the full NCM guide. [S1/S6] | Missing in reviewed NCM; defer unless installed customers need it. Do not treat niche legacy parity as a release blocker. E11. | P3 |

### Interpreting the matrix

There are 65 assessed capability lines, deliberately separating recovery, governance and operational quality from simple menu availability. The lines are not equally valuable, so an unweighted percentage would be misleading. Do not equate a Present classification with a completed acceptance test: the live appliance has no backups with which to prove those paths. The strongest conclusion is **foundational archive capability exists; operational protection is not established; full NCCM breadth is materially incomplete**.

## Comparison with other recognized systems

The table records positive capabilities supported by the sources reviewed. D = documented; L = limited, integrated or edition/device-dependent; U = not established by this assessment. U is not a claim that the competitor lacks the feature. Oxidized is evaluated as the backup project plus its documented web extension/hooks, not as a separately assembled enterprise suite.

| Capability | ManageEngine NCM | BackBox | Restorepoint / Skylar Compliance | Oxidized | NetBrain |
|---|---|---|---|---|---|
| Scheduled backup/archive | D | D | D | D | L: broader NCCM |
| Multi-vendor collection | D | D | D | D | D: broader network model |
| Version comparison | D | U | D | D | D: change validation |
| Event-triggered capture | D | U | U | L: syslog integration | U |
| External change/exception notification | D | D: backup failures | D | L: hooks | L: workflow/ITSM |
| Native device restore | D | D | D | U: backup-focused | L: change rollback |
| Explicit backup-validity assurance | U | D: five-step verification | D: integrity verified | U | U |
| Golden configuration / baseline | D | L: automation | D | U | D |
| Bulk configuration automation | D: configlets | L: broader offering | D | U | D |
| Change approval workflow | D | U | U | U | D |
| Compliance evaluation/remediation | D | L: broader offering | D | U | D |
| Firmware vulnerability/upgrade | D | U | U | U | U |
| Distributed collection/control | D: Enterprise | U | L: edition/agents | L: assemble instances | L: platform design |
| Git-backed history | U | U | U | D | U |
| Pre/during/post-change network checks | U | L: pre/post workflow | U | U | D |

**ManageEngine NCM** is the closest alternative full-NCCM benchmark here. Its documented backup, comparison, running/startup synchronization, approvals, configlets, compliance, firmware and report features exceed the current ZenPlus module. Central/probe operation is an Enterprise distinction. Avoid using vendor count alone as proof of support for a specific firewall mode or OS version. [S25/S27/S28]

**BackBox** sets the recovery benchmark: it documents verification at backup creation and again before restoration, a five-step process, and device/bare-metal restore workflows. ZenPlus needs validated artifacts and a tested recovery process, not just stored CLI output. Broader BackBox configuration automation/compliance offerings require scope confirmation. [S20/S29]

**Restorepoint / Skylar Compliance** documents version comparisons, visibility of ignored changes, editable configuration restore, policy checks and notification. Current documentation uses the Skylar Compliance name; older references and APIs retain Restorepoint. This highlights gaps in ZenPlus's raw-versus-normalized view, recovery, policy evidence and operational history. [S30/S31/S32]

**Oxidized** already provides a substantial open-source backup benchmark: extensible device models, adaptive retrieval threads, Git outputs, REST access and optional syslog-driven collection with user attribution. Its secret-removal option can reduce restore completeness; retain recoverable raw material separately when designing a redacted repository. Full change approval and compliance are not established as native Oxidized features in the reviewed sources. [S33/S34]

**NetBrain** is the broader change-assurance benchmark. Its documented process validates before, during and after changes, supports intent-based checks, approvals, task history and rollback triggers. ZenPlus can eventually connect its monitoring data to similar safeguards, but maps and alerts alone do not provide these workflows. It is not treated here as a like-for-like backup-only product. [S23/S35]

\pagebreak

## Highest-impact findings and remediation

### F1. No current backup coverage on the appliance - P0

Live evidence E1-E3 shows no connection profiles or backups. First establish the eligible production network-device list. Exclude monitoring-only targets and label lab devices explicitly. Assign owners, device families, approved credentials, backup type requirements and recovery criticality. Enroll a small representative pilot, validate complete captures, then expand. This is an operational setup gap as well as a product adoption gap; no new feature is needed merely to create the first SSH snapshot.

Acceptance: every in-scope production device has either a validated fresh backup or a named exception with owner and expiry. Coverage uses that eligible set as its denominator. A timestamp alone must not imply recoverability.

### F2. Secret and certificate changes can be invisible - P0

E4/E10: `_VOLATILE_PATTERNS` replaces whole PEM bodies and Cisco secret/password values before both change classification and the returned diff. A synthetic Cisco enable-secret change and a synthetic certificate-body change both produced different raw text but identical normalized text. An interface-description change remained visible. This does not mean raw data is lost: snapshots are stored. It means change flags, diffs and change alerts can miss relevant security changes.

Remediation: maintain encrypted raw artifacts, stable sensitive-field fingerprints where appropriate, a normalized comparison representation, and a redacted display representation. Make exclusions platform-specific, visible and versioned. Allow authorized raw comparisons. Test stable and re-salted secret formats separately so security changes remain detectable without constant false positives.

Acceptance: known password/key/certificate changes produce a sensitive-change signal without exposing the secret; timestamp-only changes remain suppressed; the UI explains ignored fields.

### F3. A truncated or erroneous capture can be accepted - P0

E4/E5: the channel reader can stop after silence or a deadline and return accumulated content without confirming a complete command response. `_do_fetch` then saves the content and records success, with no explicit non-empty, command-error, expected-section or end-marker validation. Privilege escalation errors are swallowed. This is a source-code risk; no malformed live backup was produced during this review.

Remediation: device-driver validators must reject empty output, permission/command errors, incomplete paging, missing required sections and unresolved prompts. Distinguish success, incomplete, unsupported, authentication failure and timeout. Quarantine failed artifacts and preserve the last validated backup. Check all required contexts, partitions and ancillary files for supported firewall/load-balancer modes.

Acceptance: fault-injected captures never become a successful recovery point. Every published backup has a manifest, validation result and collector/driver version.

### F4. Scheduler authorization and NCM permissions need correction - P0

E5/E9: `run_scheduled` has no authentication or source restriction in the route. The reviewed provisioning template forwards `/api/` to the API without a route-specific restriction. This creates a potential unauthenticated job-triggering path if the deployed service matches this code; live exploitability was not tested. Config reads require a valid login but not `ncm.view`; writes use a helper that allows operator/admin or `devices.manage`, rather than requiring `ncm.manage`.

Remediation: move schedule execution into an internal worker, or require explicit service identity plus authorization and appropriate network restriction. Enforce NCM-specific permissions and device/site scope on every list, content, comparison and mutation route. Separate credential administration, raw export, restore, template authoring and approval. Audit initiating identities.

Acceptance: a role with no NCM permission cannot enumerate/read configs; cross-scope IDs fail; unauthenticated and unauthorized schedule triggers fail; approved internal jobs still run. Verify through staging API tests and deployed access configuration.

### F5. Config storage and retention do not yet protect recovery evidence - P0/P1

E4/E9: credentials use AES-GCM, but raw config bodies enter a PostgreSQL TEXT column without application-layer encryption. This does not establish whether disks or database backups are encrypted. Retention deletes old snapshots, with a default of five. Identical captures still consume the limit. Thus a useful changed version can disappear after five subsequent successful captures, even if none of those captures changed the config. Existing alert metadata can also outlive referenced versions.

Remediation: encrypt raw artifacts, separate backup-run history from unique configuration content, protect approved baselines and incident/change evidence, define count plus age policies, and retain necessary diff evidence. Add encrypted offsite archives, key recovery and an appliance restore drill. Redact routine views while preserving raw restore material.

Acceptance: a protected baseline and change evidence survive normal retention; old content remains readable after key rotation; a separate recovery environment restores configs, metadata and keys from the approved backup set.

### F6. Baseline semantics are misleading - P1

E7: the oldest entry in the currently displayed version list is labeled Baseline based only on its position. The list defaults to 50 results, even though retention can be 100. That label can therefore denote neither the first capture nor the actual oldest retained capture. It is not an approved or pinned standard and can move as new captures/pruning occur.

Remediation: rename the positional label to describe history, add explicit baseline records with name, scope, owner, approval state, config type, protected content and revision history, and show drift separately from last-capture change. Implement pagination and a full pair selector.

Acceptance: only an approved baseline is displayed as approved; retention cannot move or delete it silently; comparing old selected versions is reliable beyond 50 entries.

### F7. Change notification stops in the database - P0

E4: `_raise_change_alert` explicitly inserts an alert with no channel dispatch. The application having email/webhook channels elsewhere does not mean NCM changes reach them. Backup failures record last_error but no dedicated NCM failure-notification workflow was found.

Remediation: publish `config.changed`, `backup.failed`, `backup.stale` and `baseline.drift` events through a durable notification path. Add delivery state, retry, deduplication and escalation. Keep full secrets out of outgoing diffs. Event-triggered collection needs debounce and loop prevention.

Acceptance: a lab change generates one alert and the selected outbound notification; delivery failure is visible and retried; a stale backup escalates even if no new job runs.

### F8. Scheduling and fleet execution are fragile at scale - P1

E5/E8: scheduled devices are processed serially inside an HTTP request. The systemd caller gives up after 600 seconds; the timer fires hourly with random delay. This can cause caller timeouts or poor feedback for larger fleets. It does not prove that the server stops work when the caller times out. No durable NCM queue, per-device lock, backoff, task cancellation, or remote collector selection was found. Database-local daily/weekly timing also needs explicit timezone behavior.

Remediation: implement durable jobs, bounded concurrent workers, leases, idempotency and per-device locking. Expose due/next-run times with timezone, queue age and collector health. Store attempt and success times separately; retry transient failures without storms. Return a job ID immediately to the UI.

Acceptance: overlapping manual/scheduled requests do not duplicate a capture; worker restart resumes safely; large-fleet testing meets the agreed freshness target without blocking the API or ordinary monitoring.

### F9. Collection can alter FortiOS console settings - P1

E4: paging preparation sends `config system console`, `set output standard`, and `end`. This is configuration-mode activity, rather than a clearly session-only read. Its persistence and effect depend on the device/version and must be checked in the lab. Broad write credentials should not be required merely to collect a backup.

Remediation: prefer supported session-local controls, or handle paging without persistent changes. Where a driver requires a change, explicitly document it and restore prior state where supported. Certify behavior using least-privilege accounts.

Acceptance: scheduled collection leaves persistent device configuration unchanged, or an explicitly documented and approved exception exists for that driver.

\pagebreak

## Implementation roadmap

Estimates below are incremental person-weeks, not elapsed calendar time. With four full-time contributors, 20 person-weeks is not necessarily five elapsed weeks because of dependencies, lab access and review. The proposed envelope includes partial overlap after the relevant gates, and needs 20-30% contingency for unfamiliar device families and migrations. No budget or team availability has been confirmed.

| Phase / target window | Deliverables and dependencies | Indicative effort | Exit gate |
|---|---|---|---|
| 0. Establish truth; weeks 1-2 | Inventory eligibility, deployed version/hash check, scheduler/access verification, driver lab, backup/restore requirements; begin F2-F4 corrections. Owners: network lead, backend, security. | 5-8 person-weeks | Approved device scope and test corpus; P0 risks have reproducible checks and accountable owners. |
| 1. Trustworthy collection; weeks 3-6 | Capture validation, scoped authorization, internal scheduling, raw/normalized/redacted separation, encrypted artifacts, failure/change delivery, freshness dashboard. Depends on phase 0. | 12-18 person-weeks | Fault tests pass; representative devices collect valid data; no hidden sensitive changes; unauthorized access denied. |
| 2. Recovery and change visibility; weeks 7-14 | Durable jobs/locks, running/startup, protected baselines, retention, pair/cross-device/raw diff, search, export, restore workflow and initial recovery drills. Depends on validated drivers and authorization. | 20-28 person-weeks | Backup-and-recovery release: restore proven per supported family; pilot freshness and recovery gates pass. |
| 3. Governed NCCM; weeks 15-26 | Draft/template jobs, approval, pre/postchecks, rollback controls, policy engine, initial compliance packs, immutable audit and scheduled NCM reports. Depends on phase 2 recovery. | 30-42 person-weeks | Changes are bound to approvals; negative-path rollback and policy evidence verified in pilot. |
| 4. Enterprise expansion; months 7-12+ | Device certification expansion, remote collectors, HA/DR, ITSM/Git integrations, lifecycle/CVE, firmware repository/upgrades, targeted ACL/policy analysis and intent checks. Depends on core NCCM stability. | 70-110+ person-weeks | Scale, site-failure and vendor-specific lifecycle/change acceptance passed for the contracted scope. |

### First ten working days

1. Classify the live inventory into production network devices, lab devices and monitoring-only targets. Record vendor, model, OS, management context, owner, criticality and required backup artifacts. Do not infer support from a vendor label.
2. Compare deployed NCM files with the reviewed commit; inspect the installed scheduler, service logs, access restrictions and key/backup arrangements. This needs an authenticated read-only server session during implementation planning.
3. Select three representative device families from actual production needs, with lab instances and least-privilege CLI accounts. Include at least one firewall context/partition use case if deployed.
4. Correct normalization and completion validation, then enforce scheduler and NCM permissions before expansion. Turn the synthetic examples into regression tests using fake credentials only.
5. Establish credential profiles and SSH trust through the approved operational process. Run a pilot capture, inspect completeness, induce a benign lab change, and verify changed/unchanged and failure states.
6. Agree RPO/RTO by criticality; document recovery prerequisites. Validate an independent encrypted archive and at least one isolated restore before broad reliance on ZenPlus.

### Engineering work packages

| Package | Concrete scope | Depends on | Acceptance evidence |
|---|---|---|---|
| WP1: identity/access | ncm.view, manage, raw-export, credentials, restore, approve; device/site scoping; service identity | Phase 0 | API role matrix, negative tests and deployed endpoint access checks |
| WP2: driver contract | Discover, fetch config types, validate artifact, restore plan, apply, verify; explicit unsupported capability states | WP1, device lab | Fixtures and device results by model/OS/context |
| WP3: archive | Encrypted blobs, raw hashes, comparison versions, redacted views, manifests, protected pins, independent run history | WP1/WP2 | Migration, tamper/error detection, key rotation, retention and recovery tests |
| WP4: job service | Persisted queue, bounded workers, leases, locks, retry/backoff, timezone, cancel and progress | WP1/WP2 | Restart/overlap/failure and throughput evidence |
| WP5: change intelligence | Raw/normalized/sensitive diff, pair selection, baseline revisions, running/startup, search and event attribution | WP2/WP3 | Noise/security-change corpus and end-to-end event tests |
| WP6: restore/change execution | Preflight, approved immutable plan, prebackup, canary, maintenance window, postchecks and driver-specific rollback | WP1-WP5 | Successful restore and negative-path rollback drills |
| WP7: policy/reporting | Rule/policy packs, applicability, evidence, exceptions, remediation draft, NCM report schedules | WP3/WP5/WP6 | Pass/fail/unknown test cases and exported audit report |
| WP8: enterprise/lifecycle | Collector routing, offsite DR, OS/model enrichment, CVE/EOL feeds, ITSM and firmware workflows | Stable WP1-WP7 | Site outage, restore, feed quality and vendor upgrade tests |

## Proposed architecture

Reuse the existing FastAPI, PostgreSQL, authentication, device inventory, notification and frontend foundations. Keep the first release compatible with Netmiko while separating collection from HTTP request handling. A wholesale collector-language rewrite is not necessary to close the initial reliability gaps.

The intended flow is: **UI/API or scheduler -> authorized job -> durable queue -> assigned collector/driver -> validated encrypted artifact -> version/diff/baseline/policy evaluation -> audit and notification -> evidence/reporting.** A device-writing job adds approval, a pre-change recovery point, canary limits and postchecks before it can finish successfully.

Keep these records distinct:

- Device capability and collector assignment: exact model/OS/context, supported config types and approved driver version.
- Backup job and attempts: initiator, trigger, schedule/timezone, target snapshot, retries, start/end, validation and failure reason.
- Configuration artifact: encrypted raw content or bundle, hash, manifest, format, parser/normalizer versions and validation state.
- Configuration version and baseline: device/type/content reference, approved baseline identity, retention/pin state and source provenance.
- Change event: before/after, sensitive-field signal, collection actor, device-side editor when known, event source and linked change request.
- Change request/execution: immutable plan hash, approval scope/expiry, maintenance window, per-device steps, pre/post evidence and rollback result.
- Policy result and audit event: exact rule/config revisions, evidence, exceptions and accountable actor; independent retention.

Do not use a content hash alone as proof of authenticity. Protect the artifact store and key lifecycle, verify manifests, and audit privileged reads/exports. A redacted config is useful for collaboration but may not be sufficient for restoration. Device replacement may also need keys, certificates, licenses, boot information, controller state or vendor-specific bundles.

For future remote collection, use explicit site assignment and an authenticated collector protocol. Existing sensor management is an integration opportunity, but the current central SSH function must not be presented as distributed NCM. Start with PostgreSQL-backed jobs if suitable; select another queue only after failure and throughput requirements justify it.

\pagebreak

## Release acceptance and operational targets

These are proposed targets for validation, not measurements of the current appliance. Confirm service objectives with device owners and revise them for maintenance windows, unreachable devices and vendor limitations.

| Area | Proposed target / test |
|---|---|
| Eligibility and onboarding | 100% of production network devices classified; each enrolled or covered by a named, expiring exception. |
| Routine freshness | At least 99% of eligible reachable devices have a validated backup within the agreed RPO for 14 consecutive pilot days. Suggested initial RPO: 24 hours ordinary, 4 hours critical. |
| Change capture | For certified syslog/AAA event sources, 95th percentile event-to-validated-version under 2 minutes in the pilot. Report unsupported devices separately. |
| Trustworthy status | Empty/truncated/CLI-error captures never report validated success; failed attempts preserve the last good recovery point. |
| Comparison accuracy | Detect meaningful configuration and supported sensitive-field changes; suppress known volatile noise; allow authorized raw review. |
| Retention | Unchanged captures do not evict protected baselines or required incident evidence. Pagination exposes all retained versions. |
| Notifications | Change, failure and stale-backup events delivered to test destinations with retry/dedup and visible delivery state. |
| Recovery | Isolated restore per supported family and important context; proposed initial RTO under 30 minutes for text restore on a reachable, prebootstrapped supported device. Bare-metal RTO separately measured. |
| Change governance | Approval references the exact plan/targets; edits invalidate approval; maker/checker enforced; unsafe target or failed precheck blocks execution. |
| Rollback | Failed canary stops batch expansion; tested rollback or explicit manual recovery path restores service in the lab. Never assume atomic rollback across vendors. |
| Security | No NCM access without scoped permission; raw exports audited; service-trigger authentication; encrypted archive/key recovery and rotation tests. |
| Resilience and scale | Worker restarts and overlapping triggers do not lose or duplicate work; certify 100, then 500, then the required fleet size using realistic slow/failing devices. |
| Disaster recovery | Recover an independent appliance environment including database, artifacts, keys, baselines and schedules. Record achieved RPO/RTO. |
| Compliance | Rules tested on compliant, violating and unparseable configs; unknown is never passing; report tied to exact rules and config versions. |

### Suggested vendor certification matrix

Begin with the actual customer fleet, not every Netmiko model. For each selected platform record supported OS releases, SSH algorithms, privileges, config types, paging/large-output behavior, context/partition handling, raw-secret completeness, restore merge/replace semantics, save/commit/reboot requirements and rollback support. Include Cisco IOS/XE and relevant NX-OS/ASA, FortiOS, PAN-OS, Junos, then other devices demanded by the inventory. Aruba and F5 inventory entries do not by themselves prove NCM driver support; they are not named choices in the inspected selector.

Run positive and negative cases: expired/wrong credentials, unknown/changed host key, denied enable, unsupported command, partial disconnect, huge output, pager stalls, empty configuration, multiple contexts, unchanged capture, sensitive-only change, clock/timezone transitions, overlap, worker restart, retention, notification failure and isolated restoration. Use lab devices or vendor images with the required rights and no production-impacting fault injection.

## Build-versus-buy assessment

**Continue building if NCM is a strategic ZenPlus product feature and a narrow certified first release is acceptable.** Existing archive/UI/crypto foundations reduce the starting effort, but the recovery and job/governance layers remain substantial. Keep the customer promise scoped to tested platforms and workflows.

**Use a commercial NCCM system as the operational control now if immediate audited recovery, change approvals or compliance are required.** A pilot should test the exact production device modes, approval model, restore drill, external notifications and restricted-site deployment. Compare ManageEngine and SolarWinds for full NCCM; include BackBox/Restorepoint when recovery and compliance dominate, and NetBrain when change impact/assurance dominates. This is a functional shortlist, not a procurement award or a price comparison. [S1/S20/S23/S25/S32]

**Use Oxidized as a potential collection/archive component or interim benchmark, not as proof of full NCCM parity.** Integration still needs identity, encryption/redaction, baseline semantics, policy, recovery, durable event delivery and operational support. Evaluate maintenance ownership and licensing before adopting it into a distributed commercial appliance. [S33/S34]

The purchase evaluation should include device counts, HA/remote-site entitlements, required feature editions, maintenance/support, supported OS versions, offline operation and measured recovery outcomes. Pricing and license quotes were not requested or verified, so no total-cost figures are asserted here.

## Final recommendation

Treat the current module as **Config Backup and History**, with its supported behaviors clearly stated. Do not claim full SolarWinds NCM equivalence yet. Make the next release about validated backups, correct change detection, protected retention, scoped access and reliable notification. Then complete recoverability and baselines; after that, add governed configuration changes and compliance. Enterprise lifecycle and assurance should follow measurable recovery success, not precede it.

This report completes the requested feature assessment and plan. It does not certify live recovery or deployed security controls: those require the explicitly listed pilot and deployment verification work.

\pagebreak

## Official source register

Sources reviewed on 10 September 2026. Product documentation is a moving reference; feature availability depends on edition, release, device and configuration. Links below support the nearby source IDs throughout this report. Local evidence is separately identified as E1-E11.

- S1. [SolarWinds NCM administrator guide](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm_administrator_guide.htm?CMP=DIRECT&CMPSource=THW) - full module scope and platform boundary.
- S2. [Transfer, edit and search network configuration files](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-managing-configuration-files.htm) - backup, transfer, imports and config types.
- S3. [NCM job types](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-select-a-job-type.htm) - download, upload, scripts, export, reports, maintenance and reboot jobs.
- S4. [Establish baselines](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-what-is-a-baseline.htm) - approved full/snippet baselines, assignment and exclusions.
- S5. [Create and manage device templates](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-create-device-template.htm) - device commands, connection and template customization.
- S6. [NCM getting started guide](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm_getting_started_guide.htm) - reporting, transfer servers and legacy scope.
- S7. [Import network config files](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-importing-configuration-files.htm) - archive/file imports.
- S8. [Manually compare two network configuration files](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-comparing-configurations.htm) - same-node and cross-node comparison.
- S9. [Find and review baseline differences](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-find-review-differences-baselines-configs.htm) - automatic comparison and baseline status/view.
- S10. [Compare a config to a baseline or another config](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-compare-config-files-parent.htm) - comparison and exclusion workflow.
- S11. [Configure real-time change detection](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-configuring-real-time-configuration-change-detection.htm?CMP=DIRECT&CMPSource=THW) - event rules, retrieval and email; device-specific limitations.
- S12. [Approval system for configuration changes](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-approving-device-configuration-changes.htm) - optional approval enforcement.
- S13. [Search network config files or node properties](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-searching-for-configuration-files-web-console.htm) - indexed text search; advanced search is not regex search.
- S14. [About NCM config change templates](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-config-change-template-basics.htm) and [execute a template](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-executing-a-config-change-template.htm) - programming logic, inventory, roles and save-to-startup option.
- S15. [Access predefined NCM reports](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-access-reports.htm) - dedicated NCM report categories.
- S16. [Import and export config change templates](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-import-export-config-change-templates.htm) - portable templates.
- S17. [Create and manage policy rules](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-manage-policy-rules.htm?CMP=DIRECT&CMPSource=THW) - required/prohibited conditions, block matching and remediation.
- S18. [Run an NCM inventory scan and view data](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-inventory.htm) - device inventory and reports.
- S19. [View firmware vulnerability data](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-working-with-firmware-vulnerability-data.htm) - NCM device scope, feed import, enrichment limits and Advanced-license distinction.
- S20. [BackBox backup and recovery](https://www.backbox.com/backup-and-recovery/) - verification, restore and exception notification.
- S21. [SolarWinds firmware upgrades](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-firmware-upgrades.htm) - repository, operations and upgrade templates.
- S22. [Manage End of Support, Sales and Life data](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-managing-end-of-support-and-end-of-sales.htm) - Cisco/Palo Alto automatic sources and manual other-vendor dates.
- S23. [NetBrain change management](https://www.netbrain.com/features/network-change-management/) - pre/during/post validation, approvals, task history and rollback triggers.
- S24. [Manage Cisco ACLs and Palo Alto policies](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-manage_cisco_asa_acl_rules.htm) - scoped ACL/policy features.
- S25. [ManageEngine NCM features](https://www.manageengine.com/network-configuration-manager/features.html) - backup, storage, comparison, configlets, compliance, firmware and reports.
- S26. [SolarWinds Platform features in NCM](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm-orion-platform.htm) - shared platform and HA boundary; validate deployment entitlements.
- S27. [ManageEngine NCM Enterprise](https://www.manageengine.com/network-configuration-manager/enterprise-network-configuration-management.html) - central/probe design and distributed operations.
- S28. [ManageEngine network change management](https://www.manageengine.com/network-configuration-manager/network-change-management.html) - approvals, running/startup synchronization and notifications.
- S29. [BackBox configuration management solution brief](https://info.backbox.com/hubfs/solution-brief/new-roadmap-network-config-management.pdf) - broader configuration automation/compliance context.
- S30. [Skylar Compliance / Restorepoint basic operation](https://docs.sciencelogic.com/restorepoint/latest/Content/Web_Restorepoint/03_Basic_Operation.html) - comparison, ignored changes, restore and current product naming.
- S31. [Restorepoint API](https://docs.sciencelogic.com/restorepoint/api/5-6/api.html) - policy rules, permissions, backup failure policy and automation surface.
- S32. [ScienceLogic Restorepoint solution brief](https://sciencelogic.com/wp-content/uploads/2023/12/ScienceLogic-Restorepoint-Solution-Brief.pdf) - archive, automation, compliance and remote operation context.
- S33. [Oxidized official README](https://github.com/ytti/oxidized/blob/master/README.md) - device models, threads, API, syslog examples, Git and hooks.
- S34. [Oxidized official configuration documentation](https://github.com/ytti/oxidized/blob/master/docs/Configuration.md?plain=1) - model configuration, time limits, secret removal and significant-change storage.
- S35. [NetBrain function and workflow overview](https://www.netbraintech.com/docs/12ne3wy0aw/help/HTML/welcome.html) - broader automation and network-model scope.

## Local implementation locators

All paths are relative to `C:/Users/user/Documents/ZenPlus` at the assessed commit. These contain implementation evidence, not saved credentials.

| File / locator | Relevant evidence |
|---|---|
| server/app/api/v1/ncm.py:31 | Platform commands and supported platform selector data |
| server/app/api/v1/ncm.py:62 | SSH connection, host trust, autodetect and collection preparation |
| server/app/api/v1/ncm.py:141 | Raw channel reading and completion behavior |
| server/app/api/v1/ncm.py:199 | Volatile patterns and normalization |
| server/app/api/v1/ncm.py:222 | Snapshot persistence, change classification and retention |
| server/app/api/v1/ncm.py:273 | Change alert insertion without channel dispatch |
| server/app/api/v1/ncm.py:536 | Fetch result saved as running config and success status |
| server/app/api/v1/ncm.py:599 | Unauthenticated scheduled route and serial loop |
| server/app/api/v1/ncm.py:636 | Manual capture types and source metadata |
| server/app/api/v1/ncm.py:657 | Version listing, raw reads and same-device comparison |
| server/app/api/v1/ncm.py:713 | All-inventory coverage calculation |
| dashboard/src/pages/NcmDevicePage.tsx | Diff display, retention controls, positional Baseline label and download |
| dashboard/src/pages/NcmPage.tsx | Fleet controls, filters, profiles and bulk backup behavior |
| server/app/core/security.py | General login and operator permission helpers |
| server/app/core/permissions.py | NCM permission definitions |
| server/app/core/crypto.py | Credential encryption utility |
| scripts/migrate-023-ncm.sql | Configuration TEXT storage schema |
| scripts/systemd/zenplus-ncm-backup.timer | Hourly schedule and randomized delay |
| scripts/systemd/zenplus-ncm-backup.service | HTTP-based scheduled invocation and timeout |
| scripts/provision-main-appliance-golden.sh:272 | Reviewed generic /api proxy template; deployed configuration unverified |
