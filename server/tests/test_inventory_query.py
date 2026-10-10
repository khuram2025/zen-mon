"""Tests for app/services/inventory_query.py (Inventory phase 1, tasks 1.1 and 1.2).

Everything that reads a table runs the real service functions against the seeded fleet of
``inventory_fleet`` on the private PostgreSQL cluster (``pg_fixture``; never the appliance database), with
the clock pinned to ``FIXED_NOW``. Pure functions (filter validation, version ordering, currency) need
nothing. The assertions check each answer against an independent oracle (the full asset list filtered in
Python, or a plain SQL count) so the SQL and the intent cannot drift apart together.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.services import inventory_query as iq
from app.services import inventory_service
from inventory_fleet import (
    DEFENDER_SERVICE_SERVERS, EDGE_DEVICES, EDGE_SERVERS, FIXED_NOW, LINKED_SERVERS, NO_SECURITY_SERVERS, NO_SERVICE_LIST_SERVERS, SCOPED_TAG,
    STALE_SERVERS, TRUNCATED_SERVERS, ZENPLUS_ONLY_SERVERS, aid, fleet_db, fleet_db_fresh, fleet_info, fleet_template,
    pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

UTC = timezone.utc
F = iq.AssetFilters


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


async def rows_of(db, scope=None, **filters):
    """Every asset row for the filters (one page of 500)."""
    async with session(db) as s:
        r = await iq.list_assets(s, F(**filters), scope=scope, page_size=500)
    return r["items"]


async def total_of(db, scope=None, **filters):
    async with session(db) as s:
        return (await iq.list_assets(s, F(**filters), scope=scope, page_size=1))["total"]


def keys(rows):
    return {r["key"] for r in rows}


def names(rows):
    return {r["name"] for r in rows}


# ════════════════════════════════════════════════════════════════════
# Pure functions
# ════════════════════════════════════════════════════════════════════

def test_filters_normalize_trims_and_drops_empty_values():
    f = F(q="  web  ", vendor="", tag=(" Prod ", "prod", "PCI", ""), os_family=" Windows ").normalized()
    assert (f.q, f.vendor, f.os_family) == ("web", None, "windows")
    assert f.tag == ("Prod", "PCI")                      # case-insensitive duplicates collapse, empties drop


@pytest.mark.parametrize("field,value", [
    ("kind", "router"), ("freshness", "old"), ("health", "broken"), ("security_agent", "yes"),
    ("memory", "huge"), ("disks", "7"),
])
def test_filters_reject_values_outside_the_vocabulary(field, value):
    with pytest.raises(iq.InvalidFilter):
        F(**{field: value}).normalized()


def test_product_version_needs_a_product():
    with pytest.raises(iq.InvalidFilter):
        F(product_version="1.0").normalized()
    F(product="google-chrome", product_version="152.0.8001.12").normalized()


def test_as_params_is_the_public_vocabulary():
    f = F(kind="server", os_family="windows", tag=("a", "b"), health="stale").normalized()
    assert f.as_params() == {"kind": "server", "os_family": "windows", "health": "stale", "tag": ["a", "b"]}


def test_health_vocabulary_and_sql_cover_the_same_keys():
    assert set(iq.HEALTH_SQL) == set(iq.HEALTH_KEYS)
    assert len(iq.HEALTH_KEYS) == 8


@pytest.mark.parametrize("sort,order", [("bogus", "asc"), ("name", "sideways")])
def test_order_sql_uses_an_allowlist(sort, order):
    with pytest.raises(iq.InvalidFilter):
        iq._order_sql(sort, order)


def test_order_sql_is_a_total_order_with_nulls_last():
    sql = iq._order_sql("model", "desc")
    assert "NULLS LAST" in sql and sql.endswith("a.kind ASC, a.id ASC")


@pytest.mark.parametrize("page,size", [(0, 10), (1, 0), (1, 501), (100_001, 10)])
def test_page_arguments_are_bounded(page, size):
    with pytest.raises(iq.InvalidFilter):
        iq._page_args(page, size)


def test_like_escape_neutralises_wildcards():
    assert iq._like("50%_off\\") == "%50\\%\\_off\\\\%"


def test_natural_key_orders_releases_and_puts_missing_last():
    assert sorted(["7.10", "7.2", "17.9", "7.4"], key=iq._natural_key) == ["7.2", "7.4", "7.10", "17.9"]
    assert sorted([None, "1"], key=iq._natural_key) == ["1", None]


def test_version_label_orders_by_comparator_and_refuses_to_guess():
    assert iq.version_label("151.0.7922.110", "152.0.8001.12") == "upgrade"
    assert iq.version_label("152.0.8001.12", "151.0.7922.110") == "downgrade"
    assert iq.version_label("1.0", "1.0.0") is None            # equal
    assert iq.version_label("nightly-build", "release-candidate") is None      # not orderable
    assert iq.version_label(None, "1.0") is None and iq.version_label("1.0", None) is None


def test_currency_splits_latest_previous_and_older():
    cur, order = iq._currency("win_build", [("10.0.20348.587", 6), ("10.0.20348.2402", 14), ("10.0.20348.2700", 41), (None, 2)])
    assert cur == {"latest": 41, "previous": 14, "older": 6, "unknown": 2,
                   "latest_version": "10.0.20348.2700", "previous_version": "10.0.20348.2402"}
    assert order == ["10.0.20348.2700", "10.0.20348.2402", "10.0.20348.587", None]


def test_currency_single_version_has_no_previous():
    cur, _ = iq._currency("fortios", [("7.4.4", 9)])
    assert (cur["latest"], cur["previous"], cur["older"], cur["previous_version"]) == (9, 0, 0, None)


def test_currency_is_none_when_versions_cannot_be_ordered():
    cur, order = iq._currency("cisco_ios", [("15.2(7)E8", 3), ("12.2(55)SE12", 2)])
    # Different trains: the comparator refuses, so there is no latest and the order falls back to natural.
    assert cur is None or cur["latest"] >= 0
    assert set(order) == {"15.2(7)E8", "12.2(55)SE12"}


def test_scheme_for_family_and_product():
    assert iq.scheme_for("windows") == "win_build"
    assert iq.scheme_for("fortios") == "fortios" and iq.scheme_for("fortiap") == "fortios"
    assert iq.scheme_for("ios-xe") == "cisco_iosxe"
    assert iq.scheme_for("linux", "Ubuntu") == "dpkg"
    assert iq.scheme_for("something-new") == "semverish"


def test_parse_asset_ref_and_key():
    u = uuid.uuid4()
    assert iq.parse_asset_ref("server", str(u)) == ("server", u)
    assert iq.parse_asset_ref("router", str(u)) is None and iq.parse_asset_ref("server", "nope") is None
    assert iq.parse_asset_key(f"device:{u}") == ("device", u)
    with pytest.raises(iq.InvalidFilter):
        iq.parse_asset_key("device")


def test_iso_is_utc_with_z_and_whole_seconds():
    assert iq.iso(datetime(2026, 10, 8, 14, 0, 0, 999, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-08T12:00:00Z"
    assert iq.iso(None) is None


# ════════════════════════════════════════════════════════════════════
# The asset union
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_union_counts_a_server_and_its_snmp_twin_once(fleet_db, fleet_info, pinned_clock):
    rows = await rows_of(fleet_db)
    e = fleet_info.expected
    assert len(rows) == e["servers"] + e["devices_in_union"] == fleet_info.assets_total == 145
    assert Counter(r["kind"] for r in rows) == {"server": 60, "device": 85}
    twin_ids = set(fleet_info.linked_device_ids.values())
    assert len(twin_ids) == 5 == e["linked"]
    assert not twin_ids & {r["id"] for r in rows}, "a server's SNMP twin must not appear as an asset of its own"
    assert len({r["key"] for r in rows}) == len(rows)
    # the raw tables hold five more devices than the union shows
    assert fleet_db.scalar("SELECT count(*) FROM devices") == 90


@pytest.mark.asyncio
async def test_twin_enriches_the_server_it_belongs_to(fleet_db, fleet_info, pinned_clock):
    row = next(r for r in await rows_of(fleet_db) if r["name"] == LINKED_SERVERS[0])
    assert (row["vendor"], row["model"], row["serial"]) == ("Dell", "PowerEdge R650", "DELL00001")
    assert row["hw_source"] == "device"
    assert "snmp" in row["sources"] and "ssh" not in row["sources"]
    plain = next(r for r in await rows_of(fleet_db) if r["name"] == "srv-w22-01")
    assert (plain["vendor"], plain["model"], plain["serial"]) == (None, None, None)
    assert plain["sources"] == ["agent"]


@pytest.mark.asyncio
async def test_asset_row_fields(fleet_db, fleet_info, pinned_clock):
    rows = {r["name"]: r for r in await rows_of(fleet_db)}
    s = rows["srv-w22-01"]
    assert s["key"] == f"server:{fleet_info.server_ids['srv-w22-01']}"
    assert (s["os_family"], s["os_product"], s["os_release"], s["os_source"]) == ("windows", "Windows Server", "2022", "agent")
    assert s["os_version"].startswith("10.0.20348") and s["os_build"].startswith("20348")
    assert s["freshness"] == "fresh" and s["inventory_age_s"] is not None and 0 <= s["inventory_age_s"] < 24 * 3600
    d = rows["fg-01"]
    assert (d["vendor"], d["os_family"], d["os_product"], d["os_source"]) == ("Fortinet", "fortios", "FortiOS", "template")
    assert d["sources"] == ["snmp"] and d["tags"] and d["ip"].startswith("10.")


@pytest.mark.asyncio
async def test_unknown_is_a_value_not_a_gap(fleet_db, fleet_info, pinned_clock):
    rows = await rows_of(fleet_db)
    unknown = [r for r in rows if r["os_family"] == "unknown"]
    assert len(unknown) == fleet_db.scalar(
        "SELECT count(*) FROM inventory_identity i WHERE i.os_family = 'unknown' AND (i.asset_kind = 'server' OR EXISTS "
        "(SELECT 1 FROM devices d WHERE d.id = i.asset_id AND NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id)))") > 0
    assert all(r["os_product"] is None and r["os_version"] is None for r in unknown)
    # They are counted, listed and drillable.
    assert await total_of(fleet_db, os_family="unknown") == len(unknown)


@pytest.mark.asyncio
async def test_asset_with_no_identity_row_is_still_listed_as_unknown(fleet_db_fresh, fleet_info, pinned_clock):
    db = fleet_db_fresh
    db.execute("SET zenplus.inventory_suppress = 'on'")      # no effect across connections; the delete trigger is harmless
    db.execute("DELETE FROM inventory_identity WHERE asset_kind = 'server' AND asset_id = $1", aid("server", "srv-w22-01"))
    rows = {r["name"]: r for r in await rows_of(db)}
    r = rows["srv-w22-01"]
    assert (r["os_family"], r["os_product"], r["vendor"]) == ("unknown", None, None)
    assert len(rows) == 145


@pytest.mark.asyncio
async def test_untagged_assets_have_an_empty_tag_list(fleet_db, fleet_info, pinned_clock):
    rows = {r["name"]: r for r in await rows_of(fleet_db)}
    assert rows["srv-w22-06"]["tags"] == [] and rows["sw-misc-04"]["tags"] == []


# ── freshness at the boundaries ──────────────────────────────────────

async def _fresh_of(db, name):
    return next(r["freshness"] for r in await rows_of(db) if r["name"] == name)


@pytest.mark.asyncio
async def test_server_freshness_boundaries(fleet_db_fresh, pinned_clock):
    db, name = fleet_db_fresh, "srv-w22-01"
    sid = aid("server", name)

    async def at(delta):
        db.execute("UPDATE servers SET inventory_updated_at = $2 WHERE id = $1", sid, FIXED_NOW - delta if delta is not None else None)
        return await _fresh_of(db, name)

    assert await at(timedelta(hours=24)) == "fresh"                  # exactly on the threshold is still fresh
    assert await at(timedelta(hours=24, seconds=1)) == "aging"
    assert await at(timedelta(days=7)) == "aging"                    # exactly stale_days is still aging
    assert await at(timedelta(days=7, seconds=1)) == "stale"
    assert await at(timedelta(seconds=-5)) == "fresh"                # a clock a few seconds ahead
    assert await at(None) == "never"


@pytest.mark.asyncio
async def test_device_freshness_uses_its_own_threshold(fleet_db_fresh, pinned_clock):
    db, name = fleet_db_fresh, "fg-01"
    did = aid("device", name)

    async def at(delta):
        db.execute("UPDATE devices SET identity_updated_at = $2 WHERE id = $1", did, FIXED_NOW - delta if delta is not None else None)
        return await _fresh_of(db, name)

    assert await at(timedelta(hours=2)) == "fresh"
    assert await at(timedelta(hours=2, seconds=1)) == "aging"        # a server would still be fresh here
    assert await at(timedelta(days=7, seconds=1)) == "stale"
    assert await at(None) == "never"


@pytest.mark.asyncio
async def test_freshness_thresholds_come_from_settings(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    db.execute("UPDATE servers SET inventory_updated_at = $2 WHERE id = $1", aid("server", "srv-w22-01"), FIXED_NOW - timedelta(hours=30))
    assert await _fresh_of(db, "srv-w22-01") == "aging"
    db.execute("INSERT INTO system_settings (key, value) VALUES ('inventory', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
               {"fresh_hours": {"server": 48, "device": 2}, "stale_days": 3})
    assert await _fresh_of(db, "srv-w22-01") == "fresh"
    db.execute("UPDATE servers SET inventory_updated_at = $2 WHERE id = $1", aid("server", "srv-w22-01"), FIXED_NOW - timedelta(days=4))
    assert await _fresh_of(db, "srv-w22-01") == "stale"


@pytest.mark.asyncio
async def test_freshness_buckets_partition_the_union(fleet_db, pinned_clock):
    rows = await rows_of(fleet_db)
    counts = Counter(r["freshness"] for r in rows)
    assert sum(counts.values()) == len(rows) and set(counts) == set(iq.FRESHNESS)
    for bucket in iq.FRESHNESS:
        assert await total_of(fleet_db, freshness=bucket) == counts[bucket]
    assert await total_of(fleet_db, freshness="stale_or_never") == counts["stale"] + counts["never"]
    never = [r for r in rows if r["freshness"] == "never"]
    assert all(r["inventory_at"] is None and r["inventory_age_s"] is None for r in never)


# ════════════════════════════════════════════════════════════════════
# Filters
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_every_simple_filter_agrees_with_the_full_list(fleet_db, pinned_clock):
    rows = await rows_of(fleet_db)

    def check(filters, predicate):
        return filters, {r["key"] for r in rows if predicate(r)}

    cases = [
        check({"kind": "server"}, lambda r: r["kind"] == "server"),
        check({"kind": "device"}, lambda r: r["kind"] == "device"),
        check({"os_family": "windows"}, lambda r: r["os_family"] == "windows"),
        check({"os_family": "FortiOS"}, lambda r: r["os_family"] == "fortios"),
        check({"os_family": "unknown"}, lambda r: r["os_family"] == "unknown"),
        check({"os_product": "windows server"}, lambda r: (r["os_product"] or "").lower() == "windows server"),
        check({"os_product": "unknown"}, lambda r: r["os_product"] is None),
        check({"os_release": "2019"}, lambda r: r["os_release"] == "2019"),
        check({"os_release": "unknown"}, lambda r: r["os_release"] is None),
        check({"os_family": "ios-xe", "os_version": "17.9.4a"}, lambda r: r["os_family"] == "ios-xe" and r["os_version"] == "17.9.4a"),
        check({"os_version": "unknown"}, lambda r: r["os_version"] is None),
        check({"vendor": "cisco"}, lambda r: (r["vendor"] or "").lower() == "cisco"),
        check({"model": "fortigate-100f"}, lambda r: (r["model"] or "").lower() == "fortigate-100f"),
        check({"site": "hq"}, lambda r: (r["site"] or "").lower() == "hq"),
        check({"source": "agent"}, lambda r: "agent" in r["sources"]),
        check({"source": "ssh"}, lambda r: "ssh" in r["sources"]),
        check({"source": "snmp"}, lambda r: "snmp" in r["sources"]),
        check({"source": "icmp"}, lambda r: "icmp" in r["sources"]),
        check({"freshness": "fresh"}, lambda r: r["freshness"] == "fresh"),
        check({"freshness": "aging"}, lambda r: r["freshness"] == "aging"),
        check({"freshness": "stale"}, lambda r: r["freshness"] == "stale"),
        check({"freshness": "never"}, lambda r: r["freshness"] == "never"),
        check({"freshness": "not_applicable"}, lambda r: r["freshness"] == "not_applicable"),
        check({"freshness": "stale_or_never"}, lambda r: r["freshness"] in ("stale", "never")),
        # X3: an asset that cannot report by design has no 'missing' identity either (it is counted on its own line)
        check({"health": "missing_os_version"}, lambda r: r["os_version"] is None and r["freshness"] != "not_applicable"),
        check({"health": "missing_model"}, lambda r: r["kind"] == "device" and r["model"] is None and r["freshness"] != "not_applicable"),
        check({"health": "missing_serial"}, lambda r: r["kind"] == "device" and r["serial"] is None and r["freshness"] != "not_applicable"),
        check({"health": "unknown_vendor"}, lambda r: r["kind"] == "device" and r["vendor"] is None and r["freshness"] != "not_applicable"),
        check({"vendor": "unknown"}, lambda r: r["vendor"] is None),
        check({"model": "unknown"}, lambda r: r["model"] is None),
        check({"health": "stale"}, lambda r: r["freshness"] == "stale"),
        check({"health": "never"}, lambda r: r["freshness"] == "never"),
        check({"health": "software_truncated"}, lambda r: r["software_truncated"]),
        check({"tag": ("prod",)}, lambda r: "prod" in r["tags"]),
        check({"tag": ("PROD", "pci")}, lambda r: "prod" in r["tags"] and "pci" in r["tags"]),
        check({"tag": ("no-such-tag",)}, lambda r: False),
    ]
    for filters, expected in cases:
        got = keys(await rows_of(fleet_db, **filters))
        assert got == expected, f"{filters}: {len(got)} != {len(expected)}"
    # all but the deliberately empty case select something on this fleet, so the comparison is not vacuous
    assert [f for f, e in cases if not e] == [{"tag": ("no-such-tag",)}]


@pytest.mark.asyncio
async def test_health_predicates_cover_network_devices_only_for_hardware_identity(fleet_db, pinned_clock):
    """S3: servers have no hardware identity until phase 3, so counting them would be noise."""
    for health in ("missing_model", "missing_serial", "unknown_vendor"):
        rows = await rows_of(fleet_db, health=health)
        assert rows and {r["kind"] for r in rows} == {"device"}, health
    servers = [r for r in await rows_of(fleet_db, kind="server") if r["model"] is None and r["vendor"] is None]
    assert servers, "fixture servers do lack hardware identity"        # ... yet none of them is 'missing a model'


@pytest.mark.asyncio
async def test_software_truncated_and_security_agent_health(fleet_db, fleet_info, pinned_clock):
    trunc = names(await rows_of(fleet_db, health="software_truncated"))
    assert trunc == set(TRUNCATED_SERVERS)
    missing = await rows_of(fleet_db, health="missing_security_agent")
    # missing: a complete, normalised list that shows no endpoint protection (a software row of that category or a running
    # Defender service). The independent oracle reads the raw tables.
    expected = fleet_db.query(
        """SELECT s.display_name FROM servers s WHERE s.inventory_baselined_at IS NOT NULL AND NOT s.software_truncated
              AND NOT EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.category = 'endpoint-protection')
              AND NOT EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id
                               AND v.service_name IN ('WinDefend', 'Sense') AND lower(v.state) = 'running')
              AND EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.norm_rev IS NULL)""")
    assert names(missing) == {x[0] for x in expected}
    # the ZenPlus Agent's own entry and a stopped sensor are not protection; a running Defender service is. A server that
    # never sent a service list (NO_SERVICE_LIST_SERVERS) cannot be judged for Defender: unknown (X9), not missing.
    assert set(ZENPLUS_ONLY_SERVERS) <= names(missing)
    assert set(NO_SECURITY_SERVERS) - set(DEFENDER_SERVICE_SERVERS) - set(NO_SERVICE_LIST_SERVERS) <= names(missing)
    assert not set(NO_SERVICE_LIST_SERVERS) & names(missing)
    assert not set(DEFENDER_SERVICE_SERVERS) & names(missing)
    assert not set(TRUNCATED_SERVERS) & names(missing), "a truncated list cannot prove the absence"
    assert {r["kind"] for r in missing} == {"server"}
    # not baselined (never reported / unknown OS) and truncated lists are 'unknown', not 'missing'
    unknown = names(await rows_of(fleet_db, security_agent="unknown"))
    assert unknown and not unknown & names(missing)
    assert set(TRUNCATED_SERVERS) <= unknown
    present = names(await rows_of(fleet_db, security_agent="present"))
    servers = names(await rows_of(fleet_db, kind="server"))
    assert present | names(missing) | unknown == servers
    assert not (present & names(missing)) and not (present & unknown)
    assert names(await rows_of(fleet_db, security_agent="missing")) == names(missing)


@pytest.mark.asyncio
async def test_product_filter_is_a_semi_join_on_the_catalog_key(fleet_db, pinned_clock):
    chrome = names(await rows_of(fleet_db, product="google-chrome"))
    expected = {x[0] for x in fleet_db.query(
        "SELECT s.display_name FROM servers s WHERE EXISTS (SELECT 1 FROM server_software_inventory w "
        "WHERE w.server_id = s.id AND w.name_key = 'google-chrome')")}
    assert chrome == expected and len(chrome) > 20
    rows = await rows_of(fleet_db, product="google-chrome")
    assert {r["kind"] for r in rows} == {"server"}
    v = fleet_db.scalar("SELECT version FROM server_software_inventory WHERE name_key = 'google-chrome' "
                        "GROUP BY version ORDER BY count(*) DESC, version LIMIT 1")
    by_version = names(await rows_of(fleet_db, product="google-chrome", product_version=v))
    expected_v = {x[0] for x in fleet_db.query(
        "SELECT s.display_name FROM servers s JOIN server_software_inventory w ON w.server_id = s.id "
        "WHERE w.name_key = 'google-chrome' AND w.version = $1", v)}
    assert by_version == expected_v and by_version < chrome
    assert await total_of(fleet_db, product="no-such-product") == 0
    assert await total_of(fleet_db, product="google-chrome", kind="device") == 0


@pytest.mark.asyncio
async def test_hardware_filters_describe_servers_only(fleet_db, pinned_clock):
    servers = await rows_of(fleet_db, kind="server")
    cpu = fleet_db.scalar("SELECT cpu_model FROM servers GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    exp = {x[0] for x in fleet_db.query("SELECT display_name FROM servers WHERE lower(cpu_model) = lower($1)", cpu)}
    assert names(await rows_of(fleet_db, cpu_model=cpu)) == exp
    unknown_cpu = names(await rows_of(fleet_db, cpu_model="unknown"))
    assert unknown_cpu == {x[0] for x in fleet_db.query("SELECT display_name FROM servers WHERE cpu_model IS NULL")}
    assert not unknown_cpu & {r["name"] for r in await rows_of(fleet_db, kind="device")}
    # buckets partition the servers
    buckets = [b[0] for b in iq.MEMORY_BUCKETS] + ["unknown"]
    seen = set()
    for b in buckets:
        got = names(await rows_of(fleet_db, memory=b))
        assert not got & seen
        seen |= got
    assert seen == names(servers)
    seen = set()
    for b in [b[0] for b in iq.DISK_BUCKETS] + ["unknown"]:
        got = names(await rows_of(fleet_db, disks=b))
        assert not got & seen
        seen |= got
    assert seen == names(servers)
    assert names(await rows_of(fleet_db, virtualization="vmware")) == {
        x[0] for x in fleet_db.query("SELECT display_name FROM servers WHERE virtualization = 'vmware'")}
    assert await total_of(fleet_db, virtualization="unknown") == fleet_db.scalar("SELECT count(*) FROM servers WHERE virtualization IS NULL")


@pytest.mark.asyncio
async def test_security_agent_values_partition_the_servers(fleet_db_fresh, pinned_clock):
    """present, missing and unknown are a partition of the servers, also after a server loses its baseline: with no
    software list nothing can be said about it, so it moves from 'missing' to 'unknown'."""
    victim = fleet_db_fresh.scalar(
        "SELECT s.display_name FROM servers s WHERE s.inventory_baselined_at IS NOT NULL AND NOT s.software_truncated "
        "AND NOT EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.category = 'endpoint-protection') "
        "AND EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id) ORDER BY s.display_name LIMIT 1")
    assert victim in names(await rows_of(fleet_db_fresh, security_agent="missing"))
    fleet_db_fresh.execute("UPDATE servers SET inventory_baselined_at = NULL WHERE display_name = $1", victim)
    servers = names(await rows_of(fleet_db_fresh, kind="server"))
    present = names(await rows_of(fleet_db_fresh, security_agent="present"))
    missing = names(await rows_of(fleet_db_fresh, security_agent="missing"))
    unknown = names(await rows_of(fleet_db_fresh, security_agent="unknown"))
    assert victim in unknown and victim not in present | missing
    assert present | missing | unknown == servers
    assert not (present & missing) and not (present & unknown) and not (missing & unknown)
    async with session(fleet_db_fresh) as sess:
        s = await iq.summary(sess, scope=None, use_cache=False)
    assert s["software"]["security_agent_coverage"] == {
        "covered": len(present), "missing": len(missing), "unknown": len(unknown), "total": len(servers)}
    assert s["health"]["missing_security_agent"] == len(missing)


@pytest.mark.asyncio
async def test_module_filters_select_devices_with_a_component(fleet_db, pinned_clock):
    def oracle(cls=None, model=None, unknown=False):
        cond = ["e.device_id = d.id", "e.class IN ('chassis','backplane','module','powerSupply','fan','cpu','stack')"]
        args = []
        if cls:
            args.append(cls)
            cond.append(f"e.class = ${len(args)}")
        if model:
            args.append(model)
            cond.append(f"btrim(e.model_name) = ${len(args)}")
        if unknown:
            cond.append("NULLIF(btrim(e.model_name), '') IS NULL")
        return {x[0] for x in fleet_db.query(
            "SELECT d.hostname FROM devices d WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id) "
            f"AND EXISTS (SELECT 1 FROM device_entities e WHERE {' AND '.join(cond)})", *args)}

    psu = names(await rows_of(fleet_db, module_class="powerSupply"))
    assert psu == oracle("powerSupply") and psu
    assert names(await rows_of(fleet_db, module_class="powerSupply", module_model="PWR-C1-715WAC-P")) == oracle("powerSupply", "PWR-C1-715WAC-P")
    assert names(await rows_of(fleet_db, module_model="EX4300-48T")) == oracle(model="EX4300-48T")
    assert await total_of(fleet_db, module_class="fan", module_model="PWR-C1-715WAC-P") == 0
    # a component class only network devices have: a server filter on it matches nothing, and the rows are devices
    assert {r["kind"] for r in await rows_of(fleet_db, module_class="chassis")} == {"device"}
    assert await total_of(fleet_db, module_class="chassis", kind="server") == 0
    # a polled device always has a chassis row: the class list is the devices that were ever walked
    assert names(await rows_of(fleet_db, module_class="chassis")) == oracle("chassis")
    with pytest.raises(iq.InvalidFilter):
        F(module_class="port").normalized()
    assert F(module_class="fan", module_model=" X ").normalized().as_params() == {"module_class": "fan", "module_model": "X"}


@pytest.mark.asyncio
async def test_free_text_search(fleet_db, fleet_info, pinned_clock):
    by_name = names(await rows_of(fleet_db, q="srv-w19"))
    assert by_name == {n for n in fleet_info.server_ids if n.startswith("srv-w19")}
    ip = next(r["ip"] for r in await rows_of(fleet_db) if r["name"] == "fg-01")
    assert "fg-01" in names(await rows_of(fleet_db, q=ip))
    assert names(await rows_of(fleet_db, q="FG200FTK"))                  # serial
    assert all((r["model"] or "").lower().startswith("c9300") for r in await rows_of(fleet_db, q="c9300"))
    assert names(await rows_of(fleet_db, q="fortinet")) == names(await rows_of(fleet_db, vendor="Fortinet"))
    assert names(await rows_of(fleet_db, q="Windows Server")) >= names(await rows_of(fleet_db, os_product="Windows Server"))
    assert names(await rows_of(fleet_db, q="17.9.4a")) == names(await rows_of(fleet_db, os_version="17.9.4a"))


@pytest.mark.asyncio
async def test_free_text_wildcards_are_literal(fleet_db, pinned_clock):
    assert await total_of(fleet_db, q="%") == 0
    assert await total_of(fleet_db, q="_") == 0
    assert await total_of(fleet_db, q="\\") == 0
    assert await total_of(fleet_db, q="srv-w1_-01") == 0                  # '_' is not 'any character'


@pytest.mark.asyncio
async def test_filters_combine_with_and(fleet_db, pinned_clock):
    rows = await rows_of(fleet_db)
    got = keys(await rows_of(fleet_db, kind="device", vendor="Cisco", freshness="fresh", tag=("prod",)))
    exp = {r["key"] for r in rows if r["kind"] == "device" and (r["vendor"] or "").lower() == "cisco"
           and r["freshness"] == "fresh" and "prod" in r["tags"]}
    assert got == exp and got
    got = keys(await rows_of(fleet_db, kind="server", os_family="windows", health="missing_os_version"))
    assert got == {r["key"] for r in rows if r["kind"] == "server" and r["os_family"] == "windows" and r["os_version"] is None}
    assert await total_of(fleet_db, kind="device", os_family="windows") == 0
    assert await total_of(fleet_db, product="google-chrome", health="stale") == len(
        [r for r in rows if r["freshness"] == "stale" and r["key"] in keys(await rows_of(fleet_db, product="google-chrome"))])


# ── sorting and paging ───────────────────────────────────────────────

def vnum(value):
    """The order the version sorts use: numeric parts first (7.2 < 7.10 < 17.9), the text as tie-break; None stays None."""
    if value is None:
        return None
    return (tuple(int(x) for x in re.findall(r"[0-9]+", value)), value)


SORT_VALUE = {
    "name": lambda r: r["name"].lower(), "kind": lambda r: r["kind"], "asset_type": lambda r: r["asset_type"],
    "status": lambda r: r["status"], "vendor": lambda r: (r["vendor"] or "").lower() if r["vendor"] else None,
    "model": lambda r: r["model"].lower() if r["model"] else None, "serial": lambda r: r["serial"],
    "os_family": lambda r: r["os_family"], "os_product": lambda r: r["os_product"].lower() if r["os_product"] else None,
    "os_release": lambda r: vnum(r["os_release"]), "os_version": lambda r: vnum(r["os_version"]),
    "site": lambda r: r["site"].lower() if r["site"] else None, "last_seen": lambda r: r["last_seen"],
    "inventory_at": lambda r: r["inventory_at"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", sorted(SORT_VALUE))
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_sorting_is_total_and_nulls_come_last(fleet_db, pinned_clock, sort, order):
    async with session(fleet_db) as s:
        items = (await iq.list_assets(s, F(), scope=None, sort=sort, order=order, page_size=500))["items"]
    vals = [SORT_VALUE[sort](r) for r in items]
    present = [v for v in vals if v is not None]
    assert vals == present + [None] * (len(vals) - len(present)), "NULLs last in both directions"
    assert present == sorted(present, reverse=(order == "desc"))
    # ties fall back to the asset address, ascending: the order is deterministic
    for a, b in zip(items, items[1:]):
        if SORT_VALUE[sort](a) == SORT_VALUE[sort](b):
            assert (a["kind"], a["id"]) < (b["kind"], b["id"])


@pytest.mark.asyncio
async def test_composite_and_derived_sorts(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        by_os = (await iq.list_assets(s, F(), scope=None, sort="os", page_size=500))["items"]
        by_fresh = (await iq.list_assets(s, F(), scope=None, sort="freshness", order="desc", page_size=500))["items"]
        by_ip = (await iq.list_assets(s, F(kind="device"), scope=None, sort="ip", page_size=500))["items"]
    pairs = [((r["os_product"] or "").lower(), r["os_version"]) for r in by_os if r["os_product"]]
    assert pairs == sorted(pairs, key=lambda p: (p[0], p[1] is None, vnum(p[1]) or ((), "")))      # BN-11: numeric parts, not text
    rank = {"fresh": 0, "aging": 1, "stale": 2, "never": 3, "not_applicable": 4}
    assert [rank[r["freshness"]] for r in by_fresh] == sorted((rank[r["freshness"]] for r in by_fresh), reverse=True)
    import ipaddress
    ips = [ipaddress.ip_address(r["ip"]) for r in by_ip]
    assert ips == sorted(ips), "IP addresses sort numerically, not as text"


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["name", "vendor", "freshness", "os"])
async def test_paging_covers_every_row_exactly_once(fleet_db, pinned_clock, sort):
    async with session(fleet_db) as s:
        full = (await iq.list_assets(s, F(), scope=None, sort=sort, page_size=500))["items"]
        seen, page = [], 1
        while True:
            r = await iq.list_assets(s, F(), scope=None, sort=sort, page=page, page_size=13)
            assert r["total"] == len(full) and r["page"] == page and r["page_size"] == 13
            seen += r["items"]
            if not r["items"]:
                break
            page += 1
    assert [x["key"] for x in seen] == [x["key"] for x in full]
    assert page == -(-len(full) // 13) + 1


@pytest.mark.asyncio
async def test_a_page_past_the_end_is_empty_but_keeps_the_total(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        r = await iq.list_assets(s, F(kind="server"), scope=None, page=99, page_size=50)
    assert r["items"] == [] and r["total"] == 60
    async with session(fleet_db) as s:
        r = await iq.list_assets(s, F(vendor="nobody"), scope=None)
    assert r["items"] == [] and r["total"] == 0


@pytest.mark.asyncio
async def test_list_rejects_bad_arguments(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        for kw in ({"sort": "password"}, {"order": "up"}, {"page_size": 501}, {"page": 0}):
            with pytest.raises(iq.InvalidFilter):
                await iq.list_assets(s, F(), scope=None, **kw)
        with pytest.raises(iq.InvalidFilter):
            await iq.list_assets(s, F(kind="gateway"), scope=None)


# ── facets ───────────────────────────────────────────────────────────

def facet_param(name, value):
    return {"tag": (value,)} if name == "tag" else {name: value}


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [{}, {"kind": "device"}, {"os_family": "windows"}, {"vendor": "Cisco", "freshness": "fresh"},
                                    {"tag": ("prod",), "kind": "server"}])
async def test_a_facet_count_is_the_total_of_choosing_that_value(fleet_db, pinned_clock, active):
    """Principle 1 for the facet panel: clicking a value gives a list of exactly that size."""
    async with session(fleet_db) as s:
        res = await iq.asset_facets(s, F(**active), scope=None)
        assert res["total"] == (await iq.list_assets(s, F(**active), scope=None, page_size=1))["total"]
        for name, group in res["facets"].items():
            for item in group["items"]:
                if name == "tag":
                    chosen = F(**{**active, "tag": tuple(active.get("tag", ())) + (item["value"],)})
                    if item["value"] in [t.lower() for t in active.get("tag", ())]:
                        continue
                else:
                    chosen = F(**{**active, name: item["value"]})
                got = (await iq.list_assets(s, chosen, scope=None, page_size=1))["total"]
                if name == "source":
                    pass
                assert got == item["count"], f"{active} + {name}={item['value']}: {got} != {item['count']}"


@pytest.mark.asyncio
async def test_facets_ignore_their_own_filter_only(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        base = await iq.asset_facets(s, F(), scope=None)
        narrowed = await iq.asset_facets(s, F(kind="device", vendor="Cisco"), scope=None)
    # the vendor facet still lists every vendor of the devices (its own filter is ignored) ...
    devices_only = await rows_of(fleet_db, kind="device")
    vendors = Counter(r["vendor"] for r in devices_only if r["vendor"])
    assert {i["value"]: i["count"] for i in narrowed["facets"]["vendor"]["items"]} == dict(vendors)
    # ... while every other facet honours it
    assert {i["value"] for i in narrowed["facets"]["kind"]["items"] if i["count"]} == {"device"}
    cisco = await rows_of(fleet_db, kind="device", vendor="Cisco")
    assert sum(i["count"] for i in narrowed["facets"]["freshness"]["items"]) == len(cisco)
    assert base["total"] == 145 and narrowed["total"] == len(cisco)


@pytest.mark.asyncio
async def test_facets_list_fixed_vocabularies_with_zero_counts(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        res = await iq.asset_facets(s, F(kind="server", os_family="windows"), scope=None)
    f = res["facets"]
    assert [i["value"] for i in f["kind"]["items"]] == ["server", "device"]
    assert [i["value"] for i in f["freshness"]["items"]] == list(iq.FRESHNESS)
    assert [i["value"] for i in f["health"]["items"]] == list(iq.HEALTH_KEYS)
    assert {i["value"]: i["count"] for i in f["kind"]["items"]}["device"] == 0
    health = {i["value"]: i["count"] for i in f["health"]["items"]}
    assert health["unknown_vendor"] == 0 and health["missing_model"] == 0       # devices only


@pytest.mark.asyncio
async def test_facets_unknown_values_are_listed_and_labelled(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        f = (await iq.asset_facets(s, F(), scope=None))["facets"]
    fam = {i["value"]: i for i in f["os_family"]["items"]}
    assert fam["unknown"]["label"] == "Unknown" and fam["unknown"]["count"] > 0
    assert fam["fortios"]["label"] == "FortiOS" and fam["ios-xe"]["label"] == "IOS XE"
    prod = {i["value"]: i for i in f["os_product"]["items"]}
    assert prod["unknown"]["label"] == "Unknown"
    # vendor/model/site have no 'unknown' entry; the health panel carries them
    assert "unknown" not in {i["value"] for i in f["vendor"]["items"]}
    assert f["tag"]["distinct"] == len(f["tag"]["items"]) > 5
    assert all(i["label"] for g in f.values() for i in g["items"])


@pytest.mark.asyncio
async def test_tag_facet_counts_assets_not_tag_occurrences(fleet_db, pinned_clock):
    rows = await rows_of(fleet_db)
    async with session(fleet_db) as s:
        f = (await iq.asset_facets(s, F(), scope=None))["facets"]
    counts = {i["value"]: i["count"] for i in f["tag"]["items"]}
    for tag in ("prod", "pci", SCOPED_TAG, "dc-east"):
        assert counts[tag] == sum(1 for r in rows if tag in [t.lower() for t in r["tags"]])


@pytest.mark.asyncio
async def test_source_facet_counts_assets_having_the_source(fleet_db, pinned_clock):
    rows = await rows_of(fleet_db)
    async with session(fleet_db) as s:
        f = (await iq.asset_facets(s, F(), scope=None))["facets"]
    counts = {i["value"]: i["count"] for i in f["source"]["items"]}
    assert counts == dict(Counter(src for r in rows for src in set(r["sources"])))


# ════════════════════════════════════════════════════════════════════
# The flyout
# ════════════════════════════════════════════════════════════════════

async def detail(db, kind, name_or_id, scope=None, ids=None, **kw):
    async with session(db) as s:
        return await iq.asset_detail(s, kind, name_or_id, scope=scope, **kw)


@pytest.mark.asyncio
async def test_server_flyout(fleet_db, fleet_info, pinned_clock):
    d = await detail(fleet_db, "server", fleet_info.server_ids["srv-w22-01"])
    assert d["asset"]["name"] == "srv-w22-01" and d["device"] is None and d["server"]
    ident = {i["field"]: i for i in d["identity"]}
    assert [i["field"] for i in d["identity"]] == ["vendor", "model", "serial", "os_family", "os_product", "os_release",
                                                    "os_version", "os_build", "os_edition"]
    assert ident["os_product"]["value"] == "Windows Server" and ident["os_product"]["source"] == "agent"
    assert ident["os_product"]["source_detail"] == "agent" and ident["os_version"]["age_s"] is not None
    assert ident["vendor"]["value"] is None and ident["vendor"]["source"] is None        # unknown is a value
    sv = d["server"]
    assert sv["cpu_model"] and sv["memory_total_bytes"] > 0 and sv["baselined"] is True
    assert isinstance(sv["disks"], list) and sv["disks"]
    sw = sv["software"]
    assert len(sw["top"]) == min(10, sw["total"] - sw["hidden"]) and sw["shown"] == len(sw["top"])
    assert sw["hidden"] > 0                                                              # KB updates, drivers, language packs
    assert all(t["category"] not in ("update", "driver", "os-component") for t in sw["top"])
    assert sw["security_agent"]["present"] is True and sw["security_agent"]["products"]
    cats = [t["category"] == "endpoint-protection" for t in sw["top"]]
    assert cats == sorted(cats, reverse=True), "endpoint-protection products are listed first"
    assert d["links"] == {"server_id": fleet_info.server_ids["srv-w22-01"], "device_id": None}
    assert d["raw"]["os_name"].startswith("Microsoft Windows Server 2022")
    assert d["changes"]["total"] >= 1 and len(d["changes"]["items"]) <= 10


@pytest.mark.asyncio
async def test_server_flyout_flags(fleet_db, fleet_info, pinned_clock):
    nosec = await detail(fleet_db, "server", fleet_info.server_ids[NO_SECURITY_SERVERS[0]])
    assert nosec["server"]["software"]["security_agent"] == {
        "present": False, "state": "missing", "reason": None, "products": [], "services": []}
    defender = await detail(fleet_db, "server", fleet_info.server_ids[DEFENDER_SERVICE_SERVERS[0]])
    assert defender["server"]["software"]["security_agent"] == {
        "present": True, "state": "present", "reason": None, "products": [], "services": ["WinDefend"]}
    trunc = await detail(fleet_db, "server", fleet_info.server_ids[TRUNCATED_SERVERS[0]])
    assert trunc["server"]["software"]["truncated"] is True and trunc["asset"]["software_truncated"] is True
    sec = trunc["server"]["software"]["security_agent"]       # a cut list cannot show the absence of protection (UP-13)
    assert sec["present"] is None and sec["state"] == "unknown" and sec["reason"] == "list_truncated"
    never = await detail(fleet_db, "server", fleet_info.server_ids["srv-new-01"])
    assert never["asset"]["freshness"] == "never" and never["server"]["baselined"] is False
    assert never["server"]["software"]["security_agent"]["present"] is None, "no inventory yet is unknown, not 'absent'"
    assert never["server"]["software"]["security_agent"]["reason"] == "no_software_list"
    assert never["server"]["software"]["total"] == 0 and never["changes"]["total"] == 0
    assert all(i["value"] is None for i in never["identity"])


@pytest.mark.asyncio
async def test_server_with_an_snmp_twin_links_both_and_shows_its_changes(fleet_db, fleet_info, pinned_clock):
    name = LINKED_SERVERS[2]            # an edge server whose twin has a firmware event
    d = await detail(fleet_db, "server", fleet_info.server_ids[name])
    assert d["links"] == {"server_id": fleet_info.server_ids[name], "device_id": fleet_info.linked_device_ids[name]}
    assert any(c["asset_kind"] == "device" and c["category"] == "firmware" for c in d["changes"]["items"])
    assert {i["field"]: i["value"] for i in d["identity"]}["vendor"] == "Dell"
    # the twin itself is not an asset
    assert await detail(fleet_db, "device", fleet_info.linked_device_ids[name]) is None


@pytest.mark.asyncio
async def test_device_flyout_with_template_source_modules_and_ha(fleet_db, fleet_info, pinned_clock):
    d = await detail(fleet_db, "device", fleet_info.device_ids["fg-02"])
    assert d["server"] is None and d["device"]
    ident = {i["field"]: i for i in d["identity"]}
    assert ident["os_version"]["source"] == "template" and ident["os_version"]["source_detail"] == "template"
    assert ident["os_version"]["age_s"] is not None and ident["os_family"]["value"] == "fortios"
    assert ident["vendor"]["value"] == "Fortinet" and ident["model"]["value"]
    dev = d["device"]
    assert dev["profile"] == "FortiGate" and dev["group"] == "Firewalls" and dev["device_type"] == "firewall"
    assert dev["ha"] == {"mode": "Active-Passive", "role": "Primary", "peer": None, "sync": None,
                         "observed_at": dev["ha"]["observed_at"], "age_s": dev["ha"]["age_s"]}
    assert dev["modules"][0]["class"] == "chassis" and dev["modules"][0]["count"] == 1
    assert dev["modules"][0]["items"][0]["serial"]
    assert d["raw"]["sys_descr"].startswith("FortiGate")
    assert d["links"] == {"server_id": None, "device_id": fleet_info.device_ids["fg-02"]}


@pytest.mark.asyncio
async def test_device_flyout_modules_by_class_and_model_and_pan_ha(fleet_db, fleet_info, pinned_clock):
    d = await detail(fleet_db, "device", fleet_info.device_ids["sw-c9-03"])
    classes = [(g["class"], g["model"], g["count"]) for g in d["device"]["modules"]]
    assert ("chassis", d["asset"]["model"], 1) in classes
    assert any(c == "powerSupply" and m == "PWR-C1-715WAC-P" and n == 2 for c, m, n in classes)
    assert d["device"]["ha"] is None
    assert not any(g["class"] == "port" for g in d["device"]["modules"]), "ports are noise here"
    pa = await detail(fleet_db, "device", fleet_info.device_ids["pa-03"])
    assert pa["device"]["ha"]["mode"] == "active-passive" and pa["device"]["ha"]["role"] == "active"
    assert pa["device"]["ha"]["peer"] == "passive"
    assert pa["changes"]["items"][0]["category"] == "hardware"


@pytest.mark.asyncio
async def test_never_polled_device_flyout_is_all_unknown(fleet_db, fleet_info, pinned_clock):
    d = await detail(fleet_db, "device", fleet_info.device_ids["edge-unk-01"])
    assert d["asset"]["freshness"] == "never" and d["device"]["ha"] is None and d["device"]["modules"] == []
    assert all(i["value"] is None for i in d["identity"][:3])
    assert {i["field"]: i["value"] for i in d["identity"]}["os_family"] is None


@pytest.mark.asyncio
async def test_flyout_is_none_for_unknown_malformed_and_wrong_kind(fleet_db, fleet_info, pinned_clock):
    sid = fleet_info.server_ids["srv-w22-01"]
    assert await detail(fleet_db, "server", str(uuid.uuid4())) is None
    assert await detail(fleet_db, "server", "not-a-uuid") is None
    assert await detail(fleet_db, "router", sid) is None
    assert await detail(fleet_db, "device", sid) is None                  # right id, wrong kind
    assert await detail(fleet_db, "server", sid.upper()) is not None      # uuids are case-insensitive


# ════════════════════════════════════════════════════════════════════
# Operating systems
# ════════════════════════════════════════════════════════════════════

def walk_os(tree):
    """(filters, count) for every node of the tree, as the UI would drill it."""
    kind = tree["kind"]
    base = {"kind": kind} if kind else {}
    for fam in tree["families"]:
        yield {**base, "os_family": fam["family"]}, fam["count"]
        for prod in fam["products"]:
            pf = {**base, "os_family": fam["family"], "os_product": prod["product"] or "unknown"}
            yield pf, prod["count"]
            for rel in prod["releases"]:
                rf = {**pf, "os_release": rel["release"] or "unknown"}
                yield rf, rel["count"]
                for ver in rel["versions"]:
                    yield {**rf, "os_version": ver["version"] or "unknown"}, ver["count"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "server", "device"])
async def test_os_tree_counts_equal_the_drilled_list_totals(fleet_db, pinned_clock, kind):
    async with session(fleet_db) as s:
        tree = await iq.os_tree(s, scope=None, kind=kind)
    nodes = list(walk_os(tree))
    assert len(nodes) > 20
    for filters, count in nodes:
        assert await total_of(fleet_db, **filters) == count, filters
    assert tree["total"] == await total_of(fleet_db, **({"kind": kind} if kind else {}))
    assert sum(f["count"] for f in tree["families"]) == tree["total"]


@pytest.mark.asyncio
async def test_os_tree_shape_order_and_unknown(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        tree = await iq.os_tree(s, scope=None)
    fams = [f["family"] for f in tree["families"]]
    assert fams[-1] == "unknown" and fams[0] == "windows"
    assert [f["count"] for f in tree["families"][:-1]] == sorted((f["count"] for f in tree["families"][:-1]), reverse=True)
    win = tree["families"][0]
    assert win["label"] == "Windows"
    server_prod = next(p for p in win["products"] if p["product"] == "Windows Server")
    assert [r["release"] for r in server_prod["releases"]] == ["2022", "2019", "2016"]       # newest first
    unknown = tree["families"][-1]
    assert unknown["products"][0]["product"] is None and unknown["products"][0]["releases"][0]["release"] is None
    assert unknown["products"][0]["releases"][0]["versions"][0]["version"] is None


@pytest.mark.asyncio
async def test_os_tree_build_currency_and_latest_version(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        tree = await iq.os_tree(s, scope=None, kind="server")
    win = next(f for f in tree["families"] if f["family"] == "windows")
    prod = next(p for p in win["products"] if p["product"] == "Windows Server")
    rel = next(r for r in prod["releases"] if r["release"] == "2022")
    versions = {v["version"]: v["count"] for v in rel["versions"]}
    assert set(versions) <= {"10.0.20348.587", "10.0.20348.2402", "10.0.20348.2700"} and len(versions) == 3
    cur = rel["currency"]
    assert rel["latest_version"] == cur["latest_version"] == "10.0.20348.2700"
    assert cur["previous_version"] == "10.0.20348.2402"
    assert (cur["latest"], cur["previous"], cur["older"], cur["unknown"]) == (
        versions["10.0.20348.2700"], versions["10.0.20348.2402"], versions["10.0.20348.587"], 0)
    assert cur["latest"] + cur["previous"] + cur["older"] == rel["count"] == 18      # 22 minus 4 never-reported
    assert [v["version"] for v in rel["versions"]] == ["10.0.20348.2700", "10.0.20348.2402", "10.0.20348.587"]
    assert rel["distinct_versions"] == 3


@pytest.mark.asyncio
async def test_os_tree_firmware_shows_the_raw_string_when_it_differs(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        tree = await iq.os_tree(s, scope=None, kind="device")
    assert {f["family"] for f in tree["families"]} >= {"fortios", "panos", "ios-xe", "ios", "aos-cx", "junos", "nx-os", "tmos", "unknown"}
    forti = next(f for f in tree["families"] if f["family"] == "fortios")
    rel = next(r for r in forti["products"][0]["releases"] if r["release"] == "7.2")
    ver = next(v for v in rel["versions"] if v["version"] == "7.2.8")
    assert ver["raw_example"] == "v7.2.8,build1639,240614 (GA.M)"
    cisco = next(f for f in tree["families"] if f["family"] == "ios-xe")
    xe = {v["version"]: v["raw_example"] for r in cisco["products"][0]["releases"] for v in r["versions"]}
    assert xe["17.9.4a"] == "17.09.04a", "Cisco zero-pads its release train; the normalised form drops the zeros"
    pan = next(f for f in tree["families"] if f["family"] == "panos")
    assert all(v["raw_example"] is None for r in pan["products"][0]["releases"] for v in r["versions"]), "raw == normalised: nothing to show"
    assert next(r for r in forti["products"][0]["releases"] if r["release"] == "7.4")["currency"]["latest_version"] == "7.4.4"


@pytest.mark.asyncio
async def test_os_tree_kind_filter_is_validated(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        with pytest.raises(iq.InvalidFilter):
            await iq.os_tree(s, scope=None, kind="router")


# ════════════════════════════════════════════════════════════════════
# Hardware
# ════════════════════════════════════════════════════════════════════

async def hw(db, **kw):
    async with session(db) as s:
        return await iq.hardware(s, scope=kw.pop("scope", None), **kw)


@pytest.mark.asyncio
async def test_hardware_vendor_and_model_tables_add_up_and_drill(fleet_db, pinned_clock):
    h = await hw(fleet_db)
    net = h["network"]
    assert net["devices"] == sum(v["count"] for v in net["vendors"]) == await total_of(fleet_db, kind="device") == 85
    for v in net["vendors"]:
        if v["vendor"] is None:
            # 'unknown' is a value: every device with no vendor, whether or not it can report (health=unknown_vendor is the
            # worklist and skips the ones that cannot)
            assert await total_of(fleet_db, kind="device", vendor="unknown") == v["count"]
            assert await total_of(fleet_db, kind="device", health="unknown_vendor") == v["count"] - v["not_collected"]
            assert await total_of(fleet_db, kind="device", vendor="unknown", health="missing_serial") == v["serial_missing"]
            assert not [m for m in net["models"] if m["vendor"] is None]
            continue
        assert await total_of(fleet_db, kind="device", vendor=v["vendor"]) == v["count"]
        models = [m for m in net["models"] if m["vendor"] == v["vendor"]]
        assert sum(m["count"] for m in models) == v["count"]
        assert v["models"] == v["models_known"] == len([m for m in models if m["model"] is not None])
        assert v["unknown_model_devices"] == sum(m["count"] for m in models if m["unknown"])
        for m in models:
            assert m["unknown"] == (m["model"] is None)
            f = {"kind": "device", "vendor": v["vendor"], "model": m["model"] or "unknown"}
            assert await total_of(fleet_db, **f) == m["count"], f
            assert await total_of(fleet_db, **{**f, "health": "missing_serial"}) == m["serial_missing"], f
    assert net["vendors"][-1]["vendor"] is None                         # unknown vendor listed last
    assert sum(v["serial_missing"] for v in net["vendors"]) == await total_of(fleet_db, kind="device", health="missing_serial")
    # N models plus M devices with no model, without counting rows: the Overview's model figure is the same number
    assert net["models_known"] == len([m for m in net["models"] if m["model"] is not None])
    assert net["unknown_model_devices"] == await total_of(fleet_db, kind="device", model="unknown")


@pytest.mark.asyncio
async def test_hardware_firmware_version_counts_and_modules_per_model(fleet_db, pinned_clock):
    h = await hw(fleet_db)
    rows = await rows_of(fleet_db, kind="device")
    for m in h["network"]["models"]:
        group = [r for r in rows if r["vendor"] == m["vendor"] and r["model"] == m["model"]]
        assert m["firmware_versions"] == len({r["os_version"] for r in group if r["os_version"]})
    c93 = next(m for m in h["network"]["models"] if m["model"] == "C9300-48P")
    expected = fleet_db.scalar(
        "SELECT count(*) FROM device_entities e JOIN devices d ON d.id = e.device_id JOIN inventory_identity i "
        "ON i.asset_id = d.id AND i.asset_kind = 'device' WHERE e.class = 'module' AND i.hw_model = 'C9300-48P'")
    assert c93["modules"] == expected > 0


@pytest.mark.asyncio
async def test_hardware_serial_coverage_is_the_share_of_devices_with_a_serial(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        hw = await iq.hardware(s, scope=None)
    for row in hw["network"]["vendors"] + hw["network"]["models"]:
        # over the devices that can report: one that cannot, by design, is neither covered nor a gap (X3)
        reporting = row["count"] - row["not_collected"]
        if reporting:
            assert row["serial_coverage_pct"] == round(100.0 * (reporting - row["serial_missing"]) / reporting, 1)
            assert 0.0 <= row["serial_coverage_pct"] <= 100.0
        else:
            assert row["serial_coverage_pct"] is None
    assert any(0 < r["serial_coverage_pct"] < 100 for r in hw["network"]["vendors"] if r["serial_coverage_pct"] is not None)


@pytest.mark.asyncio
async def test_hardware_matrix_for_a_vendor(fleet_db, pinned_clock):
    assert (await hw(fleet_db))["matrix"] is None
    h = await hw(fleet_db, vendor="fortinet")
    mx = h["matrix"]
    assert mx["vendor"] == "Fortinet" and mx["releases"][0] == "7.4" and "7.2" in mx["releases"]
    known = [r for r in mx["releases"] if r != "unknown"]
    assert known == sorted(known, key=iq._natural_key, reverse=True)
    assert mx["releases"][-1] == "unknown"
    assert sum(m["count"] for m in mx["models"]) == await total_of(fleet_db, kind="device", vendor="Fortinet")
    for m in mx["models"]:
        assert sum(c["count"] for c in m["cells"]) == m["count"]
        for c in m["cells"]:
            f = {"kind": "device", "vendor": "Fortinet", "os_release": c["release"]}
            f.update({"model": m["model"]} if m["model"] else {"health": "missing_model"})
            assert await total_of(fleet_db, **f) == c["count"], f
    assert (await hw(fleet_db, vendor="no such vendor"))["matrix"] == {"vendor": "no such vendor", "releases": [], "models": []}


@pytest.mark.asyncio
async def test_hardware_module_groups(fleet_db, pinned_clock):
    h = await hw(fleet_db)
    groups = {(g["class"], g["model"]): g for g in h["modules"]}
    assert ("port", None) not in groups and all(g["class"] != "port" for g in h["modules"])
    assert [g["class"] for g in h["modules"]][0] == "chassis"
    ps = groups[("powerSupply", "PWR-C1-715WAC-P")]
    assert ps["count"] == 2 * ps["devices"] and ps["with_serial"] <= ps["count"] and ps["class_label"] == "Power supply"
    assert sum(g["count"] for g in h["modules"] if g["class"] == "chassis") == fleet_db.scalar(
        "SELECT count(*) FROM device_entities e WHERE e.class = 'chassis' AND e.device_id NOT IN (SELECT device_id FROM servers WHERE device_id IS NOT NULL)")


@pytest.mark.asyncio
async def test_hardware_server_distributions_partition_the_servers(fleet_db, pinned_clock):
    h = await hw(fleet_db)
    s = h["servers"]
    assert s["total"] == 60
    for key in ("cpu_models", "memory", "disks"):
        assert sum(i["count"] for i in s[key]) == 60, key
    assert sum(i["count"] for i in s["virtualization"]) == 60
    assert [i["value"] for i in s["memory"]][:6] == [b[0] for b in iq.MEMORY_BUCKETS]
    # every item is a link: its count is the Assets total for the filter it names
    for item in s["cpu_models"]:
        assert await total_of(fleet_db, cpu_model=item["value"]) == item["count"]
    for item in s["memory"]:
        assert await total_of(fleet_db, memory=item["value"]) == item["count"]
    for item in s["disks"]:
        assert await total_of(fleet_db, disks=item["value"]) == item["count"]
    for item in s["virtualization"]:
        assert await total_of(fleet_db, virtualization=item["value"]) == item["count"]
    assert s["cpu_models_more"] == {"models": 0, "servers": 0}
    unknown_cpu = next(i for i in s["cpu_models"] if i["value"] == "unknown")
    assert unknown_cpu["count"] == 8 and unknown_cpu["label"] == "Unknown"       # 4 unknown-OS + 4 never reported


@pytest.mark.asyncio
async def test_hardware_virtualisation_is_null_until_a_server_reports_it(fleet_db_fresh, pinned_clock):
    fleet_db_fresh.execute("UPDATE servers SET virtualization = NULL")
    assert (await hw(fleet_db_fresh))["servers"]["virtualization"] is None
    fleet_db_fresh.execute("UPDATE servers SET virtualization = 'kvm' WHERE display_name = 'srv-w22-01'")
    v = (await hw(fleet_db_fresh))["servers"]["virtualization"]
    assert v[0] == {"value": "kvm", "label": "kvm", "count": 1} and v[-1]["value"] == "unknown"


@pytest.mark.asyncio
async def test_hardware_cpu_list_is_capped_with_a_remainder(fleet_db_fresh, pinned_clock):
    async with session(fleet_db_fresh) as s:
        h = await iq.hardware(s, scope=None, cpu_limit=2)
    cpu = h["servers"]["cpu_models"]
    assert len([i for i in cpu if i["value"] != "unknown"]) == 2
    more = h["servers"]["cpu_models_more"]
    assert more["models"] == 1 and more["servers"] > 0
    assert sum(i["count"] for i in cpu) + more["servers"] == 60


# ════════════════════════════════════════════════════════════════════
# Changes
# ════════════════════════════════════════════════════════════════════

async def chg(db, scope=None, page=1, page_size=50, tz="UTC", **kw):
    async with session(db) as s:
        return await iq.changes(s, scope=scope, params=iq.ChangeFilters(**kw), page=page, page_size=page_size, tz=tz)


@pytest.mark.asyncio
async def test_changes_default_window_is_seven_days(fleet_db, fleet_info, pinned_clock):
    c = await chg(fleet_db, page_size=200)
    assert c["window"] == {"since": "2026-10-01T12:00:00Z", "until": None, "tz": "UTC", "tz_fallback": False}
    expected = fleet_db.scalar("SELECT count(*) FROM inventory_change_events WHERE observed_at >= $1", FIXED_NOW - timedelta(days=7))
    assert c["total"] == expected == len(c["items"]) == c["stats"]["total"]
    assert c["total"] < fleet_info.event_count                         # the older events stay out
    ats = [x["observed_at"] for x in c["items"]]
    assert ats == sorted(ats, reverse=True)


@pytest.mark.asyncio
async def test_changes_stats_match_the_event_table(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=1)
    st = c["stats"]
    sql = lambda cat, act=None: fleet_db.scalar(            # noqa: E731
        "SELECT count(*) FROM inventory_change_events WHERE observed_at >= $1 AND category = $2 AND ($3::text IS NULL OR action = $3)",
        FIXED_NOW - timedelta(days=7), cat, act)
    assert st["software"]["added"] == sql("software", "added") == 12
    assert st["software"]["removed"] == sql("software", "removed") == 12
    assert st["software"]["changed"] == sql("software", "changed") == 15
    assert (st["os"], st["firmware"], st["hardware"]) == (sql("os"), sql("firmware"), sql("hardware")) == (4, 11, 2)
    assert st["software"]["upgraded"] == 12 and st["software"]["downgraded"] == 1
    assert st["software"]["upgraded"] + st["software"]["downgraded"] < st["software"]["changed"]      # unorderable + marker remain
    assert st["total"] == 12 + 12 + 15 + 4 + 11 + 2


@pytest.mark.asyncio
async def test_changes_labels_upgrades_downgrades_and_unorderable_versions(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=200, category="software", action="changed")
    by = Counter((r["subject"], r["label"]) for r in c["items"])
    assert by[("Google Chrome", "upgrade")] == 12 and by[("Google Chrome", "downgrade")] == 1
    nightly = next(r for r in c["items"] if r["subject"].startswith("Notepad++"))
    assert (nightly["old_value"], nightly["new_value"], nightly["label"]) == ("nightly-build", "release-candidate", None)
    marker = next(r for r in c["items"] if r["marker"])
    assert marker["subject"] == "(change budget exceeded)" and marker["label"] is None and marker["detail"]["budget_exceeded"] is True
    other = await chg(fleet_db, page_size=200, category="firmware")
    assert all(r["label"] is None for r in other["items"])          # labels are for software version changes


@pytest.mark.asyncio
async def test_changes_pair_an_installation_and_a_removal_into_an_upgrade_or_downgrade(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=200, category="software")
    java = [r for r in c["items"] if r["subject_key"] == "java-se-runtime"]
    assert len(java) == 12
    assert all(r["pair"] and r["pair"]["label"] == "upgrade" for r in java)
    ids = {r["id"] for r in java}
    assert {r["pair"]["event_id"] for r in java} == ids, "each half points at the other"
    zip_rows = [r for r in c["items"] if r["subject_key"] == "7-zip"]
    assert len(zip_rows) == 2 and all(r["pair"]["label"] == "downgrade" for r in zip_rows)
    assert c["stats"]["software"]["paired"] == {"upgraded": 6, "downgraded": 1}
    assert all(r["pair"] is None for r in c["items"] if r["subject_key"] in ("teamviewer", "anydesk"))
    # filters never split what the stats strip counts: raw events stay raw
    assert c["stats"]["software"]["added"] == 12 and c["stats"]["software"]["removed"] == 12


@pytest.mark.asyncio
async def test_changes_pairing_needs_exactly_one_addition_and_one_removal(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    at = FIXED_NOW - timedelta(hours=1)
    sid = aid("server", "srv-w22-01")
    for action, old, new in (("added", None, "2.0"), ("added", None, "3.0"), ("removed", "1.0", None)):
        db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, asset_name, category, action, subject, "
                   "subject_key, old_value, new_value) VALUES ($1,'server',$2,'srv-w22-01','software',$3,'Ambig 1','ambig',$4,$5)",
                   at, sid, action, old, new)
    c = await chg(db, page_size=200, subject_key="ambig")
    assert c["total"] == 3 and all(r["pair"] is None for r in c["items"])
    assert c["stats"]["software"]["paired"] == {"upgraded": 0, "downgraded": 0}


@pytest.mark.asyncio
async def test_changes_filters(fleet_db, fleet_info, pinned_clock):
    async def total(**kw):
        return (await chg(fleet_db, page_size=1, **kw))["total"]

    st = (await chg(fleet_db, page_size=1))["stats"]
    assert await total(category="software") == st["software"]["added"] + st["software"]["removed"] + st["software"]["changed"]
    assert await total(category="os") == 4 and await total(category="firmware") == 11 and await total(category="hardware") == 2
    assert await total(category="software", action="added") == 12
    assert await total(action="changed") == 15 + 4 + 11 + 2
    assert await total(kind="device") == 11 + 2                               # firmware (the twins' two included) + hardware
    assert await total(kind="server") == 12 + 12 + 15 + 4
    assert await total(subject_key="google-chrome") == 13
    assert await total(subject_key="nonexistent") == 0
    assert await total(q="chrome") == 13
    assert await total(q="152.0.8001.12") == 13            # twelve upgrades to it, one downgrade from it
    assert await total(q="%") == 0
    assert await total(label="upgrade") == st["software"]["upgraded"] == 12
    assert await total(label="downgrade") == st["software"]["downgraded"] == 1
    assert await total(label="upgrade", category="software", action="changed") == 12
    assert await total(label="upgrade", category="firmware") == 0
    c = await chg(fleet_db, page_size=50, label="downgrade")
    assert [r["label"] for r in c["items"]] == ["downgrade"] and c["items"][0]["old_value"] == "152.0.8001.12"


@pytest.mark.asyncio
async def test_changes_stats_and_series_ignore_the_chip_filters(fleet_db, pinned_clock):
    full = await chg(fleet_db, page_size=1)
    chip = await chg(fleet_db, page_size=1, category="os", action="changed", label="upgrade")
    assert chip["stats"] == full["stats"] and chip["series"] == full["series"]
    assert chip["total"] == 0
    narrowed = await chg(fleet_db, page_size=1, kind="device")
    assert narrowed["stats"]["total"] == narrowed["total"] == 13 and narrowed["stats"]["software"]["added"] == 0


@pytest.mark.asyncio
async def test_changes_asset_filter_includes_the_snmp_twin(fleet_db, fleet_info, pinned_clock):
    name = LINKED_SERVERS[2]
    c = await chg(fleet_db, asset=f"server:{fleet_info.server_ids[name]}")
    assert {r["asset_name"] for r in c["items"]} <= {name, f"twin-{name}"}
    twin_events = [r for r in c["items"] if r["asset_kind"] == "device"]
    assert len(twin_events) == 1 and twin_events[0]["category"] == "firmware" and twin_events[0]["asset_name"] == f"twin-{name}"
    only_server = await chg(fleet_db, asset=f"server:{fleet_info.server_ids[name]}", kind="server")
    assert only_server["total"] == len(c["items"]) - len(twin_events) > 0
    assert {r["asset_kind"] for r in only_server["items"]} == {"server"}
    only_device = await chg(fleet_db, asset=f"server:{fleet_info.server_ids[name]}", kind="device")
    assert [r["id"] for r in only_device["items"]] == [r["id"] for r in twin_events]
    dev = await chg(fleet_db, asset=f"device:{fleet_info.device_ids['pa-02']}")
    assert dev["total"] == 1 and dev["items"][0]["subject"] == "PAN-OS"
    assert (await chg(fleet_db, asset=f"server:{uuid.uuid4()}"))["total"] == 0
    with pytest.raises(iq.InvalidFilter):
        await chg(fleet_db, asset="nonsense")


@pytest.mark.asyncio
async def test_changes_row_carries_asset_presence_and_the_server_a_twin_belongs_to(fleet_db, fleet_info, pinned_clock):
    c = await chg(fleet_db, page_size=200, since=FIXED_NOW - timedelta(days=60))
    gone = next(r for r in c["items"] if r["asset_name"] == "srv-retired-01")
    assert gone["asset_present"] is False and gone["server_id"] is None
    twin = next(r for r in c["items"] if r["asset_name"] == f"twin-{LINKED_SERVERS[2]}")
    assert twin["asset_present"] is True and twin["server_id"] == fleet_info.server_ids[LINKED_SERVERS[2]]
    plain = next(r for r in c["items"] if r["asset_name"] == "fg-01")
    assert plain["asset_present"] is True and plain["server_id"] is None
    assert plain["asset_key"] == f"device:{fleet_info.device_ids['fg-01']}"


@pytest.mark.asyncio
async def test_changes_truncated_snapshot_events_are_flagged(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=200, subject_key="wireshark")
    flagged = [r for r in c["items"] if r["snapshot_truncated"]]
    assert len(flagged) == 2 and all(r["action"] == "removed" for r in flagged)


@pytest.mark.asyncio
async def test_changes_window_since_until(fleet_db, pinned_clock):
    wide = await chg(fleet_db, page_size=200, since=FIXED_NOW - timedelta(days=365))
    assert wide["total"] == fleet_db.scalar("SELECT count(*) FROM inventory_change_events")
    old = await chg(fleet_db, page_size=200, since=FIXED_NOW - timedelta(days=365), until=FIXED_NOW - timedelta(days=8))
    assert {r["asset_name"] for r in old["items"]} == {"srv-w22-01", "fg-02"}
    assert old["window"]["until"] == "2026-09-30T12:00:00Z"
    # until is exclusive
    t = fleet_db.scalar("SELECT min(observed_at) FROM inventory_change_events WHERE observed_at >= $1", FIXED_NOW - timedelta(days=7))
    excl = await chg(fleet_db, page_size=1, since=t, until=t + timedelta(microseconds=0) + timedelta(seconds=1))
    assert excl["total"] >= 1
    with pytest.raises(iq.InvalidFilter):
        await chg(fleet_db, since=FIXED_NOW, until=FIXED_NOW)
    naive = await chg(fleet_db, page_size=1, since=datetime(2026, 10, 1, 12, 0, 0))        # naive means UTC
    assert naive["window"]["since"] == "2026-10-01T12:00:00Z"


@pytest.mark.asyncio
async def test_changes_paging_is_stable_and_newest_first(fleet_db, pinned_clock):
    full = await chg(fleet_db, page_size=500, since=FIXED_NOW - timedelta(days=365))
    seen = []
    for p in range(1, 20):
        r = await chg(fleet_db, page=p, page_size=9, since=FIXED_NOW - timedelta(days=365))
        assert r["total"] == full["total"]
        seen += [x["id"] for x in r["items"]]
        if not r["items"]:
            break
    assert seen == [x["id"] for x in full["items"]]
    ats = [(x["observed_at"], x["id"]) for x in full["items"]]
    assert ats == sorted(ats, reverse=True)


@pytest.mark.asyncio
async def test_changes_daily_series_by_category(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=1)
    s = c["series"]
    days = [p["day"] for p in s]
    assert days == [(datetime(2026, 10, 1) + timedelta(days=i)).date().isoformat() for i in range(8)]     # 10-01 .. 10-08, none missing
    assert sum(p["total"] for p in s) == c["total"]
    assert sum(p["software"] for p in s) == 39 and sum(p["firmware"] for p in s) == 11
    assert all(p["total"] == p["software"] + p["os"] + p["firmware"] + p["hardware"] for p in s)
    # the local calendar day depends on the zone
    nz = await chg(fleet_db, page_size=1, tz="Pacific/Auckland")
    assert nz["window"]["tz"] == "Pacific/Auckland" and sum(p["total"] for p in nz["series"]) == c["total"]
    assert [p["total"] for p in nz["series"]] != [p["total"] for p in s]
    with pytest.raises(iq.InvalidFilter):
        await chg(fleet_db, tz="not a zone!")                       # malformed: the caller's mistake
    fallback = await chg(fleet_db, page_size=1, tz="Mars/Olympus")     # well-formed but unknown to this host: UTC, and it says so
    assert fallback["window"]["tz"] == "UTC" and fallback["window"]["tz_fallback"] is True
    assert [p["total"] for p in fallback["series"]] == [p["total"] for p in s]


@pytest.mark.asyncio
async def test_changes_series_is_capped_for_huge_windows(fleet_db, pinned_clock):
    c = await chg(fleet_db, page_size=1, since=datetime(2001, 1, 1, tzinfo=UTC))
    assert len(c["series"]) == iq.MAX_SERIES_DAYS and c["series"][-1]["day"] == "2026-10-08"


@pytest.mark.asyncio
async def test_changes_default_zone_is_the_appliance_zone(fleet_db_fresh, pinned_clock):
    fleet_db_fresh.execute("INSERT INTO system_settings (key, value) VALUES ('company', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                           {"timezone": "Asia/Karachi"})
    async with session(fleet_db_fresh) as s:
        c = await iq.changes(s, scope=None, params=iq.ChangeFilters(), page_size=1)
    assert c["window"]["tz"] == "Asia/Karachi"


@pytest.mark.asyncio
async def test_scoped_changes_are_asset_driven(fleet_db, pinned_clock, monkeypatch):
    """Doc 02 section 14: the natural scoped feed walks the whole time index. A restricted scope must build
    the visible asset set first and reach events per asset (a lateral join through idx_inv_events_asset)."""
    seen = []
    real = iq._run

    async def spy(db, sql, params):
        seen.append(sql)
        return await real(db, sql, params)

    monkeypatch.setattr(iq, "_run", spy)
    c = await chg(fleet_db, scope=[SCOPED_TAG], page_size=50)
    event_sql = [s for s in seen if "inventory_change_events" in s and "FROM inventory_change_events e WHERE e.asset_kind = v.kind" in s]
    assert event_sql, "scoped event statements are lateral over the visible set"
    assert all("CROSS JOIN LATERAL" in s and "unnest(CAST(:v_kinds AS text[])" in s for s in event_sql)
    assert all(("OFFSET 0" in s) or ("LIMIT :e_inner" in s) for s in event_sql), "the lateral subquery must not be flattened"
    # no statement reads events without being driven by assets or by an id list
    for s in seen:
        if "FROM inventory_change_events e" in s and "e.asset_kind = v.kind" not in s:
            assert "jsonb_to_recordset" in s or "JOIN inventory_change_events" in s
    assert c["total"] > 0
    # the unrestricted form is the plain time-index read
    seen.clear()
    await chg(fleet_db, scope=None, page_size=5)
    assert any("FROM inventory_change_events e WHERE e.observed_at >= :e_since" in s for s in seen)
    assert not any("LATERAL (SELECT e.id" in s for s in seen)


@pytest.mark.asyncio
async def test_a_wide_scope_reads_events_as_a_semi_join_and_gets_the_same_answer(fleet_db, pinned_clock, monkeypatch):
    """Above WIDE_SCOPE_FRACTION of the estate the per-asset probes are replaced by a hash semi-join over the window;
    the answer must not change, whichever way a scope is served."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    narrow = await chg(fleet_db, scope=[SCOPED_TAG], page_size=500, since=since)
    big = await chg(fleet_db, scope=["prod"], page_size=500, since=since)

    seen = []
    real = iq._run

    async def spy(db, sql, params):
        seen.append(sql)
        return await real(db, sql, params)

    monkeypatch.setattr(iq, "_run", spy)
    monkeypatch.setattr(iq, "WIDE_SCOPE_FRACTION", 0.0)
    monkeypatch.setattr(iq, "WIDE_SCOPE_MIN_ASSETS", 0)
    wide_narrow = await chg(fleet_db, scope=[SCOPED_TAG], page_size=500, since=since)
    wide_big = await chg(fleet_db, scope=["prod"], page_size=500, since=since)
    assert wide_narrow == narrow and wide_big == big
    event_sql = [s for s in seen if "FROM inventory_change_events e" in s and "unnest(CAST(:v_kinds" in s]
    assert event_sql and not any("CROSS JOIN LATERAL" in s for s in event_sql)
    assert all("(e.asset_kind, e.asset_id) IN" in s for s in event_sql)
    # the two forms agree with the table on the unrestricted side as well
    assert big["total"] <= (await chg(fleet_db, scope=None, page_size=500, since=since))["total"]


