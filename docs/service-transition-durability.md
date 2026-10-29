# Confirmed service transitions survive API restarts

A VMware regression exposed a gap before the notification outbox. The central
poller saved a service recovery, then its one-time HTTP request to the local
alert engine failed while that API was restarting. The check became healthy,
but its incident stayed active. Retrying provider delivery could not repair an
incident transition that the API had never received.

Migration 176 adds a PostgreSQL service transition inbox. The central poller
now commits a confirmed state change and its immutable alert-evaluation payload
in one transaction, guarded by the previous state, observation timestamp and
central ownership. A rejected or failed commit does not advance its in-memory
confirmed state. Maintenance suppression and retry thresholds stay intact.

The API worker processes each check's events in order. Incident evaluation,
notification outbox insertion and the consumed marker share one transaction.
A savepoint rolls back partial incident work when evaluation fails; the event
is retried after ten seconds and later events for that check cannot overtake
it. A replacement controller can resume the same queue. Processed rows are
removed in bounded batches after seven days; pending failures are retained.

The observed timestamp is retained for incident creation and recovery duration.
Provider delivery remains asynchronous through the existing notification
outbox and recipient keys. The worker does not send mail while holding its
transition claim. External notification delivery still has the provider's
existing uncertainty boundary.

The legacy internal HTTP endpoint remains available for existing callers.
This change covers the central service poller; device, trap and other producer
boundaries need their own audit. Do not downgrade to a poller that bypasses the
inbox while pending transitions remain. Deploy migration 176 and the API
consumer before starting the new poller on every controller candidate.

The already-stranded lab recovery predates this queue. It is repaired by
replaying the existing confirmed recovery from ClickHouse history through the
normal consumer, with the evidence and queue identity retained. No direct
incident-status edit is used to hide the failure.

Validation includes private PostgreSQL tests of producer rollback, application
role grants, duplicate state commits, worker competition, trigger/recovery
ordering and notification-enqueue failure. Go poller tests preserve maintenance
suppression and in-memory state on failed persistence. VMware acceptance must
freeze the API across both transitions and replace the controller before the
queue is consumed, then verify incident and notification outcomes.
