"""NetFlow time windows (contract §7, NF-31) and the raw/rollup/hybrid source
planner (NF-17).

Unit layer: the resolve_window validation matrix and the planner's segment
arithmetic against synthetic retention. ClickHouse layer (opt-in, see
tests/netflow_scratch_ch.py): hybrid plans over the real v2 tables and
materialized views reconcile exactly with half-open raw scans for
non-hour-aligned custom windows, with and without filters.
"""

from __future__ import annotations

import ipaddress
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.v1.netflow import common as C  # noqa: E402
from app.api.v1.netflow import filters as F  # noqa: E402
from app.api.v1.netflow import sql as S  # noqa: E402
from app.api.v1.netflow.common import (  # noqa: E402
    MAX_SPAN_RAW,
    MAX_SPAN_ROLLUP,
    NetflowError,
    QueryMeta,
    Retention,
    Window,
    _META,
    resolve_window,
    window_params,
)

NOW = datetime(2026, 9, 30, 12, 34, 56, tzinfo=timezone.utc)


def _err(fn, *a, **kw) -> NetflowError:
    with pytest.raises(NetflowError) as exc:
        fn(*a, **kw)
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, str)
    return exc.value


# ── resolve_window ────────────────────────────────────────────────────────────

def test_custom_range_needs_both_bounds():
    for kw in ({"from_ts": "2026-09-01T00:00:00Z"}, {"to_ts": "2026-09-01T00:00:00Z"}):
        err = _err(resolve_window, max_span=MAX_SPAN_ROLLUP, now=NOW, **kw)
        assert err.code == "invalid_window"
        assert "needs both from and to" in err.detail


def test_hours_is_ignored_with_custom_bounds():
    # The UI sends a rounded `hours` with custom ranges; 0 (a 20-minute range)
    # and values above the relative maximum must not fail the request.
    for hours in (0, 1080, 99999):
        w = resolve_window(hours, None, "2026-09-01T00:00:00Z", "2026-09-01T00:20:00Z",
                           max_span=MAX_SPAN_ROLLUP, now=NOW)
        assert (w.start, w.end, w.mode) == (
            datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc), "fixed")


def test_relative_hours_bounds():
    assert resolve_window(2232, max_span=MAX_SPAN_ROLLUP, now=NOW).start == NOW - timedelta(hours=2232)
    for bad in (0, -1, 2233):
        err = _err(resolve_window, bad, max_span=MAX_SPAN_ROLLUP, now=NOW)
        assert err.fields == {"hours": err.detail}
    w = resolve_window(None, max_span=MAX_SPAN_ROLLUP, now=NOW, default_hours=168)
    assert (w.end - w.start, w.end, w.mode) == (timedelta(hours=168), NOW, "relative")


