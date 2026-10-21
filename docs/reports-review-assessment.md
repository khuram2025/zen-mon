# Reports and scheduled delivery: assessment (2026-10-21)

Live assessment of the Reports section on the SMASCO appliance `10.51.2.12`
(ZenPlus 1.24.22), signed in as `admin`, plus a read of the code in this
repository at `db3e42a`. Nothing was changed in code or in appliance
configuration. The only appliance changes are the test schedules and one test
template listed in "What was created on 10.51.2.12".

Fixes belong on the dev appliance (192.168.8.221) through the normal branch, PR
and release flow.

## Summary

- **Generation is solid.** All 154 built-in templates render (154 × HTTP 200,
  no hard failures). PDF, HTML and XLSX exports work for every template
  sampled. Email delivery to `hmohammed@smasco.com` succeeded for all 15 test
  schedules and was confirmed in Outlook.
- **Scheduling has real defects.**
  - The four existing "08:00 Riyadh" schedules have delivered at 05:00 Riyadh
    since 16 Oct.
  - Emails carry no link to the full report, so a "Summary only" delivery has
    neither an attachment nor a link.
  - The schedule API accepts channel IDs, names and filters that later break
    or silently change the delivery.
- **Several figures in management reports contradict each other**: device
  downtime vs outages, MTTR, interface error rate, flapping counts, and legacy
  vs template KPIs. These are the most visible problems for a manager reading
  the PDFs.

## What was tested

