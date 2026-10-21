# Report delivery: schedules, runs, links and retention

The design behind the fixes for `docs/reports-review-assessment.md`, and the
contract that the API and the dashboard follow.

## Runs and share links

- `report_runs` keeps every generated report. Columns added in migrate-166:
  - `window_from`, `window_to`: the window the run covered.
  - `kind`: `scheduled`, `manual` or `retry`.
  - `email_subject`, `email_html`, `email_text`: the email exactly as sent, kept
    for retries.
  - `artifact` (`bytea`), `artifact_name`, `artifact_mime`: the attachment as
    sent.
  - `failed_targets` (`jsonb`): channels and addresses still owed a delivery.
  - `retry_count`, `next_retry_at`.
  - `notes` (`jsonb`).
- A run is inserted with status `running` and finishes as one of:
  - `success`;
  - `partial`: delivered with problems, such as a failed section, a missing
    attachment or some failed targets;
  - `failed`: nothing delivered;
  - `skipped`: no data and the schedule says skip.

  A crash leaves `running`; the tick marks such a run `failed` after 30
  minutes.
- Share link: `/api/v1/reports/shared/{token}`. It expires at
  `generated_at + link_expiry_days` (0 = never). Revoking sets
  `expires_at = now()`. Runs from before migrate-166 get
  `generated_at + 30 days`.
- Retention runs from the scheduler tick, at most once an hour:
  - runs older than `run_retention_days` are deleted;
  - `artifact`, `email_html` and `email_text` are cleared after
    `artifact_retention_days`.
- Report settings live in `system_settings['reports']`, with code defaults:

  | Setting | Range | Default |
  |---|---|---|
  | `link_expiry_days` | 0–365 (0 = never) | 30 |
  | `run_retention_days` | 7–730 | 180 |
  | `artifact_retention_days` | 1–365 | 30 |
  | `max_attachment_mb` | 1–50 | 10 |
  | `retry_attempts` | 0–5 | 3 |

## Delivery

- **Failed sections.** A section that failed to generate makes the run
  `partial` and is named in the run notes.
- **Attachment size.** An attachment larger than `max_attachment_mb` is left
  out. The email then says so and carries the link if one is available; the
  run is `partial`.
- **Retries.** Temporary failures are retried from the stored email after 5,
  15 and 45 minutes (`retry_attempts`): an SMTP connection failure or 4xx
  reply, and a webhook 5xx, 429 or network error. Permanent failures are not
  retried and stay in the run's notes, so a later successful retry still
  leaves the run `partial`:
  - an SMTP 5xx refusal;
  - another webhook 4xx;
  - a missing or disabled channel.

  `POST /reports/runs/{id}/retry` resends to the remaining failed targets at
  once. One retry of a run runs at a time (an advisory lock), so a click
  during the timer's retry is refused with 409.
- **Report links.** The base URL comes from `APP_BASE_URL`, then the Branding
  base URL. Without one, emails say "Open ZenPlus → Reports → Delivered
  reports to view it", and the "Summary only" format is refused for new or
  changed schedules.
- **Empty reports.** A report is empty when no section has a table row or a
  chart point.
  - With `skip_when_empty` on, nothing is sent and the run is `skipped`.
  - With it off, the subject starts with `[No data]`.
- **Run notes.** Callers who may not see a run's delivery addresses get them
  replaced by `[address]` in the notes.
- **Email options** (schedule fields):
  - `subject_template`: placeholders `{title}`, `{period}`, `{date}`.
  - `reply_to`: one address.
  - `group_send`: one message to all direct recipients, who can see each
    other; `cc` applies only then. Default: one message per recipient.
- **SMTP** (`app/core/smtp_tls.py`, used by every sender):
  - `tls` requires STARTTLS.
  - `none` uses STARTTLS when the server offers it, plain otherwise.
    Gateways saved before the field existed show as `none` and relied on
    STARTTLS; a plain relay on port 25 must not be forced into it.

## Schedules API (`/api/v1/report-schedules`)

New or changed fields on every schedule:

| Field | Meaning |
|---|---|
| `skip_when_empty` | bool |
| `subject_template` | string or null |
| `reply_to` | email or null |
| `group_send` | bool |
| `cc` | email list, ≤ 50, only with `group_send` |
| `suggested_time` | `{hour, minute}` or null: the time written in the name when it differs from the stored time. The UI offers "Run at HH:MM instead" (PUT `hour` and `minute`) |
| `overdue` | bool: enabled, due more than 10 minutes ago and not running |
| `is_legacy` | bool |
| `replacement` | template key for a legacy type, else null |
| `recipients_withheld` | bool, with `recipient_count` |

Callers who are not report managers get `recipients: []`, `cc: []` and
`recipients_withheld: true`. An account limited to a visibility scope sees
only the schedules it owns.

Validation:
- `notify_channels` must be existing, enabled channel ids.
- The name is trimmed and must not be empty.
- `filters` keys are limited to `device_ids`, `group_ids`, `locations`,
  `device_types`, `netflow` and `application_id`.
- Weekly needs `day_of_week`; monthly needs `day_of_month` (1–31, clamped to
  the month's length). Fields that do not apply to the frequency are cleared.

Formats:
- Templates: `pdf`, `excel`, `html`, `csv`, `none`.
- Legacy: `pdf`, `excel`, `csv`, `none`.

Periods add `previous_work_week`: the most recent completed five-day work week
starting on the organisation week start (Sunday → Sun–Thu, Monday → Mon–Fri,
Saturday → Sat–Wed).

Legacy types (`executive_summary`, `device_health`, `service_health`,
`alert_analysis`, `full_report`) can no longer be created; existing ones keep
running. Their replacements:

| Legacy type | Replacement template |
|---|---|
| `executive_summary` | `executive_overview` |
| `device_health` | `availability` |
| `service_health` | `service_owner_sla` |
| `alert_analysis` | `alerts` |
| `full_report` | `network_operations` |

New endpoints:
- `POST /{id}/convert`: switches a legacy schedule to its replacement template
  and keeps the rest.
- `GET /status`: returns `{scheduler_last_tick_at, scheduler_healthy,
  overdue_count, share_links_available, settings}`.

`POST /{id}/run-now` refuses with 409 while the same schedule is running.

## Runs API (`/api/v1/reports/runs`)

- `GET ?limit=50&offset=0&schedule_id=&status=` (limit ≤ 500) returns
  `{data, total, limit, offset}`. Each run carries:
  - `id`, `title`, `report_type`, `period`, `token`, `token_withheld`,
    `status`, `delivered_to`, `generated_at`, `schedule_id`, `schedule_name`;
  - `kind`, `error`, `notes`, `expires_at`, `revoked`;
  - `has_artifact`, `artifact_name`, `retry_count`, `next_retry_at`,
    `failed_targets`.
- `GET /{id}/artifact` downloads the stored attachment. It needs
  `reports.export` and the same link visibility as the token.
- `POST /{id}/revoke`: report manager or schedule owner.
- `POST /{id}/retry`: report manager.
- `GET` / `PUT /api/v1/reports/settings`: the settings above. Reading needs
  `reports.view`; changing needs `reports.manage`.

## Scope filters on template reports

`report_render.resolve_device_scope(db, device_ids=, group_ids=, locations=,
device_types=)` turns a group, location or device-type scope into device ids
(None = no scope). The viewer, preview, CSV export and schedules all use it.
