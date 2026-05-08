# ZenPlus Virtual Appliance Taskable Plan

Purpose: turn ZenPlus into an enterprise-ready virtual appliance that can be exported as an OVA, registered to the remote update server, and safely updated through signed OTA releases.

This plan is taskable by phase. Do not mark a phase complete until every verification item in that phase passes. The expected final output is:

- `zenplus-appliance-<version>-amd64.ova`
- SHA-256 checksum for the OVA
- signed OTA release package for the same version
- release notes
- SBOM
- test evidence bundle
- documented install and recovery procedure

## Completion Rules

- Every task must have a recorded verification result.
- Any failed verification blocks the next phase unless the product owner explicitly accepts the risk in writing.
- All commands that mutate the appliance image must be scripted or documented.
- Final appliance image must not contain private keys, customer data, build caches, shell history, appliance registration state, or default reusable secrets.

## Phase 1: Product Baseline

### 1.1 Freeze Appliance Delivery Model

Task:
Define the enterprise product delivery model as an OVA-first virtual appliance. The one-line installer remains supported only for lab, development, or bring-your-own-Ubuntu deployments.

Implementation notes:
- Document OVA as the primary enterprise artifact.
- Document one-line installer as non-enterprise/lab path.
- Define `/opt/zenplus` as the application root.
- Define `zentryc.com` as the update and license authority.

Verification criteria:
- A product decision document exists and states OVA is the primary enterprise delivery.
- README or deployment docs clearly separate OVA from one-line installer.
- No enterprise release checklist depends on GitHub clone on customer systems.

Exit gate:
Enterprise delivery model is approved by product and engineering.

### 1.2 Define Supported Appliance Profiles

Task:
Define supported VM sizing tiers and the performance claim each tier must satisfy.

Implementation notes:
- Small: 4 vCPU, 8 GB RAM, 100 GB disk.
- Medium: 8 vCPU, 16 GB RAM, 250 GB disk.
- Large: 16 vCPU, 32 GB RAM, 500 GB+ disk.
- Device count must remain provisional until load testing completes.

Verification criteria:
- Sizing table exists in deployment docs.
- Each profile includes vCPU, RAM, disk, and expected device-count target.
- Sizing claims are marked "validated" only after Phase 11 performance testing.

Exit gate:
Sizing table is documented and linked from the OVA release checklist.

### 1.3 Define Network Model

Task:
Define the inbound and outbound network requirements for the appliance.

Implementation notes:
- Customer LAN access should use HTTPS `443`.
- HTTP `80` should redirect to HTTPS.
- Internal services should bind to loopback only.
- OTA requires outbound HTTPS to `zentryc.com`.

Verification criteria:
- Network matrix exists with ports, listener, bind address, and purpose.
- Matrix states which ports are customer-facing and which are loopback-only.
- Firewall rules in later phases match this matrix.

Exit gate:
Network model is approved before OS hardening starts.

## Phase 2: Base OS Image

### 2.1 Build Clean Ubuntu Base VM

Task:
Create a clean Ubuntu Server 24.04 LTS minimal VM as the base appliance image.

Implementation notes:
- Use Ubuntu Server 24.04 LTS x86_64.
- Use UEFI when possible.
- No desktop packages.
- UTC timezone.
- Minimal services only.

Verification criteria:
- `lsb_release -a` shows Ubuntu 24.04 LTS.
- `systemctl get-default` returns `multi-user.target`.
- No desktop display manager is installed or active.
- `timedatectl` reports UTC or a documented appliance default.

Exit gate:
Base VM boots cleanly and has no unnecessary desktop stack.

### 2.2 Remove Unnecessary Base Services

Task:
Remove or disable services not needed by the appliance.

Implementation notes:
- Remove `snapd` unless a required package depends on it.
- Remove `popularity-contest`, `landscape-common`, and unused cloud-init state for final OVA if not needed.
- Disable sleep, suspend, hibernate targets.

