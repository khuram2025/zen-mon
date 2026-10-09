"""HTTP-level tests of the phase-1 Inventory read API, and the backend half of the drill-down contract.

Requests go through the real application (routing, query validation, response models, the permission
chain) against the seeded fleet of ``inventory_fleet`` on the private PostgreSQL cluster, with the clock
pinned. The query semantics themselves are covered in test_inventory_query.py; this file checks what a
client can observe: statuses, error bodies, the one filter vocabulary shared by every list endpoint, the
cache, and, above all, T-UI2 (doc 04): every number the dashboard turns into a link equals the ``total``
of the list that link opens.

The drill-down helpers below map each number of ``/summary``, ``/os``, ``/hardware`` and ``/changes`` to
the filters the contract (05-P1-API-CONTRACT.md, "Drill-down map") names for it. They are the executable
form of that table: change one and the other must change with it.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Iterator

import pytest

from app.services import inventory_query as iq
from inventory_fleet import (
    FIXED_NOW, SCOPED_TAG, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
def client(fleet_db):
    with api_client(fleet_db, role="t_api_view", permissions=["inventory.view"]) as c:
        yield c


def get(client, path: str, expect: int = 200, **params) -> Any:
    resp = client.get(API + path, params={k: v for k, v in params.items() if v is not None})
    assert resp.status_code == expect, (path, params, resp.status_code, resp.text[:400])
    return resp.json()


def total(client, **params) -> int:
    return get(client, "/assets", page_size=1, **params)["total"]


# ════════════════════════════════════════════════════════════════════
# Shapes and errors
# ════════════════════════════════════════════════════════════════════

def test_assets_list_shape_and_paging_envelope(client, pinned_clock):
    body = get(client, "/assets", page_size=7, page=2, sort="vendor", order="desc")
    assert set(body) == {"generated_at", "items", "total", "page", "page_size", "sort", "order"}
    assert (body["page"], body["page_size"], body["sort"], body["order"]) == (2, 7, "vendor", "desc")
    assert len(body["items"]) == 7 and body["total"] == 145
    assert ISO_Z.match(body["generated_at"])
    row = body["items"][0]
    assert UUID_RE.match(row["id"]) and row["key"] == f"{row['kind']}:{row['id']}"
    assert set(row) >= {"name", "ip", "vendor", "model", "serial", "os_family", "os_version", "site", "tags", "sources",
                        "inventory_at", "inventory_age_s", "freshness", "software_truncated"}


def test_every_list_parameter_has_a_documented_default(client, pinned_clock):
    body = get(client, "/assets")
    assert (body["page"], body["page_size"], body["sort"], body["order"]) == (1, 50, "name", "asc")
    names = [r["name"].lower() for r in body["items"]]
    assert names == sorted(names)


@pytest.mark.parametrize("path,params", [
    ("/assets", {"kind": "router"}), ("/assets", {"freshness": "old"}), ("/assets", {"health": "broken"}),
    ("/assets", {"sort": "bogus"}), ("/assets", {"order": "sideways"}), ("/assets", {"page": 0}),
    ("/assets", {"page_size": 0}), ("/assets", {"page_size": 501}), ("/assets", {"page": 100_001}),
    ("/assets", {"product_version": "1.0"}), ("/assets", {"memory": "huge"}), ("/assets", {"disks": "7"}),
    ("/assets", {"security_agent": "yes"}), ("/assets", {"module_class": "port"}),
    ("/assets", {"q": "x" * 201}),
    ("/assets/facets", {"kind": "router"}), ("/assets/facets", {"product_version": "1.0"}),
    ("/os", {"kind": "router"}),
    ("/changes", {"category": "cars"}), ("/changes", {"action": "mutated"}), ("/changes", {"kind": "router"}),
    ("/changes", {"asset": "server:not-a-uuid"}), ("/changes", {"asset": "router:00000000-0000-0000-0000-000000000000"}),
    ("/changes", {"label": "sidegrade"}), ("/changes", {"tz": "not a zone!"}), ("/changes", {"tz": "../etc/passwd"}), ("/changes", {"page_size": 501}),
    ("/changes", {"since": "2026-10-08T00:00:00Z", "until": "2026-10-07T00:00:00Z"}),
    ("/changes", {"since": "yesterday"}),
])
def test_bad_parameters_are_422_with_a_reason(client, path, params):
    body = get(client, path, expect=422, **params)
    assert body["detail"]


HOSTILE = ["\x00", "a\x00b", "'; DROP TABLE servers; --", "%", "\\", "\u00e9" * 300, "\U0001F600", "\n\r\t", "{{7*7}}", ":f_q", "$1"]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_text_is_data_never_a_server_error(client, value, pinned_clock):
    """NUL bytes, quotes, wildcards, bind-looking text and oversized strings in any text filter: 200 or 422, never 5xx."""
    from urllib.parse import quote, urlencode

    def call(path: str, **params):
        return client.get(f"{API}{path}?{urlencode(params)}")        # percent-encoded: the client would refuse raw control bytes

    for param in ("q", "vendor", "model", "site", "os_family", "os_product", "os_release", "os_version", "product",
                  "cpu_model", "virtualization", "module_model", "source", "tag"):
        for path in ("/assets", "/assets/facets"):
            resp = call(path, **{param: value})
            assert resp.status_code in (200, 422), (path, param, repr(value), resp.status_code, resp.text[:200])
    for params in ({"q": value}, {"subject_key": value}, {"asset": value}, {"tz": value}):
        resp = call("/changes", **params)
        assert resp.status_code in (200, 422), (params, resp.status_code, resp.text[:200])
    assert call("/hardware", vendor=value).status_code in (200, 422)
    assert client.get(f"{API}/assets/server/{quote(value, safe='')}").status_code in (404, 422)
    assert total(client) == 145                                       # the data is untouched


@pytest.mark.asyncio
async def test_a_lone_surrogate_cannot_reach_the_database_driver(fleet_db, pinned_clock):
    from inventory_fleet import sessions
    async with sessions(fleet_db) as sm:
        async with sm() as s:
            r = await iq.list_assets(s, iq.AssetFilters(q="\ud800x", vendor="\ud800"), scope=None)
    assert r["total"] == 0


def test_unknown_values_are_values_not_errors(client, pinned_clock):
    """A free-text filter that matches nothing is an empty list, not an error."""
    for params in ({"vendor": "no such vendor"}, {"os_release": "99.9"}, {"tag": ["nope"]}, {"product": "no-such-product"},
                   {"q": "zzzzzz"}, {"site": "Atlantis"}, {"source": "carrier-pigeon"}):
        body = get(client, "/assets", **params)
        assert body["items"] == [] and body["total"] == 0, params


def test_flyout_404_is_one_body_for_unknown_malformed_and_wrong_kind(client, fleet_info, pinned_clock):
    known_server = fleet_info.server_ids["srv-w22-01"]
    known_device = fleet_info.device_ids["fg-01"]
    bodies = []
    for kind, ident in (("server", "00000000-0000-0000-0000-000000000000"), ("device", "00000000-0000-0000-0000-000000000000"),
                        ("server", "not-a-uuid"), ("router", known_server), ("device", known_server), ("server", known_device)):
        resp = client.get(f"{API}/assets/{kind}/{ident}")
        assert resp.status_code == 404, (kind, ident)
        bodies.append(resp.json())
    assert all(b == bodies[0] for b in bodies) and bodies[0] == {"detail": "Asset not found"}


def test_flyout_of_each_kind(client, fleet_info, pinned_clock):
    srv = get(client, f"/assets/server/{fleet_info.server_ids['srv-w22-01']}")
    assert srv["asset"]["kind"] == "server" and srv["server"] and srv["device"] is None
    assert srv["links"] == {"server_id": fleet_info.server_ids["srv-w22-01"], "device_id": None}
    dev = get(client, f"/assets/device/{fleet_info.device_ids['sw-c9-04']}")
    assert dev["asset"]["kind"] == "device" and dev["device"] and dev["server"] is None
    assert dev["links"] == {"server_id": None, "device_id": fleet_info.device_ids["sw-c9-04"]}
    assert {m["class"] for m in dev["device"]["modules"]} >= {"chassis", "powerSupply"}
    for body in (srv, dev):
        assert [i["field"] for i in body["identity"]] == [
            "vendor", "model", "serial", "os_family", "os_product", "os_release", "os_version", "os_build", "os_edition"]
        assert len(body["changes"]["items"]) <= 10 and body["changes"]["total"] >= len(body["changes"]["items"])


def test_flyout_of_a_linked_server_links_both_detail_pages(client, fleet_info, pinned_clock):
    name = "srv-w22-02"
    body = get(client, f"/assets/server/{fleet_info.server_ids[name]}")
    assert body["links"] == {"server_id": fleet_info.server_ids[name], "device_id": fleet_info.linked_device_ids[name]}
    # the SNMP side of a server is not an asset of its own: asking for it by id is a 404 (it is the server's)
    get(client, f"/assets/device/{fleet_info.linked_device_ids[name]}", expect=404)


def test_the_os_kind_filter_narrows_the_tree(client, pinned_clock):
    every, servers, devices = (get(client, "/os", **kw) for kw in ({}, {"kind": "server"}, {"kind": "device"}))
    assert every["total"] == servers["total"] + devices["total"] == 145
    assert (every["kind"], servers["kind"], devices["kind"]) == (None, "server", "device")
    assert {f["family"] for f in devices["families"]} & {"windows", "linux"} == set()


def test_the_hardware_matrix_needs_a_vendor_and_is_case_insensitive(client, pinned_clock):
    assert get(client, "/hardware")["matrix"] is None
    m = get(client, "/hardware", vendor="cISCO")["matrix"]
    assert m["vendor"] == "Cisco" and m["models"] and m["releases"]
    assert get(client, "/hardware", vendor="no such vendor")["matrix"] == {"vendor": "no such vendor", "releases": [], "models": []}


# ════════════════════════════════════════════════════════════════════
# One vocabulary for the list and the facets
# ════════════════════════════════════════════════════════════════════

FILTER_SAMPLES: dict[str, Any] = {
    "q": "fg-", "kind": "device", "os_family": "windows", "os_product": "Windows Server", "os_release": "2019",
    "os_version": "17.9.4a", "vendor": "Cisco", "model": "C9300-48P", "site": "HQ", "tag": ["prod", "pci"],
    "source": "snmp", "freshness": "stale_or_never", "health": "missing_serial", "product": "google-chrome",
    "product_version": "152.0.8001.12", "cpu_model": "unknown", "memory": "le16", "disks": "3-4",
    "virtualization": "vmware", "security_agent": "present", "module_class": "powerSupply", "module_model": "unknown",
}


def test_the_filter_vocabulary_is_the_dataclass_fields(client):
    """Adding a filter to the dataclass without a route parameter (or the reverse) is caught here."""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(iq.AssetFilters)}
    assert set(FILTER_SAMPLES) == fields


@pytest.mark.parametrize("name", sorted(FILTER_SAMPLES))
def test_every_filter_name_is_accepted_by_the_list_and_the_facets(client, name, pinned_clock):
    value = FILTER_SAMPLES[name]
    if name == "product_version":
        params = {"product": "google-chrome", "product_version": value}
    else:
        params = {name: value}
    lst = get(client, "/assets", page_size=1, **params)
    fac = get(client, "/assets/facets", **params)
    assert fac["total"] == lst["total"], (name, fac["total"], lst["total"])


def test_all_filters_at_once_are_a_valid_request_that_matches_nothing(client, pinned_clock):
    params = {k: v for k, v in FILTER_SAMPLES.items() if k != "product_version"}
    params["product_version"] = "1"
    body = get(client, "/assets", **params)
    assert body["total"] == 0


def test_facet_counts_are_what_choosing_the_value_would_total(client, pinned_clock):
    base = {"kind": "device", "freshness": "fresh"}
    facets = get(client, "/assets/facets", **base)
    assert facets["total"] == total(client, **base)
    for name in ("os_family", "vendor", "model", "site", "source", "health", "freshness", "kind"):
        for item in facets["facets"][name]["items"][:6]:
            extra = {"health": item["value"]} if name == "health" else {name: item["value"]}
            assert total(client, **{**base, **extra}) == item["count"], (name, item)


# ════════════════════════════════════════════════════════════════════
# Caching
# ════════════════════════════════════════════════════════════════════

def test_summary_is_cached_for_sixty_seconds_and_can_be_cleared(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_api_view", permissions=["inventory.view"]) as c:
        first = get(c, "/summary")
        fleet_db_fresh.execute("DELETE FROM servers WHERE display_name = 'srv-w22-01'")
        again = get(c, "/summary")
        assert again == first and again["assets"]["server"] == 60            # served from the process cache
        assert get(c, "/assets", page_size=1)["total"] == 144               # lists are never cached
        iq.clear_caches()
        fresh = get(c, "/summary")
        assert fresh["assets"]["server"] == 59


# ════════════════════════════════════════════════════════════════════
# T-UI2: the drill-down contract, backend half
# ════════════════════════════════════════════════════════════════════

Link = tuple[str, int, dict[str, Any]]      # (label, the number on the page, the filters of the list behind it)


def _os_path(family: dict, product: dict | None = None, release: dict | None = None, version: dict | None = None,
             kind: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"os_family": family["family"]}
    if kind:
        p["kind"] = kind
    if product is not None:
        p["os_product"] = product["product"] or "unknown"
    if release is not None:
        p["os_release"] = release["release"] or "unknown"
    if version is not None:
        p["os_version"] = version["version"] or "unknown"
    return p


def summary_asset_links(s: dict[str, Any]) -> Iterator[Link]:
    """Every number of /summary that opens the Assets list. Documented in the contract's drill-down map."""
    a, cov = s["assets"], s["coverage"]
    yield "assets.total", a["total"], {}
    yield "assets.server", a["server"], {"kind": "server"}
    yield "assets.device", a["device"], {"kind": "device"}
    for k in ("fresh", "aging", "stale", "never", "not_applicable"):
        yield f"coverage.{k}", cov[k], {"freshness": k}
    yield ("coverage.fresh+aging+stale+never+not_applicable",
           cov["fresh"] + cov["aging"] + cov["stale"] + cov["never"] + cov.get("not_applicable", 0), {})
    for i, row in enumerate(s["os"]["top"]):
        yield (f"os.top[{i}] {row['family']}/{row['product']}/{row['release']}", row["count"],
               {"os_family": row["family"], "os_product": row["product"] or "unknown", "os_release": row["release"] or "unknown"})
    yield "os.unknown", s["os"]["unknown"], {"os_family": "unknown"}
    for section in ("top", "sprawl"):
        for i, row in enumerate(s["software"][section]):
            yield f"software.{section}[{i}] {row['product_key']}", row["hosts"], {"product": row["product_key"]}
    sec = s["software"]["security_agent_coverage"]
    yield "security_agent_coverage.covered", sec["covered"], {"security_agent": "present"}
    yield "security_agent_coverage.missing", sec["missing"], {"security_agent": "missing"}
    yield "security_agent_coverage.unknown", sec["unknown"], {"security_agent": "unknown"}
    yield "security_agent_coverage.total", sec["total"], {"kind": "server"}
    yield "hardware.devices", s["hardware"]["devices"], {"kind": "device"}
    for i, row in enumerate(s["hardware"]["top_models"]):
        yield f"hardware.top_models[{i}] {row['vendor']} {row['model']}", row["count"], {
            "kind": "device", "vendor": row["vendor"], "model": row["model"]}
    for key, n in s["health"].items():
        if key == "not_collected_by_design":
            yield f"health.{key}", n["count"], {"freshness": "not_applicable"}
        else:
            yield f"health.{key}", n, {"health": key}


