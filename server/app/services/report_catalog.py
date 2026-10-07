"""Cross-domain built-in templates (executive and persona reports).

Domain templates live next to their sections in report_packs/*; this module
holds the reports that combine several areas. Imported after the packs, so
any registered section may be used. Entries use S(section_id, title, **options).

The persona pages that used to be separate screens (/reports/executive,
/technical, /business) open these templates.
"""

from __future__ import annotations

from app.services.report_templates import S, template  # noqa: F401

template(
    "executive_overview", "Executive Overview",
    "Leadership summary of the reporting period: availability against target, service SLA, "
    "incidents and outages, alert pressure and the current state of the estate.",
    "executive",
    ["availability_kpis", "availability_trend", "service_availability", "top_outages",
     "alert_kpis", "device_status_now"],
    audience=("Executive", "IT manager"), tags=("executive", "sla", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="BarChart3", featured=True,
    filterable=("devices",),
)

template(
    "network_operations", "Network Operations Review",
    "Engineering view of the network: lowest-availability devices, latency, the busiest "
    "interfaces, alert volume and the noisiest sources.",
    "performance",
    ["device_status_now", "device_uptime", "network_performance", "interface_utilization",
     "alert_trend", "noisy_sources", "top_outages"],
    audience=("Network engineer", "NOC operator"), tags=("operations", "weekly review"),
    default_period="last_7d", frequency="weekly", icon="Wrench", featured=True,
)

template(
    "service_owner_sla", "Service Owner SLA Review",
    "Confirmed availability of business services against their targets, with outages and "
    "certificates that need attention.",
    "services",
    ["service_availability", "availability_kpis", "top_outages"],
    audience=("Service owner", "IT manager"), tags=("sla", "services"),
    default_period="previous_month", frequency="monthly", icon="Briefcase", featured=True,
)