Verification criteria:
- `systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target` shows masked or disabled.
- `snap list` is unavailable or shows no required appliance dependency.
- `systemctl --failed` returns no failed units.

Exit gate:
Base OS has no unnecessary active services.

### 2.3 Create Disk Layout

Task:
Prepare appliance disk layout for application, PostgreSQL, ClickHouse, logs, and future growth.

Implementation notes:
- Minimum acceptable: single 100 GB disk.
- Preferred layout:
  - `/`: 20-30 GB
  - `/opt/zenplus`: 30 GB
  - `/var/lib/postgresql`: 20 GB
  - `/var/lib/clickhouse` or `/data/clickhouse`: remaining space
  - swap: 4 GB minimum
- If using LVM, document online expansion procedure.

Verification criteria:
- `lsblk -f` output is captured in release evidence.
- `df -h` shows required mount points or accepted single-disk layout.
- Swap is enabled and visible in `swapon --show`.
- Storage expansion procedure exists and has been tested in Phase 12.

Exit gate:
Disk layout supports expected retention and future expansion.

### 2.4 Install Runtime Packages

Task:
Install only runtime packages required by the final appliance.

Implementation notes:
- Required runtime packages include:
  - Python 3.12
  - PostgreSQL
  - Redis
  - Nginx
  - curl
  - jq
  - openssl
  - logrotate
  - sudo
  - libcap2-bin
  - firewall tooling
- Node.js and Go are build-time tools and should not remain in the final OVA unless explicitly justified.

Verification criteria:
- `python3 --version` shows Python 3.12.x.
- `psql --version`, `redis-server --version`, and `nginx -v` work.
- `node --version` and `go version` are absent on final OVA, or an approved exception is documented.
- Package list is captured with `dpkg-query -W`.

Exit gate:
Runtime package set is minimal and reproducible.

## Phase 3: Application Packaging

### 3.1 Fix Known Packaging Defects

Task:
Fix the defects found during live installation testing.

Implementation notes:
- Add `openpyxl` to `server/requirements.txt`.
- Build Go poller using `-buildvcs=false`.
- Remove stale services such as `netmon-gunicorn`, `netmon-celery`, and `netmon-celery-beat` from health checks and release manifests unless those services are reintroduced.
- Ensure admin password hash generation uses the installed Python environment and produces a valid bcrypt hash.

Verification criteria:
- `pip install -r server/requirements.txt` installs `openpyxl`.
- `python -c "import openpyxl"` succeeds in the production venv.
- `go build -buildvcs=false -o /tmp/zenplus-poller ./cmd/poller` succeeds.
- `curl http://localhost/api/v1/system/health` contains only real services.
- Login with initialized admin credentials returns a bearer token, not HTTP 500.

Exit gate:
Fresh install no longer requires manual repair.

### 3.2 Pin and Lock Dependencies

Task:
Make Python, Node, and Go dependencies reproducible.

Implementation notes:
- Python requirements must be pinned.
- Node dependencies must use `package-lock.json`.
- Go dependencies must use `go.sum`.
- Generate a dependency inventory for each release.

Verification criteria:
- `server/requirements.txt` contains pinned versions for all runtime dependencies.
- `dashboard/package-lock.json` exists and `npm ci` succeeds.
- `poller/go.sum` exists and `go mod verify` succeeds.
- Dependency inventory is included in release evidence.

Exit gate:
Builds are repeatable from pinned dependencies.

### 3.3 Build Production Artifacts

Task:
Build dashboard, poller, and Python runtime artifacts for the appliance.

Implementation notes:
- Dashboard is prebuilt into `dashboard/dist`.
- Poller is precompiled into `/opt/zenplus/bin/zenplus-poller`.
- Python venv is created under `/opt/zenplus/venv`.
- Consider an offline wheelhouse for customer environments without internet.

