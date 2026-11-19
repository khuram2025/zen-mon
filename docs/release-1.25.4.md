# ZenPlus 1.25.4

**Applications → Synthetics** has been redesigned. The list shows, for each
scenario, uptime, an availability sparkline, average and p95 duration, failures
and the last failure over a selectable time range (default 24 hours; 1 h,
24 h, 7 d, 30 d or a custom range, kept in the link). Clicking a scenario opens
its new detail page with uptime, runs, failures, outages and downtime,
availability and duration trends, and a per-step table (pass rate, executed /
failed / skipped, average and p95 time, status codes). Clicking a step shows its
trend, failures and errors; clicking a run shows every step of that run with a
timing waterfall, assertions, the error and the response snippet. Grouped errors
and outages link to the matching runs.

Each step now records where its time went (connect, TLS, send, wait for first
byte, download) together with the server address, response size, content type,
redirects and HTTP version. Runs recorded before this release have no timing
breakdown.

Fixes: run times are now shown in the browser's time zone instead of being
offset; history is no longer cut off at the last 200 runs; scenario variables,
request bodies and credential headers (Authorization, cookies, tokens, API keys)
are masked for users who cannot manage scenarios, and saving a scenario with
masked values keeps the stored secrets; the scenario editor no longer drops
variables and the TLS-verification setting when saving.

No migrations. Appliance-only release requiring the signed 1.24.36 bridge;
existing Windows agent packages are preserved.
