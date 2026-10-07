"""Report section packs.

Every module in this package registers report sections for one product area
with ``report_sections.register_section`` and may add built-in templates for
that area with ``report_templates.template``. Modules are discovered
automatically (imported in name order) when report_sections is imported.

Authoring rules (enforced by tests/test_report_packs_contract.py):
- scope "device": every query honours ctx.device_ids (ctx.pg_device_clause /
  ctx.ch_device_clause); "netflow": go through netflow_report.resolve_scope;
  anything else is "fleet" and is omitted for visibility-scoped accounts.
- requires: the module view permission(s) the data belongs to; sensitive data
  declares the narrower permission (udt.view_users, audit.view, ...).
- Missing data is "not measured" (—), never zero. Timestamps are formatted
  with ctx.fmt_dt / ctx.fmt_short / ctx.fmt_date (organisation timezone).
- ClickHouse runs off the event loop (await ctx.ch_rows(sql, params)).
- A section never raises for "no data": it returns an empty-state note.
"""

import pkgutil
from importlib import import_module

PACKS: tuple[str, ...] = tuple(sorted(
    m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")))

for _name in PACKS:
    import_module(f"{__name__}.{_name}")
