# ZenPlus — Alerting & Notification System Audit (Network Focus)

Date: 2026-06-10
Auditor: senior monitoring/observability engineer
Branch: `feat/servers-monitoring-module`
Scope: alert condition types, evaluation engine, alert lifecycle, notification channels, SNMP trap handling — network use-cases.
Method: deep code read + read-only queries against the live PostgreSQL DB and a probe of the internal alert-engine endpoint.

---

## TL;DR

ZenPlus alerting is an **event-driven, status-transition engine**, not a metric-scanning rule evaluator. The Go poller calls three internal HTTP endpoints (`/alert-engine/evaluate`, `/evaluate-service`, `/evaluate-trap`) only **when a device/service ping status flips or a trap arrives**. The schema and Pydantic models advertise a rich metric vocabulary (cpu, memory, if_errors, if_discards, if_oper_status, etc.) but the **engine only ever receives `ping_status`, `rtt`, `packet_loss` from the poller** — every SNMP-metric and NetFlow-metric alert type is declared but **never evaluated**. There is dependency-based suppression, recovery auto-resolve, dedup-by-recovery, ack, and basic templating. There is **no real cooldown enforcement, no `for N minutes` hold, no escalation chains, no on-call schedules, no maintenance-window suppression for device alerts, no flap detection, and no digest/rate-limiting**. Trap alerts are explicitly "alerts-only" — they never dispatch to channels.

---

## 1. Alert condition types available today

### 1a. Schema-allowed metrics vs. actually-evaluated metrics

The DB CHECK constraint (`alert_rules_metric_check`, live) allows a broad vocabulary:

```
ping_status, rtt, packet_loss, jitter, service_status,
cpu, memory, uptime_reset, temperature, fan_state, psu_state,
if_in_bps, if_out_bps, if_errors, if_discards, if_oper_status,
session_count, vpn_tunnel_state, ha_state,
trap
```
(`scripts/migrate-022-alert-trap-oid.sql:8-18`; verified live via `pg_get_constraintdef`)

**But the create/update API and the UI restrict what can actually be created**, and the engine only evaluates three:

- `alert_rules.py:23` — `ConditionItem.metric` pattern = `^(ping_status|rtt|packet_loss|jitter|service_status)$`
- `alert_rules.py:33` — `AlertRuleCreate.metric` pattern = `^(ping_status|rtt|packet_loss|jitter|service_status|trap)$`
- `AlertRuleFormDialog.tsx:273-276` — the metric dropdown offers only `ping_status`, `rtt`, `packet_loss`, `jitter`.

The engine's live metric map (`alert_engine.py:425-429`):
```python
metric_values = {
    "ping_status": 1.0 if event.new_status == "up" else 0.0,
    "rtt": float(event.rtt_ms or 0.0),
    "packet_loss": float(event.packet_loss or 0.0) * 100.0,
}
```
There is **no `jitter` key** in `metric_values`, so a rule with metric=`jitter` (allowed by the API/UI) always evaluates `None` → `_eval_one` returns `False` → the rule **never fires** (`alert_engine.py:317-324`). `jitter` is a dead metric.

**Conclusion on condition types actually working today:**

| Condition | Status | Threshold scope |
| --- | --- | --- |
| Device down (`ping_status == 0`) | WORKS | per-device, per-group, per-device_type, per-location, or global |
| Latency (`rtt > N` ms) | WORKS | same scopes |
| Packet loss (`packet_loss > N` %) | WORKS | same scopes |
| Jitter | DEAD — accepted by API/UI but never in `metric_values` | — |
| Service check status | WORKS (separate endpoint) | per-check, per-service-group |
| Interface down (`if_oper_status`) | NOT IMPLEMENTED — schema-only | — |
| Interface utilization (`if_in_bps`/`if_out_bps`) | NOT IMPLEMENTED | — |
| Errors/discards (`if_errors`/`if_discards`) | NOT IMPLEMENTED | — |
| CPU / Memory | NOT IMPLEMENTED | — |
| Temperature / fan / PSU | NOT IMPLEMENTED | — |
| Custom SNMP OID threshold | DOES NOT EXIST (no per-OID numeric threshold concept) | — |
| NetFlow-based (top-talker / bps / conversation threshold) | DOES NOT EXIST | — |
| SNMP trap-based | WORKS as "alerts-only" (no channel dispatch) | per-device, per-group, OID-prefix filter |
| NCM config-change | WORKS as "alerts-only" (separate path, per-device toggle) | per-device |
| Server (agent) health / software baseline | WORKS (separate `server_health_service`/`baseline_service` path, alerts-only) | per-server |

