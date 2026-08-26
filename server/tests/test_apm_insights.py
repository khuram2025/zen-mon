"""Unit tests for APM deep-dive helpers (heatmap buckets, SLO daily series)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.apm_insights import (
    LATENCY_EDGES_MS,
    latency_bucket_expr,
    latency_bucket_labels,
    latency_bucket_mid_ms,
)
from app.services import apm_slo_service


def test_latency_bucket_labels_cover_every_edge_plus_overflow():
    labels = latency_bucket_labels()
    assert len(labels) == len(LATENCY_EDGES_MS) + 1
    assert labels[0].startswith("<")
    assert labels[-1].startswith(">")
    assert "1s" in "".join(labels)


def test_latency_bucket_expr_is_valid_clickhouse_multiif():
    expr = latency_bucket_expr()
    assert expr.startswith("multiIf(")
    assert expr.endswith(f", {len(LATENCY_EDGES_MS)})")
    for edge in LATENCY_EDGES_MS:
        assert str(edge) in expr


def test_latency_bucket_mid_ms_sits_inside_the_bucket():
    assert latency_bucket_mid_ms(0) == LATENCY_EDGES_MS[0] / 2
    mid = latency_bucket_mid_ms(3)
    assert LATENCY_EDGES_MS[2] < mid < LATENCY_EDGES_MS[3]
    assert latency_bucket_mid_ms(99) > LATENCY_EDGES_MS[-1]


def test_slo_series_availability_accumulates_budget(monkeypatch):
    days = [
        (datetime(2026, 8, 20, tzinfo=timezone.utc), 1000, 0),
        (datetime(2026, 8, 21, tzinfo=timezone.utc), 1000, 10),
    ]

    class _Client:
        def query(self, sql, parameters=None):
            assert "apm_span_metrics_1h" in sql
            return SimpleNamespace(result_rows=days)

    monkeypatch.setattr("app.core.database.get_clickhouse_client", lambda: _Client())
    slo = {
        "id": str(uuid4()), "name": "checkout", "service_name": "checkout",
        "env": "prod", "operation": None, "sli_type": "availability",
        "latency_threshold_ms": None, "target": 99.0, "window_days": 7,
    }
    points = apm_slo_service.compute_slo_series(slo)
    assert len(points) == 2
    assert points[0]["sli"] == pytest.approx(100.0)
    assert points[0]["budget_remaining"] == pytest.approx(1.0)
    # Day 2: 10/1000 errors = 1% vs 1% budget → remaining 0 after two days mixed.
    assert points[1]["bad"] == 10
    assert points[1]["sli"] == pytest.approx(99.0)
    assert 0.0 <= points[1]["budget_remaining"] <= 1.0


def test_slo_series_returns_empty_on_clickhouse_error(monkeypatch):
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("ch down")

    monkeypatch.setattr("app.core.database.get_clickhouse_client", lambda: _Boom())
    points = apm_slo_service.compute_slo_series({
        "service_name": "x", "target": 99.9, "window_days": 30,
        "sli_type": "availability", "env": None, "operation": None,
    })
    assert points == []
