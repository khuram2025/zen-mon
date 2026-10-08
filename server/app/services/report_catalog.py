"""Cross-domain built-in templates (executive, persona and review reports).

Domain templates live next to their sections in report_packs/*; this module
holds the reports that combine several areas. Imported after the packs, so
any registered section may be used. Entries use S(section_id, title, **options).

The persona pages that used to be separate screens (/reports/executive,
/technical, /business) open executive_overview, network_operations and
service_owner_sla.
"""

from __future__ import annotations

from app.services.report_templates import S, template

# ─── Executive & SLA ────────────────────────────────────────────────────────

template(
    "executive_overview", "Executive Overview",
    "Leadership summary of the reporting period: availability against target, service SLA, "
    "outages, how fast alerts were handled, certificates at risk and the current state of the estate.",
    "executive",
    [S("availability_kpis"), S("availability_trend"),
     S("service_sla_compliance", "Business Service SLA", top_n=15),
     S("top_outages", "Longest Outages", top_n=10),
     S("alert_response_metrics", "Incident Response"),
     S("tls_certificate_expiry", "Certificates Needing Renewal", top_n=10),
     S("device_status_now", "Estate Status Now", top_n=10)],
    audience=("Executive", "IT manager"), tags=("executive", "sla", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="BarChart3", featured=True,
    filterable=("devices",),
)

template(
    "monthly_it_operations_review", "Monthly IT Operations Review",
    "One document for the monthly operations meeting: network and service availability, "
    "incidents and response times, server health, capacity running out, configuration "
    "protection and licence headroom.",
    "executive",
    [S("availability_kpis"), S("availability_by_group", "Availability by Site and Group"),
     S("service_sla_compliance", "Service SLA", top_n=15),
     S("service_outages", "Service Incidents", top_n=10),
     S("alert_kpis"), S("alert_response_metrics", "Response Performance"),
     S("server_fleet_status", "Server Fleet", top_n=10),
     S("capacity_filesystem_forecast", "Disks Filling Up", top_n=10),
     S("ncm_protection", "Configuration Protection"),
     S("license_usage", "Licence Headroom")],
    audience=("Executive", "IT manager"), tags=("monthly review", "operations", "board pack"),
    default_period="previous_month", frequency="monthly", icon="Briefcase", featured=True,
)

template(
    "quarterly_service_review", "Quarterly Service Review",
    "Quarter-on-quarter view of the business services: SLA attainment per service, group and "
    "owner, the incidents behind any misses, response times and certificate risk.",
    "executive",
    [S("service_sla_compliance", "SLA Attainment", top_n=50),
     S("service_by_group", "Availability by Group and Owner"),
     S("service_outages", "Incidents in the Quarter", top_n=25),
     S("service_response_times", "Response Times", top_n=15),
     S("tls_certificate_expiry", "Certificate Risk", top_n=15)],
    audience=("Executive", "Service owner"), tags=("quarterly review", "sla", "services"),
    default_period="previous_quarter", frequency="monthly", icon="Gauge",
)

template(
    "infrastructure_health_scorecard", "Infrastructure Health Scorecard",
    "Weekly scorecard of the estate: what is down, availability, the busiest devices and links, "
    "hardware warnings, server health and disks close to full.",
    "executive",
    [S("device_status_now", "Status Now", top_n=10), S("availability_kpis"),
     S("device_resource_utilization", "Busiest Devices (CPU)", metric="cpu", top_n=10),
     S("interface_utilization_p95", "Busiest Links", top_n=10),
     S("hardware_environment", "Hardware Warnings", top_n=10),
     S("server_fleet_status", "Servers", top_n=10),
     S("capacity_filesystem_forecast", "Disks Close to Full", top_n=10)],
    audience=("IT manager", "Executive"), tags=("scorecard", "weekly review"),
    default_period="last_7d", frequency="weekly", icon="Activity", featured=True,
    filterable=("devices",),
)

# ─── Operations personas ────────────────────────────────────────────────────

template(
    "network_operations", "Network Operations Review",
    "Engineering view of the network: what is down, latency and loss, the busiest and "
    "unhealthiest interfaces, device load, alert volume, the noisiest sources and outages.",
    "performance",
    [S("device_status_now", top_n=25),
     S("device_latency_loss", "Highest Latency", top_n=10),
     S("interface_utilization_p95", "Busiest Interfaces (95th Percentile)", top_n=15),
     S("interface_health", "Interface Errors and Discards", top_n=10),
     S("device_resource_utilization", "Device CPU", metric="cpu", top_n=10),
     S("alert_trend"), S("noisy_sources", top_n=10), S("top_outages", top_n=15)],
    audience=("Network engineer", "NOC operator"), tags=("operations", "weekly review"),
    default_period="last_7d", frequency="weekly", icon="Wrench", featured=True,
    filterable=("devices",),
)

template(
    "noc_daily_briefing", "NOC Daily Briefing",
    "Start-of-shift briefing: devices down now, the open alert backlog, yesterday's alerts, "
    "outages and service incidents, and certificates about to expire.",
    "alerts",
    [S("device_status_now", "Down and Degraded Now", top_n=25),
     S("alert_backlog_aging", "Open Alerts by Age", top_n=15),
     S("recent_alerts", "Alerts in the Period", top_n=25),
     S("top_outages", "Outages", top_n=10),
     S("service_outages", "Service Incidents", top_n=10),
     S("tls_certificate_expiry", "Certificates Expiring Within 14 Days", days=14, top_n=10)],
    audience=("NOC operator",), tags=("daily", "shift handover"),
    default_period="yesterday", frequency="daily", icon="Bell", featured=True,
)

template(
    "service_owner_sla", "Service Owner SLA Review",
    "Confirmed availability of business services against their targets, by group and owner, "
    "with the incidents behind any misses, response times and certificates that need attention.",
    "services",
    [S("service_sla_compliance", top_n=50), S("service_by_group"),
     S("service_outages", top_n=25), S("service_response_times", top_n=15),
     S("tls_certificate_expiry", top_n=15)],
    audience=("Service owner", "IT manager"), tags=("sla", "services"),
    default_period="previous_month", frequency="monthly", icon="Briefcase", featured=True,
)

# ─── Security, capacity and digital experience summaries ───────────────────

template(
    "security_compliance_summary", "Security and Compliance Summary",
    "Monthly security posture: configuration compliance and its trend, certificates, user "
    "access and sign-ins, rogue endpoints, NetFlow security findings and server software "
    "baselines.",
    "security",
    [S("ncm_compliance", "Configuration Compliance"),
     S("ncm_compliance_trend", "Compliance Trend"),
     S("tls_certificate_expiry", "Certificates", top_n=15),
     S("access_review", "User Access"),
     S("login_activity", "Sign-in Activity"),
     S("udt_rogue_watch", "Rogue and Watched Endpoints", top_n=15),
     S("netflow_security_findings", "Traffic Security Findings", top_n=10),
     S("server_software_compliance", "Server Software Baselines", top_n=15)],
    audience=("Security & audit", "Executive"), tags=("security", "compliance", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="ShieldCheck", featured=True,
)

template(
    "capacity_risk_summary", "Capacity Risk Summary",
    "Where capacity runs out first: saturated links and their forecast, disks filling up, "
    "switch ports, firewall sessions and licence headroom.",
    "capacity",
    [S("interface_utilization_p95", "Links at or Above 70% (95th Percentile)", top_n=15,
       threshold=70, only_above=True),
     S("netflow_capacity", "Link Capacity Forecast (NetFlow)", top_n=10),
     S("capacity_filesystem_forecast", "Disks Filling Up", top_n=15),
     S("udt_port_capacity", "Switch Port Capacity", top_n=15),
     S("firewall_sessions", "Firewall Sessions", top_n=10),
     S("license_usage", "Licence Headroom")],
    audience=("Capacity planner", "IT manager"), tags=("capacity", "forecast", "monthly review"),
    default_period="last_30d", frequency="monthly", icon="TrendingUp", featured=True,
)

template(
    "digital_experience_executive", "Digital Experience Executive Summary",
    "How customers experienced the applications: audience, Core Web Vitals and their trend, "
    "SLO error budgets, service health and synthetic journeys.",
    "experience",
    [S("rum_audience"), S("rum_experience"), S("rum_vitals_trend", "Core Web Vitals Trend"),
     S("apm_slo_budget_trend", "SLO Error Budgets"), S("apm_services", "Service Health"),
     S("synthetics", "Synthetic Journeys")],
    audience=("Executive", "Application owner"), tags=("digital experience", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="MousePointerClick",
    filterable=("applications",),
)
