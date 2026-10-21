# ZenPlus 1.24.23 — Reports: delivery you can rely on

This release fixes the problems found in a field assessment of report scheduling, and adds the delivery features that were missing. The design is described in `docs/reports-delivery.md`.

## Scheduled delivery

- **Retries.** A delivery that fails temporarily is sent again from the stored email after 5, 15 and 45 minutes. This covers a mail server or network outage, an SMTP 4xx, and a webhook 5xx or 429. Permanent refusals stay listed on the run and are not retried.
- **Delivered reports.** Each run shows what happened: running, delivered, delivered with problems, failed or skipped.
  - **Download** the attachment exactly as it was emailed.
  - **Revoke** the run's link.
  - **Retry** the failed recipients.
  - The list can be paged and filtered by status and by schedule.
- **Link expiry and retention.** Report links now expire after 30 days by default, and any link can be revoked. Old runs and stored attachments are removed after the retention periods. All of this is set under Reports → Schedules → Report settings: link lifetime, run and attachment retention, the attachment size limit and the number of retries.
- **No link to the report.** When no appliance URL is set (Settings → General → Company), report emails now say where to find the report. The "Summary only" format, which relies on that link, can't be chosen until a URL is set.
- **Empty reports.** A report with no data for its period gets "[No data]" in the subject, or is not sent at all if the schedule says so.
- **Email options.** Each schedule can set its own subject (with `{title}`, `{period}` and `{date}`) and reply-to address. It can also send one email to all its recipients with a CC list, instead of one email per recipient.
- **New options.** Template reports can be attached as CSV. Schedules and the viewer can limit a report to device groups, locations or device types. A new period, "Previous work week", covers Sunday–Thursday when the week starts on Sunday.
- **Schedule times.** When a schedule's name gives a different time from the one it actually runs at, the schedules page offers "Run at 08:00 instead". Overdue schedules and a stopped scheduler are flagged.
- **SMTP.** A gateway set to "none" now uses STARTTLS when the server offers it, and sends plainly otherwise, so plain relays on port 25 work.

## Report figures

- **Interface errors.** Interfaces whose devices report no packet counters no longer show a 1,000,000 ppm (100 %) error rate.
- **Availability.** Availability Summary now shows Unreachable time (failed probes) and Confirmed outage time separately. "Total downtime" is renamed "Unreachable time". The availability percentage is unchanged.
- **MTTR** covers incidents raised in the report period.
- **Flapping.** The two interface sections now agree on how many interfaces flapped, and ports shut down by an administrator are left out.
- **Past periods.** Periods without data show "—" instead of a green 0. Findings that describe the current state are labelled "as of" the generation time.
- **PDF and CSV output.**
  - PDF tables wrap long names instead of cutting them off.
  - Section titles within a template are distinct.
  - Scope labels name the devices.
  - Attachment file names are shortened at word boundaries.
  - Classic CSV uptime shows two decimals.

## Behaviour changes

- **Classic report types are retired.** Executive Summary, Device Health, Service Health, Alert Analysis and Full Report schedules keep running and can be converted to their template in one click; new ones can't be created.
- **Schedule ownership.** A classic schedule whose owner was deactivated no longer runs, as template schedules already behave.
- **Recipient visibility.** Recipient addresses are shown only to report managers and to the schedule's owner. Accounts limited to a visibility scope see only their own schedules and runs.
- **API.** Weekly and monthly schedules created through the API must state their day. Notification channels must exist and be enabled.

Schema: migrate-166 (report runs, schedule email options, the new period). Existing report links get a 30-day lifetime from when they were generated.
