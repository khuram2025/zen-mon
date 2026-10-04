"""Collector/data acceptance fixes (wave C, area collector-data).

* NF-32 / CR-02: an IPv6 flow from or to the unspecified address ``::``
  (DAD neighbour solicitation, MLD) is IPv6, never the IPv4 talker 0.0.0.0:
  the family comes from the address pair, on raw records and the rollup.
* X-28: count=all (the default) says when visible exporters report each flow
  at both ingress and egress, instead of disagreeing with Capacity silently.
* X-08: exporters whose records were stored unscaled because their sampling
  rate was ambiguous are flagged in health and on every flow answer.
* Collector receive buffer: install/update raise net.core.rmem_max to the
  collector's NETFLOW_RCVBUF (scripts/setup-netflow-sysctl.sh).

Layers: pure helpers and in-process HTTP over a fake ClickHouse (always);
scratch ClickHouse via NETFLOW_TEST_CLICKHOUSE (netflow_scratch_ch.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.api.v1.netflow as netflow  # noqa: E402
from app.api.v1.netflow import access, common as C, filters as F, sql as S  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_classify, netflow_groups, netflow_health as nh  # noqa: E402

from test_netflow_core import USER, FakeCH, FakeDB, default_responder  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


# ── NF-32: the address family comes from the pair ────────────────────────────

def test_src_dst_family_is_the_row_family_not_one_column():
    v4 = "(src_addr6 = toIPv6('::') AND dst_addr6 = toIPv6('::'))"
    assert S.IS_V4_FLOW == v4
    assert S.SRC == f"if({v4}, IPv4ToIPv6(src_addr), src_addr6)"
    assert S.DST == f"if({v4}, IPv4ToIPv6(dst_addr), dst_addr6)"
    # The conversation rollup's ip_version agrees with SRC/DST by construction.
    assert S.TABLES[C.CONVERSATIONS_1H].ip_version == f"if({v4}, 4, 6)"
    # IPv4 networks require an IPv4 flow, so 0.0.0.0/8 cannot match '::'.
    pred = S.side_in_network("src", ipaddress.ip_network("0.0.0.0/8"), None, None)
    assert pred.startswith(f"({v4} AND src_addr BETWEEN")
    # '::' is special-use, not Public.
    assert "::/128" in S.NET_CLASSES[S.SPECIAL_USE_CLASS]


def test_src_filter_for_the_unspecified_address_is_ipv6():
    f = F.parse_flow_filter({"src": "::"})
    sql, params = F.compile_filter(f, C.RAW, F.UNRESTRICTED)
    assert f"({S.SRC} = toIPv6(%(nf_src)s))" in sql and params["nf_src"] == "::"
    sql, params = F.compile_filter(F.parse_flow_filter({"src": "0.0.0.0"}), C.RAW, F.UNRESTRICTED)
    assert f"({S.IS_V4_FLOW} AND src_addr = toIPv4(%(nf_src)s))" in sql


# Scratch ClickHouse: the same rows read through raw records and the rollup.

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

RAW_COLS = [
    "timestamp", "received_at", "exporter_ip", "exporter_ip6", "src_addr", "dst_addr", "src_addr6", "dst_addr6",
    "src_port", "dst_port", "protocol", "bytes", "packets", "sampling_interval", "fields_present",
    "fw_event", "flow_direction", "ip_version", "next_hop", "next_hop6",
]
Z4, Z6 = ipaddress.IPv4Address("0.0.0.0"), ipaddress.IPv6Address("::")
EXP = ipaddress.IPv4Address("127.0.0.96")


@pytest.fixture(scope="module")
def ch_v6unspec():
    client = scratch_client()
    with flow_db(client) as db:
        t = S.floor_to(datetime.now(UTC), 3600) - timedelta(hours=3) + timedelta(minutes=10)
        rows = [
            # DAD neighbour solicitation: '::' -> solicited-node multicast (ICMPv6).
            [t, t, EXP, Z6, Z4, Z4, Z6, ipaddress.IPv6Address("ff02::1:ff00:96"), 0, 34560, 58, 72, 1, 1,
             (1 << 31) | (1 << 11), 0, 255, 6, Z4, Z6],
            # MLD report from '::'.
            [t, t, EXP, Z6, Z4, Z4, Z6, ipaddress.IPv6Address("ff02::16"), 0, 36608, 58, 336, 4, 1,
             (1 << 31) | (1 << 11), 0, 255, 6, Z4, Z6],
            # A real IPv4 0.0.0.0 talker: DHCP discover.
            [t, t, EXP, Z6, Z4, ipaddress.IPv4Address("255.255.255.255"), Z6, Z6, 68, 67, 17, 590, 1, 1,
             (1 << 31) | (1 << 2), 0, 255, 4, Z4, Z6],
        ]
        client.insert(f"{db}.flow_records", rows, column_names=RAW_COLS)
        yield client, db, t


def _addresses(client, db, table, query):
    caps = S.table_caps(table)
    where, params = F.compile_filter(F.parse_flow_filter(query), table, F.UNRESTRICTED, volume_guard=False)
    sql = (f"SELECT {S.display(S.SRC)} AS s, {S.display(S.DST)} AS d, {caps.ip_version or 'ip_version'} AS v "
           f"FROM {db}.{table} WHERE {where} ORDER BY s, d")
    return [tuple(r) for r in client.query(sql, parameters=params).result_rows]


@pytest.mark.parametrize("table", [C.RAW, C.CONVERSATIONS_1H])
def test_unspecified_ipv6_endpoint_is_never_ipv4_zero(ch_v6unspec, table):
    client, db, _ = ch_v6unspec
    everything = _addresses(client, db, table, {})
    assert ("::", "ff02::1:ff00:96", 6) in everything and ("::", "ff02::16", 6) in everything
    assert ("0.0.0.0", "255.255.255.255", 4) in everything
    # src=:: finds the IPv6 rows only; src=0.0.0.0 the IPv4 row only.
    assert {r[1] for r in _addresses(client, db, table, {"src": "::"})} == {"ff02::1:ff00:96", "ff02::16"}
    assert _addresses(client, db, table, {"src": "0.0.0.0"}) == [("0.0.0.0", "255.255.255.255", 4)]
    assert _addresses(client, db, table, {"talker": "0.0.0.0/8"}) == [("0.0.0.0", "255.255.255.255", 4)]
    assert {r[0] for r in _addresses(client, db, table, {"talker": "::/128"})} == {"::"}
    assert {r[2] for r in _addresses(client, db, table, {"ip_version": "6"})} == {6}
    # Network class: '::' is special-use, like 0.0.0.0/8, never Public.
    cls = client.query(
        f"SELECT DISTINCT {S.netclass_expr('src')} FROM {db}.{table} WHERE {S.SRC} = toIPv6('::')"
    ).result_rows
    assert cls == [(S.SPECIAL_USE_CLASS,)]


def test_unspecified_next_hop_of_ipv6_flow_is_ipv6(ch_v6unspec):
    client, db, _ = ch_v6unspec
    rows = client.query(
        f"SELECT {S.display(S.NEXT_HOP)}, ip_version FROM {db}.flow_records ORDER BY ip_version").result_rows
    assert sorted(set(rows)) == [("0.0.0.0", 4), ("::", 6)]


# ── X-28 / X-08: notices every flow answer carries ───────────────────────────

BIDIR = "127.0.0.95"
OTHER = "127.0.0.61"


def _notes_responder(both=(BIDIR,), ambiguous=()):
    def respond(sql, params):
        if "uniqExact(flow_direction) = 2" in sql:
            return [(ip,) for ip in both]
        if "sampling_ambiguous_records" in sql and "HAVING" in sql:
            return [(ip,) for ip in ambiguous]
        return default_responder(sql, params)
    return respond


def _client(vis: FlowVisibility, db=None) -> TestClient:
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield db or FakeDB()

    async def user():
        return USER

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


@pytest.fixture
def notes(monkeypatch):
    state = {"both": (BIDIR,), "ambiguous": ()}
    ch = FakeCH(lambda sql, params: _notes_responder(state["both"], state["ambiguous"])(sql, params))
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)

    async def no_refresh(*a, **kw):
        return None

    async def no_repair():
        return {"pending_hours": {}, "rebuilt_hours": {}, "unrecoverable_hours": {}}

    from app.services import netflow_rollup

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    monkeypatch.setattr(netflow_rollup, "repair_status", no_repair)
    C._retention_cache.update(value=None, at=0.0)
    C._repair_cache.update(value=None, at=0.0)
    F.invalidate_counting_cache()
    state["ch"] = ch
    yield state
    F.invalidate_counting_cache()
    C._retention_cache.update(value=None, at=0.0)
    C._repair_cache.update(value=None, at=0.0)


def _warnings(r) -> dict[str, str]:
    assert r.status_code == 200, r.text
    return {w["code"]: w["message"] for w in r.json()["meta"]["warnings"]}


def test_double_count_warning_text_and_scope():
    f = F.parse_flow_filter({})
    msg = F.double_count_warning(f, {BIDIR})
    assert msg == ("Totals count these flows twice: exporter 127.0.0.95 reports each flow at both ingress and "
                   "egress. Counting 'Edge exporters only' (count=edge) counts each flow once, as Capacity does")
    assert F.double_count_warning(F.parse_flow_filter({"exporter": OTHER}), {BIDIR}) is None
    assert F.double_count_warning(F.parse_flow_filter({"iface": f"7@{BIDIR}"}), {BIDIR}) is not None
    assert F.double_count_warning(F.parse_flow_filter({"direction": "ingress"}), {BIDIR}) is None
    assert F.double_count_warning(F.parse_flow_filter({"count": "edge"}), {BIDIR}) is None
    assert F.double_count_warning(f, set()) is None and F.double_count_warning(f, None) is None
    many = F.double_count_warning(f, {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "2001:db8::1"})
    assert "exporters 10.0.0.1, 10.0.0.2, 10.0.0.3 and 2 more report" in many


@pytest.mark.parametrize("path", ["/overview", "/top-talkers", "/interfaces", "/timeseries"])
def test_count_all_answers_say_they_include_both_directions(notes, path):
    client = _client(F.UNRESTRICTED)
    w = _warnings(client.get("/api/v1/netflow" + path, params={"envelope": 1, "exporter": BIDIR, "minutes": 10}))
    assert "counting_both_directions" in w and BIDIR in w["counting_both_directions"]
    # Edge counting dedupes them, a single direction has no duplicates, and
    # another exporter's answer is not affected.
    for extra in ({"count": "edge"}, {"direction": "ingress"}, {"exporter": OTHER}):
        params = {"envelope": 1, "exporter": BIDIR, "minutes": 10, **extra}
        assert "counting_both_directions" not in _warnings(client.get("/api/v1/netflow" + path, params=params))


def test_double_count_lookup_is_scoped_cached_and_respects_both_setting(notes, monkeypatch):
    scoped = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                            permissions=frozenset({"netflow.view"}))
    notes["both"] = ()
    client = _client(scoped)
    assert "counting_both_directions" not in _warnings(client.get("/api/v1/netflow/overview", params={"envelope": 1}))
    lookups = [(s, p) for s, p, _ in notes["ch"].calls if "uniqExact(flow_direction) = 2" in s]
    assert len(lookups) == 1 and any(list(v) == ["10.9.0.1"] for v in lookups[0][1].values() if isinstance(v, list))
    client.get("/api/v1/netflow/overview", params={"envelope": 1})
    assert len([s for s, _, _ in notes["ch"].calls if "uniqExact(flow_direction) = 2" in s]) == 1   # cached

    # An exporter set to count_direction 'both' is the operator's word that
    # its two directions are different traffic: no notice.
    async def both_setting(db):
        return {BIDIR: "both"}

    monkeypatch.setattr(F, "_count_direction_settings", both_setting)
    F.invalidate_counting_cache()
    notes["both"] = (BIDIR,)
    w = _warnings(_client(F.UNRESTRICTED).get("/api/v1/netflow/overview", params={"envelope": 1}))
    assert "counting_both_directions" not in w


def test_notice_lookup_failure_is_silent_and_does_not_short_circuit(monkeypatch):
    async def boom(*a, **kw):
        C.current_meta().ch_unanswered = True
        raise C.NetflowError(503, "down", code="unavailable")

    monkeypatch.setattr(F, "run_query", boom)
    F.invalidate_counting_cache()
    meta = C.QueryMeta()
    token = C._META.set(meta)
    try:
        assert asyncio.run(F.double_counted_exporters(FakeDB(), F.UNRESTRICTED)) is None
        assert asyncio.run(F.ambiguous_sampling_exporters(F.UNRESTRICTED)) is None
        assert meta.ch_unanswered is False
    finally:
        C._META.reset(token)
        F.invalidate_counting_cache()


def test_notice_lookups_run_together_so_a_silent_clickhouse_costs_one_budget(monkeypatch):
    # NF-31 (wave-c-notes #29): the two lookups used to run one after the
    # other, each waiting out its own budget + grace on an unanswering server.
    async def slow_double(db, vis):
        await asyncio.sleep(0.3)
        return frozenset({BIDIR})

    async def slow_ambiguous(vis):
        await asyncio.sleep(0.3)
        return frozenset({BIDIR})

    monkeypatch.setattr(F, "double_counted_exporters", slow_double)
    monkeypatch.setattr(F, "ambiguous_sampling_exporters", slow_ambiguous)
    meta = C.QueryMeta()
    token = C._META.set(meta)
    try:
        loop = asyncio.new_event_loop()
        try:
            started = loop.time()
            loop.run_until_complete(F.warn_exporter_notes(F.FlowFilter(), F.UNRESTRICTED, FakeDB()))
            elapsed = loop.time() - started
        finally:
            loop.close()
        assert elapsed < 0.5, elapsed
        assert [w["code"] for w in meta.warnings] == ["counting_both_directions", "sampling_ambiguous"]
    finally:
        C._META.reset(token)


def test_ambiguous_sampling_is_flagged_on_flow_answers(notes):
    notes["both"] = ()
    notes["ambiguous"] = (BIDIR,)
    client = _client(F.UNRESTRICTED)
    w = _warnings(client.get("/api/v1/netflow/top-talkers", params={"envelope": 1, "minutes": 10}))
    assert w["sampling_ambiguous"].startswith("Some volumes from exporter 127.0.0.95 are not scaled for sampling")
    w = _warnings(client.get("/api/v1/netflow/top-talkers", params={"envelope": 1, "exporter": OTHER}))
    assert "sampling_ambiguous" not in w


def _stats(**over) -> dict:
    st = {"protocols": ["netflow9"], "domains": [952], "last_dgram": datetime.now(UTC), "dg15": 10, "dec15": 10,
          "wr15": 10, "wait15": 0, "unk15": 0, "perr60": 0, "unsup60": 0, "impl60": 0, "q60": 0, "gaps60": 0,
          "units60": 10, "clamped60": 0, "offset15": 0, "samp60": 1000, "dg60": 10, "dec60": 10, "wr60": 10,
          "amb60": 0}
    st.update(over)
    return st


def test_health_names_ambiguous_sampling():
    now = datetime.now(UTC)
    row = nh.build_row(BIDIR, now=now, rc=None, st=_stats(amb60=3), live=None, learned=None, tpl=None,
                       setting=None, owner=None, device=None, since=None)
    issues = {i["code"]: i for i in row["issues"]}
    assert issues["sampling_ambiguous"]["severity"] == "warning"
    assert issues["sampling_ambiguous"]["message"].startswith("3 records in the last hour named no sampler")
    row = nh.build_row(BIDIR, now=now, rc=None, st=_stats(), live=None, learned=None, tpl=None,
                       setting=None, owner=None, device=None, since=None)
    assert "sampling_ambiguous" not in {i["code"] for i in row["issues"]}
    assert nh.FIELD_BITS[27] == "sampling_ambiguous"
    assert "sampling_ambiguous_records" in nh._STATS_COLUMNS


def test_collector_stats_column_is_created_everywhere():
    mig = (ROOT / "scripts" / "migrate-137-netflow-schema-v2-clickhouse.sql").read_text()
    schema = (ROOT / "poller" / "cmd" / "netflow-collector" / "schema.go").read_text()
    assert "ADD COLUMN IF NOT EXISTS sampling_ambiguous_records UInt64" in mig
    assert "ADD COLUMN IF NOT EXISTS sampling_ambiguous_records UInt64" in schema


# ── Collector receive buffer: net.core.rmem_max >= NETFLOW_RCVBUF ────────────

SCRIPT = ROOT / "scripts" / "setup-netflow-sysctl.sh"


def _run_sysctl_script(tmp_path, current: int, *, unit_rcvbuf=8388608, env_rcvbuf=None, writable=True):
    state = tmp_path / "rmem_max"
    state.write_text(str(current))
    fake = tmp_path / "sysctl"
    fail = "" if writable else "exit 1"
    fake.write_text(f"""#!/bin/bash