def test_minutes_beat_hours_and_are_bounded():
    w = resolve_window(24, 90, max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert w.end - w.start == timedelta(minutes=90)
    _err(resolve_window, None, 0, max_span=MAX_SPAN_ROLLUP, now=NOW)
    _err(resolve_window, None, 31 * 1440 + 1, max_span=MAX_SPAN_RAW, now=NOW)


def test_years_outside_range_are_rejected_before_clickhouse():
    # 0001-01-01 used to reach ClickHouse and come back as a 500 carrying
    # CANNOT_PARSE_DATETIME, the server address and version.
    err = _err(resolve_window, None, None, "0001-01-01T00:00:00Z", "0002-01-01T00:00:00Z",
               max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert "between 2000 and 2027" in err.detail
    _err(resolve_window, None, None, "2026-01-01T00:00:00Z", "2028-01-02T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)


def test_inverted_and_future_ranges():
    _err(resolve_window, None, None, "2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)
    _err(resolve_window, None, None, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)
    err = _err(resolve_window, None, None, "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z",
               max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert "future" in err.detail


def test_end_is_clamped_to_server_now_plus_60s():
    w = resolve_window(None, None, "2026-09-30T00:00:00Z", "2026-12-31T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert w.end == NOW + timedelta(seconds=60)
    assert w.start == datetime(2026, 9, 30, tzinfo=timezone.utc)


def test_span_limit_names_the_limit():
    frm, to = "2026-08-01T00:00:00Z", "2026-09-15T00:00:00Z"   # 45 days
    assert resolve_window(None, None, frm, to, max_span=MAX_SPAN_ROLLUP, now=NOW).end - \
        resolve_window(None, None, frm, to, max_span=MAX_SPAN_ROLLUP, now=NOW).start == timedelta(days=45)
    err = _err(resolve_window, None, None, frm, to, max_span=MAX_SPAN_RAW, now=NOW)
    assert "31 days" in err.detail and "45 days" in err.detail
    _err(resolve_window, None, None, "2026-06-01T00:00:00Z", "2026-09-15T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)
    err = _err(resolve_window, 745, max_span=MAX_SPAN_RAW, now=NOW)
    assert "31 days" in err.detail


def test_timestamp_parsing():
    w = resolve_window(None, None, "2026-09-01T02:00:00.750+02:00", "2026-09-01 03:00:00",
                       max_span=MAX_SPAN_ROLLUP, now=NOW)
    # Offsets are honoured, naive means UTC, fractions are truncated (ClickHouse
    # binds datetimes at second precision).
    assert w.start == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    err = _err(resolve_window, None, None, "yesterday", "2026-09-01T00:00:00Z", max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert err.fields == {"from": err.detail}


def test_time_zone_validation():
    assert resolve_window(1, tz="Asia/Kolkata", max_span=MAX_SPAN_ROLLUP, now=NOW).tz == "Asia/Kolkata"
    assert resolve_window(1, tz=None, max_span=MAX_SPAN_ROLLUP, now=NOW).tz == "UTC"
    err = _err(resolve_window, 1, tz="Mars/Olympus", max_span=MAX_SPAN_ROLLUP, now=NOW)
    assert err.code == "invalid_window"


def test_first_resolved_window_is_recorded_in_meta():
    meta = QueryMeta()
    token = _META.set(meta)
    try:
        first = resolve_window(24, max_span=MAX_SPAN_ROLLUP, now=NOW)
        resolve_window(1, max_span=MAX_SPAN_ROLLUP, now=NOW)
        resolve_window(2, max_span=MAX_SPAN_ROLLUP, now=NOW, record=False)
    finally:
        _META.reset(token)
    assert meta.window == first


def test_window_dependency():
    app = FastAPI()

    @app.get("/w")
    def w(win: Window = Depends(window_params(168, MAX_SPAN_RAW))):
        return {"span": (win.end - win.start).total_seconds(), "mode": win.mode, "tz": win.tz}

    client = TestClient(app)
    assert client.get("/w").json() == {"span": 168 * 3600.0, "mode": "relative", "tz": "UTC"}
    assert client.get("/w", params={"hours": 0, "from": "2026-09-01T00:00:00Z",
                                    "to": "2026-09-02T00:00:00Z", "tz": "Europe/Berlin"}).json() == \
        {"span": 86400.0, "mode": "fixed", "tz": "Europe/Berlin"}


# ── Planner (pure) ────────────────────────────────────────────────────────────

RET = Retention(days={C.RAW: 30, C.CONVERSATIONS_1H: 90, C.QOS_1H: 90, C.INTERFACE_5M: 400}, earliest={})


def _w(start: datetime, end: datetime) -> Window:
    return Window(start=start, end=end, mode="fixed")


def _plan(start, end, f=None, **kw):
    return S.build_plan(_w(start, end), f or F.FlowFilter(), retention=RET, now=NOW, **kw)


def _segs(plan):
    return [(s.table, s.start, s.end) for s in plan.segments]


def _contiguous(plan):
    for a, b in zip(plan.segments, plan.segments[1:]):
        assert a.end == b.start
    if plan.segments:
        assert plan.segments[0].start == plan.effective_from
        assert plan.segments[-1].end == plan.effective_to


def test_short_window_is_raw_and_exact():
    s, e = NOW - timedelta(hours=24, minutes=7), NOW
    plan = _plan(s, e)
    assert plan.source == "raw" and _segs(plan) == [(C.RAW, s, e)]
    assert (plan.effective_from, plan.effective_to, plan.granularity_s, plan.warnings) == (s, e, 1, ())


def test_long_unaligned_window_is_hybrid_with_raw_edges():
    s = datetime(2026, 9, 20, 10, 30, 15, tzinfo=timezone.utc)
    e = s + timedelta(hours=72)
    plan = _plan(s, e)
    h = lambda hh: datetime(2026, 9, 20, hh, tzinfo=timezone.utc)  # noqa: E731
    assert plan.source == "hybrid"
    assert _segs(plan) == [
        (C.RAW, s, h(11)),
        (C.CONVERSATIONS_1H, h(11), h(10) + timedelta(hours=72)),
        (C.RAW, h(10) + timedelta(hours=72), e),
    ]
    assert (plan.effective_from, plan.effective_to, plan.granularity_s) == (s, e, 3600)
    assert not plan.retention_limited
    _contiguous(plan)


def test_aligned_window_is_rollup_only():
    s = datetime(2026, 9, 20, tzinfo=timezone.utc)
    plan = _plan(s, s + timedelta(hours=72))
    assert plan.source == "rollup" and _segs(plan) == [(C.CONVERSATIONS_1H, s, s + timedelta(hours=72))]


def test_filter_the_rollup_cannot_answer_forces_raw():
    s = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    plan = _plan(s, s + timedelta(days=5), F.parse_flow_filter({"dscp": "0"}))
    assert plan.source == "raw" and _segs(plan) == [(C.RAW, s, s + timedelta(days=5))]
    # ...but the QoS rollup can: the same window is hybrid there.
    assert _plan(s, s + timedelta(days=5), F.parse_flow_filter({"dscp": "0"}), rollup=C.QOS_1H).source == "hybrid"
    # and a dropped dimension no longer blocks the rollup
    assert _plan(s, s + timedelta(days=5), F.parse_flow_filter({"dscp": "0"}), drop=frozenset({"dscp"})).source == "hybrid"


def test_raw_only_window_older_than_raw_retention_is_narrowed_and_reported():
    s, e = NOW - timedelta(days=40), NOW - timedelta(days=20)
    plan = _plan(s, e, F.parse_flow_filter({"iface": "7"}))
    assert plan.source == "raw"
    assert _segs(plan) == [(C.RAW, NOW - timedelta(days=30), e)]
    assert plan.retention_limited and plan.warnings[0][0] == "raw_retention"
    gone = _plan(NOW - timedelta(days=50), NOW - timedelta(days=40), F.parse_flow_filter({"iface": "7"}))
    assert gone.empty and gone.effective_from == gone.effective_to and gone.retention_limited


def test_old_window_uses_rollup_and_drops_edges_it_cannot_answer_exactly():
    s = NOW - timedelta(days=40, minutes=17)
    e = s + timedelta(days=4)
    plan = _plan(s, e)
    b_start, b_end = S.ceil_to(s, 3600), S.floor_to(e, 3600)
    assert plan.source == "rollup" and _segs(plan) == [(C.CONVERSATIONS_1H, b_start, b_end)]
    assert (plan.effective_from, plan.effective_to) == (b_start, b_end)
    assert plan.retention_limited and {w[0] for w in plan.warnings} == {"raw_retention"}
    # A short window older than raw retention still goes to the rollup.
    short = _plan(NOW - timedelta(days=35, hours=5), NOW - timedelta(days=35))
    assert short.source == "rollup"


def test_window_reaching_past_rollup_retention():
    s = NOW - timedelta(days=93)
    plan = _plan(s, NOW)
    roll_from = S.ceil_to(NOW - timedelta(days=90), 3600)
    assert plan.source == "hybrid"
    assert _segs(plan) == [(C.CONVERSATIONS_1H, roll_from, S.floor_to(NOW, 3600)), (C.RAW, S.floor_to(NOW, 3600), NOW)]
    assert plan.effective_from == roll_from
    assert "rollup_retention" in {w[0] for w in plan.warnings}


def test_head_beyond_raw_retention_but_tail_within():
    s = NOW - timedelta(days=30, minutes=30)
    plan = _plan(s, NOW)
    assert _segs(plan)[0] == (C.CONVERSATIONS_1H, S.ceil_to(s, 3600), S.floor_to(NOW, 3600))
    assert _segs(plan)[-1] == (C.RAW, S.floor_to(NOW, 3600), NOW)
    _contiguous(plan)


def test_window_inside_one_bucket_is_raw():
    s = datetime(2026, 9, 29, 10, 5, tzinfo=timezone.utc)
    plan = _plan(s, s + timedelta(minutes=50), min_rollup_span=timedelta(0))
    assert plan.source == "raw" and _segs(plan) == [(C.RAW, s, s + timedelta(minutes=50))]


def test_interface_rollup_uses_five_minute_buckets():
    s = datetime(2026, 9, 20, 10, 2, tzinfo=timezone.utc)
    plan = _plan(s, s + timedelta(days=3, minutes=1), F.parse_flow_filter({"iface": "3", "iface_dir": "in"}),
                 rollup=C.INTERFACE_5M)
    assert plan.source == "hybrid" and plan.granularity_s == 300
    assert _segs(plan)[1][0] == C.INTERFACE_5M and _segs(plan)[1][1] == datetime(2026, 9, 20, 10, 5, tzinfo=timezone.utc)
    assert _plan(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc),
                 rollup=C.INTERFACE_5M).source == "interface_rollup"


def test_hour_filter_in_half_hour_zone_cannot_use_hour_buckets():
    s = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    kolkata = F.parse_flow_filter({"hour": "9", "tz": "Asia/Kolkata"})
    tokyo = F.parse_flow_filter({"hour": "9", "tz": "Asia/Tokyo"})
    assert _plan(s, s + timedelta(days=4), kolkata).source == "raw"
    assert _plan(s, s + timedelta(days=4), tokyo).source == "hybrid"
    assert _plan(s, s + timedelta(days=4), kolkata, rollup=C.INTERFACE_5M).source != "raw"


def test_plan_records_meta_and_from_sql_shape():
    s = datetime(2026, 9, 20, 10, 30, 15, tzinfo=timezone.utc)
    plan = _plan(s, s + timedelta(hours=72), F.parse_flow_filter({"protocol": "0"}))
    meta = QueryMeta()
    plan.record(meta)
    assert (meta.source, meta.effective_from, meta.granularity_s) == ("hybrid", s, 3600)
    sql, params = plan.from_sql({"bytes": "bytes", "flows": ("flows", "toUInt64(1)")},
                                F.parse_flow_filter({"protocol": "0"}), F.UNRESTRICTED)
    assert sql.count("UNION ALL") == 1          # one raw part (both edges), one rollup part
    assert "toUInt64(1) AS flows FROM zenplus.flow_records" in sql
    assert "flows AS flows FROM zenplus.flow_conversations_1h" in sql
    assert "(timestamp >= %(nf_t0_from)s AND timestamp < %(nf_t0_to)s) OR (timestamp >= %(nf_t1_from)s" in sql
    assert S.VOLUME_GUARD in sql                 # raw part only
    assert sql.count(S.VOLUME_GUARD) == 1
    assert params["nf_protocol"] == 0            # zero is a value
    assert params["nf_t0_from"] == s and params["nf_t2_to"] == s.replace(minute=0, second=0) + timedelta(hours=72)
    empty = _plan(NOW - timedelta(days=50), NOW - timedelta(days=40), F.parse_flow_filter({"iface": "7"}))
    sql, _ = empty.from_sql({"bytes": "bytes"}, F.FlowFilter(), F.UNRESTRICTED)
    assert "WHERE (0)" in sql


# ── Hybrid reconciliation on real tables (opt-in ClickHouse) ──────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

RAW_COLS = [
    "timestamp", "received_at", "exporter_ip", "exporter_ip6", "src_addr", "dst_addr", "src_addr6", "dst_addr6",
    "src_port", "dst_port", "protocol", "tos", "tcp_flags", "bytes", "packets", "sampling_interval",
    "first_switched_ms", "last_switched_ms", "input_if", "output_if", "fields_present", "fw_event",
    "flow_direction", "ip_version",
]
TRACKED = (1 << 31) | 0x1FF      # v2 collector, v5-style fields present (incl. TCP flags)


def _fixture_rows(start: datetime, minutes: int) -> list[list]:
    rng = random.Random(1515)
    z4, z6 = ipaddress.IPv4Address("0.0.0.0"), ipaddress.IPv6Address("::")
    e1, e6 = ipaddress.IPv4Address("10.9.0.1"), ipaddress.IPv6Address("2001:db8:ffff::1")
    rows = []
    for m in range(minutes):
        t = start + timedelta(minutes=m)
        ms = int(t.timestamp() * 1000)
        flags = 0x02 if m % 3 == 0 else (0 if m % 11 == 0 else 0x1B)
        legacy = m % 13 == 0                      # pre-v2 row: presence unknown
        # TCP at the exact minute (hour boundaries fall on real flows).
        rows.append([t, t + timedelta(seconds=2), e1, z6,
                     ipaddress.IPv4Address(f"10.0.0.{m % 40 + 1}"), ipaddress.IPv4Address(f"192.0.2.{m % 7 + 1}"), z6, z6,
                     rng.randint(32768, 60999), 443, 6, 0, flags, 1000 + m % 17, 3, 1,
                     ms - 1500, ms, 1 + m % 3, 70000, 0 if legacy else TRACKED, 0, 0, 4])
        # UDP/DNS marked EF at half past.
        t2 = t + timedelta(seconds=30)
        rows.append([t2, t2, e1, z6,
                     ipaddress.IPv4Address(f"10.1.0.{m % 5 + 1}"), ipaddress.IPv4Address("8.8.8.8"), z6, z6,
                     rng.randint(32768, 60999), 53, 17, 46 << 2, 0, 200 + m % 5, 1, 1,
                     0, 0, 2, 5, TRACKED, 0, 1, 4])
        if m % 7 == 0:                            # IPv6 flow from an IPv6 exporter
            t3 = t + timedelta(seconds=45)
            rows.append([t3, t3, z4, e6, z4, z4,
                         ipaddress.IPv6Address("2001:db8::10"), ipaddress.IPv6Address("2001:db8:1::20"),
                         rng.randint(32768, 60999), 443, 6, 0, 0x18, 5000, 4, 1,
                         ms, ms + 45000, 9, 10, TRACKED, 0, 0, 6])
    # Rows every aggregate must exclude: a corrupt byte count and an NSEL
    # create event (no volume of its own).
    t = start + timedelta(hours=30, seconds=7)
    rows.append([t, t, e1, z6, ipaddress.IPv4Address("10.0.0.1"), ipaddress.IPv4Address("192.0.2.1"), z6, z6,
                 1, 443, 6, 0, 0x1B, 5 * 10**12, 1, 1, 0, 0, 1, 1, TRACKED, 0, 0, 4])
    rows.append([t, t, e1, z6, ipaddress.IPv4Address("10.0.0.2"), ipaddress.IPv4Address("192.0.2.2"), z6, z6,
                 1, 443, 6, 0, 0x02, 900, 1, 1, 0, 0, 1, 1, TRACKED | (1 << 13), 1, 0, 4])
    return rows


@pytest.fixture(scope="module")
def ch_flows():
    client = scratch_client()
    with flow_db(client) as db:
        start = S.floor_to(datetime.now(timezone.utc), 3600) - timedelta(days=9, hours=2)
        client.insert(f"{db}.flow_records", _fixture_rows(start, 9 * 1440), column_names=RAW_COLS)
        yield client, db, start


def _raw_exact(client, db, w: Window, f, drop=frozenset()):
    where, params = F.compile_filter(f, C.RAW, F.UNRESTRICTED, drop)
    return client.query(
        f"SELECT count(), sum(bytes), uniqExact({S.SRC}) FROM {db}.flow_records "
        f"WHERE timestamp >= %(a)s AND timestamp < %(b)s AND {where}",
        parameters={**params, "a": w.start, "b": w.end},
    ).result_rows[0]


def _planned(client, w: Window, f, rollup=C.CONVERSATIONS_1H, drop=frozenset()):
    plan = S.build_plan(w, f, retention=RET, rollup=rollup, drop=drop, now=datetime.now(timezone.utc))
    columns = {"bytes": "bytes", "flows": ("flows", "toUInt64(1)")}
    select = "sum(flows), sum(bytes)"
    if rollup == C.CONVERSATIONS_1H:          # the QoS rollup keeps no addresses
        columns["src"] = S.SRC
        select += ", uniqExact(src)"
    sql, params = plan.from_sql(columns, f, F.UNRESTRICTED, drop)
    return plan, tuple(client.query(f"SELECT {select} FROM {sql} AS src", parameters=params).result_rows[0])


def _windows(start: datetime) -> list[Window]:
    return [
        _w(start + timedelta(days=1, hours=10, minutes=30, seconds=15), start + timedelta(days=4, hours=10, minutes=30, seconds=15)),
        _w(start + timedelta(days=2), start + timedelta(days=5)),
        _w(start + timedelta(hours=10, minutes=7), start + timedelta(days=8, hours=10, minutes=22)),
        _w(start + timedelta(days=3, hours=5, minutes=59, seconds=59), start + timedelta(days=7, hours=6, seconds=1)),
    ]


@pytest.mark.parametrize("query", [
    {}, {"protocol": "6"}, {"talker": "10.0.0.0/28"}, {"dst_ports": "53,443"}, {"netclass": "Private 10/8"},
    {"ip_version": "6"}, {"exporter": "2001:db8:ffff::1"}, {"not_dst": "8.8.8.8"}, {"hour": "10", "tz": "Asia/Tokyo"},
    {"app": "DNS"},
])
def test_hybrid_reconciles_with_raw(ch_flows, monkeypatch, query):
    client, db, start = ch_flows
    monkeypatch.setattr(C, "CH_DB", db)
    f = F.parse_flow_filter(query)
    for w in _windows(start):
        plan, got = _planned(client, w, f)
        assert plan.source in ("hybrid", "rollup"), (query, plan.source)
        assert got == tuple(_raw_exact(client, db, w, f)), (query, w)


@pytest.mark.parametrize("query", [{}, {"dscp": "46"}, {"tcpflag": "syn_only"}, {"tcpflag": "no_flags"}, {"protocol": "17"}])
def test_qos_hybrid_reconciles_with_raw(ch_flows, monkeypatch, query):
    client, db, start = ch_flows
    monkeypatch.setattr(C, "CH_DB", db)
    f = F.parse_flow_filter(query)
    for w in _windows(start):
        plan, got = _planned(client, w, f, rollup=C.QOS_1H)
        assert plan.source in ("hybrid", "rollup")
        assert got == tuple(_raw_exact(client, db, w, f)[:2]), (query, w)


def test_adjacent_windows_add_up(ch_flows, monkeypatch):
    client, db, start = ch_flows
    monkeypatch.setattr(C, "CH_DB", db)
    a = start + timedelta(days=1, minutes=13, seconds=4)
    mid = a + timedelta(days=3, minutes=41)
    b = mid + timedelta(days=3, hours=2, minutes=5)
    f = F.FlowFilter()
    _, left = _planned(client, _w(a, mid), f)
    _, right = _planned(client, _w(mid, b), f)
    _, whole = _planned(client, _w(a, b), f)
    assert left[0] + right[0] == whole[0] and left[1] + right[1] == whole[1]
    assert whole == tuple(_raw_exact(client, db, _w(a, b), f))


def test_volume_guard_and_nsel_events_excluded_from_raw_aggregates(ch_flows, monkeypatch):
    client, db, start = ch_flows
    monkeypatch.setattr(C, "CH_DB", db)
    w = _w(start + timedelta(hours=29, minutes=59, seconds=30), start + timedelta(hours=30, minutes=1))
    unguarded = client.query(
        f"SELECT count() FROM {db}.flow_records WHERE timestamp >= %(a)s AND timestamp < %(b)s",
        parameters={"a": w.start, "b": w.end}).result_rows[0][0]
    guarded = _raw_exact(client, db, w, F.FlowFilter())[0]
    assert unguarded - guarded == 2
