"""NetFlow forensics (NF-10, NF-11, FE-04, CR-17): api/v1/netflow/forensics.py and
services/netflow_forensics_export.py.

Layers:
  * unit — cursor encoding and validation, the keyset/snapshot SQL, duplicate
    skipping across pages, the partial-count test, row formatting (absent
    fields are null, never zero), CSV formula neutralisation, export columns;
  * scratch ClickHouse (NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py):
    a fixture larger than one page with heavy sort-key ties and identical
    duplicate records is paged completely under four sort orders with no
    duplicates or omissions; rows arriving mid-paging stay out; talker CIDR
    keeps destination-only rows; IPv6 CIDR search; firewall events are shown
    (no volume guard); the export iterator returns the complete result; a
    window older than raw retention is reported as unavailable;
  * live API (NETFLOW_B3_API, e.g. http://127.0.0.1:8113, admin credentials
    from NETFLOW_B3_USER/NETFLOW_B3_PASS, default admin/admin123): paging via
    the cursor, string 400s for invalid input, the legacy list and CSV, the
    streamed export with its headers, and the export-job lifecycle (queue,
    progress, download, delete). With NETFLOW_B3_PG (the API's PostgreSQL,
    e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/zenplus) it also checks
    the audit rows and that a viewer without netflow.export gets 403.
"""

from __future__ import annotations

import asyncio
import collections
import gzip
import io
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.v1.netflow import common as C, filters as F, forensics as FX  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError  # noqa: E402

UNRESTRICTED = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"netflow.view"}))
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _spec(**kw) -> FX.SearchSpec:
    base = dict(start=T0 - timedelta(hours=1), end=T0, snapshot_ms=int(T0.timestamp() * 1000),
                filter=F.FlowFilter(), vis=UNRESTRICTED)
    base.update(kw)
    return FX.SearchSpec(**base)


def _row(**values) -> tuple:
    """A result row in the SELECT layout with sensible defaults."""
    d = {
        "ts_ms": int(T0.timestamp() * 1000) - 1000, "rx_ms": int(T0.timestamp() * 1000) - 900, "rk": 1, "sv": 0,
        "exporter": "192.0.2.1", "observation_domain": 0, "export_protocol": "netflow9", "flow_version": 9,
        "collector_id": "c1", "src": "10.0.0.1", "dst": "10.0.0.2", "src_port": 51000, "dst_port": 443,
        "protocol": 6, "ip_version": 4, "tcp_flags": 0x12, "tos": 184, "input_if": 3, "output_if": 70000,
        "packets": 10, "bytes": 1500, "sampling_interval": 1, "duration_ms": 250, "src_as": 0, "dst_as": 0,
        "src_mask": 24, "dst_mask": 24, "next_hop": "0.0.0.0", "flow_direction": 255, "app_id": 0, "fw_event": 0,
        "xlate_src": "::", "xlate_dst": "::", "xlate_src_port": 0, "xlate_dst_port": 0, "flow_end_reason": 0,
        "fields_present": (1 << 31) | 0b111111,
    }
    d.update(values)
    return tuple(d[name] for name in FX._COLS)


# ── Cursor ────────────────────────────────────────────────────────────────────

def _cursor(**kw) -> FX.Cursor:
    base = dict(sort="bytes", order="asc", start=T0 - timedelta(hours=1), end=T0, snapshot_ms=123,
                sort_value=100, ts_ms=1_790_000_000_123, row_key=2 ** 64 - 5, dup=2, offset=500,
                fingerprint="abc", mode="relative")
    base.update(kw)
    return FX.Cursor(**base)


def test_cursor_round_trips_including_uint64_row_keys():
    c = _cursor()
    assert FX.decode_cursor(FX.encode_cursor(c)) == c
    t = _cursor(sort="timestamp", sort_value=None)
    assert FX.decode_cursor(FX.encode_cursor(t)) == t


@pytest.mark.parametrize("token", ["", "not base64!", "e30", "x" * 2000,
                                   FX.encode_cursor(_cursor())[:-6] + "AAAAAA"])
