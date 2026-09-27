from __future__ import annotations

import gzip
import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core import gzip_request
from app.core.gzip_request import GzipRequestMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(GzipRequestMiddleware)

    @app.post("/api/v1/agents/results/host")
    async def results(request: Request):
        body = await request.body()
        return {"json": json.loads(body), "encoding": request.headers.get("content-encoding")}

    @app.post("/api/v1/other")
    async def other(request: Request):
        return {"size": len(await request.body())}

    return TestClient(app)


def test_decompresses_agent_uploads():
    payload = {"batch_id": "b-1", "metrics": [{"kind": "cpu"}] * 200}
    response = _client().post(
        "/api/v1/agents/results/host",
        content=gzip.compress(json.dumps(payload).encode()),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"json": payload, "encoding": None}


def test_plain_uploads_are_unchanged():
    response = _client().post("/api/v1/agents/results/host", json={"batch_id": "b-2"})
    assert response.status_code == 200
    assert response.json()["json"] == {"batch_id": "b-2"}


def test_only_agent_routes_are_decompressed():
    compressed = gzip.compress(b'{"a": 1}')
    response = _client().post(
        "/api/v1/other", content=compressed, headers={"Content-Encoding": "gzip"},
    )
    assert response.json() == {"size": len(compressed)}


def test_rejects_invalid_and_oversized_bodies(monkeypatch):
    client = _client()
    bad = client.post(
        "/api/v1/agents/results/host", content=b"not gzip",
        headers={"Content-Encoding": "gzip"},
    )
    assert bad.status_code == 400

    monkeypatch.setattr(gzip_request, "MAX_DECOMPRESSED_BYTES", 1024)
    bomb = client.post(
        "/api/v1/agents/results/host", content=gzip.compress(b"0" * 100_000),
        headers={"Content-Encoding": "gzip"},
    )
    assert bomb.status_code == 413

    truncated = gzip.compress(b'{"a": 1}')[:-4]
    cut = client.post(
        "/api/v1/agents/results/host", content=truncated,
        headers={"Content-Encoding": "gzip"},
    )
    assert cut.status_code == 400
