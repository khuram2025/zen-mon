"""Template report scope, labels, file names and CSV (assessment F7, F8, L3,
L5, L6, catalog deprecation).

Runs on fakes only: a Postgres stand-in answering by SQL substring, and the
FastAPI app through TestClient without its startup hooks."""
from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import reports as reports_api
from app.core import scoping, security
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import report_render as rr
from app.services import report_sections as rs
from app.services.branding import Branding
from app.services.report_csv import render_csv

D1, D2, D3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
G1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class Res:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class DB:
    def __init__(self, *handlers):
        self.handlers = list(handlers)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql, params = " ".join(str(stmt).split()), dict(params or {})
        self.calls.append((sql, params))
        for key, rows in self.handlers:
            if key in sql:
                return Res(rows(sql, params) if callable(rows) else rows)
        return Res()


def run(coro):
    return asyncio.run(coro)


# ── resolve_device_scope ────────────────────────────────────────────────────

def test_resolver_without_a_scope_is_none_or_the_device_ids():
    db = DB()
    assert run(rr.resolve_device_scope(db)) is None
    assert run(rr.resolve_device_scope(db, device_ids=[D1])) == [D1]
    assert db.calls == []


def test_resolver_ands_every_filter_like_the_legacy_reports():
    db = DB(("FROM devices d WHERE", [(D1,), (D2,)]))
    ids = run(rr.resolve_device_scope(db, device_ids=[D1, D2, D3], group_ids=[G1],
                                      locations=["Jeddah"], device_types=["router", "switch"]))
    assert ids == [D1, D2]
    sql, params = db.calls[0]
    assert ("d.id::text = ANY(:device_ids) AND d.group_id::text = ANY(:group_ids) AND "
            "d.location = ANY(:locations) AND d.device_type = ANY(:device_types)") in sql
    assert params == {"device_ids": [D1, D2, D3], "group_ids": [G1], "locations": ["Jeddah"],
                      "device_types": ["router", "switch"]}


def test_resolver_returns_an_empty_list_when_nothing_matches():
    assert run(rr.resolve_device_scope(DB(), locations=["Nowhere"])) == []


def test_apply_scope_fails_closed_and_keeps_the_scope_for_the_label():
    out = run(rr.apply_device_scope(DB(), {"application_id": "shop"}, locations=["Nowhere"]))
    assert out == {"application_id": "shop", "locations": ["Nowhere"], "device_ids": [scoping.NO_MATCH_ID]}
    assert run(rr.apply_device_scope(DB(), {"netflow": "x"})) == {"netflow": "x"}


def test_parse_scope_values():
    assert rr.parse_scope_values(" Jeddah, Riyadh ,,Jeddah", "locations") == ["Jeddah", "Riyadh"]
    assert rr.parse_scope_values("", "locations") is None
    assert rr.parse_scope_values([G1], "group_ids", uuids=True) == [G1]
    with pytest.raises(HTTPException):
        rr.parse_scope_values("core", "group_ids", uuids=True)
    with pytest.raises(HTTPException):
        rr.parse_scope_values(",".join(str(i) for i in range(rr.MAX_SCOPE_VALUES + 1)), "locations")


# ── scope labels (L5) ───────────────────────────────────────────────────────

def test_scope_label_names_a_few_devices_and_previews_many():
    names = ["Amman_New100F", "Dammam", "FortiGate-101F"]
    assert rr.scope_label_for({"device_ids": [D1, D2, D3]}, {"devices": names}) == \
        "Amman_New100F, Dammam, FortiGate-101F"
    many = [f"node-{i}" for i in range(12)]
    assert rr.scope_label_for({"device_ids": [str(uuid.uuid4()) for _ in many]}, {"devices": many}) == \
        "12 selected devices (node-0, node-1, node-2 …)"
    # Without names (the sync helper on its own): counted.
    assert rr.scope_label_for({"device_ids": [D1]}) == "1 selected device"
    assert rr.scope_label_for({"device_ids": [D1, D2]}) == "2 selected devices"
    assert rr.scope_label_for(None) == "All monitored infrastructure"