def test_malformed_cursor_is_a_400_not_a_500(token):
    with pytest.raises(NetflowError) as exc:
        FX.decode_cursor(token)
    assert exc.value.status_code == 400 and exc.value.code == "invalid_filter"


def test_cursor_rejects_unknown_sort_and_bad_keys():
    import base64

    def enc(body):
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

    good = json.loads(base64.urlsafe_b64decode(FX.encode_cursor(_cursor()) + "=="))
    for mutate in (
        lambda b: b.update(s="src_addr"),              # not a sortable column
        lambda b: b.update(o="sideways"),
        lambda b: b.update(k=[100, "x", 5]),
        lambda b: b.update(k=[True, 1, 5]),
        lambda b: b.update(d=0),
        lambda b: b.update(k=[100, 1, 2 ** 64]),
        lambda b: b.update(v=2),
    ):
        body = dict(good)
        mutate(body)
        with pytest.raises(NetflowError):
            FX.decode_cursor(enc(body))


def test_filter_fingerprint_tracks_the_filter():
    a = FX.filter_fingerprint(F.parse_flow_filter({"src": "10.0.0.0/8"}))
    assert a == FX.filter_fingerprint(F.parse_flow_filter({"src": "10.0.0.0/8"}))
    assert a != FX.filter_fingerprint(F.parse_flow_filter({"src": "10.0.0.0/16"}))
    assert a != FX.filter_fingerprint(F.parse_flow_filter({"src": "10.0.0.0/8", "tz": "Asia/Riyadh"}))


# ── SQL ───────────────────────────────────────────────────────────────────────

def test_where_is_half_open_snapshotted_scoped_and_unguarded():
    vis = FlowVisibility(restricted=True, exporters=frozenset({"192.0.2.1"}), user_id=None, permissions=frozenset())
    sql, params = FX.build_where(_spec(vis=vis, filter=F.parse_flow_filter({"talker": "10.0.0.0/24"})))
    assert "timestamp >= %(fx_start)s AND timestamp < %(fx_end)s" in sql
    assert "received_at <= fromUnixTimestamp64Milli(toInt64(%(fx_snap)s), 'UTC')" in sql
    assert "exporter_ip IN" in sql                      # visibility scope
    assert "fw_event NOT IN" not in sql                 # forensics shows every record
    assert "src_addr" in sql and "dst_addr" in sql      # talker is source OR destination
    assert "BETWEEN" not in sql.split("AND timestamp < %(fx_end)s")[0]
    empty = FlowVisibility(restricted=True, exporters=frozenset(), user_id=None, permissions=frozenset())
    sql, _ = FX.build_where(_spec(vis=empty))
    assert " AND 0" in sql or sql.endswith("0")         # empty scope: no rows, never unfiltered


def test_keyset_predicates():
    # Rows after the cursor key are selected strictly; the key's own duplicates
    # come from a separate equal-key part (see build_page_sql).
    c = _cursor(sort="timestamp", order="desc", sort_value=None)
    sql, params = FX.build_where(_spec(sort="timestamp", order="desc", after=c))
    assert "timestamp <= fromUnixTimestamp64Milli(toInt64(%(fx_kt)s), 'UTC')" in sql
    assert f"{FX.ROW_KEY} < %(fx_kr)s" in sql
    assert params["fx_kt"] == c.ts_ms and params["fx_kr"] == c.row_key
    eq, _ = FX.build_where(_spec(sort="timestamp", order="desc", after=c), part="equal")
    assert f"{FX.ROW_KEY} = %(fx_kr)s" in eq and "timestamp = fromUnixTimestamp64Milli" in eq
    c = _cursor(sort="bytes", order="asc")
    sql, params = FX.build_where(_spec(sort="bytes", order="asc", after=c))
    assert "((bytes) > %(fx_ks)s OR ((bytes) = %(fx_ks)s AND (timestamp > " in sql
    assert f"{FX.ROW_KEY} > %(fx_kr)s" in sql and params["fx_ks"] == 100
    page_sql, p = FX.build_page_sql(_spec(sort="bytes", order="asc"), 501)
    assert page_sql.endswith("ORDER BY sv ASC, timestamp ASC, rk ASC LIMIT %(fx_limit)s") and p["fx_limit"] == 501
    count_sql, _ = FX.build_count_sql(_spec(after=c))
    assert "fx_kr" not in count_sql and "timeout_overflow_mode = 'break'" in count_sql


