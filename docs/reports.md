# Reports

Reports are built from **sections** (self-contained units of content) and **templates**
(named, ordered lists of sections). Built-in templates ship with the code; administrators
customise a copy, or build their own, in the Template Designer. Every report renders the
same way on screen, as PDF, HTML, Excel and in scheduled emails, in the organisation's
branding, timezone and date/time format.

## Where things live

| Concern | Code |
| --- | --- |
| Section registry, `SectionCtx`, `build_sections`, HTML/PDF/email renderers | `server/app/services/report_sections.py` |
| Section packs (one module per product area) | `server/app/services/report_packs/*.py` |
| Template catalog: categories, `template()`, validation | `server/app/services/report_templates.py` |
| Cross-domain templates (executive and persona reports) | `server/app/services/report_catalog.py` |
| Render pipeline shared by viewer, preview, downloads | `server/app/services/report_render.py` |
| Charts from series (matplotlib) | `server/app/services/report_charts.py` |
| Excel output | `server/app/services/report_xlsx.py` |
| Organisation branding and regional formatting | `server/app/services/branding.py` |
| Calendar periods (previous month, …) | `server/app/services/report_periods.py` |
| Scheduled delivery | `server/app/services/report_scheduler.py`, `server/app/api/v1/report_schedules.py` |
| API: catalog, render, preview, organisation templates, branding | `server/app/api/v1/reports.py` |
| Dashboard contract (types, hooks, periods, preferences) | `dashboard/src/features/reports/*.ts` |
| Library, viewer, designer, schedules | `dashboard/src/pages/reports/*`, `dashboard/src/features/reports/{library,render,viewer,designer,schedules}` |
| Branding settings (Settings → General → Company) | `dashboard/src/components/settings/CompanySettings.tsx` |

## Adding a section

Create or extend a module in `report_packs/` (modules are discovered automatically) and
register the function:

```python
from app.services.report_sections import SectionCtx, _section, register_section, top_n_option

@register_section(
    "device_status_now", title="Current Device Status", category="Availability",
    description="Up, down, degraded and unknown nodes right now.",
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[top_n_option(25, 200)],
)
async def device_status_now(ctx: SectionCtx) -> dict:
    clause, params = ctx.pg_device_clause("d.id")
    ...
    return _section("device_status_now", "Current Device Status", kpis=[...], tables=[...])
```

Rules, enforced by `server/tests/test_report_packs_contract.py`:

- **scope** — `"device"` only when every query honours `ctx.device_ids`
  (`ctx.pg_device_clause` / `ctx.ch_device_clause`; `scoping.NO_MATCH_ID` must match
  nothing); `"netflow"` only through `netflow_report.resolve_scope(ctx)` (category
  `Traffic` is reserved for these); otherwise `"fleet"`, which is omitted for accounts
  limited to a visibility scope.
- **requires** — the module permission(s) of the data, all required. Sensitive data uses
  the narrower permission (`udt.view_users`, `audit.view`). A viewer without them gets a
  "not included" stub, never the data.
- **options** — `int`/`number` (with `min`/`max`), `select` (default among the choices) or
  `bool`. Read them with `ctx.opt("key")`; templates and the designer set them.
- **Output** — KPI values are formatted strings, `"—"` for not measured (never 0); charts
  carry a `series` (`area`, `line`, `bars`, `stacked`, `rank`, `donut`) and the renderers
  draw images from it; table rows have exactly one cell per header; empty data returns a
  note, never an exception.
- **Time** — show times with `ctx.fmt_dt` / `ctx.fmt_short` / `ctx.fmt_date`, chart points
  with `ctx.iso(ts)`, ClickHouse buckets with `ctx.ch_bucket(...)` so days start at local
  midnight in the organisation timezone.
- **Cost** — aggregate in ClickHouse/Postgres, pick rollups by window length, run
  ClickHouse with `await ctx.ch_rows(...)` (off the event loop), disclose retention limits
  with `ctx.window_note(days, "<dataset>")`.

Each section runs in its own savepoint; a failure becomes a reader-facing note and the rest
of the report is unaffected.

## Adding a built-in template

At the bottom of a pack module (domain templates) or in `report_catalog.py`
(cross-domain):

```python
template("server_cpu_increases", "Server CPU – Biggest Increases",
         "Servers whose CPU rose most against the previous period.", "servers",
         [S("server_cpu_change", None, top_n=15, min_change=5)],
         audience=("Systems engineer",), default_period="last_7d", frequency="weekly",
         icon="Cpu", featured=True)
```

Keys are `lower_snake_case`. `report_templates.validate_catalog()` (run by the contract
test) rejects unknown sections, invalid options, categories, periods and audiences. Every
template is also published in `report_sections.REPORT_PRESETS`, so schedules can run it by
key.

## Organisation templates

Stored in `custom_reports` (migrate-156): `definition` holds the ordered section entries
with titles and options; `visibility` is `private` (owner and full administrators) or
`shared` (everyone with `reports.view`). Creating and editing needs `reports.manage`;
owners edit their own, report managers also curate shared ones; someone else's private
template is a 404 everywhere. Create, update, clone and delete are audit-logged as
`reports.template.*`.

## Branding

Settings → General → Company stores the organisation profile in `system_settings.company`
and the logos in `company_logo` (light backgrounds) and `company_logo_dark` (report cover).
Uploads are identified by their bytes (PNG, JPEG, WebP, SVG without scripts or event
handlers, 2 MB) and served with a sandboxing Content-Security-Policy. `branding.load_branding`
is the single reader; every renderer, the scheduler and report emails use it. Report
viewers read it through `GET /reports/branding` (no `settings.view` needed).

Times print as `<date> <time> <zone>`, where the zone label is the UTC offset
(`GMT+3`, `GMT-4`, `GMT+5:30`) or `UTC`; the dashboard (`lib/branding.ts`) prints the same.

## Permissions

| Action | Permission |
| --- | --- |
| Library, viewer, JSON render | `reports.view` (+ each section's `requires`) |
| PDF / HTML / Excel download | `reports.export` |
| Designer, preview, organisation templates, schedules | `reports.manage` |
| Share links of scheduled runs | the schedule owner and report managers |

Shared report links (`/api/v1/reports/shared/{token}`) are served with
`Cache-Control: no-store`, `no-referrer`, `noindex` and a strict Content-Security-Policy.

## Schedules

Periods are rolling (`last_24h`, `last_7d`, `last_30d`, `last_90d`) or calendar windows in
the organisation timezone and week start (`today`, `yesterday`, `week_to_date`,
`previous_week`, `month_to_date`, `previous_month`, `quarter_to_date`,
`previous_quarter`). A schedule runs a built-in template key, a legacy report type, or an
organisation template (`report_type = "custom"` + `custom_report_id`), renders with its
owner's permissions, and delivers PDF, Excel or HTML to notification channels and direct
email recipients. Due schedules are claimed before generation, so two API workers never
deliver the same run twice.