if [[ "$1" == "-n" ]]; then cat {state}; exit 0; fi
{fail}
for a in "$@"; do [[ "$a" == net.core.rmem_max=* ]] && echo "${{a#*=}}" > {state}; done
exit 0
""")
    fake.chmod(0o755)
    unit = tmp_path / "collector.service"
    unit.write_text(f"[Service]\nEnvironment=NETFLOW_RCVBUF={unit_rcvbuf}\n")
    home = tmp_path / "zp"
    home.mkdir(exist_ok=True)
    if env_rcvbuf is not None:
        (home / ".env").write_text(f"FOO=1\nNETFLOW_RCVBUF={env_rcvbuf}\n")
    conf = tmp_path / "99-zenplus-netflow.conf"
    env = {**os.environ, "ZENPLUS_DIR": str(home), "ZENPLUS_SYSCTL": str(fake),
           "ZENPLUS_NETFLOW_SYSCTL_CONF": str(conf), "ZENPLUS_NETFLOW_UNIT": str(unit)}
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return res, int(state.read_text()), (conf.read_text() if conf.exists() else None)


def test_rmem_max_is_raised_to_the_requested_buffer(tmp_path):
    res, value, conf = _run_sysctl_script(tmp_path, 212992)
    assert res.returncode == 0 and value == 8388608
    assert "net.core.rmem_max = 8388608" in conf.splitlines()
    # Idempotent.
    res, value, conf2 = _run_sysctl_script(tmp_path, value)
    assert res.returncode == 0 and value == 8388608 and conf2 == conf


def test_rmem_max_is_never_lowered_and_env_overrides_the_unit(tmp_path):
    res, value, conf = _run_sysctl_script(tmp_path, 33554432)
    assert res.returncode == 0 and value == 33554432 and "net.core.rmem_max = 33554432" in conf
    res, value, conf = _run_sysctl_script(tmp_path, 212992, env_rcvbuf=16777216)
    assert value == 16777216 and "net.core.rmem_max = 16777216" in conf


def test_rmem_max_refused_is_reported_not_failed(tmp_path):
    res, value, conf = _run_sysctl_script(tmp_path, 212992, writable=False)
    assert res.returncode == 0 and value == 212992 and conf is None
    assert "could not raise net.core.rmem_max" in res.stderr


def test_installer_cli_update_and_ota_run_the_sysctl_setup():
    install = (ROOT / "install.sh").read_text()
    assert install.count("setup-netflow-sysctl.sh") >= 2          # install + `zenplus update` (reinstall_units)
    release = (ROOT / "scripts" / "build-release.py").read_text()
    assert '"script": "code/scripts/setup-netflow-sysctl.sh"' in release
