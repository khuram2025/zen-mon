"""Phase 1 touchpoints in existing pages (task 1.9): the two API additions the Devices list and the server
Software tab need.

* ``GET /devices`` gains optional ``vendor``, ``model`` and ``os_version`` filters (exact, case-insensitive,
  trimmed). They only narrow what the caller may already see: the tag scope is applied first and is not
  touched, and a missing or blank value leaves the list exactly as it was. ``unknown`` means "no value" (RG-10),
  every item carries ``os_version_display``, the normalised version where the Inventory identity has one (RG-11), and
  ``facets=true`` adds the vendor, model and OS version menus over ALL devices (RG-03).
* ``GET /servers/{id}/software`` gains ``product_key`` and ``category`` on every item and ``agent_truncated`` on
  the response. The existing keys, their values and the ordering do not change. It answers 404 for an unknown server and
  for one outside the caller's tag scope (RG-05 / F6).

The oracle is plain SQL over the seeded fleet (tests/inventory_fleet.py), independent of the code under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from inventory_fleet import (
    EDGE_DEVICES, SCOPED_TAG, TRUNCATED_SERVERS, aid, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

DEVICES = "/api/v1/devices"
SERVERS = "/api/v1/servers"


def device_client(db, tags=()):
    return api_client(db, role="t_touch_dev", permissions=["devices.view"], scope_tags=tuple(tags))


def server_client(db):
    return api_client(db, role="t_touch_srv", permissions=["servers.view"])


def list_devices(c, expect: int = 200, **params) -> dict[str, Any]:
    resp = c.get(DEVICES, params={"limit": 200, **params})
    assert resp.status_code == expect, (params, resp.status_code, resp.text[:300])
    return resp.json()


def hostnames(body: dict[str, Any]) -> set[str]:
    return {d["hostname"] for d in body["data"]}


def sql_hostnames(db, where: str, *args) -> set[str]:
    return {r[0] for r in db.query(f"SELECT hostname FROM devices WHERE {where}", *args)}


VISIBLE = ("EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(tags, '[]'::jsonb)) t "
           "WHERE lower(t) = ANY($1::text[]))")


# ── GET /devices: vendor / model / os_version ───────────────────────────────

def test_the_fleet_has_vendors_models_and_versions_to_filter_on(fleet_db):
    assert fleet_db.scalar("SELECT count(DISTINCT vendor) FROM devices WHERE vendor IS NOT NULL") >= 5
    assert fleet_db.scalar("SELECT count(*) FROM devices WHERE vendor IS NULL") >= 3, "unknown must exist in the fixture"
    assert fleet_db.scalar("SELECT count(DISTINCT os_version) FROM devices WHERE os_version IS NOT NULL") >= 5


def test_vendor_filter_is_exact_and_case_insensitive(fleet_db):
    expected = sql_hostnames(fleet_db, "lower(btrim(vendor)) = 'cisco'")
    assert len(expected) >= 5
    with device_client(fleet_db) as c:
        assert hostnames(list_devices(c, vendor="Cisco")) == expected
        assert hostnames(list_devices(c, vendor="cisco")) == expected
        assert hostnames(list_devices(c, vendor="  CISCO  ")) == expected
        body = list_devices(c, vendor="Cisco")
        assert body["meta"]["total"] == len(expected)
        # exact: a fragment of a vendor is not a vendor, and '%' / '_' are literal characters
        for partial in ("Cis", "isco", "Palo", "%", "_isco", "C%"):
            assert list_devices(c, vendor=partial)["meta"]["total"] == 0, partial


# The OS version the list shows (os_version_display): the normalised version of the Inventory identity where it has one,
# else the device's own column.
DISPLAY_OS = ("COALESCE(NULLIF(btrim((SELECT i.os_version FROM inventory_identity i WHERE i.asset_kind = 'device' "
              "AND i.asset_id = devices.id)), ''), NULLIF(btrim(devices.os_version), ''))")


def test_model_and_os_version_filters(fleet_db):
    model = fleet_db.scalar("SELECT model FROM devices WHERE model IS NOT NULL GROUP BY model ORDER BY count(*) DESC, model LIMIT 1")
    version = fleet_db.scalar(f"SELECT {DISPLAY_OS} v FROM devices WHERE {DISPLAY_OS} IS NOT NULL GROUP BY v "
                              "ORDER BY count(*) DESC, v LIMIT 1")
    by_model = sql_hostnames(fleet_db, "lower(btrim(model)) = $1", model.lower())
    by_version = sql_hostnames(fleet_db, f"lower({DISPLAY_OS}) = $1", version.lower())
    assert by_model and by_version
    with device_client(fleet_db) as c:
        assert hostnames(list_devices(c, model=model.upper())) == by_model
        assert hostnames(list_devices(c, os_version=version)) == by_version
        assert hostnames(list_devices(c, os_version=version.upper())) == by_version
        assert list_devices(c, model="no such model at all")["meta"]["total"] == 0


def test_filters_combine_with_each_other_and_with_the_existing_ones(fleet_db):
    expected = sql_hostnames(fleet_db, "lower(vendor) = 'cisco' AND lower(model) = 'ws-c2960x-48ts-l'")
    assert expected
    with device_client(fleet_db) as c:
        assert hostnames(list_devices(c, vendor="cisco", model="WS-C2960X-48TS-L")) == expected
        # AND with another vendor: nothing
        assert list_devices(c, vendor="Fortinet", model="WS-C2960X-48TS-L")["meta"]["total"] == 0
        # AND with the existing status filter and search
        up = sql_hostnames(fleet_db, "lower(vendor) = 'cisco' AND status = 'up'")
        assert hostnames(list_devices(c, vendor="cisco", status="up")) == up
        named = sql_hostnames(fleet_db, "lower(vendor) = 'cisco' AND hostname ILIKE '%c29%'")
        assert named and hostnames(list_devices(c, vendor="cisco", search="c29")) == named


def test_blank_values_leave_the_list_unchanged(fleet_db):
    with device_client(fleet_db) as c:
        whole = list_devices(c)
        assert whole["meta"]["total"] == fleet_db.scalar("SELECT count(*) FROM devices")
        for params in ({"vendor": ""}, {"vendor": "   "}, {"model": ""}, {"os_version": " "},
                       {"vendor": "", "model": "", "os_version": ""}):
            body = list_devices(c, **params)
            assert body["meta"]["total"] == whole["meta"]["total"], params
            assert hostnames(body) == hostnames(whole), params


def test_unfiltered_response_shape_and_order_are_unchanged(fleet_db):
    with device_client(fleet_db) as c:
        body = list_devices(c)
        filtered = list_devices(c, vendor="Cisco")
    assert set(body) == {"data", "meta"} and set(body["meta"]) == {"total", "skip", "limit"}
    names = [d["hostname"] for d in body["data"]]
    assert names == sorted(names), "still ordered by hostname"
    # the filtered rows are the same rows, in the same order
    assert [d["hostname"] for d in filtered["data"]] == [n for n in names if n in hostnames(filtered)]
    assert {"vendor", "model", "os_version"} <= set(body["data"][0])


def test_oversized_filter_values_are_rejected_not_truncated(fleet_db):
    with device_client(fleet_db) as c:
        resp = c.get(DEVICES, params={"vendor": "x" * 201})
        assert resp.status_code == 422


def test_tag_scope_still_applies_with_the_new_filters(fleet_db):
    """The filters narrow; they never widen. A scoped caller gets exactly the in-scope subset of what the
    unscoped caller gets for the same filter, and naming an out-of-scope vendor or model finds nothing."""
    visible = sql_hostnames(fleet_db, VISIBLE, [SCOPED_TAG])
    assert visible and visible < sql_hostnames(fleet_db, "true"), "the scope must hide something"
    assert visible >= set(EDGE_DEVICES)
    cases = ({"vendor": "Cisco"}, {"vendor": "Fortinet"}, {"model": "BIG-IP i2800"}, {"os_version": "6.5.59"},
             {"vendor": "cisco", "model": "N9K-C93180YC-FX"})
    with device_client(fleet_db) as full:      # two clients cannot be open at once: collect the unscoped answers first
        wide = [hostnames(list_devices(full, **params)) for params in cases]
    assert all(wide), "every case must match something for the comparison to mean anything"
    with device_client(fleet_db, tags=[SCOPED_TAG]) as scoped:
        everything = list_devices(scoped)
        assert hostnames(everything) == visible and everything["meta"]["total"] == len(visible)
        for params, unscoped in zip(cases, wide):
            got = list_devices(scoped, **params)
            assert hostnames(got) == unscoped & visible, params
            assert got["meta"]["total"] == len(unscoped & visible), params
        assert any(unscoped - visible for unscoped in wide), "at least one filter must match devices the scope hides"
    # the scope is also applied to vendors that exist only outside it
    outside_vendors = {r[0].lower() for r in fleet_db.query(
        "SELECT DISTINCT vendor FROM devices WHERE vendor IS NOT NULL AND NOT " + VISIBLE, [SCOPED_TAG])}
    inside_vendors = {r[0].lower() for r in fleet_db.query(
        "SELECT DISTINCT vendor FROM devices WHERE vendor IS NOT NULL AND " + VISIBLE, [SCOPED_TAG])}
    only_outside = outside_vendors - inside_vendors
    assert only_outside, "the fixture needs a vendor the scoped user cannot see"
    with device_client(fleet_db, tags=[SCOPED_TAG]) as scoped:
        for vendor in only_outside:
            assert list_devices(scoped, vendor=vendor)["meta"]["total"] == 0, vendor


def test_a_user_whose_scope_matches_nothing_gets_nothing_whatever_the_filter(fleet_db):
    with device_client(fleet_db, tags=["no-such-tag"]) as c:
        assert list_devices(c)["meta"]["total"] == 0
        assert list_devices(c, vendor="Cisco")["meta"]["total"] == 0


# ── GET /servers/{id}/software ──────────────────────────────────────────────

def software(c, server: str, expect: int = 200, **params) -> dict[str, Any]:
    resp = c.get(f"{SERVERS}/{aid('server', server)}/software", params=params)
    assert resp.status_code == expect, (server, resp.status_code, resp.text[:300])
    return resp.json()


def pick_server(db, truncated: bool) -> str:
    return db.query(
        "SELECT s.display_name FROM servers s WHERE s.software_truncated = $1 AND "
        "(SELECT count(*) FROM server_software_inventory i WHERE i.server_id = s.id) > 5 "
        "ORDER BY s.display_name LIMIT 1", truncated)[0][0]


def test_items_carry_product_key_and_category_and_keep_their_old_fields(fleet_db):
    name = pick_server(fleet_db, False)
    with server_client(fleet_db) as c:
        body = software(c, name)
    stored = {(r[0], r[1]): (r[2], r[3]) for r in fleet_db.query(
        "SELECT package_name, version, name_key, category FROM server_software_inventory WHERE server_id = $1",
        aid("server", name))}
    assert body["items"] and len(body["items"]) == len(stored) == body["total"]
    for item in body["items"]:
        assert set(item) == {"package_name", "version", "vendor", "install_date", "updated_at", "product_key", "category"}
        assert (item["product_key"], item["category"]) == stored[(item["package_name"], item["version"])]
    assert any(i["product_key"] for i in body["items"]) and any(i["category"] for i in body["items"])
    assert set(body) == {"items", "total", "truncated", "agent_truncated"}
    assert body["truncated"] is False


def test_order_is_unchanged(fleet_db):
    name = pick_server(fleet_db, False)
    with server_client(fleet_db) as c:
        names = [i["package_name"] for i in software(c, name)["items"]]
    assert names == sorted(names, key=str.lower)


def test_every_product_key_is_a_catalog_address(fleet_db):
    """The link target must exist: each key a package carries is a product the catalog knows."""
    name = pick_server(fleet_db, False)
    with server_client(fleet_db) as c:
        keys = {i["product_key"] for i in software(c, name)["items"] if i["product_key"]}
    known = {r[0] for r in fleet_db.query("SELECT DISTINCT name_key FROM server_software_inventory WHERE name_key IS NOT NULL")}
    assert keys and keys <= known


def test_agent_truncated_follows_the_server_flag(fleet_db):
    assert set(TRUNCATED_SERVERS) == {r[0] for r in fleet_db.query("SELECT display_name FROM servers WHERE software_truncated")}
    with server_client(fleet_db) as c:
        for name in TRUNCATED_SERVERS:
            body = software(c, name)
            assert body["agent_truncated"] is True, name
            assert body["truncated"] is False, "the response itself is complete: only the agent's list is cut"
        assert software(c, pick_server(fleet_db, False))["agent_truncated"] is False


def test_truncated_and_agent_truncated_are_independent(fleet_db):
    cut = pick_server(fleet_db, True)
    whole = pick_server(fleet_db, False)
    with server_client(fleet_db) as c:
        a = software(c, cut, limit=3)
        b = software(c, whole, limit=3)
    assert (a["truncated"], a["agent_truncated"]) == (True, True) and len(a["items"]) == 3 and a["total"] > 3
    assert (b["truncated"], b["agent_truncated"]) == (True, False) and len(b["items"]) == 3
    assert {i["package_name"] for i in a["items"]} <= {r[0] for r in fleet_db.query(
        "SELECT package_name FROM server_software_inventory WHERE server_id = $1", aid("server", cut))}


def test_rows_without_a_key_report_null_not_a_guess(fleet_db_fresh):
    name = pick_server(fleet_db_fresh, False)
    sid = aid("server", name)
    fleet_db_fresh.execute(
        "UPDATE server_software_inventory SET name_key = NULL, category = NULL "
        "WHERE ctid = (SELECT ctid FROM server_software_inventory WHERE server_id = $1 ORDER BY package_name LIMIT 1)", sid)
    with server_client(fleet_db_fresh) as c:
        body = software(c, name)
    unkeyed = [i for i in body["items"] if i["product_key"] is None]
    assert len(unkeyed) == 1 and unkeyed[0]["category"] is None
    assert all(i["product_key"] for i in body["items"] if i is not unkeyed[0])


def test_a_server_without_software_has_empty_items_and_no_flag(fleet_db_fresh):
    name = fleet_db_fresh.query(
        "SELECT s.display_name FROM servers s WHERE NOT EXISTS (SELECT 1 FROM server_software_inventory i WHERE i.server_id = s.id) "
        "ORDER BY 1 LIMIT 1")[0][0]
    with server_client(fleet_db_fresh) as c:
        body = software(c, name)
    assert body == {"items": [], "total": 0, "truncated": False, "agent_truncated": False}


def test_another_servers_flag_does_not_leak(fleet_db_fresh):
    """agent_truncated is read for the requested server only."""
    a, b = pick_server(fleet_db_fresh, False), pick_server(fleet_db_fresh, True)
    fleet_db_fresh.execute("UPDATE servers SET software_truncated = false WHERE id = $1", aid("server", b))
    fleet_db_fresh.execute("UPDATE servers SET software_truncated = true WHERE id = $1", aid("server", a))
    with server_client(fleet_db_fresh) as c:
        assert software(c, a)["agent_truncated"] is True
        assert software(c, b)["agent_truncated"] is False


# ── GET /devices: unknown, os_version_display and facets (RG-10, RG-11, RG-03) ──

def facet_items(body: dict[str, Any], name: str) -> dict[str, int]:
    return {i["value"]: i["count"] for i in body["facets"][name]["items"]}


@pytest.mark.parametrize("column", ["vendor", "model"])
def test_rg10_unknown_means_no_value_for_vendor_and_model(fleet_db, column):
    """RG-10: ``?vendor=unknown`` used to match devices whose vendor is the TEXT 'unknown' (none) while the Devices page and
    the Inventory API meant 'no vendor'. Both now mean: NULL, blank, or the text 'unknown'."""
    missing = sql_hostnames(fleet_db, f"{column} IS NULL OR btrim({column}) = '' OR lower(btrim({column})) = 'unknown'")
    assert missing, "the fleet needs devices without a " + column
    with device_client(fleet_db) as c:
        for spelling in ("unknown", "Unknown", "UNKNOWN", " unknown "):
            body = list_devices(c, **{column: spelling})
            assert hostnames(body) == missing and body["meta"]["total"] == len(missing), spelling
        # a blank value is still 'not set': it filters nothing
        assert list_devices(c, **{column: " "})["meta"]["total"] == fleet_db.scalar("SELECT count(*) FROM devices")


def test_rg10_a_device_whose_vendor_is_the_text_unknown_is_unknown_too(fleet_db_fresh):
    sample = fleet_db_fresh.scalar("SELECT hostname FROM devices WHERE vendor IS NOT NULL ORDER BY hostname LIMIT 1")
    fleet_db_fresh.execute("UPDATE devices SET vendor = 'Unknown' WHERE hostname = $1", sample)
    expected = sql_hostnames(fleet_db_fresh, "vendor IS NULL OR btrim(vendor) = '' OR lower(btrim(vendor)) = 'unknown'")
    with device_client(fleet_db_fresh) as c:
        assert sample in hostnames(list_devices(c, vendor="unknown")) and hostnames(list_devices(c, vendor="unknown")) == expected


def test_rg10_unknown_os_version_is_the_displayed_version_being_empty(fleet_db):
    """A device whose own column is empty but whose normalised identity has a version is NOT unknown (RG-11)."""
    expected = sql_hostnames(fleet_db, f"{DISPLAY_OS} IS NULL OR lower({DISPLAY_OS}) = 'unknown'")
    raw_empty = sql_hostnames(fleet_db, "os_version IS NULL OR btrim(os_version) = ''")
    assert expected and expected < raw_empty, "fixture: some devices with no own version do have a normalised one"
    with device_client(fleet_db) as c:
        body = list_devices(c, os_version="unknown")
        assert hostnames(body) == expected and body["meta"]["total"] == len(expected)


def test_rg11_the_list_carries_the_normalised_os_version_and_keeps_the_legacy_one(fleet_db):
    with device_client(fleet_db) as c:
        body = list_devices(c)
    stored = {r[0]: (r[1], r[2]) for r in fleet_db.query(
        f"SELECT hostname, os_version, {DISPLAY_OS} FROM devices")}
    assert len(body["data"]) == len(stored)
    for d in body["data"]:
        raw, display = stored[d["hostname"]]
        assert d["os_version"] == raw                           # the legacy field is untouched
        assert d["os_version_display"] == display, d["hostname"]
    by = {d["hostname"]: d for d in body["data"]}
    assert by["fg-01"]["os_version"] is None and by["fg-01"]["os_version_display"] == "7.2.8"        # FortiOS: template-reported
    assert by["lb-01"]["os_version"] is None and by["lb-01"]["os_version_display"] == "17.1.1.3"      # F5
    assert by["sw-c9-01"]["os_version"] == "17.09.04a" and by["sw-c9-01"]["os_version_display"] == "17.9.4a"   # normalised spelling wins


def test_rg11_a_device_with_no_identity_row_shows_its_own_version(fleet_db_fresh):
    host = fleet_db_fresh.scalar("SELECT hostname FROM devices WHERE os_version IS NOT NULL ORDER BY hostname LIMIT 1")
    own = fleet_db_fresh.scalar("SELECT os_version FROM devices WHERE hostname = $1", host)
    fleet_db_fresh.execute("DELETE FROM inventory_identity WHERE asset_kind = 'device' AND asset_id = (SELECT id FROM devices WHERE hostname = $1)", host)
    with device_client(fleet_db_fresh) as c:
        item = next(d for d in list_devices(c)["data"] if d["hostname"] == host)
    assert item["os_version_display"] == own.strip()


def test_rg03_facets_are_not_computed_unless_asked(fleet_db):
    with device_client(fleet_db) as c:
        assert "facets" not in list_devices(c)
        assert "facets" not in list_devices(c, facets="false")
        body = list_devices(c, facets="true")
    assert set(body) == {"data", "meta", "facets"} and set(body["facets"]) == {"vendor", "model", "os_version"}


def test_rg03_facets_cover_every_device_not_the_page(fleet_db):
    """RG-03: the menus were built from the first 200 devices the page loaded. They now count ALL devices, so a small page
    (limit=3) still gets the whole estate's menu, and each count is what the list gives when the value is chosen."""
    total = fleet_db.scalar("SELECT count(*) FROM devices")
    assert total > 3
    with device_client(fleet_db) as c:
        body = list_devices(c, facets="true", limit=3)
        assert len(body["data"]) == 3 and body["meta"]["total"] == total
        for name in ("vendor", "model", "os_version"):
            facet = body["facets"][name]
            assert sum(i["count"] for i in facet["items"]) == total, name            # every device is in exactly one bucket
            assert facet["distinct"] == len(facet["items"])
            for item in facet["items"]:
                landed = list_devices(c, **{name: item["value"]})["meta"]["total"]
                assert landed == item["count"], (name, item)
    vendors = facet_items(body, "vendor")
    oracle = {r[0]: r[1] for r in fleet_db.query(
        "SELECT lower(btrim(vendor)), count(*) FROM devices WHERE vendor IS NOT NULL AND btrim(vendor) <> '' GROUP BY 1")}
    assert {k.lower(): v for k, v in vendors.items() if k != "unknown"} == oracle
    assert vendors["unknown"] == fleet_db.scalar("SELECT count(*) FROM devices WHERE vendor IS NULL OR btrim(vendor) = ''")
    versions = facet_items(body, "os_version")
    assert versions["7.2.8"] == fleet_db.scalar(f"SELECT count(*) FROM devices WHERE {DISPLAY_OS} = '7.2.8'") >= 3   # the Fortinet ones are counted