Verification criteria:
- `test -f /opt/zenplus/bin/zenplus-poller` passes.
- `getcap /opt/zenplus/bin/zenplus-poller` shows `cap_net_raw+ep`.
- `test -f /opt/zenplus/dashboard/dist/index.html` passes.
- `/opt/zenplus/venv/bin/python -c "import fastapi, openpyxl, clickhouse_connect"` succeeds.
- `curl http://localhost/` returns dashboard HTML after services start.

Exit gate:
All runtime artifacts are present without requiring Node or Go on the customer appliance.

### 3.4 Remove Build-Time Artifacts

Task:
Remove build-only tools and caches from the final image.

Implementation notes:
- Remove Node.js and npm if not needed at runtime.
- Remove Go toolchain if not needed at runtime.
- Remove npm, pip, Go, and build caches.
- Remove test artifacts and local developer state.

Verification criteria:
- `du -sh /root/.cache /home/*/.cache` shows no large build caches, or caches are removed.
- `find /opt/zenplus -name node_modules` returns no runtime-bundled node_modules unless explicitly required.
- `go version` and `node --version` are absent or exception is documented.
- OVA cleanup script removes known build caches.

Exit gate:
Final appliance contains only runtime dependencies.

## Phase 4: First-Boot Flow

### 4.1 Create First-Boot Systemd Unit

Task:
Create `zenplus-first-boot.service` to initialize each imported appliance exactly once.

Implementation notes:
- Generate `.env`.
- Initialize PostgreSQL user and database.
- Configure Redis password.
- Start and verify ClickHouse.
- Run DB migrations.
- Seed required data.
- Write sentinel only after success.

Verification criteria:
- `systemctl status zenplus-first-boot` shows successful completion after first boot.
- `/var/lib/zenplus/.initialized` exists only after successful initialization.
- Removing the sentinel in a test VM causes first-boot to rerun.
- Failed first-boot does not start dependent API/poller services.

Exit gate:
First boot is deterministic, idempotent, and fails closed.

### 4.2 Generate Per-Appliance Secrets

Task:
Generate secrets uniquely on first boot, never at OVA build time.

Implementation notes:
- Generate PostgreSQL password.
- Generate ClickHouse password.
- Generate Redis password.
- Generate JWT secret.
- Generate temporary admin password.

Verification criteria:
- Before first boot, `/opt/zenplus/.env` does not exist.
- After first boot, `/opt/zenplus/.env` exists with mode `0640` or stricter.
- Two imported appliance instances have different secrets.
- No generated secret appears in logs or shell history.

Exit gate:
OVA contains no reusable appliance secrets.

### 4.3 Replace Default Admin Password

Task:
Eliminate reusable `admin/admin123` in the enterprise appliance.

Implementation notes:
- Generate random temporary admin password at first boot.
- Print it only to VM console.
- Force password change on first login.
- Store only bcrypt hash.

Verification criteria:
- `admin/admin123` does not work on a fresh OVA.
- Temporary password works once.
- User is forced to change password before accessing dashboard.
- Password hash in DB is recognized by passlib/bcrypt.

Exit gate:
No default reusable admin password remains.

### 4.4 Clear Registration State

Task:
Ensure each OVA import starts unregistered.

Implementation notes:
- Do not bake appliance ID or API key into OVA.
- Clear `updater/config/agent.conf` appliance fields.
- Delete subscription cache before export.

Verification criteria:
- `agent.conf` contains no appliance ID or API key in exported image.
- `subscription.json` is absent in exported image.
- Dashboard shows appliance as unregistered until license key is entered.
- Registration creates a new appliance row on the remote server.

Exit gate:
Every imported appliance has a unique registration identity.

## Phase 5: Security Hardening

### 5.1 Harden Systemd Services

Task:
Apply least-privilege systemd settings to API, poller, updater, and dependency wait services.

