# Sensor deployment — 6 September 2026

The controller at `https://192.168.8.221` manages a running remote probe in VMware Workstation. Open **Settings → Sensors → VMware-Probe-01** for assignments, health, authorization, remote commands, and signed runtime upgrades.

| Item | Deployed value |
| --- | --- |
| VMware display name | ZenPlus Sensor 01 |
| Controller sensor name | VMware-Probe-01 |
| Sensor ID | bdb01c22-1ffe-43c4-9570-4eac4a96c3a9 |
| Guest hostname | vmware-probe-01 |
| Guest IPv4 | 192.168.18.128/24, DHCP on VMware NAT |
| Controller-observed source IPv4 | 192.168.8.189, the NAT host |
| Resources | 1 vCPU, 1 GB RAM, 12 GB virtual disk |
| Guest OS | Ubuntu 24.04.4 LTS, amd64 |
| Runtime | sensor-1.23.4 |
| VM file | `C:\Users\user\Documents\Virtual Machines\ZenPlus Sensor 01\ZenPlus Sensor 01.vmx` |
| Local console access | `LOCAL-ACCESS.txt` beside the VM; restricted to the Windows user and SYSTEM |
| Local appliance download | `C:\Users\user\Documents\ZenPlus Sensor Downloads\zenplus-sensor.ova` |

NAT permits outbound access to the controller and this site's targets. A remote site normally uses that site's port group/bridge and gateway. Target reachability, DNS and SNMP credentials must be valid from the probe's network.

## Deploy another probe

1. Open **Settings → Sensors**, download OVA for VMware, QCOW2 for KVM/importable cloud disks, or VHDX for Hyper-V. The generic image contains no enrollment identity or shared administrator password.
2. Create a sensor. Set the controller to `https://192.168.8.221`, supply its trusted CA, and select DHCP or static networking. Enable the console administrator and set a unique password if local configuration access is required.
3. Download the sensor's enrollment seed ISO and attach it before first boot. Treat personalized images/seed media as credentials. Do not clone an already enrolled disk for a different sensor.
4. Import the generic image. Hyper-V requires a Generation 1 VM with the disk on IDE; KVM uses the QCOW2. Cloud providers require their own disk import procedure and compatible networking.
5. Boot, verify the reported hostname/source IP in the controller, then select **Authorize sensor**. Pending probes can identify themselves by heartbeat but cannot fetch monitoring credentials or submit results.
6. Assign devices and service checks from sensor details. Device polling ownership must point to this sensor for SNMP collection.
7. After successful enrollment, disconnect sensitive seed media and keep it in restricted storage or remove it. This deployed VM has its seed detached and cloud-init disabled after successful provisioning; subsequent configuration uses `sensor-config`.

The generic image starts with signed runtime `sensor-1.23.3`; the controller publishes `sensor-1.23.4`. Select **Upgrade** after authorization to apply the newer runtime.

## Local configuration

Log in at the VM console and run `sudo sensor-config`. The menu provides status, controller/enrollment, DHCP/static IP/DNS, and recent service logs. Network changes use `netplan try` and retain a backup; perform them at the VM console. Direct status is `sudo sensor-config status`.

Changing controller requires a new enrollment token and a valid HTTPS trust chain. Existing state and buffered results are archived locally with root-only access rather than transmitted to the new controller. Network/cloud-init cache files and the console account are independent of the runtime upgrade.

## Controller-managed updates

The sensor runs as the dedicated unprivileged `zenplus-sensor` account, with the ICMP capability and a systemd sandbox. It connects outbound over verified HTTPS; no inbound management port is needed for controller commands. Console/SSH administration uses a separate account.

The controller queues reload, buffer flush, log-level and upgrade commands. For upgrades, the probe verifies an Ed25519 signature, platform, increasing version, same-controller HTTPS URL and binary SHA-256 before atomic replacement. The previous binary remains at `/var/lib/zenplus-sensor/bin/zenplus-sensor.previous`. The durable spool under `/var/lib/zenplus-sensor/wal` retries results until acknowledged; default retention is 72 hours/512 MB, with drops reported when limits are exceeded.

