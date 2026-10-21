"""Built-in report template catalog.

A *template* is a named, ordered list of section entries
(``{id, title?, options?}``, see report_sections.SECTION_REGISTRY) with
catalogue metadata: category, audience, tags, a default period and a
suggested delivery frequency. Built-in templates are defined here, versioned
with the code and read-only; administrators clone one to customise it, which
creates an organisation template in ``custom_reports`` (migrate-156).

Every template is also published in ``report_sections.REPORT_PRESETS`` (title,
description, category, section ids, entries) so the render API, scheduler and
older callers resolve built-in keys the same way. ``validate_catalog`` (run by
the test suite) keeps every entry pointing at a real section with valid
options.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


class _LazySections:
    """report_sections, imported on first use. report_sections imports this
    module (and the packs that call template()) at its end, so a top-level
    import here would make `import report_templates` fail when it runs first."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("app.services.report_sections"), name)


rs = _LazySections()

# ─── Categories ─────────────────────────────────────────────────────────────

CATEGORIES: list[dict[str, str]] = [
    {"id": "executive", "title": "Executive & SLA", "icon": "BarChart3",
     "description": "Leadership summaries: availability against target, incidents, risk and trends."},
    {"id": "availability", "title": "Availability & Uptime", "icon": "Activity",
     "description": "Node and site uptime, outages, downtime and maintenance."},
    {"id": "performance", "title": "Performance", "icon": "Gauge",
     "description": "Latency, packet loss and response times across the network."},
    {"id": "interfaces", "title": "Interfaces & Bandwidth", "icon": "Network",
     "description": "Utilisation, throughput, errors, discards and port state."},
    {"id": "hardware", "title": "Hardware Health", "icon": "Cpu",
     "description": "CPU, memory, temperature, power and load-balancer health."},
    {"id": "capacity", "title": "Capacity & Forecasting", "icon": "TrendingUp",
     "description": "Headroom, saturation risk and growth projections."},
    {"id": "services", "title": "Services & Web Checks", "icon": "Globe",
     "description": "HTTP, TCP, DNS, ICMP and TLS checks: availability, incidents and certificates."},
    {"id": "traffic", "title": "Traffic Analysis", "icon": "Waypoints",
     "description": "NetFlow/sFlow volumes, applications, conversations and QoS."},
    {"id": "paths", "title": "Network Paths", "icon": "Route",
     "description": "Hop-by-hop path probes: latency, loss and route changes."},
    {"id": "alerts", "title": "Alerts & Events", "icon": "Bell",
     "description": "Alert volumes, response times, noisy sources, traps and events."},
    {"id": "configuration", "title": "Configuration & Compliance", "icon": "FileCheck2",
     "description": "Configuration backups, changes, drift and policy compliance."},
    {"id": "device_tracker", "title": "Device Tracker", "icon": "MonitorSmartphone",
     "description": "Endpoints, switch ports, IP and MAC history and user logins."},
    {"id": "inventory", "title": "Inventory & Lifecycle", "icon": "Boxes",
     "description": "Assets by vendor, model, site and software version."},
    {"id": "servers", "title": "Servers & Agents", "icon": "Server",
     "description": "Server load, disks, processes, services, software and agent health."},
    {"id": "applications", "title": "Application Performance", "icon": "AppWindow",
     "description": "Service golden signals, errors, SLOs and synthetic journeys."},
    {"id": "experience", "title": "Digital Experience", "icon": "MousePointerClick",
     "description": "Real-user sessions, Core Web Vitals and usage analytics."},
    {"id": "security", "title": "Security & Audit", "icon": "ShieldCheck",
     "description": "Access, sign-ins, administrative changes and exposure."},
    {"id": "platform", "title": "Platform & Operations", "icon": "Settings2",
     "description": "Monitoring coverage, sensors, data quality and report delivery."},
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]
CATEGORY_TITLES = {c["id"]: c["title"] for c in CATEGORIES}

PERIODS = (
    "last_24h", "last_7d", "last_30d", "last_90d",
    "today", "yesterday", "week_to_date", "previous_week", "previous_work_week",
    "month_to_date", "previous_month", "quarter_to_date", "previous_quarter",
)
FREQUENCIES = ("daily", "weekly", "monthly")
AUDIENCES = ("Executive", "IT manager", "Network engineer", "NOC operator", "Systems engineer",
             "Application owner", "Service owner", "Security & audit", "Capacity planner")


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str
    category: str
    sections: tuple[dict, ...]
    audience: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    default_period: str = "last_7d"
    frequency: str = "weekly"
    icon: str = ""
    featured: bool = False
    # Optional scope filter the viewer offers ('devices', 'applications', 'netflow').
    filterable: tuple[str, ...] = field(default=())

    @property
    def section_ids(self) -> list[str]:
        return [e["id"] for e in self.sections]

    def requires(self) -> set[str]:
        """Union of the module permissions its sections need."""
        out: set[str] = set()
        for sid in self.section_ids:
            out.update((rs.SECTION_REGISTRY.get(sid) or {}).get("requires") or ())
        return out


