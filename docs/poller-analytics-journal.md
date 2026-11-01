# Poller analytics durable batch handoff

This opt-in commissioning feature stages native poller batches in the shared
PostgreSQL journal before analytics insertion. Set
`POLLER_INSERT_JOURNAL_ENABLED=1` on poller candidates only alongside
`HOST_INSERT_JOURNAL_ENABLED=1` on API candidates and commissioned controller
admission. Migrations through 175 and the API recovery worker are prerequisites.
Every candidate must carry the same implementation. Customer image acceptance
and the remaining ingestion release gates are separate requirements.

The covered tables are ping and service metrics, SNMP scalar and interface
metrics, decoded traps, and device/service diagnostic transition logs. This
does not cover NetFlow, sFlow, APM, NetPath or the remaining incident evaluation
boundaries. Existing confirmation thresholds and status policies are unchanged.

The internal channels and the current partial batch are volatile pre-admission
buffers. `WriteResult` and the other queue methods do not acknowledge durable
acceptance. A batch becomes durably admitted only when its PostgreSQL handoff
commits. A crash can lose samples that have not reached that boundary; UDP can
also lose packets before collection. Do not claim zero loss for every observed
sample or an unlimited reliable-trap protocol. The pre-admission loss window
includes the configured flush interval and any staging backpressure.

The batch codec freezes columns, UUIDs, UTC timestamps, nullable values, IP
addresses and exact integer values. A table-qualified content identity maps the
original block to one shared receipt. A failed PostgreSQL handoff retains that
batch and stops adding new samples to it until the handoff succeeds. Full
channels apply backpressure in this mode. The standalone default retains its
existing behavior while the feature is disabled.

The admitted API recovery worker inserts the persisted block using its original
token. A lost response keeps the block pending; another controller retries the
same identity. The producer never confirms success by inspecting raw rows or
regroups a failed handoff with newer samples. The worker checks controller
admission between blocks as well as between recovery passes.

Capacity and uncertainty rules are shared with the host journal: at most
20,000 rows and 8 MiB encoded per block, a 512 MiB compressed pending budget,
and ten million retained receipts. Configuration with a larger native batch is
refused. Uncertain writes beyond the qualified automatic replay window are
held for reconciliation; they are not discarded. Receipt retirement, safe held
work reconciliation and ClickHouse deduplication count/time qualification remain
release requirements. Queuing and recovery add latency that must be measured
under the intended workload; source tests do not establish enterprise capacity.

Do not disable recovery, change table schemas, discard receipts or downgrade
writers while work is pending or held. Preserve the complete migration set.
Commission with disposable data, compare raw and derived results, test a lost
response and controller power loss, and verify a returned replica. Run full
poller tests, cross-language PostgreSQL contracts and normal release CI before
using a build for appliance acceptance.
