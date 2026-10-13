# ZenPlus KVM and Hyper-V appliance profiles

These profiles retain the signed ZenPlus 1.24.14 application from the qualified
VMware appliance. They derive from its **unbooted sealed disks**, never from a
customer or developer VM. Public OVA bytes and its qualification remain unchanged.

- KVM: compressed QCOW2 system and data disks, UEFI Secure Boot with enrolled
  Microsoft certificates, VirtIO disks/network, libvirt 8.6 or later.
- Hyper-V: dynamic VHDX system and data disks, Generation 2, Microsoft UEFI
  Certificate Authority Secure Boot template, synthetic SCSI/network, fixed 8 GiB
  memory and four vCPUs. Linux Hyper-V storage drivers are added to initramfs.
- VMware: the existing hardware-version-20 OVA targets ESXi 8 or later, with
  Workstation 17 or later as a local evaluation option. ESXi deployment still
  needs native validation; hardware compatibility alone is not qualification.

`clock.py` remains in `appliance/vmware` for compatibility with the existing
provisioner. An absent `/etc/zentryc/hypervisor` selects VMware host UTC, preserving
existing image behavior. KVM/Hyper-V profiles explicitly select their provider,
validate the detected hypervisor and initialize UTC before any database, trial
or certificate setup. KVM uses an UTC RTC in domain XML; Hyper-V reads host UTC
from the Linux Time Synchronization PHC, avoiding Windows local-RTC ambiguity.
Hyper-V host time synchronization must remain enabled. Incorrect host time still needs
operator correction. Network matching includes both `en*` and `eth*` names.

Build with `build-platforms.sh` on the disposable Linux builder, placing the
clock and import helpers in its output directory first. Inspect both sealed
profiles for absent identities, credentials, lab keys and accounts. Verify
QCOW2 integrity and logical equality of the Hyper-V source and exported VHDX.
Record kernel/driver, disk-hash and artifact provenance before packaging.
Archive checksums are production signed on the owner-designated release host.

Import helpers verify the disk manifest, refuse existing names/directories and
copy both master disks into a new VM directory. They never boot the distributed
master disks or reuse a customer's secrets. `create-kvm.py` requires a trusted
bridge with DHCP. `Import-ZenPlus.ps1` requires an existing Hyper-V virtual switch
and administrator rights. Obtain the unique temporary `admin` login from the
VM console; first use requires changing it. The fresh trial is 30 days.

Qualification must state the actual test mode. QEMU software emulation is not
KVM acceleration validation. Disk conversion is not native Hyper-V validation.
Never advertise native host tests that were not run. First-use checks cover
clock/certificate validity, service health, the password claim and trial.
Monitoring, support, backup/restore and cold-boot persistence remain release
acceptance gates for each platform.
