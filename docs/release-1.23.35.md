# ZenPlus 1.23.35 — Branded reports library, template designer and scheduling

Reports are rebuilt around the organisation's branding. Everything below ships
in the appliance package; separately installed Windows agents are untouched.

- **Company branding.** Settings → General → Company now holds the name,
  address, contact details, appliance URL, light and dark logos (PNG, JPEG, WebP
  or SVG), timezone, date and time formats, week start, brand colour, footer,
  disclaimer, confidentiality level and paper size. PDF, HTML, Excel and email
  reports all use them; reports are no longer fixed to UTC.
- **154 built-in templates in 18 categories** built from 167 sections: devices,
  interfaces, hardware, services, alerts and events, NetFlow, NetPath and
  discovery, Device Tracker, configuration, servers, APM, browser RUM and the
  platform, plus executive, NOC, service-owner SLA, security, capacity and
  digital-experience summaries. The old persona pages open the matching
  templates.
- **Template designer.** Customise any built-in template or build one from
  scratch with a live preview. Templates are private or shared; authors can only
  add sections they are allowed to view.
- **Schedules.** Calendar periods, direct email recipients, HTML format, run
  history and branded emails. Schedules run with their owner's permissions and
  are generated one at a time, so a worker restart loses at most the run in
  progress. A partially failed SMTP delivery keeps the recipients already sent.
- **Correctness.** Report windows are half-open, availability is never rounded
  up to 100.00% when there was downtime, group availability is weighted by the
  samples behind each node, and missing data is shown as unknown rather than
  zero. Excel and CSV exports no longer include data outside the caller's
  visibility scope.

Schema: `migrate-156-report-templates.sql` (extends `custom_reports` and
`report_schedules`; number 155 is intentionally unused). Appliance-only,
minimum prerequisite 1.23.9.
