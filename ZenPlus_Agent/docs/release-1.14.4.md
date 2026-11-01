# ZenPlus Agent 1.14.4

Adds native Windows event details with persistent query-specific bookmarks,
bounded messages and per-cycle event/byte budgets. Checkpoints commit atomically
with the spool batch. A full detail queue applies backpressure; it does not
advance beyond records that could not be queued. Native bookmarks are also
checked against event identity to detect record-ID reuse after log clears.

The controller enables this protocol only after the agent advertises
`event_details_v1`. Role policies and traffic limits are managed in Agent
Policies on appliance 1.24.33 or later. Existing gzip transport, upload retry,
authorization and backpressure behavior is preserved.

Default collection: 60 seconds, 200 events, 256 KiB of event envelopes and
2 KiB per message. Lifecycle evidence gets a separate allowance. Status reports
backlog, inaccessible channels and coverage gaps; bounded collection cannot
prevent Windows itself from overwriting an old event log during a long outage.