| Area | Coverage | Result |
|---|---|---|
| Built-in templates (JSON render, each template's default period) | 154 of 154 | 154 × 200. One transient section failure (ClickHouse timeout while 3 renders ran in parallel; a retry passed) |
| Templates with at least one empty section | 54 | All explained by missing data sources (see "Data observations") |
| Fully empty templates | 31 | APM/RUM not instrumented, no NetPath probes, no F5, no syslog/traps received, no discovery findings, no SLOs, no DC logon collection, no WAN-tagged links, September has no ping data |
| Exports PDF / HTML / XLSX | 15 templates × 3 formats (one per category plus 5 management templates) | 45 × 200, valid file signatures, correct `Content-Disposition` |
| Organisation (custom) template | create, preview, render, schedule, delete guard | All working; deleting a template that is in use returns 409 as designed |
| Schedules | 1 created in the UI and 14 through the API | All 15 "Run now" runs succeeded and were delivered |
| Schedule variety | daily / weekly / monthly; rolling, calendar and previous periods; PDF / Excel / HTML / CSV / summary only; legacy and template engines; device-scoped; custom template | Delivered. Issues listed below |
| API validation | 18 negative cases | 7 accepted that should be refused (see M1) |
| Email rendering | Outlook on the web, `hmohammed@smasco.com` mailbox (an inbox rule files the mail into the `zenplus` folder) | Renders well. Issues listed below |
| Share links / run history | unauthenticated fetch of the oldest run | Still served (see H5) |

Slow renders, in seconds, measured while 3 renders ran in parallel:

| Template | Time |
|---|---|
| `netflow_apps_protocols_report` | 40 |
| `netflow_executive` | 37 |
| `netflow_sites_groups_report` | 34 |
| `netflow_country_asn_report` | 27 |
| `traffic` | 27 |
| `capacity` | 22 |
| `interface_errors_discards` | 21 |
| `server_cpu_increases` | 20 |
| `server_top_memory` | 20 |
| `network_operations` | 17 |
| `netflow_conversations_report` | 15 |

The legacy Executive Summary as CSV took 39–45 s for 34 devices.

## High

### H1. Existing "08:00 Riyadh" schedules deliver at 05:00 Riyadh

**What happens.** "Daily NOC Summary", "Daily Server Health" and "Daily Storage
and Backup" ran at 05:00 UTC (08:00 Riyadh) until 15 Oct. From 16 Oct they run
at 02:00 UTC (05:00 Riyadh). "Weekly IT Operations Review" last ran on 18 Oct at
05:00 UTC; its next run is 25 Oct at 02:00 UTC.

All four store `hour=5, timezone=Asia/Riyadh`. The hour was entered as a UTC
hour, and since the timezone change it is read as Riyadh time. The schedules
page shows "The name says 08:00; it runs at 05:00 Asia/Riyadh", but nothing has
been corrected.

**Code.**
- `scripts/migrate-163-report-schedule-timezone.sql` pins the organisation
  zone without converting the hour.
- `server/app/services/report_scheduler.py:136` (`compute_next_run`).

**Fix.**
- *Appliance (needs owner approval):* set `hour=8` on the four schedules.
- *Code:* when `name_time_mismatch` differs from the stored time by exactly the
  zone's UTC offset, offer a one-click "Use 08:00 Riyadh" fix on the schedules
  page. Add an upgrade check that lists such schedules.

### H2. Report emails have no link, and "Summary only" sends neither attachment nor link

**What happens.** Every run returned `view_url: null`, because neither
`APP_BASE_URL` nor the branding base URL is set.

- The "Interface Errors and Discards — Summary only" email arrived with KPIs
  only: no attachment and no link.
- The schedule form describes "Summary only" as "Email summary with a link, no
  attachment".

**Code.**
- `report_scheduler._base_url` (`report_scheduler.py:470`).
- The "Summary only" option card in
  `dashboard/src/components/forms/ReportScheduleFormDialog.tsx`.

**Fix.**
- *Config:* set the base URL (Branding → base URL) on 10.51.2.12.
- *Code:*
  - Refuse or warn on "Summary only" when no base URL is configured.
  - Show a banner on the schedules page.
  - Say "link unavailable" in the email instead of omitting it silently.

### H3. Interface error rate is reported as 1,000,000 ppm (100 %) when a device has no packet counters

**What happens.** In the 20 Oct report, `SMASCO-HQ-FG1 / DC` shows 198,946
errors at 1,000,000.00 ppm, rated "critical". `New_ACC_AdminB_SW06 Gi1/0/24`
shows the same.

`_health_payload` adds the errors and discards themselves to the denominator.
When the unicast counters are 0, the rate becomes errors / errors = 100 %. The
intended fallback (`_ERR_ABS_FLOOR`, "no denominator") never applies. The same
code feeds the Link Utilization page.

**Code.** `server/app/api/v1/link_utilization.py:228-245` (`_health_payload`)
and `:168` (`_ppm`).

**Fix.** Treat `in_pkts + out_pkts == 0` as "no packet counters": rate `None`,
then apply the absolute floor.

### H4. Device availability and outage figures contradict each other in management reports

**What happens.**

| Window | Total downtime | Outage events | Other |
|---|---|---|---|
| Executive Overview, last 7 days | 978 min | 71 | Longest outage 7 min; 33 of 34 nodes "below 99.9 % target" |
| 1–5 Oct | 21 min | 0 | |

Availability and downtime count every failed ping sample. "Outage events" and
"Longest outages" use confirmed episodes from `device_status_log`. Isolated
probe loss on a network with about 60 ms average RTT therefore reads as SLA
breaches on almost every node.

Services already use a confirmed basis (`basis=confirmed`). Devices do not.

**Code.** `server/app/services/report_sections.py:488` (`_node_availability`)
and `:674` (`_sec_availability_kpis`). The same arithmetic is used by the
Availability page and `report_data_service.build_executive`.

**Decision needed.** Either move device availability to the confirmed basis, or
show "Probe-loss time" and "Confirmed outage time" as separate KPIs. Then review
the 99.9 % target.

### H5. Share links never expire and run history is never purged

**What happens.** `GET /api/v1/reports/shared/{token}` needs no
authentication. The oldest run (14 Oct) still returns 200 with about 108 KB of
HTML.

- `report_runs.expires_at` is never set.
- Nothing deletes old runs; each stores the full HTML page in PostgreSQL.
- 43 runs so far; the daily volume will grow with the schedules.

**Code.**
- The run insert at `report_scheduler.py:737`.
- `reports.py:269` (the shared route).
- No retention job.

**Fix.**
- Make link expiry configurable (for example 30 or 90 days).
- Add retention for `report_runs`, tied to the storage policy.
- Let an owner revoke a link.

## Medium

### M1. The schedule API accepts definitions that break later

Results of the negative tests:

| Input | Result |
|---|---|
| `notify_channels: ["not-a-uuid"]` | Accepted. "Run now" stored the raw SQLAlchemy/asyncpg error (SQL text plus a sqlalche.me URL) in `last_error`, and the UI shows it |
| `notify_channels: [<uuid of no channel>]` | Accepted. Every run fails with "channel missing/disabled" |
| `name: "   "` | Accepted (the length check runs before trimming) |
| `filters: {foo, group_ids}` | Stored. Template engines ignore `group_ids`, `locations` and `device_types` without saying so |
| weekly without `day_of_week` | Silently becomes Monday |
| monthly without `day_of_month` | Silently becomes the 1st |
| `day_of_month: 31` | Accepted by the API; the UI only offers 1–28 (`model.ts:165`) |
| daily with `day_of_week` / `day_of_month` | Stored |

Correctly refused: invalid or display-name emails, unknown timezone, more than
50 recipients, legacy + HTML, template + CSV, `custom` without an id, non-UUID
device ids.

**Code.** `server/app/api/v1/report_schedules.py:137-180` (models) and `:221`
(`_validated_filters`).

**Fix.**
- Validate channel ids (UUID, exists, enabled).
- Trim the name.
- Whitelist filter keys per engine.
- Require the weekly/monthly day fields.
- Turn exceptions into short messages for `last_error`.

### M2. Legacy-engine emails mislabel manual runs and direct recipients

**What happens.** The "[Manual run] Legacy Executive Summary" email shows a
"SCHEDULED REPORT" badge and says "This report was delivered automatically
because a notification channel is linked to a report schedule". It was a manual
run to a direct recipient, with no channel involved. There is no manual-run
banner; template emails have one.

**Code.** `server/app/services/report_html.py:377` and `:465`.

### M3. The legacy and template engines disagree on the same week

For 14–21 Oct:

| KPI | Legacy Executive Summary | Template engine |
|---|---|---|
| MTTR | 1.3 min | 56 min (Alert Summary) |
| Incidents | 71 "▲71" (previous week = 0) | Alerts exist in the previous week |

Recipients of both will see different numbers.

**Recommendation.** Retire the five legacy types: map existing legacy schedules
to their template equivalents, then hide the legacy types from the schedule
picker.

### M4. Legacy reports can be scheduled but never previewed, and their emails ignore scope

- `/reports/view/executive_summary` shows "This report opens elsewhere".
  "Open the classic report" goes to `executive_overview`, a different template
  (`SectionReportViewer.tsx:53`, `LEGACY_PAGES`).
- The legacy email and share page come from `build_executive(db, start, end)`
  with no device filter and no owner scope, even when the attachment is
  filtered (`report_scheduler.py`, legacy branch of `generate_and_deliver`).
- Legacy CSV/XLSX generation takes 39–45 s for 34 devices.

### M5. Alert Summary MTTR covers incidents raised before the window

MTTR averages incidents that recovered in the window, wherever they started.

| Window | Alerts raised | Incidents recovered | MTTR |
|---|---|---|---|
| August | 12,970 | 37,987 | 928.7 h (longer than the 744 h window) |

**Code.** `report_sections.py:1986` (`_sec_alert_kpis`).

**Fix.** Compute MTTR over incidents raised in the window, or label the KPI
"of incidents recovered in the period" and show the count beside "Total
alerts".

### M6. Interface flapping counts disagree within one report

The 20 Oct "Interface Errors and Discards" report shows "Flapping: 386 (status
changed at least once)" and, in the next section, "Interfaces with changes: 60".

