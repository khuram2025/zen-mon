# NCCM Phase A: onboarding and recovery operations

Reviewed 22 September 2026. This release adds prerequisite visibility and access guidance. **Format-validated configuration is not restore certification.** Representative device captures, scheduled runs and isolated recovery tests remain acceptance gates.

## 1. Establish backup scope

Open NCCM and review each device's eligibility, collector support and readiness. Eligibility comes from inventory type/address or an explicit operator override. An override does not implement a driver. Do not silently change inventory classification to improve the coverage percentage.

Current fleet disposition before onboarding: 37 inventory devices; 14 automatically eligible (5 Aruba, 3 Fortinet, 6 Palo Alto); 2 F5 records classified as `other` need review. The five Aruba devices lack an explicit collector; F5 also lacks a collector. Keep these limitations visible. Fortinet and Palo Alto have implemented drivers but no certified pilot on this appliance yet. Remaining inventory records are excluded by type/address unless an operator explicitly includes them. Coverage remains fresh validated captures divided by eligible devices; unsupported eligible devices are not silently removed from that denominator.

## 2. Create a restricted connection profile

Use NCCM → Connection Profiles. Select the vendor/OS in **access guidance**, read the brief and expand commands/completeness limits. This is a help selector; the actual driver is selected in each device's settings. Profiles store SSH username/password and optionally an enable secret. They do not currently implement SSH-key authentication or API-token collection.

Use a dedicated account, restrict management source addresses to the actual collector egress, and retain device-side authentication/command audit records. Avoid shared human administrator accounts. Select the driver explicitly to limit autodetection probes. Session preparation commands can vary with the installed Netmiko version; confirm the allowlist on the exact model/OS in the pilot.

| Platform | Least-privilege starting point | Completeness boundary |
|---|---|---|
| Cisco IOS/XE | Dedicated SSH account with TACACS+ authorization permitting configuration-read and terminal preparation commands, denying device changes. Full output may require EXEC privilege 15 combined with restrictive command authorization. | Lower local privilege levels/parser views can suppress configuration. This collector uses `show running-config`, not `show running-config view full`. Verify output before accepting it. Do not grant unrestricted privilege 15 as a convenience. |
| Fortinet FortiOS | For scoped CLI collection, custom administrator with read permissions for required sections and `cli-show`; disable `cli-config`, `cli-exec`, `cli-diagnose`. No enable secret. | `show full-configuration` can omit protected admin/secret/context information for restricted profiles. Full native backup access is release-dependent and may require greater privileges. Record partial scope; do not claim full recovery or elevate automatically. |
| Palo Alto PAN-OS | Device-scoped CLI `superreader`, with unneeded web/API permissions disabled; no enable secret. | `devicereader` omits password profiles/other administrators; `vsysreader` omits global/network scope. Running config is separate from candidate config, device-state and key/certificate recovery artifacts. |
| Other implemented platforms | Exact OS-specific read role/command authorization, independently verified by its administrator. | Cisco NX-OS/ASA and other OS families do not share IOS privilege semantics. Aruba/F5 support is outstanding. |

