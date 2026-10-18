"""GET /devices/{id}/interfaces/error-totals: errors and discards per interface
over the selected window, as positive counter deltas.

ClickHouse is replaced by a fake client; nothing here touches an appliance
database.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import devices as devices_api


def _fake_clickhouse(monkeypatch, rows=None, error=None):
    calls = []

    def query(sql, parameters=None):
        calls.append((sql, parameters))
        if error:
            raise error
        return NS(result_rows=rows or [])

    from app.core import database
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: NS(query=query))
    return calls


def _call(hours=24):
    return asyncio.run(devices_api.get_device_interface_error_totals(uuid4(), hours=hours, user=NS()))


def test_totals_are_summed_per_interface(monkeypatch):
    # (if_index, in_errors, out_errors, in_discards, out_discards, samples)
    calls = _fake_clickhouse(monkeypatch, rows=[
        (1, 5, 2, 0, 7, 60),
        (2, 0, 0, 0, 0, 60),
        (1, 3, 0, 1, 0, 12),   # a second partial row for the same port is added, not replacing
    ])
    out = _call(hours=6)
    assert out == {
        1: {"in_errors": 8, "out_errors": 2, "in_discards": 1, "out_discards": 7},
        2: {"in_errors": 0, "out_errors": 0, "in_discards": 0, "out_discards": 0},
    }
    sql, params = calls[0]
    assert params["hours"] == 6
    assert "zenplus.snmp_if_metrics" in sql
    # Consecutive samples of one interface are differenced, never the raw counter.
    assert "PARTITION BY if_index ORDER BY timestamp" in sql
    assert "lagInFrame(in_errors)" in sql and "lagInFrame(out_discards)" in sql
    assert "HAVING samples > 1" in sql


def test_backwards_steps_never_count(monkeypatch):
    """A wrap or an agent restart is a backwards step: the SQL keeps only the
    positive part of each step, and a negative aggregate never leaks out."""
    calls = _fake_clickhouse(monkeypatch, rows=[(3, -4_000_000_000, -1, None, 5, 3)])
    out = _call()
    assert out == {3: {"in_errors": 0, "out_errors": 0, "in_discards": 0, "out_discards": 5}}
    sql = calls[0][0]
    for col, prev in (("in_errors", "p_ie"), ("out_errors", "p_oe"),
                      ("in_discards", "p_id"), ("out_discards", "p_od")):
        assert f"greatest(toInt64({col}) - toInt64({prev}), 0)" in sql
    assert "if(rn > 1," in sql  # the first sample of a window has no step


def test_no_samples_is_an_empty_map_not_zeros(monkeypatch):
    _fake_clickhouse(monkeypatch, rows=[])
    assert _call() == {}


def test_clickhouse_failure_is_503_with_a_plain_message(monkeypatch):
    _fake_clickhouse(monkeypatch, error=RuntimeError("Code: 241. DB::Exception: secret internals"))
    with pytest.raises(HTTPException) as exc:
        _call()
    assert exc.value.status_code == 503
    assert "DB::Exception" not in exc.value.detail
    assert "unavailable" in exc.value.detail


def test_client_construction_failure_is_also_503(monkeypatch):
    from app.core import database

    def boom():
        raise ConnectionError("clickhouse down")
    monkeypatch.setattr(database, "get_clickhouse_client", boom)
    with pytest.raises(HTTPException) as exc:
        _call()
    assert exc.value.status_code == 503


def test_route_is_registered_before_the_if_index_routes():
    prefix = devices_api.router.prefix
    paths = [getattr(r, "path", "") for r in devices_api.router.routes]
    totals = paths.index(f"{prefix}/{{device_id}}/interfaces/error-totals")
    if_index_routes = [i for i, p in enumerate(paths) if p.startswith(f"{prefix}/{{device_id}}/interfaces/{{if_index}}")]
    assert if_index_routes, "expected the per-interface routes to exist"
    assert totals < min(if_index_routes)
    route = devices_api.router.routes[totals]
    assert route.methods == {"GET"}
    assert any(d.dependency is devices_api._device_in_scope for d in route.dependencies)
