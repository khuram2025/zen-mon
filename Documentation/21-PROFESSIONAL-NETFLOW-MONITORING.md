# Professional NetFlow Monitoring

## Objective

ZenPlus needs first-class flow visibility for bandwidth, traffic analytics, and
network forensics. This closes a clear market-leader gap from
`Documentation/17-PRODUCT-ENHANCEMENT-ASSESSMENT.md` and advances Phase 3
Network Intelligence from `Documentation/06-TASK-LIST.md`.

## Market Research

The professional market pattern is consistent:

- ntopng dashboards emphasize top talkers, hosts, applications/protocols, and
  real-time refreshed traffic views.
- ElastiFlow positions the collector as a unified flow collector that receives,
  decodes, transforms, normalizes, translates, and enriches NetFlow, IPFIX, and
  sFlow records before sending them to analytics stores.
- Grafana's NetFlow integration uses a collector layer to convert NetFlow,
  IPFIX, and sFlow packets into metrics and provides a pre-built NetFlow
  overview dashboard.
- nProbe/ntopng separate collection/parsing from visualization, which matches
  ZenPlus's architecture: collector service, ClickHouse storage, FastAPI query
  API, React dashboard.

References:

- ntopng Traffic Dashboard: https://www.ntop.org/guides/ntopng/user_interface/network_interface/dashboard/dashboard.html
- ElastiFlow Unified Flow Collector: https://docs.elastiflow.com/5.6/
- Grafana Cloud NetFlow integration: https://grafana.com/docs/grafana-cloud/monitor-infrastructure/integrations/integration-reference/integration-ktranslate-netflow/
- nProbe with ntopng: https://ntop.org/guides/nprobe/using_with_ntopng.html

## Implemented Phase

ZenPlus now has a first end-to-end NetFlow implementation:

- NetFlow v5 UDP collector service.
- ClickHouse raw normalized flow table.
- ClickHouse 5-minute flow rollup.
- FastAPI NetFlow analytics endpoints.
- React NetFlow dashboard page under Monitoring.
- Synthetic NetFlow v5 packet sender for end-to-end validation.

Current collector:

```text
systemd service: zenplus-netflow-collector.service
UDP listener:    :2055
health check:    http://127.0.0.1:8091/health
binary:          /opt/zenplus/bin/zenplus-netflow-collector
source:          poller/cmd/netflow-collector
```

## Current Scope

Supported now:

- NetFlow v5 packet parsing.
- Exporter IP tracking.
- Source/destination IP.
- Input/output interface indexes.
- Packets and bytes.
- First/last switched timestamps.
- Source/destination ports.
- TCP flags.
- Protocol and ToS.
- Source/destination AS and masks.
- Batch insert into ClickHouse.

Next protocol decoders:

- NetFlow v9 templates.
- IPFIX templates.
- sFlow samples and counters.
- JFlow compatibility validation.

## Storage Model

Raw table:

```text
zenplus.flow_records
```

Rollup table:

```text
zenplus.flow_traffic_5m
```

Migration file:

```text
scripts/migrate-20260506-netflow-clickhouse.sql
```

Raw retention starts at 30 days. Five-minute rollup retention starts at 90
days. These defaults are conservative because flow records can grow quickly.

## API

Dashboard endpoints:

```text
GET /api/v1/netflow/overview
GET /api/v1/netflow/timeseries
GET /api/v1/netflow/top-talkers
GET /api/v1/netflow/top-conversations
GET /api/v1/netflow/protocols
GET /api/v1/netflow/ports
GET /api/v1/netflow/exporters
```

All routes require dashboard authentication.

## Dashboard

The new page is:

```text
/netflow
```

It shows:

- current traffic rate,
- total traffic,
- flow count,
- exporter count and receiver state,
- source/destination host counts,
- top protocol,
- traffic rate time series,
- protocol distribution,
- top talkers,
- top services/ports,
- top conversations,
- exporter cards.

This matches the market-leader dashboard pattern without copying a generic
Grafana layout. The dashboard is designed for repeated NOC use: compact,
scannable, and focused on decisions.

## Validation

Unit test:

```bash
cd /opt/zenplus/poller
/usr/local/go/bin/go test ./internal/netflow ./cmd/netflow-collector
```

End-to-end collector smoke test:

```bash
cd /opt/zenplus
python3 scripts/send-netflow-v5-sample.py --host 127.0.0.1 --port 2055
```

The test sends:

```text
10.10.10.25 -> 172.16.20.50:443
protocol TCP
125000 bytes
500 packets
```

The record is then visible in:

```text
zenplus.flow_records
GET /api/v1/netflow/overview
GET /api/v1/netflow/top-conversations
```

## Professional Hardening Roadmap

### Phase 1: Collector Completion

- Add NetFlow v9 template cache.
- Add IPFIX template cache.
- Add sFlow decoder.
- Add exporter allowlist.
- Add packet/record drop counters.
- Add collector self-metrics into the dashboard.

### Phase 2: Enrichment

- Map exporter IP to known ZenPlus devices.
- Map SNMP interface indexes to device interfaces.
- Add local/private/public classification.
- Add GeoIP/ASN enrichment as optional offline databases.
- Add application/service naming beyond port labels.

### Phase 3: Detection And Alerting

- Top talker threshold alerts.
- New exporter detected alert.
- Exporter stopped sending alert.
- Traffic spike/anomaly alerts.
- Suspicious protocol/port alerts.
- East-west traffic anomaly detection.

### Phase 4: Scale

- Flow sampling awareness.
- Kafka/Redis queue option for very high-volume sites.
- Partition sizing and retention profiles.
- Rollups by exporter/interface/protocol/app/conversation.
- Cardinality controls for very large customers.

## Final Direction

ZenPlus should keep flow collection as a dedicated collector service, not as
part of the general ICMP/SNMP poller loop. This allows UDP packet ingestion,
template cache management, batching, and high-volume tuning without risking
the core availability poller.