Implementation notes:
- API runs as `zenplus`.
- Poller runs as `zenplus`.
- Poller receives only `CAP_NET_RAW`.
- Updater runs as root only for signed update application.
- Use `PrivateTmp`, `ProtectSystem`, `ProtectHome`, `ReadWritePaths`, `NoNewPrivileges`, and `CapabilityBoundingSet` where compatible.

Verification criteria:
- `systemctl cat zenplus-api` shows hardening options.
- `systemctl cat zenplus-poller` shows only required capabilities.
- `systemd-analyze security zenplus-api zenplus-poller zenplus-updater` output is captured and reviewed.
- Services still start and pass health checks after hardening.

Exit gate:
Services run with least privilege without breaking functionality.

### 5.2 Remove Unsafe Docker Privilege

Task:
Avoid giving the application user unrestricted Docker control.

Implementation notes:
- Preferred: run ClickHouse natively or root-managed through systemd.
- If Docker remains, `zenplus` must not be in the `docker` group.
- API should not need Docker socket access.

Verification criteria:
- `id zenplus` does not include `docker`, unless a documented exception is approved.
- `sudo -u zenplus docker ps` fails on final appliance if Docker remains.
- ClickHouse still starts through root-managed systemd/Docker Compose flow.

Exit gate:
Application user cannot gain root through Docker.

### 5.3 Configure Firewall

Task:
Enable a host firewall matching the approved network model.

Implementation notes:
- Allow HTTPS `443` from customer LAN.
- Allow HTTP `80` only for redirect or disable it if not needed.
- Block direct external access to API, DB, Redis, ClickHouse, and poller health.

Verification criteria:
- `ufw status verbose` or equivalent firewall output is captured.
- From another host, only expected public ports are reachable.
- `ss -tulpn` confirms internal services bind to loopback where possible.

Exit gate:
Network exposure matches the documented model.

### 5.4 Add HTTPS by Default

Task:
Serve dashboard and API over HTTPS.

Implementation notes:
- Generate self-signed cert on first boot.
- Redirect HTTP to HTTPS.
- Add UI or CLI support to upload customer certificate and key.

Verification criteria:
- `curl -k https://localhost/` returns dashboard HTML.
- `curl -I http://localhost/` returns redirect to HTTPS.
- Uploaded customer cert survives reboot.
- Nginx config test passes with `nginx -t`.

Exit gate:
Appliance is secure-by-default for browser access.

### 5.5 Protect Stored Secrets

Task:
Protect secrets at rest and prevent accidental disclosure.

Implementation notes:
- Restrict file permissions.
- Encrypt SMTP/SMS/SNMP credentials stored in DB.
- Never log passwords, tokens, SNMP secrets, JWT secret, or API keys.

Verification criteria:
- `find /opt/zenplus -type f -name "*.conf" -o -name ".env"` permissions are reviewed.
- Support bundle redacts secrets.
- Audit of API logs shows no secret values after test operations.
- DB fields for sensitive integrations are encrypted or otherwise protected.

Exit gate:
Secrets are protected in files, DB, logs, and support bundles.

## Phase 6: OTA Update System

### 6.1 Finalize Update Bundle Format

Task:
Define and implement final `.zup` package structure.

Implementation notes:
- Include manifest.
- Include signature.
- Include checksums.
- Include dashboard dist.
- Include Go binaries.
- Include Python requirements or wheelhouse.
- Include migrations.
- Include systemd units/configs when needed.
- Include SBOM.

Verification criteria:
- A built `.zup` can be extracted in a temp directory.
- Required files exist: `manifest.json`, `manifest.json.sig`, `checksums.sha256`, SBOM.
- Manifest lists every execution step.
- Bundle contains no private key or appliance-local config.

Exit gate:
Update package format is stable and documented.

### 6.2 Harden Update Verification

Task:
Ensure the appliance rejects unsafe or tampered updates.

