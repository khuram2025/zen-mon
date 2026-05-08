# ZenPlus Full Appliance OVA Export Runbook

This runbook exports the full ZenPlus server appliance as an OVA.

Important: an OVA should be exported from the hypervisor host while the VM is powered off. Do not try to export the live root disk from inside the running appliance; it can produce an inconsistent filesystem and may leak local secrets.

## Current Environment Finding

The current development VM is itself running as a VMware guest. It does not have host-level VMware export access from inside the guest. That means the correct flow is:

1. Prepare the golden VM from inside the guest.
2. Shut it down.
3. Export from VMware ESXi, vCenter, Workstation, Fusion, or another host-side tool.

## Golden Image Preparation

For a fresh minimal Ubuntu LTS VM, use the full one-line provisioner. This
installs the application, applies appliance hardening, verifies the live system,
then cleans the VM into OVA-ready golden-image state:

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/scripts/provision-main-appliance-golden.sh | sudo bash
```

The script writes the final checklist to:

```text
/root/zenplus-appliance-export-checklist.txt
```

If you already installed ZenPlus manually and only need to clean the VM for
export, run this only on the VM that will become the reusable appliance image:

```bash
cd /home/net/zen-mon
sudo scripts/prepare-main-appliance-ova.sh --yes
sudo poweroff
```

The prep script removes generated appliance state:

- `/opt/zenplus/.env`
- first-boot sentinel
- OTA appliance ID and API key
- subscription cache
- logs
- shell history
- machine identity
- SSH host keys unless `--keep-ssh-host-keys` is passed
- private signing keys if any are accidentally present

It also runs:

```bash
sudo scripts/verify-main-appliance-ova-ready.sh
```

Do not boot the prepared VM again before export. If it boots, first-boot may regenerate secrets, and you must prepare it again.

## VMware ESXi / vCenter Export

Preferred enterprise path:

1. Power off the prepared VM.
2. In vCenter/ESXi UI, choose **Export OVF Template**.
3. Export as OVA/OVF package.
4. Name the file:

```text
zenplus-appliance-<version>-amd64.ova
```

5. Generate checksum:

```bash
sha256sum zenplus-appliance-<version>-amd64.ova > zenplus-appliance-<version>-amd64.ova.sha256
```

## VMware ovftool Export

From a workstation or build host that has VMware `ovftool` and can reach ESXi/vCenter:

```bash
ovftool \
  'vi://USER@VCENTER_OR_ESXI/Datacenter/vm/ZenPlus-Golden' \
  ./zenplus-appliance-<version>-amd64.ova

sha256sum ./zenplus-appliance-<version>-amd64.ova \
  > ./zenplus-appliance-<version>-amd64.ova.sha256
```

## VMware Workstation / Fusion Export

1. Shut down the prepared VM.
2. Use **File -> Export to OVF**.
3. If the tool produces OVF + VMDK files instead of OVA, package them:

```bash
tar -cf zenplus-appliance-<version>-amd64.ova \
  zenplus-appliance.ovf \
  zenplus-appliance.mf \
  zenplus-appliance-disk1.vmdk
```

4. Generate SHA-256 checksum.

## Acceptance Test After Export

Import the exported OVA as a new VM and verify:

```bash
systemctl --failed
systemctl status zenplus-first-boot
curl -k https://localhost/api/v1/system/health
```

Expected:

- first boot completes successfully
- new `/opt/zenplus/.env` is generated
- API, poller, nginx, PostgreSQL, Redis, and ClickHouse are healthy
- appliance is unregistered until a license key is entered
- no default reusable admin password works
- temporary admin password flow works
