"""Golden fixtures: the real response of every phase-1 Inventory endpoint for the seeded fleet.

The dashboard agents build against ``dashboard/src/pages/inventory/fixtures/*.json`` (and the mock API
serves them), so those files must be what the server really returns. This module runs each request through
the real HTTP stack (``api_client``: routing, validation, response models, the permission chain) against
the deterministic fleet of ``inventory_fleet`` with the clock pinned, and compares the JSON with the
checked-in file. Drift fails the build; ``ZENPLUS_UPDATE_FIXTURES=1`` rewrites the files:

    ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py

``index.json`` is the manifest: for every fixture the request that produced it and the caller's scope, so a
mock server can route ``GET /api/v1/inventory/...`` to the right file without guessing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.services import inventory_service
from inventory_fleet import (
    EDGE_DEVICES, EDGE_SERVERS, FIXED_NOW, SCOPED_TAG, aid, api_client, fleet_db, fleet_info, fleet_template,
    pinned_clock,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "dashboard" / "src" / "pages" / "inventory" / "fixtures"
UPDATE = os.environ.get("ZENPLUS_UPDATE_FIXTURES", "").strip() == "1"
MAX_TOTAL_BYTES = 400 * 1024
API = "/api/v1/inventory"

UNRESTRICTED = {"role": "t_golden_all", "scope": ()}
SCOPED = {"role": "t_golden_scoped", "scope": (SCOPED_TAG,)}

# name -> (caller, path under /api/v1/inventory, query params). Asset flyouts are addressed by fleet name.
CASES: dict[str, dict[str, Any]] = {
    "summary": {"who": UNRESTRICTED, "path": "/summary", "params": {}},
    "assets": {"who": UNRESTRICTED, "path": "/assets", "params": {"page_size": 50}},
    "facets": {"who": UNRESTRICTED, "path": "/assets/facets", "params": {}},
    "asset-server": {"who": UNRESTRICTED, "server": EDGE_SERVERS[1], "path": "/assets/server/{id}", "params": {}},
    "asset-device": {"who": UNRESTRICTED, "device": "sw-c9-04", "path": "/assets/device/{id}", "params": {}},
    "asset-device-ha": {"who": UNRESTRICTED, "device": EDGE_DEVICES[1], "path": "/assets/device/{id}", "params": {}},
    "os": {"who": UNRESTRICTED, "path": "/os", "params": {}},
    "hardware": {"who": UNRESTRICTED, "path": "/hardware", "params": {"vendor": "Cisco"}},
    "changes": {"who": UNRESTRICTED, "path": "/changes", "params": {"page_size": 50}},
    "summary-scoped": {"who": SCOPED, "path": "/summary", "params": {}},
    "assets-scoped": {"who": SCOPED, "path": "/assets", "params": {"page_size": 50}},
    "changes-scoped": {"who": SCOPED, "path": "/changes", "params": {"page_size": 50}},
    # software catalog, trends and settings (the second API wave)
    "software": {"who": UNRESTRICTED, "path": "/software", "params": {"page_size": 50}},
    "software-publishers": {"who": UNRESTRICTED, "path": "/software/publishers", "params": {}},
    "software-product": {"who": UNRESTRICTED, "path": "/software/git", "params": {}},
    "software-installations": {"who": UNRESTRICTED, "path": "/software/git/installations", "params": {"page_size": 50}},
    "trends": {"who": UNRESTRICTED, "path": "/trends", "params": {"metric": "assets"}},
    "settings": {"who": UNRESTRICTED, "path": "/settings", "params": {}},
    "software-scoped": {"who": SCOPED, "path": "/software", "params": {"page_size": 50}},
    "software-product-scoped": {"who": SCOPED, "path": "/software/git", "params": {}},
    "software-installations-scoped": {"who": SCOPED, "path": "/software/git/installations", "params": {"page_size": 50}},
    "trends-scoped": {"who": SCOPED, "path": "/trends", "params": {"metric": "assets"}},
}

# The CSV sample: the export of the asset list for the tag the scoped fixtures use, as an unrestricted exporter.
CSV_CASE = {"name": "export-assets", "path": "/export", "params": {"dataset": "assets", "tag": SCOPED_TAG}}

ALL_PERMISSIONS = ["inventory.view"]


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _request_path(case: dict[str, Any], fleet_info) -> str:
    path = case["path"]
    if "{id}" in path:
        ids = fleet_info.server_ids if "server" in case else fleet_info.device_ids
        path = path.replace("{id}", ids[case.get("server") or case.get("device")])
    return path


def _fetch(db, name: str, fleet_info) -> tuple[Any, dict[str, Any]]:
    case = CASES[name]
    who = case["who"]
    with api_client(db, role=who["role"], permissions=ALL_PERMISSIONS, scope_tags=who["scope"],
                    username=f"golden-{name}") as c:
        resp = c.get(API + _request_path(case, fleet_info), params=case["params"])
    assert resp.status_code == 200, (name, resp.status_code, resp.text[:300])
    return resp.json(), case


def _manifest(fleet_info) -> dict[str, Any]:
    entries = {}
    for name, case in CASES.items():
        path = _request_path(case, fleet_info)
        entries[name] = {
            "file": f"{name}.json",
            "request": f"GET {API}{path}",
            "params": case["params"],
            "caller": ("scoped to " + ", ".join(case["who"]["scope"])) if case["who"]["scope"] else "unrestricted",
        }
    # Not in "fixtures": every entry there is a JSON file a mock server parses.
    samples = {CSV_CASE["name"]: {
        "file": f"{CSV_CASE['name']}.csv",
        "request": f"GET {API}{CSV_CASE['path']}",
        "params": CSV_CASE["params"],
        "caller": "unrestricted, with inventory.export",
        "content_type": "text/csv; charset=utf-8",
        "note": "UTF-8 with a byte-order mark, CRLF line ends; the response also carries Content-Disposition, X-Export-Rows and, past 200,000 rows, X-Export-Truncated: true",
    }}
    return {
        "about": "Golden responses of the phase-1 Inventory API for the seeded fleet (server/tests/inventory_fleet.py). "
                 "Regenerate with ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py.",
        "pinned_now": FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scoped_tag": SCOPED_TAG,
        "assets": {
            "servers": fleet_info.expected["servers"],
            "devices": fleet_info.expected["devices_in_union"],
            "total": fleet_info.assets_total,
            "snmp_twins_not_counted": fleet_info.expected["linked"],
        },
        "fixtures": entries,
        "samples": samples,
    }


def _check(path: Path, data: Any) -> None:
    text = _canonical(data)
    if UPDATE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"{path.name} is missing; run with ZENPLUS_UPDATE_FIXTURES=1 to create it"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == data, (
        f"{path.name} drifted from the server's response; if the change is intended, regenerate with "
        "ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py and review the diff")


@pytest.mark.parametrize("name", list(CASES))
def test_golden_fixture(name, fleet_db, fleet_info, pinned_clock):
    data, _case = _fetch(fleet_db, name, fleet_info)
    _check(FIXTURE_DIR / f"{name}.json", data)


def _csv_response(db) -> bytes:
    with api_client(db, role="t_golden_export", permissions=["inventory.view", "inventory.export"], username="golden-export") as c:
        resp = c.get(API + CSV_CASE["path"], params=CSV_CASE["params"])
    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-disposition"] == 'attachment; filename="zenplus-inventory-assets-20261008.csv"'
    return resp.content


def test_golden_csv_sample(fleet_db, pinned_clock):
    """The export sample the dashboard's mock serves: byte for byte what the server writes (BOM, CRLF and all)."""
    path = FIXTURE_DIR / f"{CSV_CASE['name']}.csv"
    data = _csv_response(fleet_db)
    if UPDATE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
        return
    assert path.exists(), f"{path.name} is missing; run with ZENPLUS_UPDATE_FIXTURES=1 to create it"
    assert path.read_bytes() == data, (f"{path.name} drifted from the server's export; if the change is intended, regenerate with "
                                       "ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py and review the diff")