Implementation notes:
- Verify Ed25519 manifest signature.
- Verify package hash from server.
- Verify all file checksums.
- Enforce version monotonicity.
- Enforce `min_version`, architecture, OS version, and expiry.
- Reject path traversal in archives.

Verification criteria:
- Tampered manifest is rejected.
- Tampered file checksum is rejected.
- Expired manifest is rejected.
- Downgrade is rejected unless explicitly signed as recovery.
- Archive with `../` path traversal is rejected.

Exit gate:
Update verification fails closed.

### 6.3 Add Update Preflight Checks

Task:
Run preflight checks before applying an OTA package.

Implementation notes:
- Check free disk space.
- Check backup location writable.
- Check current service health.
- Check maintenance window.
- Check DB backup can be created.

Verification criteria:
- Low disk test blocks update before download or apply.
- Failed DB backup blocks update.
- Outside maintenance window blocks non-critical update.
- Preflight result is written to local history and reported to server.

Exit gate:
Updates do not start when appliance cannot safely complete them.

### 6.4 Implement Reliable Rollback

Task:
Ensure failed updates restore working service state.

Implementation notes:
- Backup code.
- Backup PostgreSQL.
- Backup critical configs.
- Backup systemd units changed by update.
- Roll back on any failed step.

Verification criteria:
- Injected failed migration triggers rollback.
- Injected failed health check triggers rollback.
- After rollback, API health returns `ok`.
- Version remains previous version after failed update.
- Failure is visible in dashboard update history and remote server.

Exit gate:
Rollback is proven with intentional failure tests.

### 6.5 Implement Update Retry Policy

Task:
Prevent appliances from repeatedly applying the same failing release.

Implementation notes:
- Track release ID and attempt count.
- Stop retrying after configured threshold.
- Resume only when a newer release or explicit retry command is issued.

Verification criteria:
- Same failed release is attempted no more than configured maximum.
- Dashboard shows update blocked after repeated failure.
- Remote server receives failure count.

Exit gate:
Bad releases do not create infinite failure loops.

## Phase 7: Remote Update Server

### 7.1 Implement Appliance Registration

Task:
Build registration API on `zentryc.com`.

Implementation notes:
- Registration token/license key is consumed once or according to subscription policy.
- Server returns appliance ID and API key once.
- Server stores only API key hash.

Verification criteria:
- Valid license registers appliance.
- Invalid license returns failure.
- Reused or over-limit license is rejected.
- API key plaintext is not stored server-side.

Exit gate:
Appliance can register securely with license key.

### 7.2 Implement Appliance Check-In

Task:
Build authenticated check-in API.

Implementation notes:
- Validate `X-Appliance-ID`.
- Validate bearer API key.
- Store current version, health, hostname, arch, OS, disk, and service status.
- Return update instruction or no-op.

Verification criteria:
- Valid appliance check-in succeeds.
- Revoked appliance check-in fails.
- Bad API key fails.
- Fleet dashboard shows latest check-in data.

Exit gate:
Remote server has reliable fleet inventory.

### 7.3 Implement Release Management

Task:
Build release upload, verification, publication, and storage.

Implementation notes:
- Server verifies package hash and manifest signature.
- Store package in object storage or protected filesystem.
- Releases remain unpublished until explicitly published.

Verification criteria:
- Unsigned release upload is rejected.
- Signed release upload is accepted.
- Server-computed SHA-256 matches submitted hash.
- Published release appears in release catalog.

Exit gate:
Only verified signed releases can be published.

### 7.4 Implement Rollout Engine

Task:
Support staged release delivery.

Implementation notes:
- Internal canary.
- Customer canary.
- Percentage rollout.
- Full rollout.
- Pause, abort, promote.
- Failure threshold auto-pause.

Verification criteria:
- Canary group receives release before stable group.
- Percentage rollout selects correct approximate share.
- Paused rollout stops offering update.
- Failure threshold pauses rollout automatically.

