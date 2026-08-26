from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reconcile-nginx-ingest.py"
SPEC = importlib.util.spec_from_file_location("reconcile_nginx_ingest", SCRIPT)
assert SPEC and SPEC.loader
reconciler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconciler)


def test_reconcile_adds_exact_route_to_each_proxying_server(tmp_path):
    config = tmp_path / "zenplus.conf"
    config.write_text(
        """server {
    listen 80;
    location /api/ { proxy_pass http://127.0.0.1:8000; }
}
server {
    listen 443 ssl;
    location /api/ { proxy_pass http://127.0.0.1:8000; }
}
""",
        encoding="utf-8",
    )

    assert reconciler.reconcile(config) is True
    updated = config.read_text(encoding="utf-8")
    assert updated.count("location = /api/v1/agents/results/host {") == 2
    assert updated.count("proxy_read_timeout 45s;") == 2
    assert updated.count("proxy_send_timeout 45s;") == 2
    assert updated.index("location = /api/v1/agents/results/host {") < updated.index(
        "location /api/ {"
    )


def test_reconcile_is_idempotent_and_refreshes_managed_timeout(tmp_path):
    config = tmp_path / "zenplus.conf"
    config.write_text(
        """server {
    # BEGIN ZenPlus host-results ingest timeout
    location = /api/v1/agents/results/host {
        proxy_read_timeout 99s;
    }
    # END ZenPlus host-results ingest timeout
    location /api/ { proxy_pass http://127.0.0.1:8000; }
}
""",
        encoding="utf-8",
    )

    assert reconciler.reconcile(config) is True
    first = config.read_bytes()
    assert b"99s" not in first
    assert reconciler.reconcile(config) is False
    assert config.read_bytes() == first


def test_reconcile_refuses_unrecognized_site(tmp_path):
    config = tmp_path / "zenplus.conf"
    config.write_text("server { location / { return 200; } }\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"location /api/"):
        reconciler.reconcile(config)