def summary_change_links(s: dict[str, Any]) -> Iterator[Link]:
    """Every number of changes_7d: the Changes list with the window the summary used."""
    c = s["changes_7d"]
    window = {"since": c["since"]}
    yield "changes_7d.total", c["total"], dict(window)
    sw = c["software"]
    yield "changes_7d.software.added", sw["added"], {**window, "category": "software", "action": "added"}
    yield "changes_7d.software.removed", sw["removed"], {**window, "category": "software", "action": "removed"}
    yield "changes_7d.software.changed", sw["changed"], {**window, "category": "software", "action": "changed"}
    yield "changes_7d.software.upgraded", sw["upgraded"], {**window, "category": "software", "action": "changed", "label": "upgrade"}
    yield "changes_7d.software.downgraded", sw["downgraded"], {**window, "category": "software", "action": "changed", "label": "downgrade"}
    for cat in ("os", "firmware", "hardware"):
        yield f"changes_7d.{cat}", c[cat], {**window, "category": cat}


def os_links(tree: dict[str, Any]) -> Iterator[Link]:
    kind = tree["kind"]
    for fam in tree["families"]:
        yield f"os:{fam['family']}", fam["count"], _os_path(fam, kind=kind)
        for prod in fam["products"]:
            yield f"os:{fam['family']}/{prod['product']}", prod["count"], _os_path(fam, prod, kind=kind)
            for rel in prod["releases"]:
                yield f"os:{fam['family']}/{prod['product']}/{rel['release']}", rel["count"], _os_path(fam, prod, rel, kind=kind)
                for ver in rel["versions"]:
                    yield (f"os:{fam['family']}/{prod['product']}/{rel['release']}/{ver['version']}", ver["count"],
                           _os_path(fam, prod, rel, ver, kind=kind))
                cur = rel["currency"]
                if cur:
                    release_path = _os_path(fam, prod, rel, kind=kind)
                    yield (f"os:{fam['family']}/{prod['product']}/{rel['release']} currency sum",
                           cur["latest"] + cur["previous"] + cur["older"] + cur["unknown"], release_path)
                    # the two newest buckets are one version each (unless two spellings compare equal, which the fleet has none of)
                    yield (f"os:{fam['family']}/{prod['product']}/{rel['release']} currency.latest", cur["latest"],
                           {**release_path, "os_version": cur["latest_version"]})
                    if cur["previous_version"]:
                        yield (f"os:{fam['family']}/{prod['product']}/{rel['release']} currency.previous", cur["previous"],
                               {**release_path, "os_version": cur["previous_version"]})