Exit gate:
Server can control fleet rollout safely.

### 7.5 Implement Remote Audit Logs

Task:
Audit all security and release actions.

Implementation notes:
- Audit admin login.
- Audit release upload/publish/promote/pause.
- Audit appliance registration/revocation.
- Audit update success/failure reports.

Verification criteria:
- Each admin action creates an audit log row.
- Audit log is immutable through normal UI.
- Audit log can be exported for support/compliance.

Exit gate:
Release and registration actions are accountable.

## Phase 8: Release Build Pipeline

### 8.1 Create Clean Release Builder

Task:
Build releases from a clean, tagged source state.

Implementation notes:
- Reject dirty worktree.
- Require annotated git tag.
- Build in clean temp directory.
- Record commit SHA in release metadata.

Verification criteria:
- Build fails when worktree is dirty.
- Build fails when version does not match tag.
- Built package metadata contains commit SHA and version.

Exit gate:
Release artifacts can be traced to source.

### 8.2 Add Automated Test Gates

Task:
Run required tests before package signing.

Implementation notes:
- Backend tests.
- Frontend build.
- Go tests.
- Migration tests.
- Smoke route tests.
- Installer/appliance smoke tests where possible.

Verification criteria:
- CI test report exists for each release.
- Failed tests block signing.
- Test report is attached to release evidence.

Exit gate:
Only tested builds can be signed.

### 8.3 Add Security and Supply-Chain Gates

Task:
Generate security evidence for each release.

Implementation notes:
- SBOM generation.
- Dependency vulnerability scan.
- Secret scan.
- SAST scan.
- Container/image scan if Docker remains.

Verification criteria:
- SBOM artifact exists.
- Vulnerability scan result exists.
- Critical vulnerabilities block release unless exception is approved.
- Secret scan passes.

Exit gate:
Release has security evidence and no unapproved critical findings.

### 8.4 Implement Signing Key Custody

Task:
Protect release signing private key.

Implementation notes:
- Preferred: HSM or offline signing host.
- Minimum: root-owned key with restricted access and documented escrow.
- Signing and publishing permissions should be separated.

Verification criteria:
- Private key is not present in repo.
- Private key is not present in OVA.
- Signing action is logged.
- Key rotation procedure is documented and tested in Phase 12.

Exit gate:
Release signing key is controlled and recoverable.

## Phase 9: OVA Preparation

### 9.1 Create OVA Prep Script

Task:
Script all cleanup steps required before exporting the OVA.

Implementation notes:
- Stop services.
- Delete generated `.env`.
- Delete first-boot sentinel.
- Clear appliance registration.
- Clear subscription cache.
- Clear logs.
- Clear shell history.
- Clear machine-id if required.
- Remove private keys and build caches.

Verification criteria:
- Script exits non-zero on failure.
- Script is idempotent.
- Running script leaves image ready for first boot.
- Cleanup output is captured in release evidence.

Exit gate:
OVA cleanup is repeatable and not manual.

### 9.2 Create Ship-Ready Verification Script

Task:
Verify the image is clean and ready to export.

Implementation notes:
- Check first-boot service enabled.
- Check application services enabled but stopped.
- Check secrets absent.
- Check registration absent.
- Check updater public key present.
- Check private key absent.
- Check logs and histories cleared.

Verification criteria:
- Script passes on prepared image.
- Script fails if `.env` exists.
- Script fails if private release key exists.
- Script fails if first-boot sentinel exists.
- Script fails if updater API key exists.

Exit gate:
No OVA is exported unless verification passes.

### 9.3 Export OVA

Task:
Export the powered-off VM as a distributable OVA.

Implementation notes:
- Use consistent file naming:
  - `zenplus-appliance-<version>-amd64.ova`
- Generate SHA-256 checksum.
- Sign checksum or artifact.
- Store artifact in release storage.

