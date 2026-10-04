"""route_template() must recover the full templated path (prefix included) at
request time: scope['route'] only carries the router-relative template."""
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.core.route_policy import route_template

seen: list[str] = []


async def capture(request: Request):
    seen.append(route_template(request))


def _app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/reports")

    @router.get("/shared/{token}")
    async def shared(token: str):
        return {"t": token}

    @router.get("/files/{path:path}")
    async def files(path: str):
        return {"p": path}

    app.include_router(router, prefix="/api/v1", dependencies=[Depends(capture)])
    return app


def test_prefix_is_recovered_for_parameterised_routes():
    client = TestClient(_app())
    seen.clear()
    assert client.get("/api/v1/reports/shared/abc123").status_code == 200
    assert client.get("/api/v1/reports/files/a/b/c.txt").status_code == 200
    assert seen == ["/api/v1/reports/shared/{token}", "/api/v1/reports/files/{path}"]