def test_scope_label_describes_groups_locations_and_types_not_resolved_devices():
    f = {"device_ids": [D1, D2], "group_ids": [G1], "locations": ["Jeddah"], "device_types": ["router"]}
    assert rr.scope_label_for(f, {"groups": ["Core Network"]}) == \
        "Group: Core Network · Location: Jeddah · Device type: router"
    assert rr.scope_label_for({"locations": ["Jeddah", "Riyadh"]}) == "Locations: Jeddah, Riyadh"
    assert rr.scope_label_for({"group_ids": [G1, D1]}) == "2 device group(s)"


def test_async_scope_label_looks_up_names():
    db = DB(("FROM devices WHERE id::text = ANY", [(D2, "Dammam", "10.0.0.2/32"), (D1, None, "10.0.0.1/32")]),
            ("FROM device_groups", [(G1, "Core Network")]))
    assert run(rr.scope_label(db, {"device_ids": [D1, D2]})) == "Dammam, 10.0.0.1"
    assert run(rr.scope_label(db, {"device_ids": [D1], "group_ids": [G1]})) == "Group: Core Network"


def test_async_scope_label_falls_back_to_counts_on_errors():
    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert run(rr.scope_label(Broken(), {"device_ids": [D1, D2]})) == "2 selected devices"


# ── file names (L3) ─────────────────────────────────────────────────────────

def test_filename_cuts_the_title_at_a_word_boundary():
    at = datetime(2026, 10, 21, 2, 25, tzinfo=timezone.utc)
    name = rr.report_filename("[Assessment] Monthly IT Operations Review - 1st 08:00", "pdf",
                              Branding(company_name="SMASCO", timezone="Asia/Riyadh"), at)
    assert name == "SMASCO-Assessment-Monthly-IT-Operations-Review-1st-08-20261021-0525.pdf"
    assert rr._slug("Short title") == "Short-title"
    assert rr._slug("x" * 60) == "x" * 48                    # one long word is still cut
    assert len(rr._slug("word " * 30)) <= 48 and not rr._slug("word " * 30).endswith("-")


# ── CSV (F8) ────────────────────────────────────────────────────────────────

def _meta():
    return {"company_name": "Acme", "title": "Weekly Ops", "period_label": "2026-10-14 — 2026-10-21",
            "timezone": "Asia/Riyadh", "tz_label": "GMT+3", "generated_label": "2026-10-21 08:00 GMT+3",
            "scope_label": "Group: Core", "confidentiality": ""}


def test_render_csv_header_kpis_tables_and_blank_lines():
    sections = [
        {"id": "availability_kpis", "title": "Availability Summary", "description": "Over all nodes.",
         "kpis": [{"label": "Availability", "value": "99.95%", "subtitle": "target 99.9%"},
                  {"label": "Outage events", "value": "3"}],
         "tables": [], "notes": ["Planned maintenance excluded."]},
        {"id": "device_availability", "title": "Node Availability", "kpis": [],
         "tables": [{"title": "Nodes", "headers": ["Node", "Availability"],
                     "rows": [["=HYPERLINK(\"x\")", "99.95%"], ["مكتب جدة", "—"]]}]},
        {"id": "ncm", "title": "Config", "restricted": True,
         "notes": ["Not included: viewing this section requires the ncm.view permission."]},
        {"id": "srv", "title": "Servers", "error": True, "notes": ["This section could not be generated."]},
    ]
    body = render_csv(_meta(), sections)
    assert body.startswith("﻿".encode("utf-8"))
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert rows[:6] == [["Organisation: Acme"], ["Report: Weekly Ops"],
                        ["Period: 2026-10-14 — 2026-10-21"], ["Timezone: Asia/Riyadh (GMT+3)"],
                        ["Generated: 2026-10-21 08:00 GMT+3"], ["Scope: Group: Core"]]
    assert rows[6] == [] and rows[7] == ["Availability Summary"]
    assert ["Availability", "99.95%", "target 99.9%"] in rows and ["Outage events", "3", ""] in rows
    assert ["Note: Planned maintenance excluded."] in rows
    i = rows.index(["Node", "Availability"])
    assert rows[i - 1] == ["Nodes"] and rows[i + 1] == ["'=HYPERLINK(\"x\")", "99.95%"]
    assert rows[i + 2] == ["مكتب جدة", "—"]
    assert ["Not included: viewing this section requires the ncm.view permission."] in rows
    assert ["This section could not be generated."] in rows