def hardware_links(h: dict[str, Any]) -> Iterator[Link]:
    net = h["network"]
    yield "network.devices", net["devices"], {"kind": "device"}
    yield "network.models_known+unknown_model_devices", net["unknown_model_devices"], {"kind": "device", "model": "unknown"}
    for v in net["vendors"]:
        base: dict[str, Any] = {"kind": "device", "vendor": v["vendor"] if v["vendor"] is not None else "unknown"}
        yield f"vendor:{v['vendor']}", v["count"], base
        yield f"vendor:{v['vendor']} serial_missing", v["serial_missing"], {**base, "health": "missing_serial"}
        yield f"vendor:{v['vendor']} not_collected", v["not_collected"], {**base, "freshness": "not_applicable"}
        if v["vendor"] is not None:
            yield f"vendor:{v['vendor']} unknown_model_devices", v["unknown_model_devices"], {**base, "model": "unknown"}
    for m in net["models"]:
        base = {"kind": "device", "vendor": m["vendor"], "model": m["model"] if m["model"] is not None else "unknown"}
        yield f"model:{m['vendor']}/{m['model']}", m["count"], base
        yield f"model:{m['vendor']}/{m['model']} serial_missing", m["serial_missing"], {**base, "health": "missing_serial"}
        yield f"model:{m['vendor']}/{m['model']} not_collected", m["not_collected"], {**base, "freshness": "not_applicable"}
    if h["matrix"]:
        mx = h["matrix"]
        for row in mx["models"]:
            base = {"kind": "device", "vendor": mx["vendor"], "model": row["model"] if row["model"] is not None else "unknown"}
            yield f"matrix:{mx['vendor']}/{row['model']}", row["count"], base
            for cell in row["cells"]:
                yield f"matrix:{mx['vendor']}/{row['model']}/{cell['release']}", cell["count"], {**base, "os_release": cell["release"]}
    for g in h["modules"]:
        p: dict[str, Any] = {"module_class": g["class"], "module_model": g["model"] or "unknown"}
        yield f"module:{g['class']}/{g['model']} devices", g["devices"], p
    srv = h["servers"]
    yield "servers.total", srv["total"], {"kind": "server"}
    for item in srv["cpu_models"]:
        yield f"cpu:{item['value']}", item["count"], {"cpu_model": item["value"]}
    for dim, key in (("memory", "memory"), ("disks", "disks"), ("virtualization", "virtualization")):
        for item in srv[dim] or []:
            yield f"{dim}:{item['value']}", item["count"], {key: item["value"]}