TEMPLATES: dict[str, Template] = {}


def _entries(items: Iterable) -> tuple[dict, ...]:
    """Same normalisation as report_sections.section_entries (kept local so
    registering a template never needs report_sections to be loaded)."""
    out = []
    for it in items or ():
        if isinstance(it, str):
            out.append({"id": it, "title": None, "options": {}})
        elif isinstance(it, dict) and isinstance(it.get("id"), str):
            title = it.get("title")
            out.append({"id": it["id"],
                        "title": title.strip()[:120] if isinstance(title, str) and title.strip() else None,
                        "options": it.get("options") if isinstance(it.get("options"), dict) else {}})
    return tuple(out)


def template(key: str, title: str, description: str, category: str, sections: Iterable,
             **kw: Any) -> Template:
    """Register a built-in template (used by this module and section packs)."""
    if key in TEMPLATES:
        raise ValueError(f"duplicate report template {key!r}")
    t = Template(key=key, title=title, description=description, category=category,
                 sections=_entries(sections), **kw)
    TEMPLATES[key] = t
    return t


def S(sid: str, title: Optional[str] = None, **options: Any) -> dict:
    """Section entry shorthand: S("device_metric_top", "Top CPU", metric="cpu")."""
    return {"id": sid, "title": title, "options": options}


def available_sections(t: Template, perms: Optional[frozenset[str]]) -> list[str]:
    return [sid for sid in t.section_ids if rs.may_view_section(perms, sid)]


def is_available(t: Template, perms: Optional[frozenset[str]]) -> bool:
    """A template is offered when the viewer can see at least one section."""
    return bool(available_sections(t, perms))


def filterable_for(entries: Iterable[dict]) -> list[str]:
    """Which scope filters a section list honours (for the viewer's pickers)."""
    ids = [e["id"] if isinstance(e, dict) else e for e in entries]
    out: list[str] = []
    if any((rs.SECTION_REGISTRY.get(s) or {}).get("scope") == "device" for s in ids):
        out.append("devices")
    if any(s.startswith("rum_") for s in ids):
        out.append("applications")
    if any((rs.SECTION_REGISTRY.get(s) or {}).get("scope") == "netflow" for s in ids):
        out.append("netflow")
    return out


def _loaded() -> None:
    """Make sure report_sections (and with it every pack's templates) is loaded
    before the catalog is iterated — loading it registers more templates."""
    importlib.import_module("app.services.report_sections")


def validate_catalog() -> list[str]:
    _loaded()
    errors: list[str] = []
    for key, t in list(TEMPLATES.items()):
        if not rs.re_key(key):
            errors.append(f"{key}: template keys are lower_snake_case, at most 50 characters")
        if t.category not in CATEGORY_TITLES:
            errors.append(f"{key}: unknown category {t.category!r}")
        if t.default_period not in PERIODS:
            errors.append(f"{key}: unknown default period {t.default_period!r}")
        if t.frequency not in FREQUENCIES:
            errors.append(f"{key}: unknown frequency {t.frequency!r}")
        if not t.sections:
            errors.append(f"{key}: has no sections")
        for a in t.audience:
            if a not in AUDIENCES:
                errors.append(f"{key}: unknown audience {a!r}")
        for e in t.sections:
            if e["id"] not in rs.SECTION_REGISTRY:
                errors.append(f"{key}: unknown section {e['id']!r}")
                continue
            errors.extend(f"{key}: {msg}" for msg in rs.option_errors(e["id"], e.get("options")))
    return errors


def catalog_entry(t: Template, perms: Optional[frozenset[str]] = None) -> dict[str, Any]:
    secs = []
    for e in t.sections:
        spec = rs.SECTION_REGISTRY.get(e["id"]) or {}
        secs.append({"id": e["id"], "title": e.get("title") or spec.get("title") or e["id"],
                     "options": rs.normalize_options(e["id"], e.get("options")),
                     "available": rs.may_view_section(perms, e["id"])})
    return {
        "key": t.key, "title": t.title, "description": t.description,
        "category": t.category, "category_title": CATEGORY_TITLES.get(t.category, t.category),
        "engine": "sections", "formats": ["pdf", "html", "xlsx", "csv"],
        "sections": t.section_ids, "entries": secs,
        "audience": list(t.audience), "tags": list(t.tags),
        "default_period": t.default_period, "frequency": t.frequency,
        "icon": t.icon, "featured": t.featured,
        "filterable": list(t.filterable) or filterable_for(t.sections),
        "requires": sorted(t.requires()),
        "available": is_available(t, perms),
    }