# ── API: /generate validation (L6), /render scope + CSV (F7, F8), catalog ──

ROLES = {"r_admin": ["system.admin"], "r_viewer": ["reports.view", "devices.view"]}


@pytest.fixture
def api(monkeypatch):
    state = {"user": SimpleNamespace(id=uuid.uuid4(), username="u", full_name="u", role="r_admin",
                                     is_active=True, scope_tags=[])}
    db = DB(("FROM devices d WHERE", [(D1,), (D2,)]),
            ("FROM device_groups", [(G1, "Core Network")]),
            ("FROM devices WHERE id::text = ANY", [(D1, "core-1", "10.0.0.1")]))

    async def role_perms(_db, role):
        return list(ROLES.get(role, []))

    async def no_role_scope(_db, role):
        return []
    monkeypatch.setattr(security, "get_role_permissions", role_perms)
    monkeypatch.setattr(reports_api, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_scope_tags", no_role_scope)

    async def fake_db():
        yield db

    async def current_user():
        return state["user"]
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = current_user
    # No context manager: startup hooks (and their database access) do not run.
    yield SimpleNamespace(db=db, state=state, client=TestClient(app))
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("body", [
    {"report_type": "executive_summary", "period": "last_year", "format": "pdf"},
    {"report_type": "executive_summary", "period": "last_24h", "format": "docx"},
    {"report_type": "weekly_magic", "period": "last_24h", "format": "pdf"},
    {"report_type": "device_health", "period": "custom", "format": "csv"},
])
def test_generate_refuses_unknown_period_format_or_type(api, body):
    r = api.client.post("/api/v1/reports/generate", json=body)
    assert r.status_code == 422, r.text


def test_report_request_accepts_the_documented_values():
    req = reports_api.ReportRequest(report_type="device_health", period="custom", format="excel",
                                    from_time="2026-10-01T00:00:00Z", to_time="2026-10-02T00:00:00Z")
    assert req.period == "custom"
    with pytest.raises(ValidationError):
        reports_api.ReportRequest(period="custom", from_time="2026-10-02T00:00:00Z",
                                  to_time="2026-10-01T00:00:00Z")


def _fake_sections(seen):
    async def build_sections(db, entries, start, end, filters, requester=None, branding=None):
        seen["filters"] = filters
        return [{"id": "availability_kpis", "title": "Availability Summary",
                 "kpis": [{"label": "Availability", "value": "99.95%", "accent": "success"}],
                 "charts": [], "tables": [], "notes": []}]
    return build_sections


def test_render_resolves_group_and_location_scope(api, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rs, "build_sections", _fake_sections(seen))
    r = api.client.get("/api/v1/reports/render/availability",
                       params=[("group_ids", G1), ("locations", "Jeddah"), ("locations", "Riyadh"),
                               ("device_types", "router")])
    assert r.status_code == 200, r.text
    data = r.json()
    assert seen["filters"]["device_ids"] == [D1, D2]
    assert data["scope_label"] == "Group: Core Network · Locations: Jeddah, Riyadh · Device type: router"
    assert data["filters"] == {"group_ids": [G1], "locations": ["Jeddah", "Riyadh"], "device_types": ["router"]}
    sql, params = next(c for c in api.db.calls if "FROM devices d WHERE" in c[0])
    assert params["group_ids"] == [G1] and params["locations"] == ["Jeddah", "Riyadh"]
    assert api.client.get("/api/v1/reports/render/availability",
                          params={"group_ids": "core"}).status_code == 400


def test_render_scope_that_matches_nothing_never_widens(api, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rs, "build_sections", _fake_sections(seen))
    api.db.handlers[0] = ("FROM devices d WHERE", [])
    r = api.client.get("/api/v1/reports/render/availability", params={"locations": "Nowhere"})
    assert r.status_code == 200 and seen["filters"]["device_ids"] == [scoping.NO_MATCH_ID]
    assert r.json()["scope_label"] == "Location: Nowhere"


def test_render_names_selected_devices(api, monkeypatch):
    monkeypatch.setattr(rs, "build_sections", _fake_sections({}))
    r = api.client.get("/api/v1/reports/render/availability", params={"device_ids": D1})
    assert r.json()["scope_label"] == "core-1"


def test_render_csv_download(api, monkeypatch):
    monkeypatch.setattr(rs, "build_sections", _fake_sections({}))
    r = api.client.get("/api/v1/reports/render/availability", params={"format": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert ".csv" in r.headers["content-disposition"] and "attachment" in r.headers["content-disposition"]
    assert r.content.startswith("﻿".encode()) and b"Availability,99.95%" in r.content
    # Downloads need reports.export, like the other formats.
    api.state["user"].role = "r_viewer"
    assert api.client.get("/api/v1/reports/render/availability", params={"format": "csv"}).status_code == 403


def test_preview_accepts_scope_lists(api, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rs, "build_sections", _fake_sections(seen))
    r = api.client.post("/api/v1/reports/preview",
                        json={"sections": ["availability_kpis"], "group_ids": [G1], "locations": ["Jeddah"]})
    assert r.status_code == 200, r.text
    assert seen["filters"]["device_ids"] == [D1, D2] and r.json()["scope_label"].startswith("Group: Core")


def test_catalog_marks_legacy_types_deprecated_with_a_replacement():
    legacy = {t["key"]: t for t in reports_api.LEGACY_TYPES}
    assert {k: t["replacement"] for k, t in legacy.items()} == {
        "executive_summary": "executive_overview", "device_health": "availability",
        "service_health": "service_owner_sla", "alert_analysis": "alerts",
        "full_report": "network_operations"}
    assert all(t["deprecated"] for t in legacy.values())
    assert all(t["replacement"] in rs.REPORT_PRESETS for t in legacy.values())


def test_template_catalog_offers_csv():
    from app.services import report_templates as rt
    entry = rt.catalog_entry(next(iter(rt.TEMPLATES.values())), None)
    assert "csv" in entry["formats"]


def test_render_takes_repeated_values_so_a_location_may_contain_a_comma(api, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rs, "build_sections", _fake_sections(seen))
    r = api.client.get("/api/v1/reports/render/availability",
                       params=[("locations", "Riyadh, HQ"), ("locations", "Jeddah"),
                               ("group_ids", G1), ("device_ids", f"{D1},{D2}"), ("device_ids", D3)])
    assert r.status_code == 200, r.text
    sql, params = next(c for c in api.db.calls if "FROM devices d WHERE" in c[0])
    assert params["locations"] == ["Riyadh, HQ", "Jeddah"]
    assert params["device_ids"] == [D1, D2, D3] and params["group_ids"] == [G1]
    assert r.json()["scope_label"].startswith("Group: Core Network · Locations: Riyadh, HQ, Jeddah")
    assert rr.split_ids(["a,b", "c"]) == ["a", "b", "c"] and rr.split_ids("a,b") == "a,b"
