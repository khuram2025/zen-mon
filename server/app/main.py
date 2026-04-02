from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.api.v1 import auth, devices, alerts, alert_rules, alert_engine
from app.api.v1 import settings as settings_api
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
    app.include_router(alert_engine.router, prefix="/api/v1")
    app.include_router(realtime.router, prefix="/api/v1")

    @app.get("/api/v1/system/health")
    async def health_check():
        return {"status": "ok", "service": "zenplus-api"}

    return app


app = create_app()