def publish_presets() -> None:
    """Expose every template through report_sections.REPORT_PRESETS."""
    for key, t in list(TEMPLATES.items()):
        rs.REPORT_PRESETS[key] = {
            "title": t.title,
            "description": t.description,
            "category": CATEGORY_TITLES.get(t.category, t.category),
            "sections": t.section_ids,
            "entries": [dict(e) for e in t.sections],
        }


# ─── The catalog ────────────────────────────────────────────────────────────
# The original section-engine presets keep their keys (schedules reference
# them); the rest of the catalog is defined in report_catalog.py.

template("availability", "Availability & SLA Report",
         "Uptime and SLA attainment for every node, the longest outages and service availability.",
         "availability", ["availability_kpis", "availability_trend", "device_availability",
                          "top_outages", "service_availability"],
         audience=("IT manager", "NOC operator"), tags=("sla", "uptime"), default_period="last_30d",
         frequency="monthly", icon="Gauge", featured=True, filterable=("devices",))
template("performance", "Performance Report",
         "Network latency, interface throughput and server load.",
         "performance", ["network_performance", "interface_utilization", "server_performance"],
         audience=("Network engineer",), tags=("latency", "throughput"), icon="Activity")
template("traffic", "Traffic Analysis Report",
         "NetFlow volumes, protocols, top applications and top talkers.",
         "traffic", ["traffic_kpis", "traffic_trend", "traffic_protocols", "traffic_ports",
                     "traffic_talkers"],
         audience=("Network engineer",), tags=("netflow",), icon="Waypoints")
template("netflow_executive", "NetFlow Executive Summary",
         "Management view of flow traffic: volume against the previous period, top capacity risks, "
         "coverage and data-quality limits, notable findings, top applications and talkers, and "
         "recommended actions.",
         "traffic", ["netflow_executive_summary", "traffic_trend", "traffic_ports", "traffic_talkers"],
         audience=("Executive", "IT manager"), tags=("netflow", "executive"), icon="Waypoints",
         featured=True)
template("alerts", "Alert Insights Report",
         "Alert volumes, the noisiest sources and the most recent alerts.",
         "alerts", ["alert_kpis", "alert_trend", "noisy_sources", "recent_alerts"],
         audience=("NOC operator", "IT manager"), tags=("alerts", "mttr"), icon="Bell")
template("capacity", "Capacity Planning Report",
         "Filesystem and link headroom with server load.",
         "capacity", ["capacity_filesystems", "capacity_interfaces", "server_performance"],
         audience=("Capacity planner",), tags=("capacity",), default_period="last_30d",
         frequency="monthly", icon="HardDrive")
template("apm_performance", "Application Performance Report",
         "Service golden signals, SLOs, errors and synthetic checks.",
         "applications", ["apm_services", "slo_attainment", "apm_errors", "synthetics"],
         audience=("Application owner",), tags=("apm", "slo"), icon="AppWindow", featured=True)
template("usage", "Usage Analytics Report",
         "Application traffic, top pages and the most active users.",
         "experience", ["usage_kpis", "usage_pages", "usage_users"],
         audience=("Application owner",), tags=("usage",), icon="PieChart")
template("rum_usage", "Digital Experience Report",
         "How real people experienced the application in their browser — audience, engagement, "
         "Core Web Vitals and reliability.",
         "experience", ["rum_audience", "rum_experience", "rum_top_pages", "rum_segments",
                        "rum_reliability"],
         audience=("Application owner", "Executive"), tags=("rum", "web vitals"),
         icon="MousePointerClick", filterable=("applications",))
template("ncm_protection", "Configuration Protection Report",
         "NCCM backup coverage, outcomes, devices needing action, compliance and baseline drift.",
         "configuration", ["ncm_protection", "ncm_breakdown", "ncm_outcomes", "ncm_at_risk",
                           "ncm_compliance", "ncm_changes"],
         audience=("Network engineer", "Security & audit"), tags=("ncm", "backup", "compliance"),
         icon="FileCheck2", featured=True, filterable=("devices",))
template("inventory", "Inventory Report",
         "Devices, interfaces, sensors and servers on record.",
         "inventory", ["inventory_summary", "inventory_breakdown", "inventory_servers"],
         audience=("IT manager",), tags=("assets",), default_period="last_30d", frequency="monthly",
         icon="Boxes")