Evidence the SNMP-metric types are never wired: there is no code in `alert_engine.py` or in `poller/internal/pinger/engine.go` that posts CPU/memory/interface values to the alert engine. The only poller→engine calls are status-transition (`engine.go:463`), trap (`engine.go:545`), and service status (`engine.go:565`). Grep for `if_oper_status`/`if_errors`/`cpu` in the engine and poller returns nothing.

### 1b. Per-device vs global; per-interface thresholds

Device rule scope is matched in `alert_engine.py:449-463`:
- `device_id` (single device), `group_id`, `device_type`, `location` (substring match), or none of them = **global**.
- Thresholds are a single number on the rule (`threshold`) or per-condition in the `conditions` array. They are **not per-interface** — there is no interface dimension anywhere in the rule schema. A "high utilization on Gi0/1" rule is impossible.
- No per-device-type *default* thresholds either: every rule carries its own absolute number.

`location` matching is a **substring, case-insensitive** test (`alert_engine.py:462`): `rule.location.lower() not in location.lower()`. A rule scoped to location "DC1" will also match a device located in "DC10" or "ADC1". This is a correctness footgun.

---

## 2. Evaluation engine

### 2a. Poll-driven, not scheduled

The engine is **edge-triggered by status transitions**, not a periodic rule scan.

- Poller pings every device on a 1 s ticker batch cycle (`engine.go:220` pingTicker = 1 s; `runPingCycle`). Underlying ping interval default `PingInterval: 500ms` (`config.go:71`).
- A device flips state only after `DownThreshold` consecutive failures (default **3**, `config.go:74`) → `down`; or RTT > `DegradedRTTMs` (default **100 ms**, `config.go:75`) or loss > `DegradedLossPct` (default **10%**, `config.go:76`) → `degraded`.
- Only on `newStatus != oldStatus` does `processStatusChange` fire `evaluateAlerts` (`engine.go:413,450-452`), which POSTs to `/alert-engine/evaluate`.

**Implication:** the alert engine only ever runs at the moment of a flip. There is no background loop re-checking thresholds. So:
- A latency rule `rtt > 100` can only fire when the device crosses the **degraded** boundary (100 ms hard-coded), because the engine is only invoked on a status change. A rule with `rtt > 250` will fire on *any* up→degraded transition where rtt happens to be 251, but will **never re-fire** if rtt later climbs from 120 → 400 while status stays `degraded` (no transition = no evaluation). Threshold rules below 100 ms (e.g. `rtt > 50`) effectively can't fire at all because the device never transitions while rtt is in 50–100 ms (still "up"). This is a fundamental mismatch between "metric threshold rule" semantics and "fires on status transition" plumbing.

### 2b. Evaluation interval

No fixed evaluation interval — it is reactive. Background loops that exist: `health_sweeper_loop` (agent staleness; `main.py:82`) and `discovery_scheduler_loop` (`main.py:85`). Neither evaluates network alert rules.

### 2c. Hysteresis / "for N minutes"

- Columns exist: `min_duration`, `max_repeat`, `duration`, `schedule_start/end/days` (`migrate-001-alerts.sql:9-13`), and `cooldown` (default 300, model).
- The engine **selects `cooldown` but never uses it** (`alert_engine.py:388,702` — column is fetched, never referenced again). There is **no cooldown enforcement**: every status flip writes a new alert row.
- `min_duration`/`max_repeat`/`duration`/`schedule_*` are **never read by the engine at all** (grep in `alert_engine.py` returns nothing). So there is **no "for N minutes" hold, no maintenance schedule, no repeat suppression** on the firing path. These are pure dead config.
- The poller's `DownThreshold` (3 consecutive misses ≈ 1.5 s of debounce at 500 ms) is the *only* hysteresis, and it is global, not per-rule.