def test_apply_page_accumulates_dup():
    """The query already left out the duplicates the cursor emitted
    (build_page_sql); apply_page cuts the page and counts the key's
    duplicates across pages."""
    spec = _spec(sort="bytes", order="asc", after=None)
    same = [_row(sv=100, ts_ms=5, rk=9) for _ in range(5)]
    rows = same + [_row(sv=101, ts_ms=1, rk=1)]
    page, nxt = FX.apply_page(spec, rows, 2)
    assert len(page) == 2 and nxt.dup == 2 and (nxt.sort_value, nxt.ts_ms, nxt.row_key) == (100, 5, 9)
    assert nxt.offset == 2
    # Next page: the 3 identical rows not yet emitted, then the rest.
    page2, nxt2 = FX.apply_page(FX.replace(spec, after=nxt), rows[2:] + [_row(sv=102)], 2)
    assert len(page2) == 2 and nxt2.dup == 4 and nxt2.offset == 4
    page3, nxt3 = FX.apply_page(FX.replace(spec, after=nxt2), rows[4:], 2)
    assert [r[FX._IDX["sv"]] for r in page3] == [100, 101] and nxt3 is None
    ids = FX.row_ids(same, nxt)
    assert ids[0].endswith("-2") and len(set(ids)) == 5


def test_count_is_partial():
    assert not FX.count_is_partial({"read_rows": "10", "total_rows_to_read": "10"}, 0.1, 10)
    assert FX.count_is_partial({"read_rows": "5", "total_rows_to_read": "10"}, 0.1, 10)
    assert FX.count_is_partial({}, 9.9, 10)
    assert not FX.count_is_partial({"read_rows": "1", "total_rows_to_read": "0"}, 0.01, 10)


def test_sort_and_order_validation():
    with pytest.raises(NetflowError) as exc:
        FX._validate_sort("src_addr", "desc")
    assert exc.value.status_code == 400 and "sort" in exc.value.fields
    FX._validate_sort("duration", "asc")


# ── Rows ──────────────────────────────────────────────────────────────────────

def test_absent_fields_are_null_not_zero():
    tracked_no_flags = (1 << 31) | (1 << 2) | (1 << 3) | (1 << 4)      # ports + ifIndexes only
    r = FX.format_row(_row(fields_present=tracked_no_flags, tcp_flags=0, tos=0))
    assert r["tcp_flags"] is None and r["flags_state"] == "absent"
    assert r["dscp"] is None and r["tos"] is None and r["duration_ms"] is None
    assert r["src_as"] is None and r["next_hop"] is None and r["src_mask"] is None
    assert r["output_if"] == 70000 and r["input_interface"]["ifindex"] == 3   # full-width ifIndex
    legacy = FX.format_row(_row(fields_present=0, tcp_flags=0))
    assert legacy["tcp_flags"] == 0 and legacy["flags_state"] == "legacy" and legacy["tracked"] is False
    exported = FX.format_row(_row(tcp_flags=0x12))
    assert exported["tcp_flags"] == 0x12 and exported["flags_state"] == "exported" and exported["dscp"] == 46


def test_duration_validity_and_nat_and_fw_event():
    r = FX.format_row(_row(duration_ms=-1))
    assert r["duration_ms"] is None
    fp = (1 << 31) | (1 << 12) | (1 << 13)
    r = FX.format_row(_row(fields_present=fp, xlate_src="203.0.113.7", xlate_src_port=40000, fw_event=3))
    assert r["nat"] == {"src": "203.0.113.7", "src_port": 40000, "dst": None, "dst_port": None}
    assert r["fw_event"] == "denied"
    r = FX.format_row(_row(src="2001:db8::5", dst="2001:db8:1::9", ip_version=6))
    assert r["src"] == "2001:db8::5" and r["ip_version"] == 6


