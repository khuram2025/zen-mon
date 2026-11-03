# ZenPlus 1.24.47

Use the configured company timezone for email, SMS, dashboard timestamps,
calendar filters and exported display times. An event at 13:00 UTC displays
as 16:00 GMT+3 when General Settings uses Asia/Riyadh, regardless of the
viewer computer's timezone. Email layout remains unchanged.

Apply regional formatting to test, trigger, recovery, reminder, escalation
and grouped notifications, gateway tests, NCM notifications and report SMS.
Preserve the original event or report generation time on retries. Include a
human-readable date, time and UTC offset in SMS. Reuse report regional
formatting, including configured date and clock formats where applicable.
Explicit report and schedule timezones remain supported.

Load public regional preferences before dashboard pages render and refresh
them after settings changes. Interpret date/time filters in that same zone;
reject nonexistent DST wall times. Cover Riyadh, UTC, half-hour offsets,
midnight rollover and repeated DST hours with regression tests.

Storage and machine API timestamps remain UTC. No schema migration.
Appliance-only release requiring the signed 1.24.36 bridge; preserve existing
Windows agent packages, manual update controls and restart alert protections.