The two sections use different sources: per-poll flaps in `interface_health`
versus the status-change history in `interface_status_changes`. The flapping
KPI also counts administratively shut ports, whose health is reset but whose
flaps are still counted (`report_packs/interfaces.py:666`).

**Fix.** Pick one definition, exclude admin-down ports, and use the same number
in both sections.

### M7. Previous-period reports mix in current-state findings and show "no data" as green

The September "Monthly IT Operations Review" (no ping data that month) shows:

- Availability "—", but "Below target 0" and "Total downtime 0 min" in green
  (also in the viewer).
- Top findings taken from "now" sections: acknowledgement overdue now (374),
  unassigned critical now (25), disks filling, server fleet critical.

A September report reads as if those were September facts.

**Fix.**
- Show "not measured" instead of 0 when there are no samples.
- Keep "now" findings out of the Top findings of a closed past period, or label
  them "as of generation".
- Optionally mark a run "no data for period".

### M8. A failed section still produces a "Succeeded" run

`generate_and_deliver` never looks at section errors. A section that hits the
ClickHouse timeout (observed once for `server_top_processes`) is emailed as
"temporarily unavailable" while the schedule shows Succeeded.

Several schedules fire in the same minute (08:00) and render one after another,
so heavy NetFlow reports make this more likely.