def _assert_links(client, links: Iterator[Link], path: str = "/assets", min_links: int = 1) -> int:
    n = 0
    for label, number, params in links:
        params = {k: v for k, v in params.items()}
        got = get(client, path, page_size=1, **params)["total"]
        assert got == number, f"{label}: the page says {number}, the list behind {params} totals {got}"
        n += 1
    assert n >= min_links
    return n


def test_every_summary_number_equals_the_total_of_its_list(client, pinned_clock):
    s = get(client, "/summary")
    n = _assert_links(client, summary_asset_links(s), min_links=40)
    assert n >= 40


def test_every_summary_change_number_equals_the_total_of_its_list(client, pinned_clock):
    s = get(client, "/summary")
    n = _assert_links(client, summary_change_links(s), path="/changes", min_links=8)
    assert n == 9
    assert s["changes_7d"]["total"] > 0


def test_summary_counts_of_groups_equal_the_nodes_of_their_pages(client, pinned_clock):
    s = get(client, "/summary")
    tree = get(client, "/os")
    # BN-12: the Overview's figures are the row counts of the OS page: every family node (the unknown family included)
    # and every release node (a missing release is a node of its own)
    assert s["os"]["families"] == len(tree["families"])
    assert s["os"]["releases"] == sum(len(p["releases"]) for f in tree["families"] for p in f["products"])
    hw = get(client, "/hardware")
    assert s["hardware"]["devices"] == hw["network"]["devices"]
    assert s["hardware"]["vendors"] == len([v for v in hw["network"]["vendors"] if v["vendor"] is not None])
    assert s["hardware"]["models"] == len([m for m in hw["network"]["models"] if m["model"] is not None])


