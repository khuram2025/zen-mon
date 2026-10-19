# ZenPlus 1.24.21 — Rule editing, rule validation and built-in event coverage

**Server alert rules can be edited.** Edit and Duplicate on a server rule now open a server-rule editor, from both the Alerts page and each server's Alerts tab. Previously saving sent it to the generic rule endpoint and failed silently. When a save is refused, the wizard and the editor keep the dialog open and list each field error inline, jumping to the step that owns it. Error messages name the field and never show a raw pattern.

**Impossible rules are refused.** Conditions are checked against the values their metric can take, and a condition that can never be true or is always true cannot be saved. For example, ping status is only 0 (down) or 1 (up), interface status is 1–7, and percentages are 0–100. Device ping rules keep their status condition in line with the chosen transition: a save whose ping status condition disagrees with the transition is corrected rather than refused, so rules saved by the old wizard ("device comes up" with ping status 0) can be duplicated and edited and are stored with ping status 1. A rule whose name or description disagrees with its condition, such as "High CPU" on a memory metric, gets a warning but can still be saved.

**Built-in events can escalate.** These events have no alert rule:
- agent offline;
- sensor offline or degraded;
- configuration backup failed or config changed;
- software baseline drift;
- server health thresholds.

They now carry a priority and are routed by that priority's default escalation policy. Each event type can be switched on or off. Two are off by default: health thresholds, because they duplicate server rules, and configuration changes, because each change opens an incident that never resolves on its own and would keep escalating until acknowledged (the device's NCM channels are still notified of every change). Only incidents raised after routing was enabled are escalated, so the upgrade never pages old open incidents. Coverage lists which built-in events are actually routed and paged.

**Coverage tells intent from gaps.** A priority that intentionally pages nobody, such as P4 with no policy, or a policy with no steps and no acknowledgement target, shows "Not paged (by design)" instead of a gap. A priority with an acknowledgement target that pages nobody remains a gap.

Migration 166 stamps a priority on open built-in incidents. No customer rule or threshold is changed. See `docs/alert-notification-policy.md`.