**Code.** `report_scheduler.py:624` onward. Add failed sections to `run_notes`,
which turns the run into `partial`.

### M9. Delivery has no retry and keeps little failure detail

- The run row is committed as `success` before delivery (`:737`). A crash
  after that leaves the run at success while the schedule says failed.
- There is no retry: an SMTP outage at 08:00 loses that day's report.
- On an exception, `last_error` is only the exception class name (`:1009`).
- "Run now" renders and sends inside the HTTP request (45 s for the legacy
  CSV) with no lock against the timer.

### M10. SMTP "encryption: none" still forces STARTTLS

`_smtp_connect` calls `starttls()` for anything that is not `ssl`
(`report_scheduler.py:366`). The same pattern is in the alert engine and in the
settings test. A plain relay on port 25 fails. Not hit on 10.51.2.12, which
uses TLS on 587.

### M11. Week start is Monday for a Saudi organisation

A weekly Sunday 08:00 schedule with "Previous week" (the suggested period)
covers 12–18 Oct when it runs on 25 Oct, missing the six days just completed.
The form shows the window but still labels the choice "suggested".

**Fix.**
- *Config:* set Branding → week start to Sunday.
- *Code:* suggest "Last 7 days" when the run day is not the first day of the
  week. Consider a work-week period (Sun–Thu).

### M12. Anyone with `reports.view` sees every schedule's recipients

`GET /report-schedules` and `GET /reports/runs` return every schedule with its
recipient email addresses and every run title. This includes accounts limited
to a visibility scope. Share tokens are withheld from them; recipients are not.

## Low / cosmetic

- **L1.** PDF tables with many columns truncate the content. The Executive
  Overview "Service SLA compliance" table shows "Raho …" and "SMAS…", and
  headers "CRITICALI…", "TARGET (SOU…", "COVERA…". KPI cards cut off "BELOW
  CONFIGURED…" and "Current status, at gene…".
- **L2.** Repeated section titles inside one template:
  - `availability_by_site_group`: 3 × "Availability by Group and Site"
  - `latency_loss_jitter`: 3 × "Latency, Packet Loss and Jitter"
  - `netpath_latency_report`: 2 × "Path Latency Trend"
  - `rum_page_load_breakdown`: 2 × "Page Load Breakdown"
- **L3.** The attachment file name truncation cuts the time:
  `…-Monthly-IT-Operations-Review-1st-08-0-20261021-0525.pdf`
  (`report_render.report_filename`).
- **L4.** "Longest Outages" renders an empty table with no "no outages in this
  period" note. Other empty sections explain themselves.
- **L5.** The scope label says "3 selected device(s)" without naming the
  devices.
- **L6.** `POST /reports/generate` turns an unknown `period` or `format` into a
  24 h PDF without an error. Tested with `last_year` + `docx`: 200, PDF.
- **L7.** `GET /reports/runs` caps `limit` at 100 and Delivered reports shows
  50, with no paging or filter by schedule. Run history per schedule shows 20.
- **L8.** The legacy CSV uptime has one decimal (99.9 %), while device
  availability headlines use two.
- **L9.** The existing "Weekly IT Operations Review" uses the *Monthly* IT
  Operations Review template over 7 days, so the content is titled "monthly"
  (configuration).

## Missing features

1. Retry failed deliveries with backoff, and resend to the failed recipients
   only.
2. Run retention and share-link expiry/revocation (see H5).
3. An attachment size guard (Exchange limits) that falls back to a link.
4. "Don't send when the period has no data", or a "no data" marker in the
   subject.
