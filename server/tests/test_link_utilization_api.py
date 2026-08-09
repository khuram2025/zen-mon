"""Link Utilization API tests — health metrics (errors/discards/flaps).

Unit tests cover the counter-delta and severity logic; live smoke tests run
against a running API + ClickHouse (skipped when down), mirroring
test_server_monitoring_api.py conventions.

Run against a specific instance with e.g.
  ZENPLUS_API=http://localhost:8001 pytest tests/test_link_utilization_api.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.link_utilization import (  # noqa: E402
    _COUNTER_COLUMNS,
    _health_cheap_columns,
    _health_payload,
    _health_precise_sql,
    _link_health,
    _pairs_sql,
    _ppm,
    _util_pct,
)

API = os.environ.get("ZENPLUS_API", "http://localhost:8000")
ADMIN_USER = os.environ.get("ZENPLUS_ADMIN", "admin")
ADMIN_PASS = os.environ.get("ZENPLUS_ADMIN_PASS", "admin123")


# ── Unit: rate maths ─────────────────────────────────────────────────────────

def test_ppm_returns_none_without_packet_counters():
    # No denominator means the count can't be judged — must not read as 0 ppm.
    assert _ppm(500, 0) is None
    assert _ppm(0, 0) is None


def test_ppm_scales_to_per_million():
    assert _ppm(1, 1_000_000) == 1.0
    assert _ppm(1_000, 1_000_000) == 1000.0


def test_util_pct_uses_the_busier_direction():
    assert _util_pct(50, 100, 1000) == 10.0
    assert _util_pct(100, 50, 1000) == 10.0
    assert _util_pct(1, 1, 0) is None


# ── Unit: severity classification ────────────────────────────────────────────

def test_clean_link_reports_no_issues():
    severity, issues = _link_health(0, 0, None, None, 0)
    assert severity == "ok"
    assert issues == []


def test_error_severity_follows_the_rate_not_the_count():
    # 517 errors against 256M frames (2 ppm) is a healthy link...
    severity, issues = _link_health(517, 0, 2.0, 0.0, 0)
    assert severity == "ok" and issues == []
    # ...the same count against a small denominator is not.
    severity, issues = _link_health(517, 0, 20_000.0, 0.0, 0)
    assert severity == "critical" and "errors" in issues


def test_error_warning_band():
    severity, issues = _link_health(10, 0, 150.0, 0.0, 0)
    assert severity == "warning" and issues == ["errors"]


def test_discards_tolerate_more_than_errors():
    # 2000 ppm: warning for discards (congestion is normal), critical for errors.
    assert _link_health(0, 10, None, 2000.0, 0)[0] == "warning"
    assert _link_health(10, 0, 2000.0, None, 0)[0] == "critical"


def test_absolute_floor_used_when_no_packet_counters():
    # Below the floor, an unmeasurable rate must not raise an alarm.
    assert _link_health(50, 0, None, None, 0) == ("ok", [])
    severity, issues = _link_health(5_000, 0, None, None, 0)
    assert severity == "warning" and issues == ["errors"]


def test_flapping_escalates_with_transition_count():
    assert _link_health(0, 0, None, None, 1) == ("warning", ["flapping"])
    severity, issues = _link_health(0, 0, None, None, 12)
    assert severity == "critical" and issues == ["flapping"]


def test_worst_issue_wins_and_all_are_listed():
    # Each rate is above its own threshold, so all three are reported and the
    # most severe one sets the overall grade.
    severity, issues = _link_health(10, 10, 20_000.0, 2_000.0, 2)
    assert severity == "critical"
    assert set(issues) == {"errors", "discards", "flapping"}


def test_a_sub_threshold_rate_is_not_listed_as_an_issue():
    # 100 ppm of discards is an order of magnitude under the discard warning
    # band, so only the error issue should appear.
    _, issues = _link_health(10, 10, 20_000.0, 100.0, 0)
    assert issues == ["errors"]


# ── Unit: payload assembly ───────────────────────────────────────────────────

def test_health_payload_totals_and_rates():
    p = _health_payload(
        {
            "d_ie": 30, "d_oe": 10, "d_id": 5, "d_od": 5,
            "d_ip": 999_950, "d_op": 0, "flaps": 0, "availability": 100,
        },
        window_seconds=100,
    )
    assert p["errors"] == 40
    assert p["discards"] == 10
    # Errored/discarded frames never reached the ucast counter, so the
    # denominator adds them back: 999950 + 40 + 10 = 1,000,000.
    assert p["error_ppm"] == 40.0
    assert p["discard_ppm"] == 10.0
    assert p["in_pps"] == pytest.approx(9999.5)
    assert p["availability_pct"] == 100.0


def test_health_payload_survives_a_zero_length_window():
    p = _health_payload({"d_ip": 10, "d_op": 10, "flaps": 0, "availability": 0}, 0)
    assert p["in_pps"] >= 0  # no ZeroDivisionError


# ── Unit: generated SQL ──────────────────────────────────────────────────────

def test_cheap_columns_alias_every_counter_and_flag_resets():
    sql = _health_cheap_columns()
    for _, alias in _COUNTER_COLUMNS:
        assert f"AS d_{alias}" in sql
    assert "has_reset" in sql and "maybe_flap" in sql
    # Reset detection compares the first sample against the smallest one.
    assert "argMin(in_errors, timestamp) != min(in_errors)" in sql


def test_precise_sql_gates_every_delta_on_having_a_predecessor():
    sql = _health_precise_sql("timestamp > now()", "(toUUID('x'),1)")
    for col, alias in _COUNTER_COLUMNS:
        assert f"greatest(toInt64({col}) - toInt64(p_{alias}), 0)" in sql
    # Without the rn > 1 gate the first sample's full counter reads as a delta.
    assert sql.count("if(rn > 1") == len(_COUNTER_COLUMNS)
    assert "countIf(rn > 1 AND oper_status != p_os)" in sql


def test_pairs_sql_rejects_a_non_uuid_device_id():
    # Values are re-typed through UUID(), so a malformed id raises instead of
    # reaching ClickHouse as SQL text.
    with pytest.raises(ValueError):
        _pairs_sql([("not-a-uuid'); DROP TABLE x --", 1)])


def test_pairs_sql_emits_typed_tuples():
    out = _pairs_sql([("af1a7e96-fd65-4f30-8c2b-c3a9266827be", 144)])
    assert out == "(toUUID('af1a7e96-fd65-4f30-8c2b-c3a9266827be'),144)"


# ── Live smoke tests ─────────────────────────────────────────────────────────

def _api_up() -> bool:
    try:
        return requests.get(f"{API}/api/v1/system/health", timeout=2).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="ZenPlus API not running")


@pytest.fixture(scope="module")
def auth() -> dict[str, str]:
    r = requests.post(
        f"{API}/api/v1/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=5,
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def fleet(auth) -> dict:
    r = requests.get(f"{API}/api/v1/link-utilization?hours=24&limit=200", headers=auth, timeout=180)
    r.raise_for_status()
    return r.json()


@live
def test_every_link_carries_a_health_block(fleet):
    assert fleet["items"], "no interfaces returned"
    for item in fleet["items"]:
        for key in ("errors", "discards", "flaps", "availability_pct", "health", "issues"):
            assert key in item, f"missing {key}"
        assert item["health"] in ("ok", "warning", "critical")
        assert item["errors"] == item["in_errors"] + item["out_errors"]
        assert item["discards"] == item["in_discards"] + item["out_discards"]


@live
def test_counters_are_deltas_not_raw_readings(fleet):
    # A cumulative reading on a busy switch runs to billions; a 24h increase
    # does not. This is the regression that made the old page unusable.
    for item in fleet["items"]:
        assert item["errors"] < 10**9, f"{item['hostname']} if{item['if_index']} looks cumulative"


@live
def test_issue_list_agrees_with_severity(fleet):
    for item in fleet["items"]:
        if item["issues"]:
            assert item["health"] in ("warning", "critical")
        else:
            assert item["health"] == "ok"


@live
def test_no_link_reads_as_up_but_never_available(fleet):
    """SPAN/mirror ports report ifOperStatus=down while forwarding.

    The page presents those as Up, so anything derived from ifOperStatus must
    be suppressed rather than printed as a contradictory "Up · 0% available".
    """
    for item in fleet["items"]:
        if item["oper_status"] == "up" and item.get("availability_pct") == 0:
            pytest.fail(
                f"{item['hostname']} if{item['if_index']} shows Up with 0% availability"
            )
        if item.get("oper_status_reliable") is False:
            assert item["availability_pct"] is None
            assert item["flaps"] == 0
            assert "flapping" not in item["issues"]


@live
def test_summary_counts_match_the_items(fleet):
    s = fleet["summary"]
    # `total` counts the filtered fleet while `items` is capped, so only compare
    # when the whole fleet came back.
    if s["returned"] >= s["total"]:
        assert s["with_errors"] == sum(1 for i in fleet["items"] if "errors" in i["issues"])
        assert s["flapping"] == sum(1 for i in fleet["items"] if "flapping" in i["issues"])
    for key in ("with_errors", "with_discards", "flapping", "unhealthy", "critical_health"):
        assert s[key] >= 0


@live
@pytest.mark.parametrize("issue", ["any", "errors", "discards", "flapping"])
def test_issue_filter_returns_only_matching_links(auth, issue):
    r = requests.get(
        f"{API}/api/v1/link-utilization?hours=24&limit=200&issue={issue}", headers=auth, timeout=180,
    )
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["issues"], "issue filter returned a healthy link"
        if issue != "any":
            assert issue in item["issues"]


@live
@pytest.mark.parametrize("sort,key", [
    ("errors", "errors"), ("discards", "discards"), ("flaps", "flaps"),
])
def test_health_sorts_are_descending(auth, sort, key):
    r = requests.get(
        f"{API}/api/v1/link-utilization?hours=24&limit=50&sort={sort}", headers=auth, timeout=180,
    )
    items = r.json()["items"]
    # Favourites are pinned above everything regardless of sort, so the ordering
    # holds within each block rather than across the whole page.
    for block in (
        [i[key] for i in items if i.get("is_favorite")],
        [i[key] for i in items if not i.get("is_favorite")],
    ):
        assert block == sorted(block, reverse=True)


@live
def test_error_rate_sort_ranks_measured_links_above_unmeasurable_ones(auth):
    r = requests.get(
        f"{API}/api/v1/link-utilization?hours=24&limit=50&sort=error_rate", headers=auth, timeout=180,
    )
    # Pinned favourites sit above the sort, so judge the unpinned tail.
    items = [i for i in r.json()["items"] if not i.get("is_favorite")]
    seen_null = False
    for item in items:
        if item["error_ppm"] is None:
            seen_null = True
        else:
            assert not seen_null, "a measured error rate sorted below an unmeasurable one"


@live
def test_detail_health_matches_the_fleet_row(auth, fleet):
    # Pick a link with real events so the comparison is meaningful.
    target = next((i for i in fleet["items"] if i["errors"] > 0 or i["discards"] > 0), None)
    if target is None:
        pytest.skip("no interface with errors or discards in this window")
    r = requests.get(
        f"{API}/api/v1/link-utilization/{target['device_id']}/{target['if_index']}?hours=24",
        headers=auth, timeout=120,
    )
    assert r.status_code == 200
    detail = r.json()
    # The window is relative to now and the poller writes every 30s, so the two
    # requests see slightly different sample sets — compare within tolerance,
    # not exactly. What matters is that they don't differ by orders of magnitude.
    assert detail["health"]["errors"] == pytest.approx(target["errors"], rel=0.05, abs=50)
    assert detail["health"]["discards"] == pytest.approx(target["discards"], rel=0.05, abs=50)
    # The legacy summary keys are computed from the same block, so these are exact.
    assert detail["summary"]["total_errors"] == detail["health"]["errors"]
    assert detail["summary"]["total_discards"] == detail["health"]["discards"]


@live
def test_detail_error_series_is_per_bucket_not_cumulative(auth, fleet):
    target = next((i for i in fleet["items"] if i["errors"] > 0), None)
    if target is None:
        pytest.skip("no interface with errors in this window")
    r = requests.get(
        f"{API}/api/v1/link-utilization/{target['device_id']}/{target['if_index']}?hours=24",
        headers=auth, timeout=120,
    )
    series = r.json()["errors"]
    assert series, "expected an error series"
    # Summed buckets should reconstruct the window total, and no single bucket
    # may exceed it — both fail if the series carries raw counter readings.
    total = sum(p["in_errors"] + p["out_errors"] for p in series)
    assert total <= r.json()["health"]["errors"] + 1
    assert all(p["in_errors"] >= 0 and p["out_errors"] >= 0 for p in series)


@live
def test_device_interface_metrics_agree_with_link_utilization(auth, fleet):
    """The two pages read the same counters and must report the same totals."""
    target = next((i for i in fleet["items"] if i["errors"] > 0), None)
    if target is None:
        pytest.skip("no interface with errors in this window")
    dev = requests.get(
        f"{API}/api/v1/devices/{target['device_id']}/interfaces/{target['if_index']}/metrics?hours=24",
        headers=auth, timeout=120,
    )
    assert dev.status_code == 200
    devices_total = dev.json()["summary"]["total_errors"]
    # Both endpoints difference the same cumulative counters over the same
    # window; allow a sample of drift between the two round-trips.
    assert devices_total == pytest.approx(target["errors"], rel=0.05, abs=50)


# ── Favourites ───────────────────────────────────────────────────────────────
#
# These mutate the admin user's favourites, so each one restores the starting
# state — a leftover star would silently reorder every other sort test.


@pytest.fixture
def unstarred_link(auth, fleet):
    """A link that is not currently a favourite, cleaned up after the test.

    Taken from the BOTTOM of the default utilization ranking, so "it ended up
    first" can only be the pin at work, never where it already sat.
    """
    target = next((i for i in reversed(fleet["items"]) if not i.get("is_favorite")), None)
    if target is None:
        pytest.skip("no unstarred interface available")
    yield target
    requests.delete(
        f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/{target['if_index']}",
        headers=auth, timeout=30,
    )


@live
def test_every_link_reports_its_favorite_state(fleet):
    # A missing flag would render every star as "off" and silently lose the pin.
    for item in fleet["items"]:
        assert isinstance(item["is_favorite"], bool)
    assert fleet["summary"]["favorites"] == sum(1 for i in fleet["items"] if i["is_favorite"])


@live
def test_starred_link_is_pinned_to_the_top(auth, unstarred_link):
    """A favourite outranks the sort — that is the whole point of the feature."""
    target = unstarred_link
    r = requests.put(
        f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/{target['if_index']}",
        headers=auth, timeout=30,
    )
    assert r.status_code == 204

    items = requests.get(
        f"{API}/api/v1/link-utilization?hours=24&limit=200", headers=auth, timeout=180,
    ).json()["items"]
    assert items[0]["device_id"] == target["device_id"]
    assert items[0]["if_index"] == target["if_index"]
    assert items[0]["is_favorite"] is True
    # The pinned block must be contiguous — no favourite interleaved below it.
    first_unstarred = next((n for n, i in enumerate(items) if not i["is_favorite"]), len(items))
    assert all(i["is_favorite"] for i in items[:first_unstarred])
    assert not any(i["is_favorite"] for i in items[first_unstarred:])


@live
def test_starring_is_idempotent(auth, unstarred_link):
    target = unstarred_link
    path = f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/{target['if_index']}"
    assert requests.put(path, headers=auth, timeout=30).status_code == 204
    assert requests.put(path, headers=auth, timeout=30).status_code == 204

    favorites = requests.get(
        f"{API}/api/v1/link-utilization/favorites", headers=auth, timeout=30,
    ).json()["items"]
    matches = [
        f for f in favorites
        if f["device_id"] == target["device_id"] and f["if_index"] == target["if_index"]
    ]
    assert len(matches) == 1


@live
def test_favorites_only_filter_returns_just_the_starred_links(auth, unstarred_link):
    target = unstarred_link
    requests.put(
        f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/{target['if_index']}",
        headers=auth, timeout=30,
    )
    body = requests.get(
        f"{API}/api/v1/link-utilization?hours=24&limit=200&favorites_only=true",
        headers=auth, timeout=180,
    ).json()
    assert body["items"], "favorites filter dropped a link that was just starred"
    assert all(i["is_favorite"] for i in body["items"])
    assert any(
        i["device_id"] == target["device_id"] and i["if_index"] == target["if_index"]
        for i in body["items"]
    )


@live
def test_unstarring_is_safe_when_not_starred(auth, fleet):
    """The UI toggle must not wedge on a favourite another tab already cleared."""
    target = fleet["items"][0]
    path = f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/{target['if_index']}"
    requests.delete(path, headers=auth, timeout=30)
    assert requests.delete(path, headers=auth, timeout=30).status_code == 204


@live
def test_cannot_star_an_unknown_interface(auth, fleet):
    target = fleet["items"][0]
    r = requests.put(
        f"{API}/api/v1/link-utilization/favorites/{target['device_id']}/999999",
        headers=auth, timeout=30,
    )
    assert r.status_code == 404