def test_icmp_keeps_type_code_and_service_label():
    r = FX.format_row(_row(protocol=1, src_port=0, dst_port=8 * 256, fields_present=(1 << 31) | (1 << 11)))
    assert r["dst_port"] == 2048 and r["service"] and r["application"] == "ICMP"


def test_csv_neutralises_formulas():
    assert FX.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert FX.csv_safe("+1") == "'+1" and FX.csv_safe("@x") == "'@x" and FX.csv_safe("\tx") == "'\tx"
    assert FX.csv_safe("Gi0/1") == "Gi0/1" and FX.csv_safe(None) == ""
    item = FX.format_row(_row())
    names = {("192.0.2.1", 3): "=cmd|' /C calc'!A0"}
    text = FX.csv_header(("input_interface", "bytes")) + FX.csv_lines([item], ("input_interface", "bytes"), names)
    lines = text.strip().splitlines()
    assert lines[0] == "input_interface,bytes"
    assert lines[1] == "'=cmd|' /C calc'!A0,1500"


def test_export_columns_validation():
    assert FX.parse_columns(None) == FX.EXPORT_COLUMN_NAMES
    assert FX.parse_columns("src,dst,src") == ("src", "dst")
    with pytest.raises(NetflowError):
        FX.parse_columns("src,password")


def test_export_dir_falls_back_to_a_private_temp_dir(tmp_path, monkeypatch):
    """The appliance data dir may belong to another account: jobs then write to
    a private temp dir instead of failing; a temp dir another account could
    write is never used."""
    import tempfile

    monkeypatch.setattr(FX, "_EXPORT_DIRS", {})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    ok = tmp_path / "exports"
    monkeypatch.setenv("NETFLOW_EXPORT_DIR", str(ok))
    assert FX.export_dir() == ok and ok.is_dir()

    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        monkeypatch.setenv("NETFLOW_EXPORT_DIR", str(locked / "exports"))
        if os.access(locked, os.W_OK):     # running as root: permissions are not enforced
            pytest.skip("permission bits are not enforced for this user")
        got = FX.export_dir()
        assert got == tmp_path / "tmp" / f"zenplus-netflow-exports-{os.getuid()}"
        assert got.is_dir() and (got.stat().st_mode & 0o077) == 0
        # A fallback dir that others can write is refused (the configured path comes back).
        monkeypatch.setattr(FX, "_EXPORT_DIRS", {})
        got.chmod(0o777)
        assert FX.export_dir() == locked / "exports"
    finally:
        locked.chmod(0o700)


def test_tcp_flag_letters():
    assert FX.tcp_flag_letters(0x12) == "AS" and FX.tcp_flag_letters(0) == "·" and FX.tcp_flag_letters(None) == ""


# ── Scratch ClickHouse ────────────────────────────────────────────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402


@pytest.fixture
def real_ch(monkeypatch):
    import threading

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    yield client
    C._retention_cache.update(value=None, at=0.0)


COLS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_addr6", "dst_addr6", "ip_version",
        "src_port", "dst_port", "protocol", "bytes", "packets", "first_switched_ms", "last_switched_ms",
        "fields_present", "fw_event", "tcp_flags", "flow_sequence"]


