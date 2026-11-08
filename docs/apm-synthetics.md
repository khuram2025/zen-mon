# APM synthetic scenarios

*Applications → Synthetics.* A scenario is a scripted user journey: ordered HTTP
steps run from the appliance on a schedule, sharing cookies and extracted
variables, each with assertions (status code, latency, body text, JSON path).

## Pages

* **Synthetics list** — every scenario with uptime, availability sparkline,
  average and p95 duration, failures and the last failure for the selected time
  range (default 24 hours; 1 h, 24 h, 7 d, 30 d presets or a custom range, kept
  in the URL). The scenario name opens its detail page.
* **Scenario detail** (`/apm/synthetics/<id>`) — uptime, runs, failures, outages
  and downtime, avg / p50 / p95 duration; availability and duration trends; a
  per-step table (pass rate, executed / failed / skipped, avg / p95, status codes,
  connect / TLS / wait / download breakdown). Clicking a step shows its trend,
  failures and errors; clicking a run shows every step of that run (waterfall,
  timings, assertions, error, response snippet); grouped errors and outages link
  to the matching runs.

## Data

Run history is kept 90 days. Each run records, per step: status code, duration,
assertion results, the phase timing (connect, TLS, send, wait for first byte,
download; a reused connection has no connect/TLS time), server address, response
size, content type, redirects and HTTP version, and the first 512 bytes of the
response body for failed steps only. Runs recorded before 1.25.4 have no phase
timing.

Scenario variables, request bodies and credential headers (Authorization,
cookies, tokens, API keys) are shown only to users who may manage scenarios
(`apm.manage`); everyone else sees them masked.

## API

`/api/v1/apm/synthetics` — every read takes `from`/`to` (ISO with offset) or
`hours`, default 24 h, and returns UTC timestamps with an explicit offset.

| Endpoint | Returns |
|---|---|
| `GET /` | scenarios with range statistics and sparkline |
| `GET /{id}` | one scenario |
| `GET /{id}/summary` | totals, trend, per-step statistics and trends, grouped errors, outages |
| `GET /{id}/runs` | runs, newest first; `status=all\|failed\|passed`, `step=<failed step>`, `limit`, `offset` |
| `GET /{id}/run-detail?at=` | one run with full step detail |
| `GET /{id}/results` | legacy run history (kept for compatibility) |

Implementation: `app.services.apm_synthetic_stats` (read models),
`app.services.apm_synthetic_service` (execution and timing). Tests:
`server/tests/test_apm_synthetic_stats.py` (the ClickHouse-query test runs when
the embedded engine `chdb` is installed and is skipped otherwise).
