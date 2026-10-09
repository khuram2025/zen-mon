"""GET /api/v1/inventory/export: the audited CSV export of the Inventory datasets.

The rules under test (doc 04 T13, doc 02 section 8 and 11): it needs ``inventory.export`` (a viewer gets lists but not
files), it contains exactly what the matching list endpoint returns to the same user (tag scope included), a cell
that a spreadsheet would run as a formula is neutralised, a hard row cap says so in a header, and every export is an
audit entry written before the first byte.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import pytest
from sqlalchemy import text

from app.services import inventory_export as export
from app.services import inventory_query as iq
from inventory_fleet import (
    EDGE_DEVICES, EDGE_SERVERS, SCOPED_TAG, add_software_rows, aid, api_client, fleet_db, fleet_db_fresh,
    fleet_info, fleet_template, pinned_clock,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
EXPORT = f"{API}/export"
PERMS = ["inventory.view", "inventory.export"]


# ── helpers ──────────────────────────────────────────────────────────

def fetch(client, dataset: str, expect: int = 200, **params):
    resp = client.get(EXPORT, params={"dataset": dataset, **{k: v for k, v in params.items() if v is not None}})
    assert resp.status_code == expect, (dataset, params, resp.status_code, resp.text[:300])
    return resp


def parse(resp) -> tuple[list[str], list[dict[str, str]]]:
    """Header and rows of a response, read the way a spreadsheet reads the file."""
    body = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body, newline="")))
    header = rows[0]
    return header, [dict(zip(header, r)) for r in rows[1:]]


def listing(client, path: str, **params) -> dict:
    resp = client.get(API + path, params={k: v for k, v in params.items() if v is not None})
    assert resp.status_code == 200, (path, params, resp.text[:300])
    return resp.json()


@pytest.fixture
def exporter(fleet_db):
    with api_client(fleet_db, role="t_ex_export", permissions=PERMS) as c:
        yield c


def audit(db) -> list[dict[str, Any]]:
    return [{"action": r[0], "actor": r[1], "role": r[2], "type": r[3], "id": r[4], "metadata": r[5]} for r in db.query(
        "SELECT action, actor_username, actor_role, resource_type, resource_id, metadata FROM audit_logs "
        "WHERE action = 'inventory.export' ORDER BY created_at, id")]


# ════════════════════════════════════════════════════════════════════
# The CSV rules
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value,expected", [
    (None, ""), ("", ""), ("plain", "plain"), (0, "0"), (12, "12"), (1.5, "1.5"), (True, "true"), (False, "false"),
    ("a,b", '"a,b"'), ('say "hi"', '"say ""hi"""'), ("two\nlines", '"two\nlines"'), ("cr\rlf", '"cr\rlf"'),
    ("=1+1", "\"'=1+1\""), ("+1", "\"'+1\""), ("-1", "\"'-1\""), ("@SUM(A1)", "\"'@SUM(A1)\""),
    ("\tcmd", "\"'\tcmd\""), ("\rcmd", "\"'\rcmd\""),
    ("=HYPERLINK(\"http://x\",\"y\")", "\"'=HYPERLINK(\"\"http://x\"\",\"\"y\"\")\""),
    ("a=b", "a=b"), ("1-2", "1-2"), ("it's", "\"it's\""), ("café", "café"),
    (["a", "b", None], "a;b;"), (("x",), "x"), ({"b": 2, "a": "="}, '"{""a"":""="",""b"":2}"'),
])
def test_csv_cell(value, expected):
    assert export.csv_cell(value) == expected


def test_a_number_is_never_neutralised_but_the_text_of_one_is():
    assert export.csv_cell(-5) == "-5" and export.csv_cell("-5") == "\"'-5\""


def test_csv_line_ends_in_crlf_and_the_cells_survive_a_round_trip():
    cells = ["=cmd", "a,b", 'q"uote', "multi\nline", None, 3, "plain", "\ttab"]
    line = export.csv_line(cells)
    assert line.endswith("\r\n") and line.count("\r\n") == 1 + "".join(cells[i] or "" for i in (3,)).count("\r\n")
    back = next(csv.reader(io.StringIO(line, newline="")))
    assert back == ["'=cmd", "a,b", 'q"uote', "multi\nline", "", "3", "plain", "'\ttab"]