def _fixture_rows(now: datetime) -> list[list]:
    base = now - timedelta(minutes=30)
    tracked = (1 << 31) | 0b111111
    rows = []
    for i in range(1205):                      # distinct records, many time and byte ties
        t = base + timedelta(seconds=i % 50)
        rows.append([t, t, "10.9.0.1", f"10.1.{i // 250}.{i % 250 + 1}", "10.3.0.9", "::", "::", 4,
                     1000 + i, 443, 6, 100 + i % 7, 1 + i % 3, 1_000 + i, 1_000 + i + (i % 11) * 10,
                     tracked, 0, 0x12, i])
    t = base + timedelta(seconds=5)
    for i in range(3000):                      # 3000 rows with identical sort keys
        rows.append([t, t, "10.9.0.1", "10.2.0.1", "10.2.0.2", "::", "::", 4, 20000 + i, 80, 6, 100, 1,
                     5_000, 5_000, tracked, 0, 0x10, 10_000 + i])
    for k in range(5):                         # 5 records stored three times each
        t = base + timedelta(seconds=7)
        for _copy in range(3):
            rows.append([t, t, "10.9.0.1", "10.4.0.1", f"10.4.0.{k + 2}", "::", "::", 4, 5555, 22, 6, 100, 1,
                         7_000, 7_000, tracked, 0, 0x02, 90_000 + k])
    return rows


def _insert(client, db, rows):
    client.insert(f"{db}.flow_records", rows, column_names=COLS)


def _all_keys(client, db, spec: FX.SearchSpec) -> collections.Counter:
    where, params = FX.build_where(spec, keyset=False)
    sql = (f"SELECT toUnixTimestamp64Milli(timestamp), {FX.ROW_KEY} FROM {db}.flow_records WHERE {where}")
    res = client.query(sql, parameters=params)
    return collections.Counter((int(a), int(b)) for a, b in res.result_rows)


def _page_all(spec: FX.SearchSpec, page_size: int) -> tuple[list[tuple], int]:
    out, pages = [], 0
    current = spec
    while True:
        rows, nxt = asyncio.run(FX.fetch_page(current, page_size, budget_s=30, label="test"))
        out.extend(rows)
        pages += 1
        if nxt is None:
            return out, pages
        # The cursor survives encoding (as it does between HTTP requests).
        current = FX.replace(current, after=FX.decode_cursor(FX.encode_cursor(nxt)))


@pytest.mark.parametrize("sort,order", [("timestamp", "desc"), ("bytes", "asc"), ("duration", "desc"),
                                        ("src_port", "asc")])
def test_paging_returns_every_record_exactly_once(real_ch, monkeypatch, sort, order):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        _insert(real_ch, db, _fixture_rows(now))
        spec = FX.SearchSpec(start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
                             snapshot_ms=int(now.timestamp() * 1000) + 60_000, filter=F.FlowFilter(),
                             vis=UNRESTRICTED, sort=sort, order=order)
        expected = _all_keys(real_ch, db, spec)
        rows, pages = _page_all(spec, 500)
        got = collections.Counter((int(r[FX._IDX["ts_ms"]]), int(r[FX._IDX["rk"]])) for r in rows)
        assert sum(expected.values()) == 4220 and pages == 9
        assert got == expected                       # no omissions, no duplicates (duplicates kept 3x)
        keys = [FX._row_key_of(spec, r) for r in rows]
        assert keys == sorted(keys, reverse=(order == "desc"))
        counted = asyncio.run(FX.count_matches(spec))
        assert counted.total == 4220 and counted.exact


def test_rows_arriving_while_paging_are_excluded(real_ch, monkeypatch):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        _insert(real_ch, db, _fixture_rows(now)[:1205])
        spec = FX.SearchSpec(start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
                             snapshot_ms=int(now.timestamp() * 1000) + 1000, filter=F.FlowFilter(),
                             vis=UNRESTRICTED, sort="timestamp", order="desc")
        first, nxt = asyncio.run(FX.fetch_page(spec, 500, budget_s=30, label="test"))
        late = now + timedelta(seconds=30)
        rows = []
        for i in range(10):     # flow time inside the window, received after the snapshot
            t = now - timedelta(minutes=40)
            rows.append([t, late, "10.9.0.1", "10.7.0.1", "10.7.0.2", "::", "::", 4, 1, 2, 6, 5, 1, 1, 2,
                         1 << 31, 0, 0, 900_000 + i])
        _insert(real_ch, db, rows)
        rest, _pages = _page_all(FX.replace(spec, after=nxt), 500)
        assert len(first) + len(rest) == 1205
        assert not any(r[FX._IDX["src"]] == "10.7.0.1" for r in rest)
        assert asyncio.run(FX.count_matches(spec)).total == 1205


