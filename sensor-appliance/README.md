# ZenPlus Sensor Appliance

This directory contains the production structure for building and publishing the lightweight ZenPlus remote sensor appliance.

The appliance is intentionally not a second controller. It is a locked-down probe VM that:

- enrolls once with a controller-issued token,
- pulls assigned device/service-check configuration,
- probes local targets from the remote site,
- buffers results locally when the WAN is unavailable,
- pushes telemetry back to the controller over outbound HTTPS,
- receives signed updates through the normal ZenPlus release process.

## Directory Layout

```text
sensor-appliance/
├── config/
│   └── zenplus-sensor.env.example
├── packer/
│   └── zenplus-sensor.pkr.hcl
├── scripts/
│   ├── build-ova.sh
│   ├── firstboot.sh
│   ├── install-sensor.sh
│   └── publish-artifacts.sh
└── systemd/
    └── zenplus-sensor.service
```

## Controller Download Flow

The controller exposes:

- `GET /api/v1/sensor/appliance/manifest`
- `GET /api/v1/sensor/appliance/ova`
- `GET /api/v1/sensor/appliance/ovf`
- `GET /api/v1/sensor/appliance/sha256`

Artifacts are served from:

```text
/opt/zenplus/artifacts/sensors/
```

Expected published files:

```text
zenplus-sensor.ova
zenplus-sensor.ovf
SHA256SUMS
```

## Operator Workflow

1. Admin opens ZenPlus controller dashboard.
2. Admin creates a Site and Sensor.
3. Controller returns a one-time enrollment token, appliance download links, and bootstrap cloud-init.
4. Admin downloads the OVA/OVF from the controller.
5. Admin deploys the VM in the remote location.
6. Admin attaches the downloaded cloud-init seed or enters controller URL, sensor name, and token in the first-boot wizard.
7. Sensor enrolls, heartbeats, pulls assigned configuration, and starts probing.

## Build Workflow

Build the OVA/OVF on a Linux build host with Packer and QEMU tools installed:

```bash
cd /opt/zenplus
sensor-appliance/scripts/build-ova.sh
```

Publish the resulting artifacts into the controller download directory:

```bash
sudo sensor-appliance/scripts/publish-artifacts.sh \
  --ova /path/to/zenplus-sensor.ova \
  --ovf /path/to/zenplus-sensor.ovf
```

After publish, the controller manifest will report the artifacts as available.

## Runtime

The first production binary is structured at:

```text
poller/cmd/sensor
```

Build it directly:

```bash
cd /opt/zenplus/poller
/usr/local/go/bin/go build -o /tmp/zenplus-sensor ./cmd/sensor
```

The initial binary implements:

- enrollment,
- heartbeat,
- ETag-aware config pull,
- interval-aware local scheduler,
- ICMP/TCP/HTTP/TLS/DNS probe workers,
- batched ping and service-result upload.

Still to add before a commercial release:

- durable local spool,
- SNMP probe worker,
- local status CLI,
- signed config verification,
- mTLS,
- update channel integration.

The existing `scripts/mock_sensor.py` remains a development harness only.