### 2d. Multi-condition (AND/OR)

Supported and working (`alert_engine.py:327-349`, `_conditions_match`). A non-empty `conditions` JSONB array is combined by `condition_logic` (AND/OR). UI exposes "Add condition" + All/Any toggle (`AlertRuleFormDialog.tsx:264-330`). Caveat: all conditions still draw only from the 3-value `metric_values` map, so compound rules are limited to ping_status/rtt/packet_loss.

### 2e. Baseline / anomaly-based alerts

- `baseline_service.py` is a **software-compliance baseline** engine (required/prohibited packages on servers), not a network metric baseline. It raises server alerts via `create_server_alert` (`baseline_service.py:232`). It has **nothing to do with RTT/traffic anomaly detection**.
- There is **no dynamic/statistical baseline feeding network alerts** — no "alert when RTT is 3σ above the 7-day baseline", no rate-of-change, no seasonality. `baseline_service` does NOT feed device alert rules.

---

## 3. Alert lifecycle

### 3a. Dedup

- **No firing-time dedup.** Every status transition INSERTs a fresh `alerts` row (`alert_engine.py:543-558`). Live DB shows the consequence: **278 active alerts** total, with a single device (`568cd587…`) holding **77 active "Device Down" + 77 active "High Latency" + 41 active "Packet Loss"** rows — i.e. the same flapping device created hundreds of duplicate un-grouped active alerts.
  - `SELECT device_id, rule_id, count(*) FROM alerts WHERE status='active' GROUP BY 1,2` → `568cd587…|Device Down|77`, `…|High Latency|77`, `…|Packet Loss|41`.
- The only dedup mechanism is on the **recovery** side: when a device recovers, the engine UPDATEs all matching open active/acknowledged down-alerts to resolved in one statement (`alert_engine.py:506-530`). But because so many active rows accumulate, the active count grows unbounded for flappy devices (these 278 never recovered/were never resolved).
- Server alerts (baseline/agent) DO dedup by a `metadata->>'dedupe'` key (`server_health_service.py:60-90`). Device/service alerts do not.

### 3b. Auto-resolve

- Device recovery (`new=up`, old in down/degraded) resolves matching open alerts (`alert_engine.py:506-530`) — but **only if the rule has `recovery_alert = true`** (`alert_engine.py:443-444`). All three live rules have `recovery_alert = false` (verified: `Device Down|f, High Latency|f, Packet Loss|f`). **Therefore today, no device alert ever auto-resolves** — which is exactly why 278 alerts sit `active` and 0 are resolved. This is the root cause of the active-alert pileup.
- The auto-resolve WHERE clause also requires `metadata->>'new_status' IN ('down','degraded')` (`alert_engine.py:517`) so it won't accidentally resolve recovery rows.

### 3c. Acknowledge

Works. `POST /alerts/{id}/acknowledge` sets status=acknowledged + acknowledged_by/at (`alerts.py:167-195`, `alert_service.py:60-71`), audit-logged. UI has Ack button (`AlertsPage.tsx:312`).

### 3d. Manual resolve

Works. `POST /alerts/{id}/resolve` (`alerts.py:198-226`, `alert_service.py:74-84`).

### 3e. Escalation chains / on-call schedules

**None.** No escalation table, no on-call rotation, no "if unacked for 15 min, notify tier-2". `notify_channels` is a flat list fired once. PagerDuty dispatch exists (`alert_engine.py:237-258`) which *delegates* escalation to PagerDuty, but ZenPlus itself has no native escalation.

### 3f. Maintenance windows / downtime suppression

- **Service checks** have maintenance windows (`service_check_maintenance` table, `migrate-006-services-v2.sql:74`; poller mutes status transitions during a window, `engine.go:688-806`). So service alerts are suppressed in maintenance.
- **Device (network) alerts have NO maintenance-window suppression.** There is no device maintenance table; the device ping path (`processStatusChange`) has no maintenance check. A planned reboot of a core switch at 2 a.m. will page.

### 3g. Dependency suppression

