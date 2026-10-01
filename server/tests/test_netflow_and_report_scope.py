from __future__ import annotations

import pytest

from app.api.v1 import netflow
from app.core import scoping
from app.services import report_sections


def test_netflow_predicate_is_a_noop_for_unrestricted_users():
    netflow._VISIBLE_EXPORTERS.set(None)
    assert netflow._visibility_predicate() == ("", {})
    sql, params = netflow._scope()
    assert "exporter_ip IN" not in sql


def test_netflow_scope_pins_flows_to_visible_exporters_and_cannot_be_widened():
    netflow._VISIBLE_EXPORTERS.set(["10.0.0.1", "10.0.0.2"])
    try:
        sql, params = netflow._scope(exporter="10.9.9.9")       # client filter narrows, never widens
        assert "exporter_ip IN %(zp_visible_exporters)s" in sql and "exporter_ip = %(exporter)s" in sql
        assert params["zp_visible_exporters"] == ["10.0.0.1", "10.0.0.2"]
        assert netflow._exporter_filter(None)[0].strip().startswith("AND exporter_ip IN")
        netflow._VISIBLE_EXPORTERS.set([])
        assert "AND 0" in netflow._exporter_filter(None)[0]
        assert "0" in netflow._scope()[0]
    finally:
        netflow._VISIBLE_EXPORTERS.set(None)


@pytest.mark.asyncio
async def test_scoped_device_filter_never_widens_and_empty_matches_nothing(monkeypatch):
    async def scope(db, user): return ["web"]
    async def visible(db, s): return ["a", "b"]
    monkeypatch.setattr(scoping, "visible_tags", scope)
    monkeypatch.setattr(scoping, "visible_device_ids", visible)
    assert await scoping.scoped_device_filter(None, object(), None) == (True, ["a", "b"])
    assert await scoping.scoped_device_filter(None, object(), ["a", "zzz"]) == (True, ["a"])
    assert await scoping.scoped_device_filter(None, object(), ["zzz"]) == (True, [scoping.NO_MATCH_ID])

    async def unrestricted(db, user): return None
    monkeypatch.setattr(scoping, "visible_tags", unrestricted)
    assert await scoping.scoped_device_filter(None, object(), ["x"]) == (False, ["x"])


def test_scopable_report_sections_exist():
    assert report_sections.DEVICE_SCOPABLE_SECTIONS <= set(report_sections.SECTION_REGISTRY)