def test_the_rule_is_the_one_of_the_dashboards_csv_export():
    """Same first characters as ``esc()`` of dashboard/src/components/availability/exportCsv.ts (plus the carriage return)."""
    from pathlib import Path
    ts = (Path(__file__).resolve().parents[2] / "dashboard" / "src" / "components" / "availability" / "exportCsv.ts").read_text(encoding="utf-8")
    assert "/^[=+\\-@\\t]/" in ts and "[\",\\n\\r']" in ts
    assert export._FORMULA_START[:5] == ("=", "+", "-", "@", "\t")


# ════════════════════════════════════════════════════════════════════
# Who may export, and the shape of the response
# ════════════════════════════════════════════════════════════════════

def test_a_viewer_reads_the_lists_but_cannot_export(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_ex_viewer", permissions=["inventory.view"]) as c:
        for path in ("/assets", "/software", "/os", "/hardware", "/changes", "/software/git/installations"):
            assert c.get(API + path).status_code == 200, path
        for dataset in export.DATASETS:
            resp = c.get(EXPORT, params={"dataset": dataset, "product": "git"})
            assert resp.status_code == 403, dataset
            assert not resp.content.startswith(b"\xef\xbb\xbf")                        # no file, no data
    assert audit(fleet_db) == []


def test_roles_that_may_export(fleet_db_fresh, pinned_clock):
    for role, perms, status in (("t_ex_a", ["inventory.export"], 200), ("t_ex_b", ["inventory.view", "inventory.export"], 200),
                                ("t_ex_c", ["inventory.manage"], 403), ("t_ex_d", [], 403), ("t_ex_e", ["reports.export", "netflow.export"], 403)):
        with api_client(fleet_db_fresh, role=role, permissions=perms) as c:
            assert c.get(EXPORT, params={"dataset": "assets"}).status_code == status, role
    with api_client(fleet_db_fresh, role="t_ex_admin", permissions=["system.admin"]) as c:
        assert c.get(EXPORT, params={"dataset": "assets"}).status_code == 200


def test_the_built_in_roles_that_hold_export(fleet_db, pinned_clock):
    from app.core.permissions import BUILTIN_ROLE_DEFINITIONS
    for role in ("admin", "operator", "viewer", "read_only", "network_team", "systems_team", "security_team", "application_team"):
        perms = BUILTIN_ROLE_DEFINITIONS.get(role, [])
        expected = 200 if ("inventory.export" in perms or "system.admin" in perms) else 403
        with api_client(fleet_db, role=f"t_{role}", permissions=list(perms)) as c:
            assert c.get(EXPORT, params={"dataset": "assets"}).status_code == expected, role


def test_unauthenticated_requests_are_refused():
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get(EXPORT, params={"dataset": "assets"}).status_code in (401, 403)


@pytest.mark.parametrize("dataset", export.DATASETS)
def test_the_response_is_a_utf8_csv_attachment_with_bom_and_crlf(exporter, pinned_clock, dataset):
    resp = fetch(exporter, dataset, product="git")
    assert resp.content.startswith(b"\xef\xbb\xbf")                                  # UTF-8 byte-order mark
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert resp.headers["content-disposition"] == f'attachment; filename="zenplus-inventory-{dataset}-20261008.csv"'   # dataset + UTC date
    assert resp.headers["cache-control"] == "no-store"
    text_ = resp.content.decode("utf-8")
    assert "\n" not in text_.replace("\r\n", "")                                      # no bare line feeds outside quoted cells
    assert text_.endswith("\r\n")
    header, rows = parse(resp)
    assert len(set(header)) == len(header) and rows
    assert int(resp.headers["x-export-rows"]) == len(rows)
    assert "x-export-truncated" not in resp.headers
    assert all(len(r) == len(header) for r in csv.reader(io.StringIO(text_.lstrip("﻿"), newline="")))


def test_the_filename_carries_the_utc_date_of_the_clock(fleet_db, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(iq, "utcnow", lambda: datetime(2027, 1, 31, 23, 59, 59, tzinfo=timezone.utc))
    with api_client(fleet_db, role="t_ex_export", permissions=PERMS) as c:
        assert 'zenplus-inventory-hardware-20270131.csv"' in fetch(c, "hardware").headers["content-disposition"]


@pytest.mark.parametrize("params,fragment", [
    ({"dataset": "nope"}, None), ({"dataset": "installations"}, "product is required"), ({"dataset": "assets", "kind": "router"}, None),
    ({"dataset": "assets", "sort": "bogus"}, "sort must be one of"), ({"dataset": "assets", "order": "up"}, None),
    ({"dataset": "software", "sort": "asset_name"}, "sort must be one of"), ({"dataset": "software", "category": "shoes"}, "category must be one of"),
    ({"dataset": "software", "hidden": "maybe"}, None), ({"dataset": "installations", "product": "git", "sort": "name"}, "sort must be one of"),
    ({"dataset": "os", "kind": "router"}, None), ({"dataset": "changes", "category": "update"}, "category must be one of"),
    ({"dataset": "changes", "category": ["os", "software"]}, "one value"), ({"dataset": "changes", "label": "sidegrade"}, None),
    ({"dataset": "changes", "since": "2026-10-08T00:00:00Z", "until": "2026-10-07T00:00:00Z"}, "until must be after since"),
    ({"dataset": "changes", "asset": "server:not-a-uuid"}, "asset must look like"),
    ({"dataset": "assets", "product_version": "1.0"}, "product_version needs product"),
])
def test_bad_requests_are_422_and_leave_no_audit_entry(fleet_db_fresh, pinned_clock, params, fragment):
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        resp = c.get(EXPORT, params=params)
        assert resp.status_code == 422, resp.text
        if fragment:
            assert fragment in str(resp.json()["detail"])
    assert audit(fleet_db_fresh) == []


def test_an_unknown_product_is_the_same_404_as_the_list(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        resp = c.get(EXPORT, params={"dataset": "installations", "product": "no-such-product"})
        listing_resp = c.get(f"{API}/software/no-such-product/installations")
        assert resp.status_code == listing_resp.status_code == 404 and resp.json() == listing_resp.json()
    assert audit(fleet_db_fresh) == []


# ════════════════════════════════════════════════════════════════════
# The file is what the list returns
# ════════════════════════════════════════════════════════════════════

ASSET_FILTERS = [{}, {"kind": "server"}, {"kind": "device", "vendor": "Cisco"}, {"os_family": "windows", "freshness": "fresh"},
                 {"health": "missing_serial"}, {"product": "google-chrome"}, {"product": "google-chrome", "product_version": "152.0.8001.12"},
                 {"q": "srv-w2"}, {"tag": ["prod", "pci"]}, {"security_agent": "missing"}, {"os_family": "unknown", "sort": "ip", "order": "desc"}]


@pytest.mark.parametrize("filters", ASSET_FILTERS)
def test_assets_export_equals_the_assets_list(exporter, pinned_clock, filters):
    params = dict(filters)
    sort, order = params.pop("sort", "name"), params.pop("order", "asc")
    expected = listing(exporter, "/assets", page_size=500, sort=sort, order=order, **params)
    header, rows = parse(fetch(exporter, "assets", sort=sort, order=order, **params))
    assert header == export.ASSET_COLUMNS
    assert [r["key"] for r in rows] == [i["key"] for i in expected["items"]] and len(rows) == expected["total"]
    for got, want in zip(rows, expected["items"]):
        for col in ("kind", "name", "ip", "vendor", "model", "serial", "os_family", "os_version", "site", "freshness", "last_seen", "inventory_at"):
            assert got[col] == ("" if want[col] is None else str(want[col])), (want["key"], col)
        assert got["tags"] == ";".join(want["tags"]) and got["sources"] == ";".join(want["sources"])
        assert got["software_truncated"] == ("true" if want["software_truncated"] else "false")


def test_assets_export_pages_across_batch_boundaries(exporter, pinned_clock, monkeypatch):
    monkeypatch.setattr(export, "BATCH", 7)           # a smaller export batch only changes how often the list is asked
    expected = listing(exporter, "/assets", page_size=500)["items"]
    _h, rows = parse(fetch(exporter, "assets"))
    assert [r["key"] for r in rows] == [i["key"] for i in expected]


@pytest.mark.parametrize("filters", [{}, {"category": "browser"}, {"category": ["security", "database"]}, {"hidden": "include"},
                                     {"q": "microsoft"}, {"publisher": "microsoft", "hidden": "include"}, {"unmatched": "true"},
                                     {"sort": "hosts", "order": "desc"}, {"sort": "first_seen", "order": "desc"}])
def test_software_export_equals_the_catalog(exporter, pinned_clock, filters):
    params = dict(filters)
    sort, order = params.pop("sort", "name"), params.pop("order", "asc")
    expected = listing(exporter, "/software", page_size=500, sort=sort, order=order, **params)
    header, rows = parse(fetch(exporter, "software", sort=sort, order=order, **params))
    assert header == export.SOFTWARE_COLUMNS
    assert [r["product_key"] for r in rows] == [i["product_key"] for i in expected["items"]] and len(rows) == expected["total"]
    for got, want in zip(rows, expected["items"]):
        assert (got["name"], got["publisher"], got["category"], got["norm_status"]) == (want["name"], want["publisher"] or "", want["category"], want["norm_status"])
        assert (int(got["hosts"]), int(got["installs"]), int(got["version_count"])) == (want["hosts"], want["installs"], want["version_count"])
        assert got["latest_version"] == (want["latest_version"] or "") and got["first_seen"] == (want["first_seen"] or "")
        assert got["last_change"] == (want["last_change"] or "")
        assert got["top_versions"].split(";")[0] == f"{want['distribution']['top'][0]['version']} ({want['distribution']['top'][0]['hosts']})"


def test_software_export_pages_across_batch_boundaries(exporter, pinned_clock, monkeypatch):
    monkeypatch.setattr(export, "BATCH", 4)
    _h, rows = parse(fetch(exporter, "software", sort="hosts", order="desc", hidden="include"))
    expected = listing(exporter, "/software", page_size=500, sort="hosts", order="desc", hidden="include")["items"]
    assert [r["product_key"] for r in rows] == [i["product_key"] for i in expected]


@pytest.mark.parametrize("product,filters", [("google-chrome", {}), ("git", {"version": "2.47.0"}), ("7-zip", {"q": "srv-w22"}),
                                              ("git", {"sort": "version", "order": "desc"}), ("java-se-runtime", {"sort": "install_date", "order": "desc"})])
def test_installations_export_equals_the_installed_on_list(exporter, pinned_clock, product, filters):
    params = dict(filters)
    sort, order = params.pop("sort", "asset_name"), params.pop("order", "asc")
    expected = listing(exporter, f"/software/{product}/installations", page_size=500, sort=sort, order=order, **params)
    header, rows = parse(fetch(exporter, "installations", product=product, sort=sort, order=order, **params))
    assert header == export.INSTALL_COLUMNS
    assert len(rows) == expected["total"] > 0
    assert [(r["asset_key"], r["package_name"], r["version"]) for r in rows] == \
           [(i["asset_key"], i["package_name"], i["version"] or "") for i in expected["items"]]
    assert {r["product_key"] for r in rows} == {product}


def test_installations_export_pages_across_batch_boundaries(exporter, pinned_clock, monkeypatch):
    monkeypatch.setattr(export, "BATCH", 5)
    _h, rows = parse(fetch(exporter, "installations", product="google-chrome"))
    expected = listing(exporter, "/software/google-chrome/installations", page_size=500)["items"]
    assert [r["asset_key"] for r in rows] == [i["asset_key"] for i in expected] and len(rows) == 41


def test_the_os_export_has_a_row_per_version_and_each_count_is_a_list_total(exporter, pinned_clock):
    for kind in (None, "server", "device"):
        header, rows = parse(fetch(exporter, "os", kind=kind))
        tree = listing(exporter, "/os", kind=kind)
        assert header == export.OS_COLUMNS
        assert sum(int(r["assets"]) for r in rows) == tree["total"]
        for r in rows[:60]:
            params = {"os_family": r["family"], "os_product": r["product"] or "unknown", "os_release": r["release"] or "unknown",
                      "os_version": r["version"] or "unknown", "kind": kind}
            assert listing(exporter, "/assets", page_size=1, **params)["total"] == int(r["assets"]), r
    _h, rows = parse(fetch(exporter, "os"))
    win = [r for r in rows if r["family"] == "windows" and r["release"] == "2022"]
    assert {r["build_currency"] for r in win} == {"latest", "previous", "older"}
    assert [r for r in rows if r["family"] == "fortios" and r["version"] == "7.2.8"][0]["raw_example"].startswith("v7.2.8")


def test_the_hardware_export_carries_the_same_numbers_as_the_hardware_page(exporter, pinned_clock):
    page = listing(exporter, "/hardware", vendor="Cisco")
    header, rows = parse(fetch(exporter, "hardware", vendor="Cisco"))
    assert header == export.HW_COLUMNS
    models = {(r["vendor"], r["model"]): r for r in rows if r["section"] == "network_model"}
    for m in page["network"]["models"]:
        got = models[(m["vendor"] or "", m["model"] or "")]
        assert (int(got["count"]), int(got["firmware_versions"]), int(got["serial_missing"])) == (m["count"], m["firmware_versions"], m["serial_missing"])
    assert sum(int(r["count"]) for r in rows if r["section"] == "network_vendor") == page["network"]["devices"]
    assert sum(int(r["count"]) for r in rows if r["section"] == "module") == sum(g["count"] for g in page["modules"])
    assert {r["section"] for r in rows} >= {"network_vendor", "network_model", "module", "server_cpu_models", "server_memory", "server_disks"}
    mem = {r["value"]: int(r["count"]) for r in rows if r["section"] == "server_memory"}
    assert mem == {m["value"]: m["count"] for m in page["servers"]["memory"]}


CHANGE_FILTERS = [{"since": "2026-01-01T00:00:00Z"}, {}, {"category": "software", "since": "2026-01-01T00:00:00Z"}, {"action": "added", "since": "2026-01-01T00:00:00Z"},
                  {"label": "upgrade", "since": "2026-01-01T00:00:00Z"}, {"label": "downgrade", "since": "2026-01-01T00:00:00Z"},
                  {"kind": "device", "since": "2026-01-01T00:00:00Z"}, {"subject_key": "google-chrome", "since": "2026-01-01T00:00:00Z"},
                  {"q": "fortios", "since": "2026-01-01T00:00:00Z"},
                  {"since": "2026-10-05T00:00:00Z", "until": "2026-10-07T00:00:00Z"}]


@pytest.mark.parametrize("filters", CHANGE_FILTERS)
def test_changes_export_equals_the_change_feed(exporter, pinned_clock, filters):
    expected = listing(exporter, "/changes", page_size=500, **filters)
    header, rows = parse(fetch(exporter, "changes", **filters))
    assert header == export.CHANGE_COLUMNS
    assert [int(r["id"]) for r in rows] == [e["id"] for e in expected["items"]] and len(rows) == expected["total"]
    for got, want in zip(rows, expected["items"]):
        assert (got["observed_at"], got["asset_key"], got["category"], got["action"], got["subject"]) == \
               (want["observed_at"], want["asset_key"], want["category"], want["action"], want["subject"])
        assert (got["old_value"], got["new_value"], got["label"]) == (want["old_value"] or "", want["new_value"] or "", want["label"] or "")
        pair = want["pair"] or {}
        assert (got["paired_event_id"], got["paired_label"]) == (str(pair.get("event_id", "")), pair.get("label") or "")
        assert got["asset_present"] == ("true" if want["asset_present"] else "false") and got["marker"] == ("true" if want["marker"] else "false")


def test_changes_export_pages_by_keyset_across_ties(exporter, pinned_clock, monkeypatch):
    """Paired upgrades are two events with one timestamp: the (time, id) keyset must not skip or repeat either half."""
    monkeypatch.setattr(export, "BATCH", 3)
    for filters in ({"since": "2026-01-01T00:00:00Z"}, {"since": "2026-01-01T00:00:00Z", "category": "software"}):
        expected = listing(exporter, "/changes", page_size=500, **filters)["items"]
        _h, rows = parse(fetch(exporter, "changes", **filters))
        assert [int(r["id"]) for r in rows] == [e["id"] for e in expected]
    paired = [e for e in listing(exporter, "/changes", page_size=500, since="2026-01-01T00:00:00Z")["items"] if e["pair"]]
    assert paired and {r["id"] for r in rows} >= {str(e["id"]) for e in paired}


def test_the_whole_file_is_one_snapshot(exporter, pinned_clock, monkeypatch):
    """The pages of a file are read in one REPEATABLE READ, read-only transaction on a session of the export's own."""
    seen: list[tuple[str, str]] = []
    real = iq.list_assets

    async def spy(db, *a, **kw):
        if kw.get("page_size") == export.BATCH:
            iso = (await db.execute(text("SHOW transaction_isolation"))).scalar()
            ro = (await db.execute(text("SHOW transaction_read_only"))).scalar()
            seen.append((iso, ro))
        return await real(db, *a, **kw)

    monkeypatch.setattr(export, "BATCH", 50)
    monkeypatch.setattr(iq, "list_assets", spy)
    fetch(exporter, "assets")
    assert len(seen) >= 3 and set(seen) == {("repeatable read", "on")}


# ════════════════════════════════════════════════════════════════════
# Scope: nothing the list would not return to the same user
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def scoped_exporter(fleet_db):
    with api_client(fleet_db, role="t_ex_scoped", permissions=PERMS, scope_tags=(SCOPED_TAG,)) as c:
        yield c


def test_a_scoped_export_holds_exactly_what_the_scoped_lists_return(scoped_exporter, pinned_clock):
    c = scoped_exporter
    _h, assets = parse(fetch(c, "assets"))
    assert {r["name"] for r in assets} == set(EDGE_SERVERS) | set(EDGE_DEVICES) and len(assets) == 15
    assert [r["key"] for r in assets] == [i["key"] for i in listing(c, "/assets", page_size=500)["items"]]
    _h, software = parse(fetch(c, "software", hidden="include"))
    expected = listing(c, "/software", page_size=500, sort="name", order="asc", hidden="include")["items"]
    assert [(r["product_key"], int(r["hosts"]), int(r["installs"])) for r in software] == [(i["product_key"], i["hosts"], i["installs"]) for i in expected]
    assert 0 < len(software) < 31
    _h, installs = parse(fetch(c, "installations", product="google-chrome"))
    assert {r["asset_name"] for r in installs} <= set(EDGE_SERVERS) and len(installs) == listing(c, "/software/google-chrome/installations")["total"]
    _h, changes = parse(fetch(c, "changes", since="2026-01-01T00:00:00Z"))
    assert [int(r["id"]) for r in changes] == [e["id"] for e in listing(c, "/changes", page_size=500, since="2026-01-01T00:00:00Z")["items"]]
    assert all(r["asset_name"] != "srv-retired-01" for r in changes)                   # a deleted asset's events stay hidden
    _h, os_rows = parse(fetch(c, "os"))
    assert sum(int(r["assets"]) for r in os_rows) == 15
    _h, hw = parse(fetch(c, "hardware"))
    assert sum(int(r["count"]) for r in hw if r["section"] == "network_vendor") == len(EDGE_DEVICES)


def test_a_scoped_export_cannot_reach_other_teams_data_by_filter(scoped_exporter, pinned_clock):
    c = scoped_exporter
    _h, rows = parse(fetch(c, "assets", q="srv-w22-01"))
    assert rows == []
    _h, rows = parse(fetch(c, "changes", asset=f"server:{aid('server', 'srv-w22-01')}", since="2026-01-01T00:00:00Z"))
    assert rows == []
    assert c.get(EXPORT, params={"dataset": "installations", "product": "no-such-product"}).status_code == 404
    other_only = "veeam-backup-and-replication"                                           # no edge server runs it
    assert c.get(EXPORT, params={"dataset": "installations", "product": other_only}).status_code == 404
    assert c.get(f"{API}/software/{other_only}/installations").status_code == 404


def test_the_export_stays_inside_a_role_scope(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_ex_team", permissions=PERMS, role_scope_tags=(SCOPED_TAG,)) as c:
        _h, rows = parse(fetch(c, "assets"))
        assert len(rows) == 15
    with api_client(fleet_db, role="t_ex_nothing", permissions=PERMS, scope_tags=("no-such-tag",)) as c:
        for dataset in ("assets", "software", "os", "hardware", "changes"):
            resp = fetch(c, dataset, since="2026-01-01T00:00:00Z" if dataset == "changes" else None)
            rows = parse(resp)[1]
            assert [r for r in rows if r.get("section", "network_vendor") in ("network_vendor", "network_model", "module")] == [], dataset
            assert dataset == "hardware" or rows == [], dataset


# ════════════════════════════════════════════════════════════════════
# T13: formula injection
# ════════════════════════════════════════════════════════════════════

EVIL = ["=cmd|' /C calc'!A0", "+SUM(1+1)", "-2+3", "@SUM(A1)", "\t=1+1", "\r=1+1", "=HYPERLINK(\"http://evil\",\"x\")", "normal, with comma", 'quote " inside']


def test_hostile_names_are_neutralised_in_every_dataset(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    for i, evil in enumerate(EVIL[:4]):
        db.execute("UPDATE servers SET display_name = $1 WHERE display_name = $2", evil, f"srv-w22-0{i + 1}")
    db.execute("UPDATE devices SET hostname = $1, location = $2, vendor = $3 WHERE hostname = 'fg-01'", "=fw|' /C calc'!A0", "+HQ", "@Evil")
    db.execute("UPDATE inventory_identity SET vendor_label = $1 WHERE asset_kind = 'device' AND asset_id = $2", "@Evil", aid("device", "fg-01"))
    add_software_rows(db, [{"server": "srv-w19-01", "title": "=Evil Tool 1.0", "vendor": "+Evil Inc", "version": "-1.0"},
                           {"server": "srv-w19-02", "title": "\t@Tabbed Tool", "vendor": "=Vendor", "version": "=2.0"}])
    db.execute("UPDATE inventory_change_events SET subject = $1, old_value = $2, new_value = $3 WHERE id = (SELECT min(id) FROM inventory_change_events)",
               "=SUBJECT()", "-old", "@new")
    with api_client(db, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        files = {
            "assets": fetch(c, "assets", sort="name"), "software": fetch(c, "software", hidden="include", q="evil"),
            "installations": fetch(c, "installations", product=db.scalar("SELECT name_key FROM server_software_inventory WHERE package_name = '=Evil Tool 1.0'")),
            "changes": fetch(c, "changes", since="2025-01-01T00:00:00Z"),
        }
    dangerous = re.compile(r"^[=+\-@\t\r]")
    for dataset, resp in files.items():
        raw = resp.content.decode("utf-8-sig")
        header, rows = parse(resp)
        for row in rows:
            for column, value in row.items():
                assert not dangerous.match(value), (dataset, column, value)            # no cell starts like a formula
        # at the byte level too: a risky first character is always preceded by the apostrophe inside the quotes
        for m in re.finditer(r'(?:^|,|\r\n)"?([=+\-@\t]|\r(?!\n))', raw):
            assert False, (dataset, raw[max(0, m.start() - 20): m.end() + 20])
    _h, assets = parse(files["assets"])
    names = {r["name"] for r in assets}
    assert "'=cmd|' /C calc'!A0" in names and "'+SUM(1+1)" in names and "'-2+3" in names and "'@SUM(A1)" in names
    fw = next(r for r in assets if r["name"].startswith("'=fw"))
    assert (fw["site"], fw["vendor"]) == ("'+HQ", "'@Evil")
    _h, installs = parse(files["installations"])
    assert installs[0]["package_name"] == "'=Evil Tool 1.0" and installs[0]["version"] == "'-1.0"
    _h, changes = parse(files["changes"])
    assert any(r["subject"] == "'=SUBJECT()" and r["old_value"] == "'-old" and r["new_value"] == "'@new" for r in changes)
    # ordinary text is untouched: commas and quotes are quoted, not prefixed
    assert export.csv_cell("normal, with comma") == '"normal, with comma"'


def test_the_list_endpoints_still_return_the_raw_text(fleet_db_fresh, pinned_clock):
    """The export neutralises for spreadsheets; the JSON API keeps the real value (the dashboard escapes on render)."""
    fleet_db_fresh.execute("UPDATE servers SET display_name = '=cmd' WHERE display_name = 'srv-w22-01'")
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS) as c:
        assert any(i["name"] == "=cmd" for i in listing(c, "/assets", q="=cmd")["items"])


# ════════════════════════════════════════════════════════════════════
# The audit trail
# ════════════════════════════════════════════════════════════════════

def test_every_export_is_an_audit_entry_with_the_filters_and_the_row_count(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, username="auditor.one", persist_user=True) as c:
        a = fetch(c, "assets", kind="server", os_family="windows", tag=["prod", "pci"])
        s = fetch(c, "software", category=["browser", "security"], q="g")
        i = fetch(c, "installations", product="git", version="2.47.0")
        ch = fetch(c, "changes", category="software", action="changed", since="2026-10-01T00:00:00Z")
        o = fetch(c, "os", kind="device")
        h = fetch(c, "hardware", vendor="Cisco")
    rows = audit(fleet_db_fresh)
    assert [r["id"] for r in rows] == ["assets", "software", "installations", "changes", "os", "hardware"]
    assert all(r["action"] == "inventory.export" and r["actor"] == "auditor.one" and r["role"] == "t_ex_export" and r["type"] == "inventory_export"
               for r in rows)
    meta = [r["metadata"] for r in rows]
    assert [m["dataset"] for m in meta] == ["assets", "software", "installations", "changes", "os", "hardware"]
    assert [m["rows"] for m in meta] == [len(parse(x)[1]) for x in (a, s, i, ch, o, h)]
    assert all(m["truncated"] is False for m in meta)
    assert meta[0]["filters"] == {"kind": "server", "os_family": "windows", "tag": ["prod", "pci"], "sort": "name", "order": "asc"}
    assert meta[1]["filters"] == {"q": "g", "category": ["browser", "security"], "sort": "name", "order": "asc"}
    assert meta[2]["filters"] == {"product": "git", "version": "2.47.0", "sort": "asset_name", "order": "asc"}
    assert meta[3]["filters"] == {"category": "software", "action": "changed", "since": "2026-10-01T00:00:00Z"}
    assert meta[4]["filters"] == {"kind": "device"} and meta[5]["filters"] == {"vendor": "Cisco"}


def test_an_export_that_returns_nothing_is_still_audited(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        assert parse(fetch(c, "assets", q="no-such-asset-anywhere"))[1] == []
    (row,) = audit(fleet_db_fresh)
    assert row["metadata"]["rows"] == 0 and row["metadata"]["filters"]["q"] == "no-such-asset-anywhere"


def test_a_refused_export_writes_no_entry_and_sends_no_data(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_ex_viewer", permissions=["inventory.view"], persist_user=True) as c:
        resp = c.get(EXPORT, params={"dataset": "assets"})
        assert resp.status_code == 403 and b"srv-" not in resp.content
    assert audit(fleet_db_fresh) == []


def test_the_audit_entry_exists_even_if_the_client_never_reads_the_body(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        with c.stream("GET", EXPORT, params={"dataset": "assets"}) as resp:
            assert resp.status_code == 200
            first = next(resp.iter_bytes())
            assert first.startswith(b"\xef\xbb\xbf")
    assert len(audit(fleet_db_fresh)) == 1


# ════════════════════════════════════════════════════════════════════
# The row cap
# ════════════════════════════════════════════════════════════════════

def test_the_cap_is_200000_rows():
    assert export.EXPORT_CAP == 200_000


@pytest.mark.parametrize("cap,batch", [(20, 7), (20, 20), (21, 20), (145, 50), (1, 500)])
def test_a_capped_export_says_so_and_stops_at_the_cap(fleet_db_fresh, pinned_clock, monkeypatch, cap, batch):
    monkeypatch.setattr(export, "EXPORT_CAP", cap)
    monkeypatch.setattr(export, "BATCH", batch)
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS, persist_user=True) as c:
        resp = fetch(c, "assets")
        _h, rows = parse(resp)
        expected = listing(c, "/assets", page_size=500)["items"]
    assert len(rows) == min(cap, 145) and [r["key"] for r in rows] == [i["key"] for i in expected][:cap]
    assert (resp.headers.get("x-export-truncated") == "true") == (cap < 145)
    assert resp.headers["x-export-rows"] == str(len(rows))
    (entry,) = audit(fleet_db_fresh)
    assert entry["metadata"]["rows"] == len(rows) and entry["metadata"]["truncated"] is (cap < 145)


def test_a_capped_change_feed_keeps_the_newest_rows(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_CAP", 10)
    monkeypatch.setattr(export, "BATCH", 4)
    with api_client(fleet_db_fresh, role="t_ex_export", permissions=PERMS) as c:
        resp = fetch(c, "changes", since="2026-01-01T00:00:00Z")
        _h, rows = parse(resp)
        expected = listing(c, "/changes", page_size=500, since="2026-01-01T00:00:00Z")["items"]
    assert resp.headers["x-export-truncated"] == "true"
    assert [int(r["id"]) for r in rows] == [e["id"] for e in expected][:10]


def test_a_capped_scoped_export_is_still_scoped(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_CAP", 5)
    with api_client(fleet_db_fresh, role="t_ex_scoped", permissions=PERMS, scope_tags=(SCOPED_TAG,)) as c:
        resp = fetch(c, "assets")
        _h, rows = parse(resp)
    assert len(rows) == 5 and resp.headers["x-export-truncated"] == "true"
    assert {r["name"] for r in rows} <= set(EDGE_SERVERS) | set(EDGE_DEVICES)


# ── hostile query strings ────────────────────────────────────────────

HOSTILE = ["\x00", "a\x00b", "'; DROP TABLE servers; --", "%", "\\", "é" * 300, "{{7*7}}", "$1"]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_filter_text_is_data_never_a_server_error(exporter, pinned_clock, value):
    from urllib.parse import urlencode
    for params in ({"dataset": "assets", "q": value}, {"dataset": "software", "q": value}, {"dataset": "software", "publisher": value},
                   {"dataset": "installations", "product": value}, {"dataset": "installations", "product": "git", "version": value},
                   {"dataset": "changes", "q": value}, {"dataset": "changes", "subject_key": value}, {"dataset": "hardware", "vendor": value},
                   {"dataset": "assets", "vendor": value}):
        resp = exporter.get(f"{EXPORT}?{urlencode(params)}")
        assert resp.status_code in (200, 404, 422), (params, resp.status_code, resp.text[:200])
    assert len(parse(fetch(exporter, "assets"))[1]) == 145