This installation has a private sensor release channel, separate from the controller vendor OTA channel:

- Signing key: `/etc/zenplus/sensor-release/private.pem`, root-only on the controller. Back it up securely before replacing the controller. It is not inside downloadable images or the API process environment.
- Public key: `/etc/zenplus/sensor-release.pub`.
- API override: `/etc/systemd/system/zenplus-api.service.d/sensor-release.conf`, setting `ZENPLUS_SENSOR_RELEASE_PUBLIC_KEY`.
- Published update: `/opt/zenplus/artifacts/sensors/bin/linux-amd64`.

For a future feature release, test the source and run on a protected build host, using the same signing identity:

```bash
sudo /opt/zenplus/venv/bin/python \
  /opt/zenplus/sensor-appliance/scripts/build-signed-runtime.py \
  --key /etc/zenplus/sensor-release/private.pem \
  --version sensor-1.23.5 \
  --source /opt/zenplus/poller \
  --output /tmp/sensor-1.23.5
```

`sensor-1.23.5` is an example next version. Publish the generated binary, `.sha256`, `manifest.json`, and `manifest.json.sig` together into the platform artifact directory, readable by the API. Use a staging directory and an atomic directory/symlink switch to avoid mixed releases. Then use **Upgrade** on a canary probe and verify its reported version and fresh results before upgrading more probes.

Main controller vendor releases may replace the sensor artifact directory. Preserve the private signing key/override and republish a matching private-channel sensor build after those releases. These source changes must be incorporated into your maintained controller release branch; they currently live in the isolated `codex/sensor-appliance-deployment` branch and are deployed on this controller. They are not a vendor release.

Probe feature updates use this signed binary path. Ubuntu packages, local console tooling, and kernel changes need a separate OS maintenance or rebuilt-appliance cycle. This implementation does not grant the controller arbitrary root command execution.

## Verification and limits

- Imported the corrected final OVA through VMware OVF Tool successfully. Its SHA-256 is `b8e01bdc22cce80ee61a56e1678d3a2363f0e084312b60b004f4c456e0ad44c6`.
- Deployed and booted the probe, completed HTTPS enrollment, tested pending authorization and approved it through the dashboard.
- Upgraded the running probe from `sensor-1.23.3` to `sensor-1.23.4` through a controller command; the command reported success and the restarted process reported the new version.
- Shut down, detached/removed the enrollment ISO, and powered on again. The same identity reconnected automatically, reported zero queued/dropped results, and resumed all three live service checks. VMware may ask to append its diagnostic `serial.log` on future power-ons; choose Append.
- Verified live ICMP gateway, TCP port 80 and controller HTTP service results.
- Verified actual SNMP UDP requests and ClickHouse persistence for sysUpTime and ifNumber using a loopback-only SNMP daemon on the probe (`127.0.0.200:1161`) and a generated read-only community. This self-check is assigned as a device; it does not prove access to an external router's SNMP agent.
- Go race tests cover sensor runtime and durable spool; 24 focused API/security/artifact tests and 2 local network validation tests pass. Dashboard production build passes. Full TypeScript checking still reports pre-existing errors in unrelated components.
- VMware was exercised. QCOW2/VHDX are published from the same clean disk but have not been boot-tested on KVM, Hyper-V or a cloud provider.
- SNMP supports v1/v2c/v3 scalar GETs, currently sysUpTime and ifNumber. Full interface tables, discovery and monitoring-template collectors remain future work. SNMPv3 authentication/privacy configurations have unit coverage; live SNMP validation used v2c.
- Static IP application and controller migration have validation/rollback code but were not exercised by changing this working VM's network/controller. TLS/DNS service workers existed previously; live checks in this deployment covered ICMP, TCP, HTTP and SNMP.

Controller pre-change backups are under `/opt/zenplus/backups/sensor-20260906`. The original local `ZenPlus` checkout had unrelated changes and was left intact; implementation is in `C:\Users\user\Documents\ZenPlus-sensor` based on controller commit `16caa97`.