@pytest.mark.parametrize("kind", [None, "server", "device"])
def test_every_os_number_equals_the_total_of_its_list(client, kind, pinned_clock):
    tree = get(client, "/os", kind=kind)
    n = _assert_links(client, os_links(tree), min_links=30)
    assert n > 30
    assert tree["total"] == total(client, **({"kind": kind} if kind else {}))


@pytest.mark.parametrize("vendor", [None, "Cisco", "Fortinet", "Palo Alto Networks", "Ubiquiti"])
def test_every_hardware_number_equals_the_total_of_its_list(client, vendor, pinned_clock):
    h = get(client, "/hardware", vendor=vendor)
    n = _assert_links(client, hardware_links(h), min_links=20)
    assert n > 20
    if vendor:
        assert h["matrix"]["vendor"].lower() == vendor.lower()


def test_changes_stats_equal_the_totals_of_their_chips(client, pinned_clock):
    base = get(client, "/changes", page_size=1)
    st = base["stats"]
    window = {"since": base["window"]["since"]}
    chips = [
        ("total", st["total"], {}),
        ("added", st["software"]["added"], {"category": "software", "action": "added"}),
        ("removed", st["software"]["removed"], {"category": "software", "action": "removed"}),
        ("changed", st["software"]["changed"], {"category": "software", "action": "changed"}),
        ("upgraded", st["software"]["upgraded"], {"category": "software", "action": "changed", "label": "upgrade"}),
        ("downgraded", st["software"]["downgraded"], {"category": "software", "action": "changed", "label": "downgrade"}),
        ("os", st["os"], {"category": "os"}),
        ("firmware", st["firmware"], {"category": "firmware"}),
        ("hardware", st["hardware"], {"category": "hardware"}),
    ]
    for label, number, params in chips:
        assert get(client, "/changes", page_size=1, **window, **params)["total"] == number, label
    # the daily series adds up to the window's stats
    assert sum(d["total"] for d in base["series"]) == st["total"]
    assert sum(d["software"] for d in base["series"]) == sum(st["software"][k] for k in ("added", "removed", "changed"))


