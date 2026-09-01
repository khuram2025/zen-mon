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
6. Admin attaches the downloaded NoCloud seed ISO before first boot. The seed carries controller trust, networking, and the one-time enrollment token.
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
- ETag-aware config pull with an atomic last-known-good cache,
- interval-aware bounded worker scheduling with per-target overlap prevention,
- ICMP/TCP/HTTP/TLS/DNS probe workers,
- a durable append-only result spool under `/var/lib/zenplus-sensor/wal`,
- oldest-first batched ping and service-result upload with stable retry keys,
- independent heartbeat, config, probe, and upload loops,
- persistent, idempotent heartbeat command handling for config reload, buffer
  flush, runtime log-level changes, and verified self-update.

The spool defaults to 512 MB and 72 hours of retention. Tune it in
`/etc/zenplus-sensor/sensor.env` with:

```text
ZENPLUS_SPOOL_MAX_MB=512
ZENPLUS_SPOOL_RETENTION_HOURS=72
```

When either limit is exceeded, the oldest records are evicted first and the
cumulative drop count is reported in the next successful heartbeat. Results
are removed only after the controller acknowledges their batch. The segmented
WAL replays records directly from disk and does not load the configured maximum
spool size into memory. `state.json` and WAL records are written with mode
`0600`; the systemd unit also applies a restrictive `UMask=0077`.

The runtime executable is installed at
`/var/lib/zenplus-sensor/bin/zenplus-sensor` inside a sensor-owned `0700` state
tree. A self-update is accepted only from an HTTPS URL on the controller origin,
must match the runtime OS and architecture, and must be newer. Before trusting
any manifest field, the runtime verifies the exact manifest bytes with the
embedded Ed25519 release public key; it then verifies the binary SHA-256 and the
replacement executable before an atomic swap. `.previous` is retained as the
rollback link. Locally built unsigned binaries remain available for initial
installation but are deliberately never advertised as self-updates.

The controller origin must use HTTPS and controller certificate validation
cannot be disabled. Private/self-signed controller PKI is supported by adding
the controller CA certificate during bootstrap; the enrollment response can
also pin that verified trust anchor.

`/etc/zenplus-sensor/sensor.env` is private to the `zenplus-sensor` account. The
containing directory permits that account to atomically replace the file after
enrollment, clearing the one-time token without a truncation window.

Still to add before a commercial release:

- SNMP probe worker,
- local status CLI,
- signed config verification,
- mTLS.

The existing `scripts/mock_sensor.py` remains a development harness only.
