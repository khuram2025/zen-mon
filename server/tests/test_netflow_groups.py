"""Managed NetFlow IP groups (CR-13): services/netflow_groups.py.

  * the legacy JSON file parser and the one-time import into
    netflow_ip_groups (gated on ZENPLUS_NETFLOW_TEST_PG, a DISPOSABLE
    database with migrate-138 applied, e.g.
    postgresql+asyncpg://postgres@127.0.0.1:55441/a3b_mig_test);
  * membership semantics (sub-groups, longest-prefix labelling);
  * the SQL predicates against ClickHouse for IPv4 and IPv6 CIDRs, checked
    against the Python membership test (gated on ZENPLUS_NETFLOW_TEST_CH).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
from urllib.parse import urlparse

import pytest

from app.services import netflow_groups as ng
from app.services.netflow_groups import Group, parse_network


def _net(*cidrs):
    return tuple(parse_network(c) for c in cidrs)


@pytest.fixture(autouse=True)
def _restore_snapshot():
    """Leave the module cache exactly as found (other tests may rely on it)."""
    st = ng._state
    before = (list(st.groups), dict(st.by_name), st.loaded_at, st.loaded)
    yield
    with ng._lock:
        st.groups, st.by_name, st.loaded_at, st.loaded = before


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_read_groups_file_reports_and_merges(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps([
        {"name": " Internal LAN ", "cidrs": ["10.0.0.0/8", "192.168.1.7/16", "bogus"]},
        {"name": "DMZ", "cidrs": ["172.16.0.0/12"], "color": "#ff0000", "description": "edge"},
        {"name": "Internal LAN", "cidrs": ["10.0.0.0/8", "fd00::/8"]},
        {"name": "", "cidrs": ["1.2.3.0/24"]},
        {"name": "Empty", "cidrs": ["nope"]},
        "not-an-object",
    ]))
    groups, problems = ng.read_groups_file(str(path))
    by_name = {g["name"]: g for g in groups}
    assert set(by_name) == {"Internal LAN", "DMZ"}
    # host bits dropped, duplicates merged, IPv6 kept
    assert by_name["Internal LAN"]["cidrs"] == ["10.0.0.0/8", "192.168.0.0/16", "fd00::/8"]
    assert by_name["DMZ"]["color"] == "#ff0000" and by_name["DMZ"]["description"] == "edge"
    assert any("bogus" in p for p in problems)
    assert any("missing name" in p for p in problems)
    assert any("Empty" in p for p in problems)
    assert any("not an object" in p for p in problems)


def test_missing_or_broken_file(tmp_path):
    assert ng.read_groups_file(str(tmp_path / "absent.json")) == ([], [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    groups, problems = ng.read_groups_file(str(bad))
    assert groups == [] and problems


def test_ipv4_mapped_ipv6_cidr_is_treated_as_ipv4():
    assert str(parse_network("::ffff:10.1.0.0/112")) == "10.1.0.0/16"
    assert parse_network("junk") is None


# ── Membership ───────────────────────────────────────────────────────────────

def test_sub_groups_and_longest_prefix_label():
    ng.set_groups_for_tests([
        Group(id="dc", name="Datacenter A", cidrs=_net("10.10.0.0/16")),
        Group(id="web", name="DC-A Web", cidrs=_net("10.10.5.0/24"), parent_id="dc"),
        Group(id="v6", name="Lab v6", cidrs=_net("2001:db8:1::/48")),
        Group(id="loop", name="Loop", cidrs=_net("10.10.0.0/16"), parent_id="loop2"),
        Group(id="loop2", name="Loop2", cidrs=_net("10.99.0.0/16"), parent_id="loop"),
    ])
    assert ng.classify_ip("10.10.5.9") == "DC-A Web"
    assert ng.classify_ip("10.10.6.9") in ("Datacenter A", "Loop")   # same prefix: name order
    assert ng.classify_ip("10.10.6.9") == "Datacenter A"
    assert ng.classify_ip("2001:db8:1::42") == "Lab v6"
    assert ng.classify_ip("::ffff:10.10.5.9") == "DC-A Web"
    assert ng.classify_ip("192.0.2.1") is None and ng.classify_ip("garbage") is None
    dc = ng.get_group("Datacenter A")
    assert set(map(str, dc.effective)) == {"10.10.0.0/16"}   # child collapsed into parent
    # a parent cycle terminates and still unions both groups' networks
    assert {str(n) for n in ng.get_group("Loop").effective} == {"10.10.0.0/16", "10.99.0.0/16"}
    assert ng.group_names() == sorted(ng.group_names(), key=str.lower)


def test_longest_prefix_compares_families_in_one_address_space():
    """An IPv6 range covering IPv4 through its mapped form (::/80 holds
    ::ffff:a.b.c.d) is a /80 there; an IPv4 /8 is a /104. The IPv4 group is
    the more specific label even though 80 > 8."""
    ng.set_groups_for_tests([
        Group(id="wide", name="All v6-mapped", cidrs=_net("::/80")),
        Group(id="ten", name="Ten", cidrs=_net("10.0.0.0/8")),
    ])
    assert ng.classify_ip("10.1.2.3") == "Ten"
    assert ng.classify_ip("192.0.2.1") == "All v6-mapped"
    assert ng.classify_ip("2001:db8::1") is None


def test_group_predicate_shapes():
    ng.set_groups_for_tests([
        Group(id="a", name="Mixed", cidrs=_net("10.1.0.0/16", "2001:db8::/32")),
        Group(id="b", name="Nothing", cidrs=()),
    ])
    pred = ng.group_predicate("Mixed")
    # IPv4 ranges need an IPv4 flow: both IPv6 twins '::' (NF-32).
    assert ("(src_addr6 = toIPv6('::') AND dst_addr6 = toIPv6('::')) AND src_addr BETWEEN "
            "toIPv4('10.1.0.0') AND toIPv4('10.1.255.255')") in pred
    assert "BETWEEN toIPv6('2001:db8::') AND toIPv6('2001:db8:ffff:ffff:ffff:ffff:ffff:ffff')" in pred
    assert "toString" not in pred
    assert ng.group_predicate("Nothing") == "0"
    assert ng.group_predicate("Unknown") is None
    assert "t.src_addr6" in ng.group_predicate("Mixed", "t.src_addr", "t.dst_addr")
    assert ng.v4_row_sql("t.src_addr") == "(t.src_addr6 = toIPv6('::') AND t.dst_addr6 = toIPv6('::'))"
    assert ng.v4_row_sql("next_hop") == "next_hop6 = toIPv6('::')"


# ── SQL predicates against ClickHouse ────────────────────────────────────────

CH_URL = os.environ.get("ZENPLUS_NETFLOW_TEST_CH")


@pytest.mark.skipif(not CH_URL, reason="set ZENPLUS_NETFLOW_TEST_CH to run SQL checks")
def test_group_predicate_matches_python_membership():
    import clickhouse_connect

    u = urlparse(CH_URL)
    client = clickhouse_connect.get_client(host=u.hostname, port=u.port or 8123,
                                           username=u.username or "default", password=u.password or "")
    db = f"nf_groups_test_{os.getpid()}"
    client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    try:
        groups = [
            Group(id="1", name="Site A", cidrs=_net("10.1.0.0/16", "192.168.10.0/24")),
            Group(id="2", name="Site A Servers", cidrs=_net("10.1.2.0/24"), parent_id="1"),
            Group(id="3", name="V6 Lab", cidrs=_net("2001:db8:10::/48", "fd00:1::/64")),
            Group(id="4", name="Everything v6", cidrs=_net("::/0")),
            Group(id="5", name="Mapped", cidrs=_net("::ffff:172.16.0.0/108")),
            Group(id="6", name="This network", cidrs=_net("0.0.0.0/8")),
            Group(id="7", name="Unspecified v6", cidrs=_net("::/128")),
        ]
        ng.set_groups_for_tests(groups)
        rng = random.Random(3)
        v4 = ["10.1.2.3", "10.1.200.1", "10.2.0.1", "192.168.10.99", "192.168.11.1", "172.16.5.5",
              "172.31.0.1", "8.8.8.8"]
        v6 = ["2001:db8:10::1", "2001:db8:11::1", "fd00:1::99", "fd00:2::1", "::1"]
        rows = []
        for i in range(3000):
            # A flow has one address family. IPv6 sources include the
            # unspecified address '::' (DAD / MLD), which must not read as
            # the IPv4 address 0.0.0.0 (NF-32); no packet is sent to '::'.
            fam6 = rng.random() < 0.4
            src = rng.choice(v6 + ["::"] if fam6 else v4)
            dst = rng.choice(v6 if fam6 else v4)
            rows.append((i, "0.0.0.0" if fam6 else src, src if fam6 else "::",
                         "0.0.0.0" if fam6 else dst, dst if fam6 else "::", src, dst))
        client.command(f"""CREATE TABLE {db}.f (i UInt32, src_addr IPv4, src_addr6 IPv6,
                           dst_addr IPv4, dst_addr6 IPv6) ENGINE = Memory""")
        client.insert(f"{db}.f", [r[:5] for r in rows],
                      column_names=["i", "src_addr", "src_addr6", "dst_addr", "dst_addr6"])
        for g in ng.groups_snapshot():
            got = {r[0] for r in client.query(
                f"SELECT i FROM {db}.f WHERE {ng.group_predicate(g.name)} AND i >= %(lo)s",
                parameters={"lo": 0}).result_rows}
            want = {r[0] for r in rows if ng.ip_in(r[5], g.effective) or ng.ip_in(r[6], g.effective)}
            assert got == want, g.name
            if g.name == "This network":
                assert not got       # an IPv6 flow from '::' is not IPv4 0.0.0.0
            if g.name == "Unspecified v6":
                assert got           # ... it is the IPv6 address '::'
        # IPv4 flows never match an IPv6-only group through the '::' placeholder
        assert not ng.ip_in("10.1.2.3", _net("2001:db8:10::/48"))
    finally:
        client.command(f"DROP DATABASE IF EXISTS {db}")


# ── One-time import (PostgreSQL) ─────────────────────────────────────────────

PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")


@pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (disposable DB with migrate-138)")
def test_json_file_is_imported_once(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    path = tmp_path / "groups.json"
    path.write_text(json.dumps([
        {"name": "Internal LAN", "cidrs": ["10.0.0.0/8", "192.168.0.0/16"]},
        {"name": "DMZ", "cidrs": ["172.16.0.0/12", "bad"]},
    ]))

    async def run():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as db:
                await db.execute(text("DELETE FROM netflow_ip_groups"))
                await db.execute(text("DELETE FROM system_settings WHERE key = :k"),
                                 {"k": ng.IMPORT_MARKER_KEY})
                await db.commit()

                summary = await ng.import_groups_file(db, str(path))
                assert summary["groups"] == 2 and any("bad" in p for p in summary["problems"])
                rows = (await db.execute(text(
                    "SELECT name, cidrs FROM netflow_ip_groups ORDER BY name"))).all()
                assert [(r.name, list(r.cidrs)) for r in rows] == [
                    ("DMZ", ["172.16.0.0/12"]), ("Internal LAN", ["10.0.0.0/8", "192.168.0.0/16"])]

                # Second call: the marker prevents a re-import.
                assert await ng.import_groups_file(db, str(path)) is None
                # Emptying the table does not resurrect the file's groups.
                await db.execute(text("DELETE FROM netflow_ip_groups"))
                await db.commit()
                assert await ng.import_groups_file(db, str(path)) is None
                assert (await db.execute(text("SELECT count(*) FROM netflow_ip_groups"))).scalar() == 0

                # load_groups reads the table (nested group included) into the snapshot.
                await db.execute(text(
                    "INSERT INTO netflow_ip_groups (id, name, cidrs) VALUES "
                    "('00000000-0000-0000-0000-00000000a001', 'Parent', ARRAY['10.20.0.0/16']), "
                    "('00000000-0000-0000-0000-00000000a002', 'Child', ARRAY['10.30.0.0/16', 'fd00::/8'])"))
                await db.execute(text(
                    "UPDATE netflow_ip_groups SET parent_id = '00000000-0000-0000-0000-00000000a001' "
                    "WHERE name = 'Child'"))
                await db.commit()
                ng.invalidate_groups_cache()
                loaded = await ng.load_groups(db, force=True)
                assert [g.name for g in loaded] == ["Child", "Parent"]
                assert {str(n) for n in ng.get_group("Parent").effective} == {
                    "10.20.0.0/16", "10.30.0.0/16", "fd00::/8"}
                assert ng.classify_ip("10.30.1.1") == "Child"
                assert "Parent" in ng.group_names()
                await db.execute(text("DELETE FROM netflow_ip_groups"))
                await db.execute(text("DELETE FROM system_settings WHERE key = :k"),
                                 {"k": ng.IMPORT_MARKER_KEY})
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ipaddress_helpers_agree_on_families():
    nets = _net("10.0.0.0/8", "2001:db8::/32")
    assert ng.ip_in("10.2.3.4", nets) and ng.ip_in("2001:db8::5", nets)
    assert not ng.ip_in("2001:db9::5", nets) and not ng.ip_in("11.0.0.1", nets)
    assert isinstance(nets[0], ipaddress.IPv4Network)