def test_golden_manifest(fleet_info):
    _check(FIXTURE_DIR / "index.json", _manifest(fleet_info))


def test_the_csv_sample_is_the_scoped_teams_assets():
    raw = (FIXTURE_DIR / f"{CSV_CASE['name']}.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and raw.endswith(b"\r\n")
    lines = raw.decode("utf-8-sig").split("\r\n")[:-1]
    assert lines[0].startswith("key,kind,name,ip,") and len(lines) == 1 + _load("assets-scoped")["total"]


def test_no_stale_or_unlisted_fixture_files():
    if UPDATE:
        for extra in FIXTURE_DIR.glob("*.json"):
            if extra.stem not in CASES and extra.name != "index.json":
                extra.unlink()
    present = {p.stem for p in FIXTURE_DIR.glob("*.json")}
    assert present == set(CASES) | {"index"}, (
        f"fixture files and cases differ: only on disk {sorted(present - set(CASES) - {'index'})}, "
        f"only in cases {sorted(set(CASES) - present)}")


def test_fixtures_stay_small():
    total = sum(p.stat().st_size for p in FIXTURE_DIR.glob("*.json"))
    assert total < MAX_TOTAL_BYTES, f"fixtures are {total} bytes; keep them under {MAX_TOTAL_BYTES}"


# ── the fixtures are the contract: sanity checks on what the dashboard will rely on ─────────

def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_fixtures_are_deterministic_across_two_requests(fleet_db, fleet_info, pinned_clock):
    first, _ = _fetch(fleet_db, "summary", fleet_info)
    second, _ = _fetch(fleet_db, "summary", fleet_info)
    assert first == second
    first, _ = _fetch(fleet_db, "changes", fleet_info)
    second, _ = _fetch(fleet_db, "changes", fleet_info)
    assert first == second


def test_the_fixture_fleet_has_the_shape_the_contract_describes():
    s = _load("summary")
    assert s["generated_at"] == FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert s["scope"] == {"restricted": False, "tags": []}
    assert s["assets"]["total"] == s["assets"]["server"] + s["assets"]["device"]
    assert s["lifecycle"] is None
    # fleet-wide figures are not given to a scoped user, who sees fewer assets
    scoped = _load("summary-scoped")
    assert scoped["scope"] == {"restricted": True, "tags": [SCOPED_TAG]}
    assert 0 < scoped["assets"]["total"] < s["assets"]["total"]
    a, sa = _load("assets"), _load("assets-scoped")
    assert a["total"] == s["assets"]["total"] and sa["total"] == scoped["assets"]["total"]
    assert all(SCOPED_TAG in row["tags"] for row in sa["items"])
    # the flyouts are the named assets
    srv = _load("asset-server")
    assert srv["asset"]["kind"] == "server" and srv["server"] is not None and srv["device"] is None
    dev = _load("asset-device")
    assert dev["asset"]["kind"] == "device" and dev["device"] is not None and dev["server"] is None
    assert _load("asset-device-ha")["device"]["ha"] is not None


def test_the_software_fixtures_describe_the_catalog_the_pages_are_built_on():
    s, sw = _load("summary"), _load("software")
    assert sw["total"] == s["software"]["products"] == len(sw["items"]) and sw["page"] == 1
    assert sum(r["installs"] for r in sw["items"]) == s["software"]["installs"]
    assert [r["hosts"] for r in sw["items"]] == sorted((r["hosts"] for r in sw["items"]), reverse=True)
    assert sw["hidden_categories"] == sorted(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    assert sw["hidden_products"] == 9 and sw["unmatched_titles"] == 0
    row = next(r for r in sw["items"] if r["product_key"] == "google-chrome")
    assert row["norm_status"] == "normalized" and row["distribution"]["top"][0]["rank"] == 1 and row["last_change"] is not None
    assert _load("software-publishers")["distinct"] == s["software"]["publishers"]
    product = _load("software-product")
    assert product["header"]["product_key"] == "git" and product["header"]["mixed_schemes"] is True
    assert {v["scheme"] for v in product["versions"]} == {"semverish", "dpkg"} and product["header"]["version_count"] == len(product["versions"])
    inst = _load("software-installations")
    assert inst["total"] == len(inst["items"]) == product["header"]["installs"]
    # a scoped caller sees fewer products, fewer hosts, and the page says so
    sc = _load("software-scoped")
    assert sc["scope"] == {"restricted": True, "tags": [SCOPED_TAG]} and 0 < sc["total"] < sw["total"]
    spr, sinst = _load("software-product-scoped"), _load("software-installations-scoped")
    assert 0 < spr["header"]["hosts"] < product["header"]["hosts"] and sinst["total"] == spr["header"]["installs"]
    assert all(r["asset_name"] in EDGE_SERVERS for r in sinst["items"])


def test_the_trend_and_settings_fixtures():
    t = _load("trends")
    assert t["restricted"] is False and t["metric"] == "assets" and t["days_with_data"] >= 3 and t["series"]
    assert all(len(s["points"]) == t["days_with_data"] for s in t["series"])
    scoped = _load("trends-scoped")
    assert scoped["restricted"] is True and scoped["series"] == [] and scoped["days_with_data"] == 0
    st = _load("settings")
    assert st["settings"] == st["defaults"] and st["updated_at"] is None and st["changed"] == []


# ── the contract document stays true to the code and to the fixtures ─────────────────────

CONTRACT = REPO_ROOT / "Documentation" / "Inventory" / "05-P1-API-CONTRACT.md"
# The JSON examples of the contract, in document order, and the golden file each one is cut from.
EXAMPLE_ORDER = ["summary", "assets", "facets", "asset-server", "asset-device", "asset-device-ha", "os", "hardware", "changes",
                 "software", "software-publishers", "software-product", "software-installations", "trends", "settings"]


def _json_blocks() -> list[Any]:
    import re
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", CONTRACT.read_text(encoding="utf-8"), re.S)]


def _is_cut_of(example: Any, full: Any) -> bool:
    """``example`` is ``full`` with some list items dropped (never a changed value)."""
    if isinstance(example, dict):
        return (isinstance(full, dict) and set(example) == set(full)
                and all(_is_cut_of(v, full[k]) for k, v in example.items()))
    if isinstance(example, list):
        return isinstance(full, list) and all(any(_is_cut_of(x, y) for y in full) for x in example)
    return example == full


def test_every_json_example_of_the_contract_is_a_real_response_cut_short():
    blocks = _json_blocks()
    assert len(blocks) == len(EXAMPLE_ORDER)
    for name, block in zip(EXAMPLE_ORDER, blocks):
        assert _is_cut_of(block, _load(name)), f"the {name} example in the contract is not a cut of {name}.json; rebuild the document"


def test_the_contract_names_every_route_and_every_filter():
    import dataclasses

    from app.core.route_policy import _iter_effective_routes
    from app.main import app
    from app.services import inventory_query as iq

    text = CONTRACT.read_text(encoding="utf-8")
    for r in _iter_effective_routes(app):
        path = getattr(r, "path_format", None) or r.path
        if path.startswith(API):
            documented = path.replace("{asset_id}", "{id}")
            assert documented in text, f"{path} is not in the contract"
    for f in dataclasses.fields(iq.AssetFilters):
        assert f"`{f.name}`" in text, f"filter {f.name} is not in the contract"
    for key in iq.HEALTH_KEYS + iq.SORT_KEYS:
        assert f"`{key}`" in text, f"{key} is not in the contract"
    for name in CASES:
        assert f"`{name}.json`" in text, f"{name}.json is not listed in the contract"
    assert f"`{CSV_CASE['name']}.csv`" in text
    # the second wave: the software filters and sorts, the settings, the metrics, the export datasets and its caps
    from app.services import inventory_export as export
    from app.services import inventory_service as svc
    from app.services import inventory_software_query as isq
    from app.services import inventory_trends_settings as its
    for f in dataclasses.fields(isq.CatalogFilters):
        assert f"`{f.name}`" in text, f"software filter {f.name} is not in the contract"
    for key in (*isq.CATALOG_SORTS, *isq.INSTALL_SORTS, *svc.SNAPSHOT_METRICS, *its.SETTINGS_FIELDS, *export.DATASETS,
                *isq.NORM_STATUSES, "X-Export-Truncated", "inventory.export", "inventory.manage", "200,000"):
        assert f"`{key}`" in text or key in text, f"{key} is not in the contract"
