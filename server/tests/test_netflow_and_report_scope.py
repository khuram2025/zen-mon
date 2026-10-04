from __future__ import annotations

import pytest

from app.api.v1.netflow.access import FlowVisibility
from app.api.v1.netflow.filters import FlowFilter, compile_filter
from app.core import scoping
from app.services import report_sections


def _vis(exporters):
    return FlowVisibility(restricted=exporters is not None,
                          exporters=None if exporters is None else frozenset(exporters),
                          user_id=None, permissions=frozenset({"netflow.view"}))


def test_netflow_predicate_is_a_noop_for_unrestricted_users():
    sql, params = compile_filter(FlowFilter(), "flow_records", _vis(None))
    assert "exporter_ip IN" not in sql
    assert not any(k.startswith("exporters") for k in params)


def test_netflow_scope_pins_flows_to_visible_exporters_and_cannot_be_widened():
    # A client filter narrows within the visible set, never widens it.
    sql, params = compile_filter(FlowFilter(exporter="10.9.9.9"), "flow_records",
                                 _vis(["10.0.0.1", "10.0.0.2"]))
    assert "exporter_ip IN %(nf_vis_v4)s" in sql and "exporter_ip = toIPv4(%(nf_exporter)s)" in sql
    assert params["nf_vis_v4"] == ["10.0.0.1", "10.0.0.2"] and params["nf_exporter"] == "10.9.9.9"
    # An empty scope matches nothing instead of falling back to every exporter.
    sql, _ = compile_filter(FlowFilter(), "flow_records", _vis([]))
    assert sql.startswith("0 AND ")


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
    assert report_sections.NETFLOW_SCOPED_SECTIONS <= set(report_sections.SECTION_REGISTRY)


def test_every_netflow_section_is_scope_safe_for_tag_scoped_accounts():
    """The released 1.23.24 render path fails closed for tag-scoped accounts (only sections in
    SCOPE_SAFE_SECTIONS run). The NetFlow sections are confined by the requester's flow visibility,
    so a scoped account with netflow.view must still get them -- and nothing fleet-wide may ride along."""
    netflow_ids = {sid for sid, info in report_sections.SECTION_REGISTRY.items() if info["category"] == "Traffic"}
    assert netflow_ids and netflow_ids == set(report_sections.NETFLOW_SCOPED_SECTIONS)
    assert netflow_ids <= report_sections.SCOPE_SAFE_SECTIONS
    assert report_sections.SCOPE_SAFE_SECTIONS == (report_sections.DEVICE_SCOPABLE_SECTIONS
                                                   | report_sections.NETFLOW_SCOPED_SECTIONS)
    assert not report_sections.NETFLOW_SCOPED_SECTIONS & {"device_uptime", "top_outages", "recent_alerts"}
