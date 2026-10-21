"""Contract for every report section and built-in template.

Each section declares a valid scope, real permissions and a valid option
schema; every built-in template points at real sections with valid options;
and every section-pack section renders a well-formed section (never raises)
when there is no data at all, with and without a device filter."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.permissions import ALL_PERMISSIONS
from app.core.scoping import NO_MATCH_ID
from app.services import report_sections as rs
from app.services import report_templates as rt

PACK_SECTIONS = sorted(sid for sid, e in rs.SECTION_REGISTRY.items()
                       if getattr(e["fn"], "__module__", "").startswith("app.services.report_packs"))


def test_every_section_declares_valid_metadata():
    perms = set(ALL_PERMISSIONS)
    for sid, e in rs.SECTION_REGISTRY.items():
        assert rs.re_key(sid), sid
        assert e["scope"] in rs.SCOPES, sid
        assert e["requires"], f"{sid} must declare the permission its data needs"
        assert set(e["requires"]) <= perms, f"{sid}: unknown permission in {e['requires']}"
        assert e["semantics"] in ("window", "snapshot"), sid
        assert e["title"] and e["description"] and e["category"], sid
        keys = [o["key"] for o in e["options"]]
        assert len(keys) == len(set(keys)), f"{sid}: duplicate option keys"
        for o in e["options"]:
            assert o["type"] in rs.OPTION_TYPES and o.get("label"), (sid, o)
            if o["type"] == "select":
                assert o.get("choices") and o["default"] in [c[0] for c in o["choices"]], (sid, o)
            if o["type"] in ("int", "number"):
                assert o.get("min") is not None and o.get("max") is not None, (sid, o)
                assert o["min"] <= o["default"] <= o["max"], (sid, o)
        # Normalising the defaults is a fixed point.
        assert rs.normalize_options(sid, {}) == rs.normalize_options(sid, rs.normalize_options(sid, {}))


def test_traffic_category_is_exactly_the_netflow_scope():
    traffic = {sid for sid, e in rs.SECTION_REGISTRY.items() if e["category"] == "Traffic"}
    assert traffic == set(rs.NETFLOW_SCOPED_SECTIONS)


def test_builtin_catalog_is_valid_and_published():
    assert rt.validate_catalog() == []
    assert set(rt.TEMPLATES) <= set(rs.REPORT_PRESETS)
    for key, t in rt.TEMPLATES.items():
        preset = rs.REPORT_PRESETS[key]
        assert preset["sections"] == t.section_ids
        assert preset["title"] == t.title


def test_no_template_repeats_a_section_title():
    # Assessment L2: a template listing one section several times (with
    # different options) must title each one, or the catalog, the table of
    # contents and restricted/failed stubs all read the same title.
    for key, t in rt.TEMPLATES.items():
        titles = [e.get("title") or rs.SECTION_REGISTRY[e["id"]]["title"] for e in t.sections]
        dupes = sorted({x for x in titles if titles.count(x) > 1})
        assert not dupes, (key, dupes)


def test_option_normalisation_clamps_and_defaults():
    for sid, e in rs.SECTION_REGISTRY.items():
        for o in e["options"]:
            if o["type"] == "int":
                assert rs.normalize_options(sid, {o["key"]: 10 ** 9})[o["key"]] == o["max"]
                assert rs.normalize_options(sid, {o["key"]: -(10 ** 9)})[o["key"]] == o["min"]
                assert rs.option_errors(sid, {o["key"]: 10 ** 9})
            if o["type"] == "select":
                assert rs.normalize_options(sid, {o["key"]: "__nope__"})[o["key"]] == o["default"]
                assert rs.option_errors(sid, {o["key"]: "__nope__"})
    assert rs.option_errors("availability_kpis", {"bogus": 1})
    assert rs.option_errors("__missing__", {}) == ["Unknown section '__missing__'"]


# ── every pack section survives "no data at all" ────────────────────────────

class _EmptyResult:
    rowcount = 0

    def fetchall(self):
        return []

    def first(self):
        return None

    def scalar(self):
        return None

    def mappings(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter(())


class EmptyDB:
    """Answers every query with no rows; records the SQL and parameters."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _EmptyResult()

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def get(self, *a, **k):
        return None


class _CHResult:
    result_rows: list = []
    column_names: tuple = ()
    first_row = None

    def named_results(self):
        return []


class EmptyCH:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        return _CHResult()

    def command(self, *a, **k):
        return None


def _shape_ok(sec: dict) -> None:
    assert isinstance(sec, dict)
    for key in ("id", "title", "kpis", "charts", "tables", "notes"):
        assert key in sec, key
    for k in sec["kpis"]:
        assert {"label", "value"} <= set(k), k
        assert isinstance(k["value"], str), k
    for c in sec["charts"]:
        assert "title" in c and ("series" in c or "png" in c), c
        s = c.get("series")
        if s:
            assert s["kind"] in ("area", "line", "bars", "donut", "rank", "stacked"), s
    for t in sec["tables"]:
        assert isinstance(t["headers"], list) and isinstance(t["rows"], list), t
        if t.get("styles"):
            assert len(t["styles"]) == len(t["headers"]), t
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["headers"], row)
    for n in sec["notes"]:
        assert isinstance(n, str)


@pytest.mark.parametrize("sid", PACK_SECTIONS or ["__none__"])
@pytest.mark.parametrize("device_ids", [None, [NO_MATCH_ID]])
def test_pack_section_renders_without_data(sid, device_ids, monkeypatch):
    if sid == "__none__":
        pytest.skip("no section packs registered")
    from app.services.branding import Branding

    ch = EmptyCH()
    db = EmptyDB()
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    to = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    filters = {"device_ids": device_ids} if device_ids else None
    out = asyncio.run(rs.build_sections(db, [sid], to - timedelta(days=7), to, filters,
                                        requester=None, branding=Branding(timezone="Asia/Riyadh")))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), f"{sid} raised on empty data: {sec['notes']}"
    _shape_ok(sec)
    if device_ids and rs.SECTION_REGISTRY[sid]["scope"] == "device":
        # A device-scoped section must carry the filter into its queries.
        sql = [c for c in ch.calls] + [c for c in db.calls]
        assert sql, f"{sid} ran no query"
        assert any(NO_MATCH_ID in str(p) for _s, p in sql), f"{sid} ignored the device filter"