@pytest.mark.asyncio
async def test_a_narrow_scope_never_uses_the_semi_join(fleet_db, pinned_clock, monkeypatch):
    seen = []
    real = iq._run

    async def spy(db, sql, params):
        seen.append(sql)
        return await real(db, sql, params)

    monkeypatch.setattr(iq, "_run", spy)
    monkeypatch.setattr(iq, "WIDE_SCOPE_MIN_ASSETS", 0)        # the count is taken, and 15 of 145 is under the fraction
    await chg(fleet_db, scope=[SCOPED_TAG], page_size=50)
    assert any("CROSS JOIN LATERAL" in s for s in seen)
    assert not any("(e.asset_kind, e.asset_id) IN" in s for s in seen)


@pytest.mark.asyncio
async def test_fan_out_is_bounded_and_the_answer_does_not_depend_on_it(fleet_db, pinned_clock, monkeypatch):
    """Past MAX_FANOUT concurrent fan-outs a request runs its blocks on its own connection; same numbers either way."""
    async with session(fleet_db) as s:
        fanned = await iq.summary(s, scope=None, use_cache=False)
        assert iq._fanout_active == 0
    monkeypatch.setattr(iq, "_fanout_active", iq.MAX_FANOUT)
    async with session(fleet_db) as s:
        serial = await iq.summary(s, scope=None, use_cache=False)
        changes_serial = await iq.changes(s, scope=None, tz="UTC")
    assert serial == fanned
    monkeypatch.setattr(iq, "_fanout_active", 0)
    async with session(fleet_db) as s:
        assert (await iq.changes(s, scope=None, tz="UTC")) == changes_serial
        assert iq._fanout_active == 0


