# ZenPlus Pollers & Collectors

ZenPlus runs three independent data-collection processes. Each is a standalone
Go binary built from `poller/cmd/<name>`, deployed to `/opt/zenplus/bin/`, and —
except the remote sensor — run as its own systemd service.

| Binary | Source | Service | Listens / connects | Purpose |
|---|---|---|---|---|
| `zenplus-poller` | `poller/cmd/poller` | `zenplus-poller.service` | → PostgreSQL, ClickHouse, Redis | ICMP ping + SNMP polling |
| `zenplus-netflow-collector` | `poller/cmd/netflow-collector` | `zenplus-netflow-collector.service` | UDP `:2055`, → ClickHouse | NetFlow v5/v9 ingestion |
| `zenplus-sensor` | `poller/cmd/sensor` | runs on remote hosts | → appliance HTTPS API | Distributed remote monitoring |

All three are built into a release by `scripts/build-release.py` and installed
or updated on appliances by the OTA updater.

---

## 1. zenplus-poller — ICMP & SNMP poller

The core poller. It reads the device inventory from PostgreSQL, pings every
device on a schedule, polls SNMP-enabled devices for interface and health
metrics, and writes time-series results to ClickHouse.

### Build
```bash
cd poller
CGO_ENABLED=0 go build -o /opt/zenplus/bin/zenplus-poller ./cmd/poller
setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
```
Raw ICMP sockets need `CAP_NET_RAW` — granted by `setcap` for a manual run and
by `AmbientCapabilities` in the systemd unit.

### Configuration
`poller/config.yaml`, with environment-variable overrides:
```yaml
poller:
  id: poller-01
  ping:
    timeout: 3s
    count: 3
    interval: 500ms
    batch_size: 500
    privileged: true          # raw sockets; requires CAP_NET_RAW
  status:
    down_threshold: 3         # consecutive failures before a device is DOWN
    degraded_rtt_ms: 100
    degraded_loss_pct: 10
  device_sync_interval: 60s   # how often the device list is re-read
health:
  port: 8081
```
Database/Redis connection settings come from `/opt/zenplus/.env`.

### Service & verification
`zenplus-poller.service` runs as user `zenplus`; health endpoint on `:8081`.
```bash
systemctl status zenplus-poller
journalctl -u zenplus-poller -f
curl -s localhost:8081/health
```

---

## 2. zenplus-netflow-collector — NetFlow v5/v9 collector

Listens for NetFlow datagrams exported by routers and firewalls, decodes v5 and
v9 (including v9 template handling), and batch-inserts normalized flow records
into ClickHouse.

### Build
```bash
cd poller
CGO_ENABLED=0 go build -o /opt/zenplus/bin/zenplus-netflow-collector ./cmd/netflow-collector
```

### Configuration
Environment variables, set in the systemd unit; ClickHouse credentials come
from `/opt/zenplus/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `NETFLOW_LISTEN` | `:2055` | UDP address for flow ingestion |
| `NETFLOW_HEALTH_LISTEN` | `:8091` | HTTP health endpoint |
| `NETFLOW_COLLECTOR_ID` | `netflow-01` | tag stored on every flow record |
| `NETFLOW_BATCH_SIZE` | `1000` | rows per ClickHouse insert |
| `NETFLOW_FLUSH_SECONDS` | `5` | max time before a partial batch is flushed |
| `CLICKHOUSE_HOST/PORT/DB/USER/PASSWORD` | — | ClickHouse connection (`.env`) |

### Schema
On startup the collector runs `CREATE TABLE IF NOT EXISTS` for `flow_records`,
`flow_traffic_5m`, and the `flow_traffic_5m_mv` rollup view. The collector owns
its schema — no `clickhouse-client` binary and no separate migration step are
required for ingestion to work on a fresh appliance.
`scripts/migrate-20260506-netflow-clickhouse.sql` holds the same DDL for
reference and for fresh `install.sh` installs.

### Exporter setup
Point each device's NetFlow / IPFIX export at the appliance IP, UDP port
**2055**. NetFlow v5 and v9 are supported.

### Service & verification
`zenplus-netflow-collector.service` (unit source: `poller/systemd/`) runs as
user `zenplus` with `Restart=always`.
```bash
systemctl status zenplus-netflow-collector
ss -ulnp | grep 2055                        # collector is bound to the port
journalctl -u zenplus-netflow-collector -f  # "templates updated" = flow arriving
curl -s localhost:8091/health
```
If the `/netflow` dashboard page is empty, check in order:
1. the collector is running and bound to UDP 2055;
2. devices are actually exporting — a rising `UdpNoPorts` in `/proc/net/snmp`
   means packets arrive but nothing is listening;
3. the table is filling — `SELECT count() FROM flow_records`.

---

## 3. zenplus-sensor — remote / distributed sensor

A lightweight agent for monitoring **remote sites**. It runs on a separate host
(not the appliance), enrolls once with the appliance API, then pulls its
assigned devices and service checks, runs ping/service checks locally, and
reports results back over HTTPS. This extends monitoring to networks the
appliance cannot reach directly.

### Build
Version metadata is stamped via ldflags:
```bash
cd poller
go build -ldflags "-X main.version=sensor-X.Y.Z \
  -X main.commit=$(git rev-parse --short HEAD) \
  -X main.buildDate=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -o zenplus-sensor ./cmd/sensor
```
Releases publish the built binary as an artifact at
`/opt/zenplus/artifacts/sensors/bin/linux-amd64/zenplus-sensor` (with a
`.sha256` and `manifest.json`) for download onto remote hosts.

### Configuration
Environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `ZENPLUS_SERVER_URL` | yes | appliance base URL |
| `ZENPLUS_SENSOR_NAME` | yes | display name for this sensor |
| `ZENPLUS_ENROLLMENT_TOKEN` | first run only | one-time enrollment token |
| `ZENPLUS_VERIFY_TLS` | no | verify the appliance TLS certificate |

### Enrollment & operation
On first start, with an enrollment token, the sensor registers with the
appliance and receives a persistent `sensor_id` + `api_key`, saved to its state
file; the enrollment token is then cleared from the env file and later restarts
reuse the saved credentials. The sensor then loops on three intervals:
heartbeat, config refresh (ETag-cached), and result upload. It appears in the
appliance's Sensors UI, where its heartbeat timestamp should advance.

---

## How releases build and deploy the pollers

`scripts/build-release.py` builds all three binaries in step `[3/7]`, and the
release manifest carries:

- `install_binary` — `zenplus-poller`, `zenplus-netflow-collector`, and the
  `zenplus-sensor` artifact;
- `install_systemd` — installs and enables `zenplus-netflow-collector.service`
  from `poller/systemd/`;
- `stop_services` / `start_services` — both include `zenplus-netflow-collector`.

`zenplus-poller`'s unit is created by `install.sh` on first install.
`zenplus-sensor` is not a service on the appliance — it is published as a
downloadable artifact for remote hosts.

### Migrations
`scripts/migrations.lock` records the SHA-256 of every shipped `migrate-*.sql`;
migrations are append-only once released. ClickHouse runs as the
`zenplus-clickhouse` Docker container, so ClickHouse migrations are applied via
`docker exec zenplus-clickhouse clickhouse-client`. The netflow collector
additionally self-creates its schema, so flow ingestion never depends on a
migration step succeeding.
