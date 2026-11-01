# Flow collector durable admission

`NETFLOW_INSERT_JOURNAL_ENABLED=1` is an opt-in commissioning mode. It requires
the API recovery worker (`HOST_INSERT_JOURNAL_ENABLED=1`), migration 175,
verified PostgreSQL TLS, controller admission and a stable explicit
`NETFLOW_COLLECTOR_ID` shared by candidates in one cell. The controller
supervisor must own the collector service. Enable only after commissioning all
five flow tables and their derived tables with the replicated analytics profile.
The collector never runs standalone schema adoption in this mode.

All five production insert paths stage exact typed blocks in PostgreSQL:
records, collector statistics, template definitions, application names and sFlow
interface counters. Record, statistics and counter batches retain their original
logical token; the first committed payload wins. Registry snapshots use their
immutable content identity. Template arrays, IPv6 addresses, full interface
indexes and UInt64 counters survive the Go to Python recovery boundary.

Success means replicated PostgreSQL admission. The API worker writes analytics
asynchronously using the persisted grouping and token. `/health` names this
boundary explicitly; its legacy `records_written` and insert-success counters
count admitted records in this mode, not query-visible rows. Collector health
does not verify analytics visibility. Pending and held journal work must be
monitored separately.

UDP reception, parsing, in-memory queues, incomplete batches and a VM-local disk
spool precede shared durable acceptance. A crash can lose unadmitted data;
exporters need independent destinations or a protocol with acknowledgements if
their requirements exceed UDP delivery. A handoff failure retains the original
batch and retries with backpressure. Queue and spool limits still apply and
their drop counters must be monitored. Template decoder caches remain local:
after takeover collection may wait for exporters to refresh templates.

The shared journal currently caps each block at 20,000 rows and 8 MiB encoded,
with 512 MiB pending compressed data and ten million retained receipts. Configure
flow batches within these limits. Finite ClickHouse deduplication time and count
windows, stale replay rejection, receipt retirement and supported operator
reconciliation remain release gates. This is not an unlimited exactly-once
claim. Default standalone collection is unchanged.

Validation includes the real five Go insert paths through disposable PostgreSQL
and Python recovery, ambiguous responses, stable identities, exact counters,
IPv6 and template arrays. VMware qualification is recorded separately; no
customer image is signed by this change.