The recommendations above are an application of vendor documentation to the current CLI collector, not a certified universal minimum for every firmware/context. Sources: [Cisco privilege/output behavior](https://www.cisco.com/c/en/us/support/docs/security-vpn/terminal-access-controller-access-control-system-tacacs-/23383-showrun.html), [Fortinet admin profiles](https://docs.fortinet.com/document/fortigate/7.4.10/administration-guide/294491/administrator-profiles), [Fortinet native backup requirements](https://docs.fortinet.com/document/fortigate/7.4.5/administration-guide/702257), [PAN-OS CLI roles](https://docs.paloaltonetworks.com/ngfw/help/12-2/configure-an-admin-role-profile-pan-os).

## 3. SSH host keys

NCCM does not verify or store device SSH host keys, and there is no SSH trust setup step. The collector accepts the key a device presents and authenticates with the profile credentials, so a device is ready as soon as it has a profile, an enabled enrollment and a working worker. Because the connection is not pinned to a verified key, restrict who can reach the management network and use dedicated, least-privilege backup accounts.

Readiness checks only for a saved entry; the real SSH handshake must verify the peer. No readiness action learns keys or initiates device authentication. A changed key stops collection with an actionable message.

## 4. Prove a first capture, then schedule

1. Select one Fortinet and one PAN-OS pilot; record model, exact firmware, HA state and intended VDOM/vsys/global scope. Have the device administrator provision the restricted account and independent fingerprint.
2. Save enabled enrollment, exact platform, profile and supported configuration types. Keep scheduling disabled initially. Readiness reports missing prerequisites individually.
3. Run a backup and follow the queued job to completion. A queued job or successful login is not a completed backup.
4. Have an authorized reviewer compare the captured configuration with an independently obtained device reference. Check all required sections, contexts and sensitive recovery material without placing raw configurations in tickets/reports. Record counts/hashes and completeness disposition. Syntax validation alone cannot detect all privilege-filtered output.
5. Enable a schedule only after review. Observe at least three due runs, including an unchanged capture: successful verification should refresh coverage without creating a duplicate version. Confirm freshness against the configured threshold and timezone.
6. On an explicitly disposable lab, test denied command, invalid credentials, interrupted/paged response and worker restart. Verify no partial snapshot replaces the previous good artifact, retry budgets remain bounded, and error messages contain no credentials or CLI output. Do not perform fault injection on production devices.
7. Test a chosen notification destination separately when explicitly authorized. An active delivery timer is not recipient-delivery evidence.

Read-only service diagnostics:

```sh
sudo systemctl is-active zenplus-api zenplus-ncm-worker.service zenplus-ncm-backup.timer zenplus-ncm-delivery.timer
sudo systemctl list-timers zenplus-ncm-backup.timer zenplus-ncm-delivery.timer
```

Use the device's safe last-error and job history first. Authentication, timeout/reachability, privilege/format rejection, credential decryption and archive failures have distinct actions. Avoid posting raw debug logs/configurations or environment output.

## 5. Archive and key recovery runbook

Owner: appliance administrator. Target: isolated recovery appliance matching the source schema/application version. This runbook does not add automated offsite backup or key rotation; these remain later-phase work.

1. Establish approved RPO/RTO and a protected off-appliance destination. Use the existing appliance backup procedure to take a transactionally consistent PostgreSQL backup including device identities, encrypted configuration versions, NCM profiles/enrollments, runs/jobs, baselines/pins, audit and notification records. A CSV or redacted download is not an archive backup.
2. Separately preserve the existing `SNMP_ENC_KEY` and required protected appliance configuration through the organization's secret backup mechanism. NCCM archive and profile decryption depend on that key. Never generate a replacement and expect it to decrypt existing data. Do not print keys into shell history, logs, this document or support messages. Restrict and encrypt both backup sets; retain recovery access independent of the failed appliance.
3. Record backup timestamp, source release/schema, archive counts and cryptographic checksums of encrypted backup packages.
4. Restore into an **isolated** appliance/database. Block network access to device management and notification destinations before restoring. Keep backup worker, schedule and delivery timers stopped; restored queued jobs/outbox must not execute during a recovery drill. Do not overwrite the production database.
5. Restore the matching protected key/configuration using the secret-management procedure. Load the environment through the service's existing protected mechanism. Run the installed verification entry point with the isolated database: `python -m app.services.ncm_maintenance verify` from the server environment. It decrypts and checks stored hashes and prints only the verified count. Verify the environment targets the isolated database before execution.
6. Check version counts, hashes, baseline/pin preservation and scoped/redacted UI access. Test an authorized raw export only into protected temporary storage. In a second isolated test with the wrong/missing key, verification must fail without disclosing data; restore the original protected key afterward. Never test wrong keys by editing the production service environment.
7. Record pass/fail, verified artifact count, measured recovery time and any lost interval. Delete protected temporary exports using the approved handling process. Device restoration is a separate vendor-specific lab drill; archive decryption success does not certify a device restore.

## 6. Acceptance record

| Gate | Evidence required | Current disposition |
|---|---|---|
| Eligibility/support | Every inventory device has a reason and support state | Implemented; explicit F5 review and Aruba/F5 limitations |
| Setup/failure handling | Scope, profile, worker/key checks; safe errors; unavailable data is unknown | Covered by code and automated contracts; see delivery evidence |
| Least-privilege pilots | One Fortinet and one PAN-OS identity/firmware/context/account; complete reference comparison | Awaiting operator-provisioned devices and profiles |
| Repeated schedules | Three successful due runs per pilot, unchanged dedup/freshness evidence | Pending pilots |
| Negative tests | Disposable device/lab identity and interruption/denial tests | Automated fixtures available; real-device lab pending |
| Recovery | Protected archive/key set restored and verified on isolated appliance, measured RPO/RTO | Runbook delivered; operational drill pending |

Phase A operational acceptance must remain open until the pending evidence is recorded. Do not publish a vendor certification or claim the fleet is protected from prerequisite checks alone.