Works for the parent/child topology case. `_find_suppressing_dependency` (`alert_engine.py:274-296`) finds an unhealthy upstream parent with `suppress_alerts=true` (`topology_dependencies`, `migrate-014`). When a downstream device goes down and its parent is already down/degraded/unknown, the alert is written as **status=resolved** with `suppressed_by_dependency` metadata and **no notification is sent** (`alert_engine.py:474-504`). Same logic for service checks (`alert_engine.py:764-803`). This is genuinely useful and one of the stronger features. Limitation: it only suppresses the *immediate* child of a directly-down parent; multi-hop chains rely on each hop's parent already being marked down.

### 3h. Flap suppression

**None.** No flap detection / flap-count damping. The 77-duplicate-active-alert situation above is the direct symptom. The only damping is the global 3-miss `DownThreshold`.

---

## 4. Notification channels

### 4a. Channel types

Two code paths exist with **different capability sets**, which is itself a problem:

**Engine dispatch (`alert_engine.py`, what actually fires on a real alert):**
- `email` (SMTP via gateway, `_send_email` `:96-118`)
- `sms` (custom HTTP gateway only, `_send_sms` `:70-93`)
- `webhook` (generic JSON, `_send_webhook` `:126-147`)
- `slack` (`_send_slack` `:150-175`)
- `teams` (`_send_teams` `:178-208`)
- `discord` (`_send_discord` `:211-234`)
- `pagerduty` (Events API v2 trigger/resolve, `_send_pagerduty` `:237-258`)
- **No `telegram` in the engine dispatch** (`_dispatch_channel` `:261-271` handles webhook/slack/teams/discord/pagerduty only). So a Telegram channel attached to a rule **silently does nothing on a real alert** — even though the channel type is creatable and "Test"-able.

**Channel CRUD + DB constraint + UI (`settings.py`, `ChannelsPage.tsx`):**
- `notification_channels_type_check` (live) allows only `email, sms, webhook, slack, telegram`. **`teams`, `discord`, `pagerduty` cannot be persisted as channels** (would violate the CHECK constraint), yet the engine has handlers for them. Net result: teams/discord/pagerduty dispatch code is **unreachable** because no channel of that type can exist; telegram channels exist but the engine ignores them. The two layers are out of sync in **both directions**.
- The test endpoint (`settings.py:744-936`) supports email/sms/webhook/slack/telegram — again no teams/discord/pagerduty, and the **test path supports telegram while the real-alert path does not.** A successful "Test" on a Telegram channel gives false confidence.

**SMS:** only `provider == "custom_http"` is implemented (`_send_sms:72`). Twilio (the seeded default `provider:"twilio"`, `migrate-001-alerts.sql:34`) is **not** implemented — `test_channel` returns "SMS test via twilio not implemented yet" (`settings.py:838`).

### 4b. Templating

- Per-rule templates: `email_subject/body`, `sms_template`, plus recovery variants (`alert_rules.py:67-72`). Rendered by naive `{key}` substring replace (`alert_engine.py:63-67`). Variables include hostname, ip_address, status, severity, rule_name, group, location, device_type, rtt, packet_loss, timestamp, duration (`alert_engine.py:399-416`). `duration` is always empty string. No Jinja, no conditionals, no per-channel formatting beyond the hard-coded Slack/Teams/Discord cards.
- **The live rule editor (`AlertRuleFormDialog.tsx`) does NOT expose template fields, notify_channels, trigger_on, recovery_alert, min_duration, schedule.** Those inputs only existed in the **old `Settings.tsx`** rule editor, which is **no longer routed** — `App.tsx` `tabRedirects` maps known settings tabs but has **no entry for `alerts`/`alert-rules`**, and `/settings/:tab` redirects unknown tabs to `/settings/general`. So the rich editor (preview/simulate/channels/templates/schedule) is **dead UI**. The reachable editor hard-codes `notify_channels: []` (`AlertRuleFormDialog.tsx:189`), meaning **every rule created through the live UI has zero channels** and can only ever write an alert row — never send email/Slack/etc. The three live rules confirm this: `notify_channels = []` for all (verified).

