# Alerts Feature Plan

## Competitive Research Notes

- SolarWinds emphasizes active-alert lists, severity/source sorting, filters, troubleshooting entry points, and acknowledgement from either the list or alert detail view.
- Dynatrace treats incidents as grouped problems with root-cause and impact context; the key UX lesson is that detail pages should guide triage, not just repeat alert text.
- ManageEngine OpManager separates active/all/event-type alarm views, exposes alarm snapshot pages, and supports acknowledge/clear/history workflows.
- Uptime.com highlights ongoing/resolved/ignored status cards, search/filter/export, and detailed alert screens with technical context and alert history.

Reference pages:

- SolarWinds Observability: Add or remove an alert acknowledgment - https://documentation.solarwinds.com/en/success_center/observability/content/alerts/alerts-acknowledge.htm
- Dynatrace Docs: Problems app and root-cause/impact context - https://docs.dynatrace.com/docs/dynatrace-intelligence/problems-app
- ManageEngine OpManager Help: Alert actions and alarm history - https://www.manageengine.com/network-monitoring/help/configuring-alert-actions.html
- Uptime.com Help: Alerts overview and alert details - https://support.uptime.com/hc/en-us/articles/360012996939-Overview-of-Alerts

## Product Direction

Build ZenPlus Alerts as an operations console:

- Header alert center for immediate critical awareness.
- Alerts page for triage, filtering, ownership, and bulk scanning.
- Alert detail page for one-alert investigation with device/service context, lifecycle timeline, and future notification-channel hooks.
- Device inventory alert badges that respect the selected device-page time window.

## Implementation Tasks

- [x] Research competitor alert UX and map patterns to ZenPlus.
- [x] Extend alert backend APIs for list filtering, detail retrieval, stats, and per-device counts.
- [x] Build professional alerts page and alert detail route.
- [x] Add header alert center with severity counts and direct drilldown.
- [x] Add device-row alert badges on `/devices` using the selected time filter.
- [x] Verify backend compile, frontend build, route smoke, and live API responses.

## Verification Checklist

- [x] Backend Python compile passes.
- [x] Frontend production build passes.
- [x] Route smoke test passes.
- [x] `/api/v1/alerts/stats` returns status/severity counters.
- [x] `/api/v1/alerts/{id}` returns detail context for drilldown.
- [x] `/api/v1/alerts/device-counts?hours=...` returns per-device alert counts.
- [x] `/alerts`, `/alerts/:id`, header alert center, and `/devices` badges render without route regressions.
