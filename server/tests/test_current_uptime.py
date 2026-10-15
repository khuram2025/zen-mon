"""GET /devices/current-uptime: the continuous-uptime origin of responding devices.

The origin is the last transition INTO up or degraded from a status in which the device did not answer (down, unknown,
maintenance), so an up<->degraded change never resets the Uptime column. Devices with no non-responding status in the
log fall back to created_at; a log contradicting the current status, or an unreadable log, gives no value (not
measured). Postgres is a disposable cluster; ClickHouse is a fake that aggregates canned transitions with the same
predicates the query states.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
CREATED = NOW - timedelta(days=7)
RESPONDING = ("up", "degraded")


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# name -> (status, tags, transitions [(minutes ago, old, new)])
DEVICES = {
    "recovered": ("up", ["team-u"], [(180, "up", "down"), (120, "down", "up"), (90, "up", "degraded"),
                                     (60, "degraded", "up")]),
    "slow": ("degraded", ["team-u"], [(300, "down", "up"), (10, "up", "degraded")]),
    "after-maint": ("up", ["team-u"], [(50, "up", "maintenance"), (20, "maintenance", "up")]),
    "first-result": ("up", ["team-u"], [(240, "unknown", "up")]),
    "fresh": ("up", ["team-u"], []),
    "steady": ("up", ["team-u"], [(2880, "up", "degraded"), (1440, "degraded", "up")]),
    "lost-write": ("up", ["team-u"], [(10, "up", "down")]),
    "is-down": ("down", ["team-u"], [(60, "up", "down")]),
    "other-team": ("up", ["team-z"], [(30, "down", "up")]),
}
ID = {name: uuid.uuid4() for name in DEVICES}


@pytest.fixture(scope="module")
def world(pg_db_module):
    for n, (name, (status, tags, _)) in enumerate(DEVICES.items(), 1):
        pg_db_module.execute(
            "INSERT INTO devices (id, hostname, ip_address, tags, status, created_at)"
            " VALUES ($1, $2, $3::inet, $4::jsonb, $5, $6)",
            ID[name], f"uptime-{name}", f"10.8.0.{n}", tags, status, CREATED)
    return pg_db_module


class FakeCH:
    """Aggregates the canned transitions per device the way the query's maxIf/countIf do."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sql: list[str] = []

    def query(self, sql, parameters=None, **kw):
        self.sql.append(sql)
        if self.fail:
            raise ConnectionError("clickhouse unavailable")
        rows = []
        for name, (_, _, transitions) in DEVICES.items():
            if not transitions:
                continue
            back = [ago(minutes=m) for m, old, new in transitions if new in RESPONDING and old not in RESPONDING]
            lost = [ago(minutes=m) for m, old, new in transitions if new not in RESPONDING]
            epoch = datetime(1970, 1, 1)        # maxIf over no row: the type's default (naive, like ClickHouse)
            rows.append((ID[name], max(back).replace(tzinfo=None) if back else epoch, len(back),
                         max(lost).replace(tzinfo=None) if lost else epoch, len(lost)))
        return SimpleNamespace(result_rows=rows)


@pytest.fixture
def fake_ch(monkeypatch):
    ch = FakeCH()
    monkeypatch.setattr("app.core.database.get_clickhouse_client", lambda: ch)
    return ch


def uptimes(db, scope=()) -> dict[str, float]:
    with api_client(db, role="t_uptime", permissions=["devices.view"], scope_tags=tuple(scope)) as c:
        resp = c.get("/api/v1/devices/current-uptime")
    assert resp.status_code == 200, resp.text[:300]
    return resp.json()["devices"]


def close(seconds: float, origin: datetime) -> bool:
    expected = (datetime.now(timezone.utc) - origin).total_seconds()
    return abs(seconds - expected) < 60


def test_query_states_the_origin_rule(world, fake_ch):
    uptimes(world)
    sql = fake_ch.sql[0]
    assert "FROM zenplus.device_status_log" in sql
    assert "new_status IN ('up', 'degraded') AND old_status NOT IN ('up', 'degraded')" in sql
    assert "new_status NOT IN ('up', 'degraded')" in sql


def test_degraded_and_up_changes_do_not_reset_uptime(world, fake_ch):
    got = uptimes(world, scope=("team-u",))
    # Back from down two hours ago; the later up->degraded->up changes do not reset it.
    assert close(got[str(ID["recovered"])], ago(minutes=120))
    # A degraded device still answers: its uptime runs from its last recovery.
    assert close(got[str(ID["slow"])], ago(minutes=300))
    # Leaving maintenance starts a new origin, as does a new device's first result.
    assert close(got[str(ID["after-maint"])], ago(minutes=20))
    assert close(got[str(ID["first-result"])], ago(minutes=240))


def test_devices_without_a_non_responding_status_count_from_creation(world, fake_ch):
    got = uptimes(world, scope=("team-u",))
    assert close(got[str(ID["fresh"])], CREATED)
    assert close(got[str(ID["steady"])], CREATED)


def test_no_value_when_not_responding_or_when_the_log_contradicts_the_status(world, fake_ch):
    got = uptimes(world, scope=("team-u",))
    assert str(ID["is-down"]) not in got
    # The log ends in down although the device is up (a lost log write): not a guess.
    assert str(ID["lost-write"]) not in got


def test_scope_limits_the_devices(world, fake_ch):
    assert str(ID["other-team"]) not in uptimes(world, scope=("team-u",))
    assert close(uptimes(world)[str(ID["other-team"])], ago(minutes=30))


def test_unreadable_log_is_not_measured(world, monkeypatch):
    monkeypatch.setattr("app.core.database.get_clickhouse_client", lambda: FakeCH(fail=True))
    assert uptimes(world) == {}