def test_talker_ipv6_firewall_events_and_export(real_ch, monkeypatch):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        t = now - timedelta(minutes=10)
        tracked = (1 << 31) | 0b111111
        rows = [
            [t, t, "10.9.0.1", "10.2.0.1", "10.2.0.2", "::", "::", 4, 1111, 80, 6, 10, 1, 1, 2, tracked, 0, 2, 1],
            [t, t, "10.9.0.1", "10.8.0.1", "10.2.0.3", "::", "::", 4, 1112, 80, 6, 10, 1, 1, 2, tracked, 0, 2, 2],
            [t, t, "10.9.0.1", "0.0.0.0", "0.0.0.0", "2001:db8::5", "2001:db8:1::9", 6, 1113, 443, 6, 10, 1, 1, 2,
             tracked, 0, 2, 3],
            [t, t, "10.9.0.1", "10.5.0.1", "10.5.0.2", "::", "::", 4, 1114, 443, 6, 0, 0, 1, 2,
             tracked | (1 << 13), 1, 0, 4],                    # NSEL "created" event: no volume
            [t, t, "10.9.0.1", "10.5.0.1", "10.5.0.2", "::", "::", 4, 1115, 443, 6, 2_000_000_000_000_000, 1, 1, 2,
             tracked, 0, 2, 5],                                # implausible volume, still stored
        ]
        _insert(real_ch, db, rows)
        window = dict(start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
                      snapshot_ms=int(now.timestamp() * 1000) + 60_000, vis=UNRESTRICTED)

        def search(q):
            spec = FX.SearchSpec(filter=F.parse_flow_filter(q), **window)
            got, _ = asyncio.run(FX.fetch_page(spec, 100, budget_s=30, label="test"))
            return [FX.format_row(r) for r in got]

        assert len(search({})) == 5                           # no volume guard in forensics
        talker = search({"talker": "10.2.0.0/24"})
        assert {r["src"] for r in talker} == {"10.2.0.1", "10.8.0.1"}   # destination-only row kept
        v6 = search({"src": "2001:db8::/32"})
        assert [(r["src"], r["dst"], r["ip_version"]) for r in v6] == [("2001:db8::5", "2001:db8:1::9", 6)]
        assert [r["src_port"] for r in search({"dst": "2001:db8:1::9"})] == [1113]
        created = search({"fw_event": "created"})
        assert len(created) == 1 and created[0]["fw_event"] == "created"
        assert len(search({"not_talker": "10.5.0.0/16"})) == 3

        spec = FX.SearchSpec(filter=F.FlowFilter(), sort="bytes", order="desc", **window)

        async def collect():
            out = []
            async for chunk in FX.iter_export_rows(spec, 10_000, chunk=2):
                out.extend(chunk)
            return out

        exported = asyncio.run(collect())
        assert len(exported) == 5 and int(exported[0][FX._IDX["bytes"]]) == 2_000_000_000_000_000


def test_window_older_than_raw_retention_is_unavailable(real_ch, monkeypatch):
    now = C.now_utc()
    C._retention_cache.update(value=C.Retention(days={C.RAW: 30}, earliest={C.RAW: now - timedelta(days=29)}),
                              at=time.monotonic())
    win = C.resolve_window(from_ts=(now - timedelta(days=60)).isoformat(), to_ts=(now - timedelta(days=40)).isoformat(),
                           max_span=C.MAX_SPAN_RAW)

    async def call():
        return await FX.forensics_search(page_size=500, cursor=None, sort="timestamp", order="desc", win=win,
                                         f=F.FlowFilter(), vis=UNRESTRICTED, db=None)

    body = asyncio.run(call())
    assert body["items"] == [] and body["total"] is None
    assert body["unavailable"]["reason"] == "raw_retention" and body["unavailable"]["raw_days"] == 30
    assert "30 days" in body["unavailable"]["message"]


