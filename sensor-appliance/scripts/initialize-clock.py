#!/usr/bin/python3
"""Initialize appliance UTC from its hypervisor before enrollment or probing."""
import subprocess

from appliance_clock import initialize_clock


def main():
    detected = subprocess.run(
        ["/usr/bin/systemd-detect-virt", "--vm"],
        text=True, capture_output=True, check=True, timeout=5,
    ).stdout.strip()
    profiles = {"vmware": "vmware", "kvm": "kvm", "qemu": "kvm", "microsoft": "hyperv"}
    if detected not in profiles:
        raise RuntimeError("Unsupported sensor appliance hypervisor: " + detected)
    initialize_clock(profiles[detected])


if __name__ == "__main__":
    main()