@pytest.mark.asyncio
async def test_the_fan_out_counter_is_released_when_a_block_fails(fleet_db, pinned_clock):
    async def boom(s):
        raise RuntimeError("block failed")

    async def fine(s):
        return 1

    async with session(fleet_db) as s:
        with pytest.raises(RuntimeError):
            await iq._concurrently(s, fine, boom)
    assert iq._fanout_active == 0


@pytest.mark.asyncio
async def test_every_read_function_tunes_its_transaction_first(fleet_db, fleet_info, pinned_clock, monkeypatch):
    """jit off and custom plans only: see _tune. A read that forgot it would be fast or slow depending on who asked first."""
    tuned = []
    real = iq._tune

    async def spy(db):
        tuned.append(db)
        await real(db)

    monkeypatch.setattr(iq, "_tune", spy)
    calls = {
        "list_assets": lambda s: iq.list_assets(s, F(), scope=None),
        "asset_facets": lambda s: iq.asset_facets(s, F(), scope=None),
        "os_tree": lambda s: iq.os_tree(s, scope=None),
        "hardware": lambda s: iq.hardware(s, scope=None),
        "changes": lambda s: iq.changes(s, scope=None),
        "asset_detail": lambda s: iq.asset_detail(s, "server", fleet_info.server_ids["srv-w22-01"], scope=None),
        "summary": lambda s: iq.summary(s, scope=None, use_cache=False),
    }
    for name, call in calls.items():
        tuned.clear()
        async with session(fleet_db) as s:
            await call(s)
        assert tuned, f"{name} does not tune its transaction"