def test_rg03_each_facet_counts_over_the_other_filters_but_not_its_own(fleet_db):
    with device_client(fleet_db) as c:
        body = list_devices(c, facets="true", vendor="Cisco", status="up", limit=1)
        vendors = facet_items(body, "vendor")
        models = facet_items(body, "model")
    assert "Cisco" in vendors and len(vendors) > 1, "the vendor menu ignores the vendor filter itself"
    assert vendors["Cisco"] == body["meta"]["total"]
    assert set(vendors) - {"Cisco", "unknown"} and sum(vendors.values()) == fleet_db.scalar("SELECT count(*) FROM devices WHERE status = 'up'")
    assert sum(models.values()) == body["meta"]["total"], "the model menu does follow the vendor and status filters"


def test_rg03_facets_follow_the_tag_scope(fleet_db):
    visible = sql_hostnames(fleet_db, VISIBLE, [SCOPED_TAG])
    assert visible and len(visible) < fleet_db.scalar("SELECT count(*) FROM devices")
    with device_client(fleet_db, tags=[SCOPED_TAG]) as c:
        body = list_devices(c, facets="true", limit=1)
    for name in ("vendor", "model", "os_version"):
        assert sum(i["count"] for i in body["facets"][name]["items"]) == len(visible), name
    outside_only = {r[0] for r in fleet_db.query(
        "SELECT DISTINCT vendor FROM devices WHERE vendor IS NOT NULL AND NOT " + VISIBLE, [SCOPED_TAG])} - {
        r[0] for r in fleet_db.query("SELECT DISTINCT vendor FROM devices WHERE vendor IS NOT NULL AND " + VISIBLE, [SCOPED_TAG])}
    assert outside_only and not (outside_only & {i["label"] for i in body["facets"]["vendor"]["items"]})