5. Work-week awareness for KSA: organisation week start, a "previous work week"
   period, business-hours availability.
6. A migration path from the legacy types to templates (see M3/M4).
7. Group and location scoping for template schedules (today only devices, RUM
   application and NetFlow; groups and locations work only in legacy
   attachments).
8. CSV for template reports (only XLSX/PDF/HTML).
9. Per-schedule subject line, CC/BCC and reply-to.
10. Missed-run detection: alert when a due schedule did not run, plus a
    scheduler heartbeat.
11. Download the stored PDF/XLSX from the run history (only HTML is stored).

## Configuration items on 10.51.2.12 (no code change; need owner approval)

1. Set `hour=8` on the four existing "08:00 Riyadh" schedules (H1).
2. Set the base URL so emails carry "Open report" links (H2).
3. Branding:
   - Company name: the reports currently say "ZenPlus" and "Prepared by
     ZenPlus", with no SMASCO logo.
   - Logo.
   - Week start Sunday (M11).
4. After H4 is decided, review the 99.9 % device availability target (33 of 34
   nodes are below it on the sample basis).
5. Check `New_ACC_B11_SW02 Gi1/0/43` on site: 1.7 billion errors in one day
   (282,081 ppm). Also `New_ACC_B8_SW01 Gi1/0/39`: 10 million errors. These
   look like real physical-layer faults, not a report defect.
6. Point "Weekly IT Operations Review" at a weekly template, or rename it (L9).

## Data observations (not defects)

- Ping/availability data starts on 2026-10-01. Every "Previous month"
  (September) report is empty for availability, but alerts exist for August
  and September.
- APM, RUM, NetPath, F5, syslog/traps, SLOs, DC logon collection and
  WAN-tagged links have no data. The 31 fully empty templates come from these
  gaps.

## What was created on 10.51.2.12

All schedules send to `hmohammed@smasco.com` at 08:00 Asia/Riyadh. Each was run
once with "Run now", and all 15 were delivered and confirmed in Outlook.

| Schedule (prefix "[Assessment]") | Template | Period | Frequency | Format |
|---|---|---|---|---|
| Executive Overview - Weekly Sunday (created in the UI) | executive_overview | last 7 d | weekly Sun | PDF |
| Monthly IT Operations Review - 1st 08:00 | monthly_it_operations_review | previous month | monthly 1st | PDF |
| Service SLA Compliance - Monthly Excel | service_sla_compliance_monthly | month to date | monthly 1st | Excel |
| NOC Daily Briefing - HTML | noc_daily_briefing | last 24 h | daily | HTML |
| Network Operations - Yesterday PDF | network_operations | yesterday | daily | PDF |
| Availability - Previous Week Excel | availability | previous week | weekly Sun | Excel |
| Legacy Executive Summary - CSV | executive_summary (legacy) | last 7 d | weekly Sun | CSV |
| Legacy Device Health - Excel | device_health (legacy) | last 24 h | daily | Excel |
| NetFlow Executive Summary - Weekly | netflow_executive | last 7 d | weekly Sun | PDF |
| Capacity Risk Summary - Monthly | capacity_risk_summary | last 30 d | monthly 1st | PDF |
| Interface Errors and Discards - Summary only | interface_errors_discards | yesterday | daily | none |
| Availability - Scoped to 3 devices | availability (Amman_New100F, Dammam, FortiGate-101F) | last 7 d | weekly Sun | PDF |
| Top Devices CPU Load - HTML | top_devices_cpu_load | last 7 d | weekly Sun | HTML |
| Security and Compliance Summary | security_compliance_summary | last 30 d | monthly 1st | PDF |
| Custom Template - Branch Availability and Servers | custom "[Assessment] Branch Availability and Server Health" | last 7 d | weekly Sun | PDF |

The 10 disabled validation schedules created for M1 were deleted. Their two
failed runs remain in Delivered reports, because deleting a schedule keeps its
runs.

**Still to verify.** The four daily schedules above should fire on the timer at
08:00 Riyadh (05:00 UTC) on 2026-10-21. The existing schedules show the timer
firing daily.
