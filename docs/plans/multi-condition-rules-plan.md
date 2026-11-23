# Multi-condition alert rules — design and plan

Shared design document (with diagrams and comments):
https://claude.ai/artifact/VmcgRMoFJh7gRoRcrsXWB2 (2026-11-23). This file keeps
the decisions and phases next to the code; the reference for what is built is
[../multi-condition-rules.md](../multi-condition-rules.md).

## Goal

One rule watches several conditions and raises one incident, for every asset
type: "(CPU > 90 % AND memory > 90 %) OR service nginx stopped" on a server,
"check slow AND its server's CPU high" on a service check, and so on. Existing
rules keep firing exactly as before.

## Model

- A rule names its subject (`subject_type`: server in phase 1) and holds a
  `condition_tree`: a root All/Any group with conditions and one level of
  groups, 2 to 10 conditions.
- A condition is a level (a reading against a limit) or a state (1 = bad);
  phase 3 adds "event seen in the last N minutes". It may name an instance or
  match any instance; phase 2 lets it read a linked asset.
- One shared engine (`app.services.rule_conditions`) validates, judges
  (three-valued, as `network_conditions`) and words trees; each source only
  supplies readings. One incident per rule + subject.

## Phases

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Engine, migrate-211, server rules (levels, services, processes, automatic services, forecasts), editor builder, refusal of ignored extra conditions on APM/RUM, Device Tracker, NetPath and service status | Built (this PR) |
| 2 | Device and service-check subjects (`combined_rule_service`, migrate-212), linked-asset conditions (device ↔ server, check → device/server, optional `service_checks.server_id`), status + SNMP in one device rule, per-condition hold. A failed workflow step is not a separate condition: it already makes the check down or degraded | Built |
| 3 | APM service, virtualisation source and appliance subjects; trap / syslog / configuration-change counts in a window on devices; combined recommended rules and template rule-pack items; replay over stored history (`combined_replay`) | Built |

## Decisions (recommended answers taken for phase 1)

| # | Question | Phase 1 |
| --- | --- | --- |
| D1 | Incident per subject or per instance | Per subject (server), listing matched instances |
| D2 | Rule-wide hold or per-condition hold | Rule-wide (phase 1) plus an optional per-condition hold (phase 2) |
| D3 | Nesting depth | One level of groups |
| D4 | Server CPU/memory averaging in combined rules | No average: each pass judges the newest fresh sample; holds count passes |
| D5 | Linked asset in maintenance / out of scope | Unknown, never a breach |
| D6 | OR rule, a second branch becomes true while open | Recorded on the incident (`matched`, `condition_states`), no new page |
| D7 | Sources that accept and ignore extra conditions | Refused at the API; stored rules stay editable |
| D8 | Optional server link on service checks | `service_checks.server_id` (API), else the server linked to the check's device |