Verification criteria:
- OVA file exists.
- SHA-256 checksum file exists.
- Checksum validates.
- Artifact name matches release version.
- OVA import test is performed in Phase 10.

Exit gate:
OVA artifact is created and integrity-protected.

## Phase 10: OVA Acceptance Testing

### 10.1 Import Fresh OVA

Task:
Import the OVA into a clean hypervisor environment.

Implementation notes:
- Test at least one target platform before release.
- Preferred: VMware ESXi or Workstation first.
- Additional: Proxmox/VirtualBox if supported.

Verification criteria:
- OVA imports without warnings requiring manual repair.
- VM boots successfully.
- Console shows setup/status information.

Exit gate:
OVA is importable by supported hypervisor.

### 10.2 Validate First Boot

Task:
Confirm first boot initializes a unique appliance.

Verification criteria:
- First boot completes without failed units.
- Temporary admin password is generated.
- `/opt/zenplus/.env` is created.
- PostgreSQL, Redis, ClickHouse, API, poller, and nginx are active.
- `curl -k https://localhost/api/v1/system/health` returns `ok`.

Exit gate:
Fresh OVA boots to healthy appliance state.

### 10.3 Validate Login and Password Rotation

Task:
Validate admin authentication flow.

Verification criteria:
- Default `admin/admin123` does not work.
- Console temporary password works.
- User is forced to change password.
- New password works after logout/login.
- Old temporary password no longer works.

Exit gate:
Admin credential flow is secure and usable.

### 10.4 Validate License Registration

Task:
Register appliance with remote server.

Verification criteria:
- Valid license key registers appliance.
- Dashboard shows registered subscription state.
- `agent.conf` receives appliance ID and API key.
- `zentryc.com` fleet dashboard shows the appliance.
- Check-in succeeds after registration.

Exit gate:
Appliance can join managed fleet.

### 10.5 Validate Monitoring Functionality

Task:
Confirm core monitoring works after OVA deployment.

Verification criteria:
- Add device from UI.
- Poller detects device status.
- Dashboard summary updates.
- Device detail page loads.
- Alerts page loads.
- Reports page loads.
- Export to CSV and Excel works.

Exit gate:
Core customer workflow works on fresh appliance.

## Phase 11: Performance Validation

### 11.1 Build Load Test Harness

Task:
Create repeatable load tests for monitored devices and dashboard users.

Implementation notes:
- Simulate device inventory.
- Simulate ping/service-check behavior.
- Generate dashboard/API load.

Verification criteria:
- Load test scripts are committed.
- Test inputs are configurable by device count.
- Test can run unattended and produce report.

Exit gate:
Performance testing is repeatable.

### 11.2 Validate Small Profile

Task:
Run performance test on Small appliance profile.

Verification criteria:
- Target device count completes polling within configured interval.
- API p95 latency is within accepted threshold.
- No service OOM or restart occurs.
- Disk growth is measured.
- Results are documented.

Exit gate:
Small profile sizing claim is validated or adjusted.

### 11.3 Validate Medium and Large Profiles

Task:
Run performance tests for higher capacity profiles.

Verification criteria:
- Medium and Large profile reports exist.
- Poller cycle time, CPU, RAM, disk growth, and API latency are captured.
- Maximum supported device counts are documented.

Exit gate:
Published sizing guide is evidence-backed.

### 11.4 Validate Update Under Load

Task:
Apply OTA update while appliance is under representative load.

Verification criteria:
- Update completes successfully.
- Downtime is measured.
- Metrics ingestion resumes.
- Dashboard returns healthy after update.
- No data corruption is observed.

Exit gate:
OTA works under realistic appliance load.

## Phase 12: Failure and Recovery Drills

### 12.1 Bad Update Rollback Drill

Task:
Publish a lab update that intentionally fails.

Verification criteria:
- Appliance rejects or rolls back failed update.
- Previous version remains active.
- API health returns `ok`.
- Failure is visible locally and remotely.

