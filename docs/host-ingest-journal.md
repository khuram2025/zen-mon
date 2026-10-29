# Durable host telemetry journal

The host telemetry batcher previously retained a combined upload's identity only
in the worker's memory. After a crash, individual uploads could be regrouped
under new ClickHouse tokens. A successful raw insert with an incomplete
materialized view was also an uncertain outcome, not proof of full success.
PR 103 refuses that raw-presence acknowledgement. This journal preserves the
original block and its membership across workers and controller replacement.

## Status and activation

This is an opt-in lab qualification feature. Migration 175 creates the journal,
receipt index and capacity counters in PostgreSQL. Set
`HOST_INSERT_JOURNAL_ENABLED=1` only on a commissioned cell with synchronous
PostgreSQL, controller fencing and qualified replicated ClickHouse settings.
Unset it for the existing standalone path. It does not journal Go polling,
APM, NetFlow or every other analytics producer.

Enable it initially only on an empty or quiesced ingest path with no uncertain
legacy batches or outstanding producer replays. The journal cannot reconstruct
group identities lost before it was enabled. A live cutover/backfill procedure
is still a release gate.

All controller candidates must carry the same code and setting. The active API
starts a recovery worker; PostgreSQL session locks serialize writers for each
table across API workers. Recovery also observes controller admission. Do not
disable the journal, roll back its writer or change a table schema while it has
pending or held work. Historical migrations remain unchanged.

## Write and recovery contract

1. The authenticated agent, batch id and metric kind provide the upload identity.
   The first submission freezes its rows, including clock correction. Producers
   must not reuse an identity for a different logical upload.
2. A PostgreSQL transaction records a compressed block, a unique write token and
   each member's mapping before any ClickHouse attempt. Capacity reservations
   commit in the same transaction. PostgreSQL failure prevents that attempt.
3. The writer records its first-attempt time, commits, then submits the original
   block with the stored token. A successful insert response is required.
4. Success changes the batch to committed and removes the bulky payload. Small
   receipts remain. A replay of a committed identity needs no ClickHouse insert,
   even after ClickHouse forgets the deduplication token.
5. A crash, lost response or failed PostgreSQL completion commit leaves the
   original batch pending. Replacement workers replay that entire batch. A
   replay mixed with new uploads does not regroup old members into a new block.
   Unknown writes never use raw-row presence to acknowledge a producer.

Graceful cancellation waits for the in-flight blocking writer before releasing
its PostgreSQL session lock. Hard process or machine failure requires the HA
controller's existing fencing contract. The lock connection is invalidated if
cleanup cannot prove that it released the session lock.

## Limits and required operational work

Each batch accepts at most 20,000 rows, 2,000 members and 8 MiB of uncompressed
encoded data. The default total pending payload budget is 512 MiB compressed;
the receipt budget is ten million. Capacity checks are transactional and return
retryable errors to producer spools. They do not discard the oldest work.
Physical PostgreSQL storage also includes indexes, row overhead and WAL; the
payload counter is not a disk reservation.

An uncertain batch older than five minutes becomes held and blocks subsequent
inserts into that table. Other tables can continue. No automatic deletion,
token replacement or blind retry is performed. Reconciliation and receipt
archival need an operator workflow before customer release. The receipt budget
is finite and needs sizing against the actual upload rate, not node count alone.

The five-minute bound is a conservative policy, not a proof that ClickHouse
still retains every deduplication record. Both its count and time windows must
cover the qualified workload, including dependent views and maintenance writes.
Other writers must not evict records during an uncertain batch. The current lab
profile uses a 10,000-block count window; sustained-load, expiry, schema-change
and receipt-retention qualification remain release gates. Do not describe this
as unlimited exactly-once ingestion.

The upstream [ClickHouse retry documentation](https://clickhouse.com/docs/guides/developer/deduplicating-inserts-on-retries)
explains that deduplication windows are finite and retries must preserve block
identity. This implementation makes that identity durable; it does not remove
the database's finite-window constraint.

## Evidence

Private disposable PostgreSQL tests cover original-group recovery after lost
responses, recovery without a producer retry, durable committed receipts,
capacity rollback, expired uncertainty, two competing workers, cancellation and
the prohibition on raw-presence acknowledgement. The feature is qualified
separately in VMware with a power loss between ClickHouse acknowledgement and
the PostgreSQL completion commit. Measured lab results belong in the acceptance
report; neither source tests nor an isolated successful crash case qualify an
appliance for customer release.

The first site A VMware run recovered in 49.47 seconds after the active VM was
powered off at that boundary. The replacement replayed the saved two-member
block automatically. Subsequent individual replays and one new upload left
three raw rows and a derived total of six, with the existing login session still
valid. This proves the tested small-block crash path, not the retention and
workload gates above.
