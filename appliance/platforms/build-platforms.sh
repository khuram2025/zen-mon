#!/bin/bash
# Derive unbooted platform profiles from the sealed, qualified application image.
set -Eeuo pipefail
base=/home/zbuild/appliance-20261011
out=/home/zbuild/appliance-platforms-20261013
test ! -e "$out/kvm/system.qcow2"
mkdir -p "$out/kvm" "$out/hyperv" "$out/evidence"
qemu-img convert -f vmdk -O qcow2 -c "$base/sealed-system-r4.vmdk" "$out/kvm/system.qcow2"
qemu-img convert -f vmdk -O qcow2 -c "$base/sealed-system-r4.vmdk" "$out/hyperv/system.qcow2"
sudo modprobe nbd max_part=16
for profile in kvm hyperv; do
    test "$(lsblk -no SIZE /dev/nbd0 | tr -d ' ')" = 0B
    sudo qemu-nbd --connect=/dev/nbd0 "$out/$profile/system.qcow2"
    cleanup_profile() {
        for suffix in sys proc dev boot ''; do
            sudo umount "/mnt/platform-profile/$suffix" 2>/dev/null || true
        done
        sudo qemu-nbd --disconnect /dev/nbd0
    }
    trap cleanup_profile EXIT
    sudo udevadm settle
    sudo mkdir -p /mnt/platform-profile
    sudo mount /dev/nbd0p1 /mnt/platform-profile
    sudo install -m 0755 "$out/clock.py" /mnt/platform-profile/usr/local/libexec/zentryc-clock
    printf '%s\n' "$profile" | sudo tee /mnt/platform-profile/etc/zentryc/hypervisor >/dev/null
    sudo chmod 0644 /mnt/platform-profile/etc/zentryc/hypervisor
    sudo sed -i 's/name: "en\*"/name: "e*"/' /mnt/platform-profile/etc/netplan/50-zentryc.yaml
    sudo sed -i 's/1\.24\.13/1.24.14/g' /mnt/platform-profile/etc/issue /mnt/platform-profile/etc/issue.net
    if [[ "$profile" == hyperv ]]; then
        sudo mount /dev/nbd0p13 /mnt/platform-profile/boot
        sudo mount --bind /dev /mnt/platform-profile/dev
        sudo mount -t proc proc /mnt/platform-profile/proc
        sudo mount -t sysfs sysfs /mnt/platform-profile/sys
        sudo mkdir -p /mnt/platform-profile/etc/dracut.conf.d
        printf '%s\n' 'hostonly="no"' 'add_drivers+=" hv_vmbus hv_storvsc hv_netvsc hv_utils "' | sudo tee /mnt/platform-profile/etc/dracut.conf.d/50-zentryc-hyperv.conf >/dev/null
        sudo chroot /mnt/platform-profile /usr/sbin/update-initramfs -u -k 7.0.0-34-generic
        sudo lsinitramfs /mnt/platform-profile/boot/initrd.img-7.0.0-34-generic | grep hv_storvsc
        for suffix in sys proc dev boot; do sudo umount "/mnt/platform-profile/$suffix"; done
    fi
    sudo python3 "$base/inspect-image.py" --root /mnt/platform-profile --out "$out/evidence/$profile-sealed" --sealed
    sudo umount /mnt/platform-profile
    sudo qemu-nbd --disconnect /dev/nbd0
    trap - EXIT
done
qemu-img convert -f vmdk -O qcow2 -c "$base/sealed-data.vmdk" "$out/kvm/data.qcow2"
qemu-img convert -f qcow2 -O vhdx -o subformat=dynamic,block_size=1048576 "$out/hyperv/system.qcow2" "$out/hyperv/system.vhdx"
qemu-img convert -f vmdk -O vhdx -o subformat=dynamic,block_size=1048576 "$base/sealed-data.vmdk" "$out/hyperv/data.vhdx"
qemu-img check "$out/kvm/system.qcow2"
qemu-img check "$out/kvm/data.qcow2"
qemu-img compare -f qcow2 -F vhdx "$out/hyperv/system.qcow2" "$out/hyperv/system.vhdx"
qemu-img compare -f vmdk -F vhdx "$base/sealed-data.vmdk" "$out/hyperv/data.vhdx"
echo 'Unbooted QCOW2 and VHDX profiles prepared; no customer credentials or lab accounts.'
