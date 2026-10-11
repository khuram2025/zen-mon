# ZenPlus VMware appliance recipe

This is the managed Ubuntu baseline (Level 1) from the appliance plan. It
packages a clean, production-signed ZenPlus release in a fresh VM. It does not
copy the development appliance, its databases, credentials, or accounts.

## Build inputs and sizing

- Canonical Ubuntu 26.04.1 server cloud VMDK, release 20260927. Validate the
  detached Canonical SHA256SUMS signature with the pinned Ubuntu cloud-image key.
- Base VMDK SHA256: `0664994108e2cc41c8ad24ffb387afb77eb3059c4db779f657aef7106e26baed`.
- A production-signed `.zup`, verified with `updater/package_trust.py`, including
  the signed per-file inventory and a clean tagged source commit.
- `init-postgres.sql` and `init-clickhouse.sql` from that same tag. These bootstrap
  files are not in the OTA payload; record their hashes in image provenance.
- Prebuilt Python 3.12 runtime and `/opt/zenplus/venv`, installed from the release
  requirements without development-only test packages. Retain the full resolved
  dependency inventory. Do not build Go or the dashboard on the customer VM.
- Independently verified, Authenticode-signed Windows agent MSI installed under
  `/opt/zenplus/artifacts/agents/windows/`.
- 4 vCPUs, 8 GiB memory, UEFI Secure Boot, VMXNET3, LSI Logic SCSI. A 40 GiB system
  disk and a 100 GiB data disk, thin provisioned; 15% of the data VG is reserved.

The runtime uses native PostgreSQL and Redis and a preloaded ClickHouse container
pinned by digest. Docker access is restricted to root. Git, Node, Go, compilers,
cloud-init, desktop software, and unnecessary device services are removed.

## Recipe order

Run these only on a disposable golden VM named `zentryc-golden`. The provisioning
inputs are staged under `/home/zimage`; the ephemeral `zimage` account must never
enter the export. Use a separate builder for source builds and disk conversion.

1. `provision.sh`: install runtime packages, set up the separate data volume,
   install verified prebuilt payloads, configure services and first boot, pin
   ClickHouse, and establish the firewall and HTTPS reverse proxy.
2. Install `sudo-client.py` as `/usr/local/bin/sudo`, `privileged.py` as
   `/usr/local/libexec/zentryc-privileged`, `policy.py` as
   `/usr/local/libexec/zentryc-policy`, and `updater-timer.py` as
   `/usr/local/sbin/zentryc-updater-timer`. All are root-owned, mode 0755. Run the
   policy helper. Its sole sudoers grant has an empty argument list. Existing
   storage/security helpers perform their own typed validation.
3. Install `console.py` as `/usr/local/libexec/zentryc-console` (root:root 0755),
   then run `finish-baseline.sh` and `minimize.sh`. Preserve `/usr/share/doc`
   copyright notices, package versions, Python dependency lock, signed release
   manifest/signature, and input hashes under `/etc/zentryc`.
4. Run `live-test.py`, `integration-test.py`, and `restore-test.py` on the
   disposable acceptance VM. Check the result files; do not accept HTTP status
   alone as proof that telemetry or restore worked. Cold reboot and recheck
   services, firewall, HTTPS, and storage.
5. Capture evidence outside the VM. Save the signed release manifest at
   `/etc/zentryc/release/manifest.json` with its signature and public key. Run
   `seal.sh` only after validation. It destroys all test data and lab accounts,
   regenerates a blank PostgreSQL cluster, clears identities and secrets, zeros
   free disk blocks, and powers off. Do not reboot the sealed golden VM.
6. Copy both powered-off VMDKs to the builder.
   If sealing stopped during bootstrap-account removal, power off first and
   verify both copied disk hashes. Mount only those copies on the isolated
   builder (`nbd0p1` root, `zentryc-data/data` data) and use `offline-seal.sh` to
   finish cleanup and zeroing. Its guards require that identity/credentials were
   already cleared. Inspect the final sealed disks before exporting; never boot
   the partially sealed original.
   Copy the final sealed disks back to the VMware export workstation, verify
   their transfer hashes, and create a clean VMX with the hardware above, one
   generated-MAC management NIC, and no seed media, UUID, NVRAM or lab devices.
   Do not boot it. Use `export-native.ps1` with VMware OVF Tool to create the OVA
   and SHA256 manifest. QEMU streamOptimized exports failed native VMware digest
   verification during qualification; do not use them for customer packages.
7. Import that exact OVA into VMware Workstation. Test an unmodified first boot,
   DHCP, unique identity/TLS/SSH keys/secrets, password claim, mandatory password
   change, fresh 30-day trial, functionality, and another cold boot. Repeat with
   a second import to detect duplicated identities. Rebuild/re-export after any
   image change. Never publish an earlier artifact after testing a newer image.
8. Production-sign the final checksums with the release key outside every VM.
   Public release must pass the GitHub checks, image acceptance, secret scanning,
   and distribution-rights review before upload and product-page changes.

## First use and recovery

Import the OVA in VMware Workstation 17 or newer. Connect the management NIC to a
trusted DHCP network (NAT is suitable for local evaluation), then power on.
First boot initializes the local databases without downloading runtime software.
The local console displays the management URL, certificate fingerprint, serial,
and unique temporary administrator password. The first HTTPS sign-in forces a
password change; the old session is revoked. No shared administrator password or
lab SSH account is shipped. The 30-day trial starts at initial boot, not build
time. Its default limits are 50 devices, 20 service checks, and 5 users.

Use the physical console for DHCP/static IPv4 settings, service status, recovery,
administrator password reset, reboot, or shutdown. Network changes use netplan's
120-second rollback. Install a trusted certificate from the management interface
for normal operation. Keep database backups and a hypervisor backup for recovery.
Database restoration is separate from application OTA rollback.

## Default port matrix

| Port | Purpose | Exposure |
| --- | --- | --- |
| TCP 443 | HTTPS interface/API and OTLP HTTP proxy | Management network |
| TCP 80 | Redirect to HTTPS | Management network |
| TCP 22 | Key-only SSH transport; no customer login account initially | Management network |
| UDP 162 | SNMP traps | Telemetry network |
| UDP 2055, 6343 | NetFlow and sFlow | Telemetry network |
| TCP 5432, 6379, 8123, 9000, 8091 | Databases and flow health | Loopback only |
| DHCP, ICMP/ICMPv6 | Address assignment and network operation | Required infrastructure |

New collectors/ports need an explicit firewall rule. The default does not expose
database services. The API's elevated operations go through a fixed JSON broker;
it cannot request an arbitrary executable, shell, or systemd service directive.

## Limits and release gate

This is a writable-root Level 1 appliance. A/B firmware, dm-verity, a Zentryc
whole-OS signing chain, HA qualification, KVM/Hyper-V qualification, and a native
ClickHouse migration remain future work. Application OTA still uses the existing
signed updater; it is not a transactional whole-OS update mechanism.

Canonical redistribution approval is an open public-release gate for a modified
Ubuntu image; the build itself is for internal qualification until that gate is
resolved. See https://canonical.com/legal/intellectual-property-policy . Retain
component license notices and provide required corresponding source material.