def test_partly_retained_window_starts_at_the_retention_horizon(real_ch, monkeypatch):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        _insert(real_ch, db, _fixture_rows(now)[:20])
        C._retention_cache.update(value=C.Retention(days={C.RAW: 1}, earliest={}), at=time.monotonic())

        async def no_names(keys, db):
            return {}

        monkeypatch.setattr(FX, "_resolve_interface_names", no_names)
        win = C.resolve_window(from_ts=(now - timedelta(days=3)).isoformat(), to_ts=now.isoformat(),
                               max_span=C.MAX_SPAN_RAW)

        async def call():
            return await FX.forensics_search(page_size=500, cursor=None, sort="timestamp", order="desc", win=win,
                                             f=F.FlowFilter(), vis=UNRESTRICTED, db=None)

        body = asyncio.run(call())
    assert len(body["items"]) == 20 and body["total"] == 20 and body["total_exact"] is True
    assert body["window"]["effective_from"] > body["window"]["from"]


# ── Live API ──────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B3_API")
PG = os.environ.get("NETFLOW_B3_PG")
ADMIN = (os.environ.get("NETFLOW_B3_USER", "admin"), os.environ.get("NETFLOW_B3_PASS", "admin123"))


def _api_up() -> bool:
    if not API:
        return False
    import requests

    try:
        requests.get(f"{API}/api/v1/health", timeout=3)
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="set NETFLOW_B3_API to a running API of this tree")