Exit gate:
Rollback is proven in lab.

### 12.2 Key Rotation Drill

Task:
Test release signing key rotation procedure.

Verification criteria:
- Old key signs update that installs new public key.
- New key signs subsequent update.
- Appliance accepts new-key update after rotation.
- Appliance rejects package signed by retired key after rotation.

Exit gate:
Signing key rotation is operationally proven.

### 12.3 Storage Expansion Drill

Task:
Validate adding disk and expanding storage.

Verification criteria:
- New virtual disk is detected.
- Expansion command succeeds.
- ClickHouse/PostgreSQL storage path grows as expected.
- Services remain healthy after expansion.

Exit gate:
Customer storage expansion procedure is safe.

### 12.4 Backup and Restore Drill

Task:
Validate backup and restore process.

Verification criteria:
- Backup creates usable PostgreSQL dump and config archive.
- Restore succeeds on test appliance.
- Dashboard data matches pre-backup state.
- Restore procedure is documented.

Exit gate:
Support can recover customer appliance data.

## Phase 13: Documentation

### 13.1 Customer Deployment Guide

Task:
Write OVA deployment guide.

Verification criteria:
- Covers import, boot, network setup, first login, password change, and license registration.
- Includes screenshots or console examples.
- Reviewed by support.

Exit gate:
Customer can deploy without engineering assistance.

### 13.2 Security Guide

Task:
Write appliance security guide.

Verification criteria:
- Documents ports, users, services, update trust model, secret storage, and cert management.
- Includes vulnerability reporting contact/process.
- Reviewed by security owner.

Exit gate:
Enterprise security questions have documented answers.

### 13.3 Operations Guide

Task:
Write admin operations guide.

Verification criteria:
- Covers service status, logs, backup, restore, updates, support bundle, storage expansion, and troubleshooting.
- Commands are tested against current appliance.

Exit gate:
Support and customers can operate appliance safely.

### 13.4 Release Notes and Known Limits

Task:
Write release-specific notes.

Verification criteria:
- Includes version, commit, supported hypervisors, sizing, known issues, and upgrade path.
- Links to SBOM and checksum.

Exit gate:
Release is understandable and auditable.

## Phase 14: Final Release Gate

### 14.1 Final Evidence Review

Task:
Review all release evidence before shipment.

Required evidence:
- OVA checksum.
- Signed OTA package.
- SBOM.
- Test reports.
- Security scan reports.
- Performance reports.
- OVA import test.
- Rollback drill result.
- Key rotation drill result.
- Documentation links.

Verification criteria:
- All required evidence exists.
- No critical unresolved defects.
- All exceptions are signed off.

Exit gate:
Release candidate can be approved.

### 14.2 Publish Appliance Release

Task:
Publish final OVA and update metadata.

Verification criteria:
- OVA is available from approved download location.
- SHA-256 checksum is published.
- Release exists on update server.
- Internal canary appliances can update to release version.
- Support team has deployment docs.

Exit gate:
Release is customer-ready.

## Immediate Engineering Backlog

These tasks should be done first because they block reliable appliance packaging.

1. Fix `server/requirements.txt` to include all runtime dependencies, including `openpyxl`.
2. Fix Go poller build command to use `-buildvcs=false`.
3. Remove stale service names from health checks and release builder.
4. Fix admin password initialization so fresh install login works without manual repair.
5. Add first-boot temporary admin password flow.
6. Add HTTPS default Nginx config.
7. Harden systemd service files.
8. Remove `zenplus` from Docker group or replace Docker-managed ClickHouse with safer service management.
9. Add OVA prep script.
10. Add ship-ready verification script.
11. Build minimal `zentryc.com` registration/check-in/release API.
12. Produce first signed internal OTA package.
13. Run fresh OVA import test.
14. Run bad-update rollback drill.
15. Create release evidence template.