def test_rg03_the_spelling_of_a_facet_value_is_the_most_common_one_and_variants_merge(fleet_db_fresh):
    names = [r[0] for r in fleet_db_fresh.query("SELECT hostname FROM devices WHERE vendor = 'Cisco' ORDER BY hostname")]
    assert len(names) >= 4
    fleet_db_fresh.execute("UPDATE devices SET vendor = 'CISCO' WHERE hostname = $1", names[0])
    fleet_db_fresh.execute("UPDATE devices SET vendor = ' cisco ' WHERE hostname = $1", names[1])
    with device_client(fleet_db_fresh) as c:
        body = list_devices(c, facets="true", limit=1)
        items = {i["label"].lower(): i for i in body["facets"]["vendor"]["items"] if i["label"] != "Unknown"}
        cisco = items["cisco"]
        assert cisco["label"] == "Cisco" and cisco["value"] == "Cisco"                 # the most common spelling labels the group
        assert cisco["count"] == fleet_db_fresh.scalar("SELECT count(*) FROM devices WHERE lower(btrim(vendor)) = 'cisco'")
        assert list_devices(c, vendor=cisco["value"])["meta"]["total"] == cisco["count"]


# ── GET /servers/{id}/software: existence and tag scope (RG-05 / F6) ──

def scoped_server_client(db, tags):
    return api_client(db, role="t_touch_srv_scope", permissions=["servers.view"], scope_tags=tuple(tags))