def _login(user, password):
    import requests

    r = requests.post(f"{API}/api/v1/auth/login", json={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin():
    return _login(*ADMIN)


def _get(headers, path, **params):
    import requests

    return requests.get(f"{API}/api/v1/netflow{path}", params=params, headers=headers, timeout=90)


@live
def test_live_search_pages_with_cursor_and_envelope(admin):
    r = _get(admin, "/forensics/search", hours=24, page_size=50, envelope=1)
    assert r.status_code == 200, r.text
    body, meta = r.json()["data"], r.json()["meta"]
    assert meta["window"]["source"] == "raw" and meta["truncated"]["limit"] == 50
    assert isinstance(body["total"], int) and body["total_exact"] in (True, False)
    if not body["has_more"]:
        pytest.skip("the scratch collector holds fewer than 51 records in 24 h")
    first_ids = {i["id"] for i in body["items"]}
    r2 = _get(admin, "/forensics/search", hours=24, page_size=50, cursor=body["next_cursor"], envelope=1)
    page2 = r2.json()["data"]
    assert r2.status_code == 200 and page2["offset"] == 50
    assert not first_ids & {i["id"] for i in page2["items"]}
    assert page2["window"]["from"] == body["window"]["from"]             # the cursor pins the window
    # A cursor only continues its own search.
    r3 = _get(admin, "/forensics/search", hours=24, page_size=50, cursor=body["next_cursor"], protocol=17)
    assert r3.status_code == 400 and isinstance(r3.json()["detail"], str)


@live
@pytest.mark.parametrize("params", [{"src": "invalid-ip"}, {"src_port": "a"}, {"dscp": "64"},
                                    {"cursor": "garbage!"}, {"sort": "password"}, {"page_size": "0"}])
def test_live_invalid_input_is_a_string_400(admin, params):
    r = _get(admin, "/forensics/search", hours=1, **params)
    assert r.status_code == 400, r.text
    body = r.json()
    assert isinstance(body["detail"], str) and body["code"] in ("invalid_filter", "invalid_window")


@live
def test_live_legacy_list_and_csv(admin):
    r = _get(admin, "/forensics", hours=1, limit=5)
    assert r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) <= 5
    r = _get(admin, "/forensics", hours=1, limit=5, format="csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("timestamp,exporter_ip,src,")


@live
def test_live_streamed_export_is_complete(admin):
    search = _get(admin, "/forensics/search", hours=1, page_size=1, protocol=17).json()
    total = search["total"]
    if not search["total_exact"] or total > FX.EXPORT_STREAM_MAX_ROWS:
        pytest.skip("result too large for the direct export on this appliance")
    # Snapshots differ between the two requests; compare on a closed window.
    frm, to = search["window"]["from"], search["window"]["to"]
    r = _get(admin, "/forensics/export", **{"from": frm, "to": to, "protocol": 17, "columns": "timestamp_utc,src,dst,bytes"})
    assert r.status_code == 200, r.text
    lines = r.text.strip().splitlines()
    assert lines[0] == "timestamp_utc,src,dst,bytes"
    assert int(r.headers["X-Total-Count"]) == len(lines) - 1 == int(r.headers["X-Exported-Rows"])
    assert r.headers["X-Truncated"] == "false"


@live
def test_live_export_job_lifecycle(admin):
    import requests

    r = requests.post(f"{API}/api/v1/netflow/forensics/export-jobs", params={"hours": 1, "protocol": 6},
                      headers=admin, timeout=60)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "queued" and job["query"]["filter"] == {"protocol": "6"}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = _get(admin, f"/forensics/export-jobs/{job['id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(2)
    assert job["status"] == "done", job
    assert job["download_ready"] and job["progress_pct"] == 100.0 and job["expires_at"]
    listed = _get(admin, "/forensics/export-jobs").json()
    assert any(j["id"] == job["id"] for j in listed["items"]) and listed["limits"]["job_max_rows"] > 0
    d = _get(admin, f"/forensics/export-jobs/{job['id']}/download")
    assert d.status_code == 200 and d.headers["content-type"] == "application/gzip"
    text = gzip.decompress(d.content).decode()
    lines = text.strip().splitlines()
    assert lines[0].startswith("timestamp_utc,received_at_utc,exporter,")
    assert len(lines) - 1 == job["row_count"] == job["total"]
    gone = requests.delete(f"{API}/api/v1/netflow/forensics/export-jobs/{job['id']}", headers=admin, timeout=15)
    assert gone.status_code == 200 and gone.json()["status"] == "deleted"
    assert _get(admin, f"/forensics/export-jobs/{job['id']}").status_code == 404
    assert _get(admin, "/forensics/export-jobs/not-a-uuid").status_code == 400


# Live, with the API's database: audit rows and the export permission.

def _run_pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(PG)
        out = []
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    res = await conn.execute(text(sql), params)
                    out.append(res.fetchall() if res.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(run())


live_pg = pytest.mark.skipif(not (_api_up() and PG), reason="set NETFLOW_B3_API and NETFLOW_B3_PG")
VIEWER, VIEWER_PASS = "b3_forensics_viewer", "B3-Viewer-Passw0rd!"


@pytest.fixture(scope="module")
def viewer():
    from app.core.security import hash_password

    _run_pg([("DELETE FROM users WHERE username = :u", {"u": VIEWER})])
    _run_pg([("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
              "(:u, :m, :h, 'viewer', '[]'::jsonb)", {"u": VIEWER, "m": f"{VIEWER}@example.test",
                                                       "h": hash_password(VIEWER_PASS)})])
    yield _login(VIEWER, VIEWER_PASS)
    _run_pg([("DELETE FROM users WHERE username = :u", {"u": VIEWER})])


@live_pg
def test_live_export_needs_netflow_export_and_is_audited(admin, viewer):
    import requests

    assert _get(viewer, "/forensics/search", hours=1, page_size=1).status_code == 200
    assert _get(viewer, "/forensics/export", hours=1).status_code == 403
    r = requests.post(f"{API}/api/v1/netflow/forensics/export-jobs", params={"hours": 1}, headers=viewer, timeout=30)
    assert r.status_code == 403
    marker = f"b3-audit-{uuid.uuid4().hex[:8]}"
    # A filter value that identifies this export in the audit metadata.
    r = _get(admin, "/forensics/export", hours=1, protocol=253, columns="src")
    assert r.status_code == 200
    rows = _run_pg([("SELECT action, metadata::text FROM audit_logs WHERE action = 'netflow.export' "
                     "AND created_at > now() - interval '5 minutes' ORDER BY created_at DESC LIMIT 5", {})])[0]
    assert any('"protocol": "253"' in m for _a, m in rows), (marker, rows)
