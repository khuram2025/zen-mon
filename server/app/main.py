import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.api.v1 import auth, devices, alerts, alert_rules, alert_engine, service_checks, reports, report_schedules, discovery, discovery_v2, users, subscription, system_updates, snmp, snmp_credentials, windows_credentials, audit_logs, netflow, manual_maps, support, traps, ncm, host_alert_rules
from app.api.v1 import settings as settings_api
from app.api.v1 import sensors as sensors_admin_api
from app.api.v1 import sensor_api
from app.api.v1 import agents as agents_runtime_api
from app.api.v1 import servers as servers_admin_api
from app.api.v1 import apm as apm_control_api
from app.api.v1 import apm_ingest as apm_ingest_api
from app.api.v1 import apm_traces as apm_traces_api
from app.api.v1 import apm_services as apm_services_api
from app.api.websocket import realtime

settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(devices.router, prefix="/api/v1")
    app.include_router(alerts.router, prefix="/api/v1")
    app.include_router(settings_api.router, prefix="/api/v1")
    app.include_router(alert_rules.router, prefix="/api/v1")
    app.include_router(host_alert_rules.router, prefix="/api/v1")
    app.include_router(alert_engine.router, prefix="/api/v1")
    app.include_router(traps.router, prefix="/api/v1")
    app.include_router(ncm.router, prefix="/api/v1")
    app.include_router(ncm.device_router, prefix="/api/v1")
    app.include_router(service_checks.router, prefix="/api/v1")
    app.include_router(service_checks.groups_router, prefix="/api/v1")
    app.include_router(service_checks.maintenance_router, prefix="/api/v1")
    app.include_router(service_checks.templates_router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(report_schedules.router, prefix="/api/v1")
    app.include_router(realtime.router, prefix="/api/v1")
    app.include_router(discovery.router, prefix="/api/v1")
    app.include_router(discovery_v2.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(subscription.router, prefix="/api/v1")
    app.include_router(system_updates.router, prefix="/api/v1")
    app.include_router(support.router, prefix="/api/v1")
    app.include_router(snmp.router, prefix="/api/v1")
    app.include_router(snmp_credentials.router, prefix="/api/v1")
    app.include_router(windows_credentials.router, prefix="/api/v1")
    app.include_router(audit_logs.router, prefix="/api/v1")
    app.include_router(netflow.router, prefix="/api/v1")
    app.include_router(manual_maps.router, prefix="/api/v1")
    app.include_router(sensors_admin_api.router, prefix="/api/v1")
    app.include_router(sensors_admin_api.sites_router, prefix="/api/v1")
    app.include_router(sensor_api.router, prefix="/api/v1")
    app.include_router(agents_runtime_api.router, prefix="/api/v1")
    app.include_router(servers_admin_api.router, prefix="/api/v1")
    app.include_router(servers_admin_api.policies_router, prefix="/api/v1")
    app.include_router(servers_admin_api.fleet_router, prefix="/api/v1")
    app.include_router(servers_admin_api.overview_router, prefix="/api/v1")
    app.include_router(servers_admin_api.baselines_router, prefix="/api/v1")
    # APM control plane (ingest keys, enrollment tokens) under /api/v1/apm/*
    app.include_router(apm_control_api.router, prefix="/api/v1")
    # APM trace explorer + waterfall under /api/v1/apm/traces
    app.include_router(apm_traces_api.router, prefix="/api/v1")
    # APM services RED + service map under /api/v1/apm/services|service-map
    app.include_router(apm_services_api.router, prefix="/api/v1")
    # APM OTLP receiver mounted at ROOT so the path is exactly /v1/traces.
    app.include_router(apm_ingest_api.router)

    @app.get("/api/v1/system/health")
    async def health_check():
        return {"status": "ok", "service": "zenplus-api"}

    @app.on_event("startup")
    async def _start_background_tasks():
        # Agent/server staleness sweep (online → stale → offline + alerts).
        from app.services.server_health_service import health_sweeper_loop
        app.state.health_sweeper = asyncio.create_task(health_sweeper_loop())
        # Discovery: recover restart-stranded runs, then fire due schedules.
        from app.services.discovery_scheduler import discovery_scheduler_loop
        app.state.discovery_scheduler = asyncio.create_task(discovery_scheduler_loop())
        # Host-metric alert rules: periodic threshold evaluation against ClickHouse.
        from app.services.host_alert_service import host_alert_evaluator_loop
        app.state.host_alert_evaluator = asyncio.create_task(host_alert_evaluator_loop())
        # Network-device (SNMP) alert rules: periodic threshold evaluation against ClickHouse.
        from app.services.network_alert_service import network_alert_evaluator_loop
        app.state.network_alert_evaluator = asyncio.create_task(network_alert_evaluator_loop())
        # Scheduled reports: fire due report schedules (render + email delivery).
        from app.services.report_scheduler import report_scheduler_loop
        app.state.report_scheduler = asyncio.create_task(report_scheduler_loop())
        # APM OTLP fallback receiver: buffered batch writer to ClickHouse apm_spans.
        await apm_ingest_api.start_batch_writer()
        # APM service registry: upsert apm_services + denormalise health from RED.
        app.state.apm_service_registry = asyncio.create_task(apm_services_api.apm_service_registry_loop())

    @app.on_event("shutdown")
    async def _stop_background_tasks():
        for attr in ("health_sweeper", "discovery_scheduler", "host_alert_evaluator", "network_alert_evaluator", "report_scheduler", "apm_service_registry"):
            task = getattr(app.state, attr, None)
            if task:
                task.cancel()
        await apm_ingest_api.stop_batch_writer()

    return app


app = create_app()