@pytest.mark.asyncio
async def test_the_planner_settings_apply_to_the_transaction_only(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        await iq._tune(s)
        assert (await s.execute(iq.text("SHOW jit"))).scalar() == "off"
        assert (await s.execute(iq.text("SHOW plan_cache_mode"))).scalar() == "force_custom_plan"
        await s.rollback()
        assert (await s.execute(iq.text("SHOW plan_cache_mode"))).scalar() == "auto"


# ════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_summary_shape_and_counts(fleet_db, fleet_info, pinned_clock):
    async with session(fleet_db) as s:
        sm = await iq.summary(s, scope=None, use_cache=False)
    assert sm["generated_at"] == "2026-10-08T12:00:00Z" and sm["scope"] == {"restricted": False, "tags": []}
    assert sm["assets"] == {"total": 145, "server": 60, "device": 85, "linked_devices": 5}
    cov = sm["coverage"]
    assert cov["fresh"] + cov["aging"] + cov["stale"] + cov["never"] + cov["not_applicable"] == 145
    assert cov["not_applicable"] > 0 and sm["health"]["not_collected_by_design"] == {"count": cov["not_applicable"]}
    assert cov["fresh_pct"] == round(100 * cov["fresh"] / (145 - cov["not_applicable"]), 1), "assets that cannot report are not in the base"
    assert cov["fresh_hours"] == {"server": 24, "device": 2} and cov["stale_days"] == 7
    assert set(sm["health"]) == set(iq.HEALTH_KEYS) | {"not_collected_by_design"}
    assert sm["data_status"] == {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}
    assert sm["lifecycle"] is None
    sw = sm["software"]
    hidden = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    assert sw["products"] == fleet_db.scalar(
        "SELECT count(DISTINCT name_key) FROM server_software_inventory WHERE NOT (category = ANY($1))", hidden)
    assert sw["installs"] == fleet_db.scalar(
        "SELECT count(*) FROM server_software_inventory WHERE NOT (category = ANY($1))", hidden)
    assert sw["hidden_categories"] == hidden
    assert not {p["category"] for p in sw["top"] + sw["sprawl"]} & set(hidden)
    cov_sec = sw["security_agent_coverage"]
    assert cov_sec["covered"] + cov_sec["missing"] + cov_sec["unknown"] == cov_sec["total"] == sm["assets"]["server"]
    assert cov_sec["missing"] == sm["health"]["missing_security_agent"]


@pytest.mark.asyncio
async def test_summary_software_ranking_is_exact(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        sm = await iq.summary(s, scope=None, use_cache=False)
    hidden = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    brute = fleet_db.query(
        """SELECT name_key, count(DISTINCT server_id) h, count(DISTINCT version) v, count(*) n FROM server_software_inventory
            WHERE NOT (category = ANY($1)) GROUP BY name_key""", hidden)
    by_hosts = sorted(brute, key=lambda r: (-r[1], -r[3], r[0]))[:8]
    assert [(p["product_key"], p["hosts"]) for p in sm["software"]["top"]] == [(r[0], r[1]) for r in by_hosts]
    assert {p["product_key"]: p["versions"] for p in sm["software"]["top"]} == {r[0]: r[2] for r in brute if r[0] in {p["product_key"] for p in sm["software"]["top"]}}
    by_versions = sorted((r for r in brute if r[2] > 1), key=lambda r: (-r[2], -r[1], r[0]))[:8]
    assert [(p["product_key"], p["versions"]) for p in sm["software"]["sprawl"]] == [(r[0], r[2]) for r in by_versions]
    names = {p["product_key"]: p["name"] for p in sm["software"]["top"]}
    assert names["google-chrome"] == "Google Chrome"


@pytest.mark.asyncio
async def test_summary_software_hides_the_configured_categories(fleet_db_fresh, pinned_clock):
    async with session(fleet_db_fresh) as s:
        base = await iq.summary(s, scope=None, use_cache=False)
        every = await iq.summary(s, scope=None, use_cache=False, settings={"hide_categories_by_default": []})
        default = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
        more = await iq.summary(s, scope=None, use_cache=False, settings={"hide_categories_by_default": default + ["browser"]})
    assert every["software"]["installs"] > base["software"]["installs"] > more["software"]["installs"]
    assert every["software"]["hidden_categories"] == []
    assert not {"google-chrome"} & {p["product_key"] for p in more["software"]["top"]}


@pytest.mark.asyncio
async def test_summary_widens_its_candidate_set_until_the_ranking_is_exact(fleet_db_fresh, pinned_clock, monkeypatch):
    """When duplicate package names make installs a poor stand-in for hosts, the candidate set widens."""
    monkeypatch.setattr(iq, "_EXACT_SOFTWARE_ROWS", 0)           # force the candidate scheme on this small fleet
    monkeypatch.setattr(iq, "_CANDIDATE_STEPS", (2, 5, 400))
    async with session(fleet_db_fresh) as s:
        narrow = await iq.summary(s, scope=None, use_cache=False)
    monkeypatch.setattr(iq, "_EXACT_SOFTWARE_ROWS", 10**9)       # exact for everything
    async with session(fleet_db_fresh) as s:
        exact = await iq.summary(s, scope=None, use_cache=False)
    assert narrow["software"]["top"] == exact["software"]["top"]
    assert narrow["software"]["products"] == exact["software"]["products"]


@pytest.mark.asyncio
async def test_summary_is_cached_for_sixty_seconds_per_scope_and_settings(fleet_db_fresh, pinned_clock, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(iq, "_monotonic", lambda: clock[0])
    calls = []
    real = iq._summary_core

    async def counting(*a, **k):
        calls.append(a[1])
        return await real(*a, **k)

    monkeypatch.setattr(iq, "_summary_core", counting)
    async with session(fleet_db_fresh) as s:
        first = await iq.summary(s, scope=None)
        again = await iq.summary(s, scope=None)
        assert again is first and len(calls) == 1
        scoped = await iq.summary(s, scope=[SCOPED_TAG])
        assert scoped["assets"]["total"] < first["assets"]["total"] and len(calls) == 2, "another scope is another entry"
        assert (await iq.summary(s, scope=[SCOPED_TAG])) is scoped
        other_settings = await iq.summary(s, scope=None, settings={"stale_days": 3})
        assert other_settings is not first and len(calls) == 3, "settings are part of the key"
        clock[0] += 59.9
        assert (await iq.summary(s, scope=None)) is first
        clock[0] += 0.2
        refreshed = await iq.summary(s, scope=None)
        assert refreshed is not first and len(calls) == 4
        await iq.summary(s, scope=None, now=FIXED_NOW)         # an explicit clock bypasses the cache
        await iq.summary(s, scope=None, use_cache=False)
        assert len(calls) == 6
    iq.clear_caches()


@pytest.mark.asyncio
async def test_summary_cache_is_bounded(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(iq, "_CACHE_MAX", 3)
    async with session(fleet_db_fresh) as s:
        for i in range(6):
            await iq.summary(s, scope=[f"tag-{i}"])
    assert len(iq._summary_cache) <= 3


@pytest.mark.asyncio
async def test_summary_runs_on_a_session_without_an_engine(fleet_db, pinned_clock):
    class NoBind:
        bind = None

        def __init__(self, inner):
            self._inner = inner

        async def execute(self, *a, **k):
            return await self._inner.execute(*a, **k)

    async with session(fleet_db) as s:
        sm = await iq.summary(NoBind(s), scope=None, use_cache=False)
    assert sm["assets"]["total"] == 145
