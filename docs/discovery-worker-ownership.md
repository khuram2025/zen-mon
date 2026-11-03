# Discovery worker ownership

Discovery v2 now claims each committed queued run with a fresh execution UUID in
PostgreSQL. A competing worker cannot claim the same run. Startup and every
60-second scheduler tick recover only expired running work; healthy owners and
queued work are retained. Queued work is scanned in groups of up to 100 for
dispatch. Running work whose owner expires is marked failed for an explicit
rerun, not automatically replayed from partial findings.

The owner renews a 90-second lease every 15 seconds using PostgreSQL time. Each
result/progress transaction locks the run row, validates the execution UUID and
unexpired lease, then checks expiry again before committing. Expiry within a
transaction rolls back its results. Cancellation is committed before signalling
a local task, so a different worker also observes revocation. Cancellation or
heartbeat failure cancels and joins asynchronous child probes. Already executing
threaded protocol calls can finish at their own protocol timeout; this does not
authorize any result commit after revocation.

Scheduled tasks launch only after commit. Scheduled and manual starts serialize
on the profile row before checking for an in-flight run. API shutdown cancels and
awaits registered background tasks, including the previously omitted escalation
sweeper and package synchronization task, then joins discovery tasks.

## Validation

Private PostgreSQL tests cover competing claims, a second worker's startup,
expired renewal and writes, expiry inside a transaction, cancellation from a
different worker, queued restart recovery, competing execution without duplicate
results, committed schedule dispatch, and joining child probes on cancellation
or heartbeat revocation. The discovery regression run has 24 passing tests,
including existing target, identification and import contracts. No real devices
or installed databases are used by these tests.

## Upgrade and recovery

Apply append-only migration 169 before starting the new executors. Stop all old
executors for the transition; old processes do not understand ownership fields.
The same coordinated stop is required before rollback. Leave the additive
columns in place. Legacy unowned running rows are marked failed; queued rows are
eligible for execution. Operator-cancelled and terminal rows are never reclaimed.

A lease duration plus the scheduler interval is not a commissioned recovery-time
guarantee: database availability, scheduling and work duration also matter. A
long transaction that exceeds its lease fails closed. Clock jumps on the
database can affect availability. These tokens protect PostgreSQL discovery
writes only. They do not fence remote device mutations, provide controller
leadership, persist every background worker's checkpoint, or make database
promotion safe. Full HA remains gated by the approved architecture.