def server_tags(db, name: str) -> set[str]:
    return {t.lower() for t in db.scalar("SELECT tags FROM servers WHERE display_name = $1", name) or []}


def test_rg05_the_software_list_of_an_unknown_server_is_404(fleet_db):
    import uuid
    with server_client(fleet_db) as c:
        resp = c.get(f"{SERVERS}/{uuid.uuid4()}/software")
        assert resp.status_code == 404 and resp.json()["detail"] == "Server not found"
        assert c.get(f"{SERVERS}/{uuid.uuid4()}").json() == resp.json(), "the same body as the server itself"


def test_rg05_the_software_list_follows_the_tag_scope_of_the_server(fleet_db):
    inside = fleet_db.query("SELECT display_name FROM servers WHERE tags @> to_jsonb($1::text) ORDER BY display_name", SCOPED_TAG)
    outside = fleet_db.query("SELECT display_name FROM servers WHERE NOT (tags @> to_jsonb($1::text)) "
                             "AND EXISTS (SELECT 1 FROM server_software_inventory i WHERE i.server_id = servers.id) ORDER BY display_name", SCOPED_TAG)
    assert inside and outside
    ins = next(r[0] for r in inside if fleet_db.scalar(
        "SELECT count(*) FROM server_software_inventory WHERE server_id = $1", aid("server", r[0])) > 0)
    out = outside[0][0]
    with scoped_server_client(fleet_db, [SCOPED_TAG]) as c:
        assert software(c, ins)["total"] > 0                                          # visible: 200
        resp = c.get(f"{SERVERS}/{aid('server', out)}/software")
        assert resp.status_code == 404 and resp.json() == c.get(f"{SERVERS}/{aid('server', out)}").json()
    with server_client(fleet_db) as c:                                                # unscoped: both are readable
        assert software(c, ins)["total"] > 0 and software(c, out)["total"] > 0


def test_rg05_a_role_level_scope_hides_the_server_too(fleet_db):
    out = fleet_db.scalar("SELECT display_name FROM servers WHERE NOT (tags @> to_jsonb($1::text)) AND EXISTS "
                          "(SELECT 1 FROM server_software_inventory i WHERE i.server_id = servers.id) ORDER BY display_name LIMIT 1", SCOPED_TAG)
    with api_client(fleet_db, role="t_touch_srv_rolescope", permissions=["servers.view"], role_scope_tags=(SCOPED_TAG,)) as c:
        assert c.get(f"{SERVERS}/{aid('server', out)}/software").status_code == 404


def test_rg05_untagged_servers_are_hidden_from_a_scoped_caller(fleet_db):
    untagged = fleet_db.query("SELECT display_name FROM servers WHERE tags IS NULL OR tags = '[]'::jsonb ORDER BY display_name")
    if not untagged:
        pytest.skip("the fleet has no untagged server")
    with scoped_server_client(fleet_db, [SCOPED_TAG]) as c:
        assert c.get(f"{SERVERS}/{aid('server', untagged[0][0])}/software").status_code == 404