def test_the_drill_helpers_cover_every_documented_number():
    """A guard on the helpers themselves: they must keep naming the numbers the contract promises."""
    s = {"assets": {"total": 1, "server": 1, "device": 0},
         "coverage": {"fresh": 1, "aging": 0, "stale": 0, "never": 0, "not_applicable": 0},
         "os": {"top": [], "unknown": 0},
         "software": {"top": [], "sprawl": [], "security_agent_coverage": {"covered": 0, "missing": 0, "unknown": 0, "total": 0}},
         "hardware": {"devices": 0, "top_models": []},
         "health": {**{k: 0 for k in iq.HEALTH_KEYS}, "not_collected_by_design": {"count": 0}}}
    labels = {l for l, _n, _p in summary_asset_links(s)}
    assert {"assets.total", "coverage.fresh", "coverage.not_applicable", "os.unknown", "security_agent_coverage.missing",
            "security_agent_coverage.unknown", "hardware.devices", "health.not_collected_by_design"} <= labels
    assert {f"health.{k}" for k in iq.HEALTH_KEYS} <= labels


def test_drill_links_are_url_expressible():
    """S6: a drill link's filters are query parameters of the assets endpoint; no helper invents a name."""
    import dataclasses
    names = {f.name for f in dataclasses.fields(iq.AssetFilters)} | {"since", "category", "action", "label"}
    s = {"assets": {"total": 1, "server": 1, "device": 0},
         "coverage": {"fresh": 1, "aging": 0, "stale": 0, "never": 0, "not_applicable": 0},
         "os": {"top": [{"family": "windows", "product": None, "release": None, "count": 1}], "unknown": 0},
         "software": {"top": [{"product_key": "x", "hosts": 1}], "sprawl": [],
                      "security_agent_coverage": {"covered": 0, "missing": 1, "unknown": 0, "total": 1}},
         "hardware": {"devices": 0, "top_models": [{"vendor": "V", "model": "M", "count": 1}]},
         "health": {**{k: 0 for k in iq.HEALTH_KEYS}, "not_collected_by_design": {"count": 0}},
         "changes_7d": {"since": "x", "total": 0, "software": {k: 0 for k in ("added", "removed", "changed", "upgraded", "downgraded")},
                        "os": 0, "firmware": 0, "hardware": 0}}
    for _l, _n, params in itertools.chain(summary_asset_links(s), summary_change_links(s)):
        assert set(params) <= names, params
