# APM Quickstart — Send your first trace (OTLP ingest)

*Status: AM-E1 shipped · 2026-06-21 · Part of the ZenPlus Application Monitoring design set ([00-INDEX.md](00-INDEX.md)).*

This gets a trace from any OpenTelemetry-instrumented app into ZenPlus APM in three steps. It uses the **FastAPI OTLP/HTTP+JSON fallback** that ships in AM-E1. The high-throughput Go collector (OTLP/gRPC `:4317`, OTLP/protobuf `:4318`) lands in a later AM-E1 task; until then use OTLP/HTTP **JSON** as shown below.

## 1. Create an ingest key

**UI:** go to **APM → Settings → Ingest Keys → Create ingest key** (type *SDK / Collector*, pick an environment). Copy the `zpi_…` key shown — it is displayed **once**.

**API:**
```bash
TOKEN=$(curl -s -X POST http://<appliance>/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)

curl -s -X POST http://<appliance>/api/v1/apm/ingest-keys \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"checkout-prod","kind":"sdk","env":"prod"}' | jq -r .key
# -> zpi_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   (copy now; shown once)
```

## 2. Point your app's OpenTelemetry SDK at ZenPlus

Set these environment variables on the instrumented service. The SDK appends `/v1/traces` to the endpoint automatically.

```bash
export OTEL_SERVICE_NAME=checkout
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod,service.version=1.4.2
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<appliance>
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20zpi_xxxxxxxxxxxxxxxxxxxx
```

Notes:
- `deployment.environment` should match the key's environment (`prod`/`staging`/`dev`). If omitted, ZenPlus uses the key's environment.
- `http/json` is required for the FastAPI fallback. For `http/protobuf` or `grpc`, use the ZenPlus Go collector on `:4318`/`:4317` (ships in a later AM-E1 task).
- The header value is URL-encoded (`%20` = space) per the OTel env-var convention.

## 3. Verify the trace landed

Send a synthetic trace with `curl` (no SDK needed):
```bash
ZPI=zpi_xxxxxxxxxxxxxxxxxxxx
NOW=$(( $(date +%s) * 1000000000 )); END=$(( NOW + 42000000 ))
curl -s -X POST http://<appliance>/v1/traces \
  -H "Authorization: Bearer $ZPI" -H 'Content-Type: application/json' -d '{
  "resourceSpans":[{"resource":{"attributes":[
    {"key":"service.name","value":{"stringValue":"checkout"}},
    {"key":"deployment.environment","value":{"stringValue":"prod"}}]},
   "scopeSpans":[{"spans":[
    {"traceId":"11111111111111111111111111111111","spanId":"2222222222222222",
     "name":"POST /orders","kind":2,
     "startTimeUnixNano":"'$NOW'","endTimeUnixNano":"'$END'",
     "attributes":[{"key":"http.route","value":{"stringValue":"/orders"}}],
     "status":{"code":1}}]}]}]}'
# -> {"partialSuccess":{}}
```

A `200` with `{"partialSuccess":{}}` means all spans were accepted (a non-zero `rejectedSpans` reports per-span decode failures). Spans are buffered and flushed to ClickHouse within ~1s. RED rollups (`apm_span_metrics_5m`) populate automatically via materialized view — service dashboards (AM-E3) read those, never the raw spans.

## Reference

| Item | Value |
|---|---|
| OTLP/HTTP+JSON traces endpoint (fallback) | `POST http://<appliance>/v1/traces` |
| OTLP/gRPC · OTLP/protobuf (Go collector) | `:4317` · `:4318` *(later AM-E1 task)* |
| Auth header | `Authorization: Bearer zpi_…` |
| Ingest liveness/counters | `GET http://<appliance>/v1/apm/ingest-stats` |
| Manage keys | `GET/POST/DELETE /api/v1/apm/ingest-keys` · UI `/apm/settings` |

Bad/revoked/missing key → `401`. Revocation propagates within ≤30s (ingest-key cache TTL). `Content-Type: application/x-protobuf` returns `415` with a pointer to the Go collector.
