# Durable OTLP trace admission

`APM_INSERT_JOURNAL_ENABLED=1` commissions the Python `/v1/traces` receiver to
commit related spans, resources and exceptions into PostgreSQL before returning
success. It requires `HOST_INSERT_JOURNAL_ENABLED=1` and a commissioned
`ZENPLUS_CONTROLLER_GRANT` on every API candidate. Install the complete migration
set through 175 and matching recovery code first. Standalone defaults retain
their synchronous analytics acknowledgement.

This mode acknowledges durable admission. Analytics visibility is asynchronous
and depends on the admitted recovery worker. Runtime status names the admission
mode. In-process ingest counters remain diagnostic, best effort and potentially
repeated by HTTP retries; they are not an exactly-once ledger or billing data.

Span identity is the authenticated ingest-key identity, trace ID and span ID.
Spans are immutable after admission. A regrouped retry joins the original
analytics block, including its original exception UUIDs and typed attributes.
An empty exception receipt preserves a span's original absence of errors.
Resource receipts also include the decoded request bytes, so an identical
request retry keeps its original resource timestamp. A different ingest key is
a different admission namespace. Reusing IDs for changed spans does not update
the first admitted payload. Duplicate span IDs inside one request are refused.

The related table set commits atomically under the shared journal advisory
locks and capacity row. A busy table, failed receipt or exhausted budget rolls
back the whole admission. No analytics call occurs before that commit. A lost
HTTP response can be retried. A lost analytics response retains the persisted
block and original insert token. Existing bounded replay, held-work and capacity
rules apply; old uncertain writes require reconciliation.

Configure exporters for at most 2,000 spans per request and keep each encoded
table block within 8 MiB and 20,000 rows. Excessive batches return 413 with an
instruction to reduce batch size; invalid identities return 400. Temporary
admission failures return 503 and Retry-After. The shared pending budget is
512 MiB and ten million receipts. Upstream retry and disk-spool limits still
apply before admission. Existing telemetry retention and clock-skew rules stay
unchanged.

Map encoding is an extension to the journal codec. Upgrade every API candidate
before enabling it. Do not downgrade to a reader without map support while any
such payload is pending or held. Never discard receipts to permit a downgrade.

This covers OTLP trace payloads. RUM raw and rollup coordination, NetFlow,
NetPath, remaining incident boundaries, bounded receipt retirement, operational
reconciliation, sustained workloads and sealed appliance qualification remain
separate release gates. It does not qualify arbitrary third-party writers to
the same analytics tables.
