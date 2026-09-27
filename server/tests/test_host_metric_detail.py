from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.host_metric_detail import (
    bucket_seconds,
    days_until_full,
    query_server_metric_detail,
)


def test_buckets_give_a_few_hundred_points_in_whole_minutes():
    assert bucket_seconds(3600) == 60          # 1 hour: every sample
    assert bucket_seconds(86400) == 360        # 24 hours: 240 points
    assert bucket_seconds(30 * 86400) % 60 == 0
    assert 30 * 86400 / bucket_seconds(30 * 86400) <= 240


def test_days_until_full():
    assert days_until_full(100, 50, 5) == 10
    assert days_until_full(100, 50, 0) is None      # not growing
    assert days_until_full(100, 50, -3) is None     # shrinking
    assert days_until_full(100, 120, 5) == 0        # already over
    assert days_until_full(0, 0, 5) is None


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    """Answers each query by the table it reads, and records parameters."""

    def __init__(self):
        self.params = []
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.rows = {
            "per_core": [(now, [10.0, 30.0]), (now + timedelta(minutes=1), [20.0, 40.0])],
            "host_cpu_metrics": [(now, 25.0, 15.0, 10.0, 1.0, 60.0)],
            "host_memory_metrics": [(now, 8e9, 4e9, 4e9, 1e9, 5e9, 2e9, 1e8, 50.0)],
            "host_disk_io_metrics": [("C:", now, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 0.5)],
            "simpleLinearRegression": [("C:", 1000.0 / 86400, 60e9, 100e9, 50, 3 * 86400)],
            "host_filesystem_metrics": [("C:", "NTFS", now, 60e9, 100e9, 60.0)],
            "host_network_metrics": [("Ethernet0", now, 100.0, 200.0, 1.0, 2.0, 0.0, 0.0, 1)],
        }

    def query(self, sql, parameters=None):
        self.params.append(parameters)
        for key in ("per_core", "simpleLinearRegression", "host_cpu_metrics", "host_memory_metrics",
                    "host_disk_io_metrics", "host_filesystem_metrics", "host_network_metrics"):
            if key in sql:
                return _Result(self.rows[key])
        raise AssertionError(sql)


def test_detail_shapes_every_breakdown():
    client = _FakeClient()
    to = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
    out = query_server_metric_detail(client, "sid", to - timedelta(hours=1), to)
    assert out["bucket_s"] == 60
    assert out["cpu"][0]["user"] == 15.0 and out["cpu"][0]["queue"] == 1.0
    assert out["cores"]["latest"] == [20.0, 40.0] and len(out["cores"]["series"]) == 2
    assert out["memory"][0]["committed"] == 5e9
    assert out["disks"][0]["device"] == "C:" and out["disks"][0]["points"][0]["read_ms"] == 6.0
    fs = out["filesystems"][0]
    assert fs["mount"] == "C:" and fs["growth_bytes_per_day"] == 1000.0
    assert fs["days_until_full"] == round((100e9 - 60e9) / 1000.0, 3)
    assert out["interfaces"][0]["name"] == "Ethernet0" and out["interfaces"][0]["points"][0]["up"] is True
    assert all(p["sid"] == "sid" for p in client.params)


def test_detail_clamps_to_retention():
    client = _FakeClient()
    to = datetime(2026, 9, 27, tzinfo=timezone.utc)
    out = query_server_metric_detail(client, "sid", to - timedelta(days=90), to)
    assert out["from"] == (to - timedelta(days=30)).isoformat()


def test_no_forecast_from_less_than_a_day_of_history():
    client = _FakeClient()
    client.rows["simpleLinearRegression"] = [("C:", 1e6 / 86400, 60e9, 100e9, 50, 4 * 3600)]
    to = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
    fs = query_server_metric_detail(client, "sid", to - timedelta(hours=1), to)["filesystems"][0]
    assert fs["growth_bytes_per_day"] is None and fs["days_until_full"] is None
