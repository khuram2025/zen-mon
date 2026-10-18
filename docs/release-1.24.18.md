# ZenPlus 1.24.18 — Escalation matrix, on-call and notification grouping

Incidents now carry a priority (P1 Critical, P2 High, P3 Warning, P4 Information) with per-priority acknowledgement targets. A rule may set it; otherwise it follows severity. Reusable escalation policies route each priority through staged steps to on-call schedules, people or channels, with:
- repeat notifications while unacknowledged, and unresolved-review updates that continue after acknowledgement;
- an acknowledgement expiry that reopens escalation;
- a fallback route when a step reaches nobody or every delivery fails.

The new Escalation & On-call page covers the P1–P4 matrix (with a recommended-matrix builder), the policy editor with a dry-run preview, on-call schedules (rotating layers, restrictions, overrides, 14-day timeline), response targets, grouping and a coverage check. Alert detail shows the route, current step and next escalation. Emails gain priority, View incident and Acknowledge; acknowledging always asks for confirmation after sign-in.

Notification grouping is optional and off after upgrade. When on, P3/P4 warnings are sent as one summary per group and channel after a short wait, and per-recipient budgets hold overflow in digests instead of dropping it. Critical and P1/P2 notifications, recoveries and escalations are never delayed. Every source now goes through one dispatcher, so grouping, deduplication and delivery keys apply everywhere. Quiet hours now apply to Device Tracker and NetPath alerts.

Migration 164 is additive. Existing rules and escalation tiers keep working unchanged. See `docs/alert-notification-policy.md`.