### 4c. Digest / rate-limiting

**None.** No batching, no "N alerts in 5 min → one digest", no per-channel rate limit, no flood protection. Combined with no dedup and no cooldown, a flapping link will emit one notification per flip to every channel.

---

## 5. SNMP trap handling

### 5a. Receiver

Go poller listens on UDP/162 (`traps.go:39,58,80`), parses SNMPv1/v2c (v3 not handled). Extracts trap-OID from `snmpTrapOID.0` varbind for v2c, or enterprise+generic+specific for v1 (`traps.go:147-174`). Resolves source IP → device via `LookupDeviceByIP` (`traps.go:186-191`). Persists to ClickHouse `zenplus.snmp_traps`, and also forwards to the API alert engine via `trapAlertSink` (`engine.go:506-518`).

### 5b. Trap → alert rules

`POST /alert-engine/evaluate-trap` (`alert_engine.py:930-1001`):
- Selects enabled `metric='trap'` rules.
- Matches `device_id`/`group_id` scope + OID filter (`_trap_oid_matches` `:921-927`: empty=any, else exact or dotted-prefix).
- INSERTs an `alerts` row.
- **Explicitly does NOT dispatch to channels** — returns `"channels": "skipped (alerts-only)"` (`:939,1000`). So trap "alerts" are visible in the Alert Center but **never email/Slack/page anyone.** This is the single biggest gap for a network shop relying on traps (linkDown, BGP, OSPF, environmental traps from gear that can't be polled fast enough).

### 5c. Severity mapping

Best-effort hard-coded map in the poller (`traps.go:203-217`): coldStart=warning, warmStart=info, linkDown=critical, linkUp=info, authFailure=warning, **everything else=info**. There is **no MIB resolution** — `TrapName` is just the raw OID (`traps.go:175`). Vendor enterprise traps (BGP, OSPF, HSRP, env) all show up as raw OIDs at `info` severity with no human name. The trap-rule severity can override via the rule (`alert_engine.py:981`), but only if an operator hand-creates an OID-specific rule.

### 5d. linkDown ↔ polling correlation

**None.** A linkDown trap and a polling-detected device-down are **two independent alert rows** with no correlation, dedup, or causal linkage. There is no logic that says "we already got a linkDown trap for this interface, suppress the redundant poll-based alert" or vice-versa. (Note: the engine has interface-down *polling* alerts entirely missing anyway — §1.) Trap alerts also bypass the dependency-suppression logic used by status alerts.

---

## 6. Security / operational notes

- The three `/alert-engine/*` endpoints are **unauthenticated by design** ("internal", `alert_engine.py:360`). They listen on the internal API port **8000** (`ss` confirms 8000 + 8001 bound). Probing `http://127.0.0.1:8000/api/v1/alert-engine/evaluate` returns 422 for empty body / "Internal Server Error" for a partial body — i.e. it **is reachable and processes anything that can reach 8000**. The public port 8001 did not respond to the probe from this host (`curl` to `192.168.8.152:8001/alert-engine/evaluate` timed out), so exposure appears limited to localhost/8000, but there is no token/allow-list on the endpoint itself — anyone who can reach 8000 can inject fake status-change/trap events and trigger notifications or write alert rows. Worth a hardening note (shared secret / localhost bind assertion).
- Engine dispatch uses `verify=False` for SMS (`alert_engine.py:85`) and map webhooks (`:665`); webhook channel honors `tls_verify` default True (`:143`).
- All notification sends are awaited **inside the request handler** (synchronous SMTP `server.quit()` etc.). A slow/unreachable SMTP or webhook endpoint blocks the evaluation request up to its timeout (10–15 s) and is run sequentially per channel — a multi-channel rule on a flapping device can stall the poller's alert call (poller client timeout 10 s, `engine.go:488`).

---

## 7. Evidence appendix (live)

```
$ psql zenplus -tAc "... counts ..."
rules|3   rules_enabled|3   channels|0   gateways|2
alerts_total|278   alerts_active|278   alerts_24h|76

$ rules: Device Down (ping_status eq 0, critical, recovery_alert=f, notify_channels=[])
         High Latency (rtt gt 100, warning, recovery_alert=f, notify_channels=[])
         Packet Loss  (packet_loss gt 5, warning, recovery_alert=f, notify_channels=[])

$ alerts by status: active|278   (resolved|0)
$ active dupes for device 568cd587…: Device Down=77, High Latency=77, Packet Loss=41

$ notification_channels_type_check = email|sms|webhook|slack|telegram   (no teams/discord/pagerduty)
$ alert_rules_metric_check includes cpu/memory/if_*/temperature/... (declared, not evaluated)

$ ss -tlnp: 0.0.0.0:8001 (public), 0.0.0.0:8000 (internal alert-engine)
$ POST :8000/api/v1/alert-engine/evaluate {} -> 422 (endpoint reachable, no auth)
```

---

## 8. Prioritized findings

**Critical / broken today**
1. No device-alert auto-resolve in practice — all 3 live rules have `recovery_alert=false`, so 278 alerts are stuck `active`, 0 ever resolved. (`alert_engine.py:443`)
2. Live rule editor hard-codes `notify_channels: []` and exposes no channel/template/trigger fields — every UI-created rule can only write a row, never notify. (`AlertRuleFormDialog.tsx:189`)
3. No dedup + no cooldown enforcement → unbounded duplicate active alerts for flappy devices (77×3 on one device). (`alert_engine.py:543`, cooldown fetched but unused `:388`)
4. Trap alerts are alerts-only — never dispatched to any channel. (`alert_engine.py:939,1000`)
5. Telegram channels never fire on real alerts (engine `_dispatch_channel` omits telegram) but pass "Test". teams/discord/pagerduty handlers are unreachable (CHECK constraint forbids those channel types). (`alert_engine.py:261-271`; `notification_channels_type_check`)

**High**
6. SNMP-metric & NetFlow alert types are schema/UI-declared but never evaluated (cpu, memory, if_oper_status, if_errors, if_discards, if_*_bps, temperature, fan, psu, etc.). (`alert_engine.py:425-429`)
7. `jitter` accepted by API/UI but missing from `metric_values` → never fires. (`alert_engine.py:425`)
8. Threshold rules only evaluate at status transitions; sub-100ms rtt rules and "keeps getting worse while still degraded" rules cannot fire. (`engine.go:413`, `alert_engine.py:467`)
9. No maintenance-window suppression for device alerts (service checks have it; devices don't).
10. `location` scope uses substring match → "DC1" matches "DC10"/"ADC1". (`alert_engine.py:462`)

**Medium**
11. Synchronous, sequential notification sends inside the eval request can block on slow SMTP/webhook. (`alert_engine.py:595-625`)
12. Unauthenticated internal alert-engine endpoints on :8000 (event injection risk). (`alert_engine.py:360`)
13. Trap severity is a 5-OID hard-coded map with no MIB resolution; vendor traps land as raw-OID/`info`. (`traps.go:203-217`)
14. min_duration/max_repeat/duration/schedule_* columns are dead config (never read by engine).

---

## 9. Gaps vs. competitors (SolarWinds NPM, PRTG, LibreNMS, Zabbix)

- Interface alerting (oper-status, % utilization, errors/discards, broadcast storms) — absent.
- CPU/memory/environmental (temp/fan/PSU) threshold alerts — absent (schema-only).
- Per-interface / per-component thresholds and per-device-class default thresholds — absent.
- Dynamic baselining / anomaly detection on network metrics — absent.
- Sustained-condition "for N minutes" / hysteresis per rule — absent (only global 3-miss debounce).
- Escalation chains, on-call schedules/rotations, scheduled silences for devices — absent.
- Alert dedup/grouping/correlation, flap damping, alert storms control, digests/rate-limiting — absent.
- Trap → notification delivery and trap↔poll correlation; MIB-aware trap naming — absent.
- NetFlow-driven alerts (traffic threshold, top-talker, new-conversation) — absent.
- Native channels: Telegram (broken on real path), Teams/Discord/PagerDuty (unreachable), Opsgenie, MS Teams workflow, ServiceNow/Jira ticketing — partial/planned only.
- Per-rule cooldown actually enforced — absent.
