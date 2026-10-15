"""Arriving at 'up' is never a problem alert (ZP-07).

Every newly added device used to get a permanent Critical "Device Down: <host>
(<ip>) is UP": devices start 'unknown', the first good ping posts unknown->up,
only down/degraded->up counted as a recovery, and the seeded "Device Down" rule
(trigger_on='any', ping_status == 0) matched because ping_status was never
evaluated. The same rule paged "is DEGRADED" on up->degraded. Nothing closed
either row: the resolvers only closed rows that recorded down/degraded.

These drive evaluate_status_change and the hold sweeper against an in-memory
alerts table.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.v1 import alert_engine
from app.api.v1.alert_engine import StatusChangeEvent, _conditions_match, evaluate_status_change
from app.services import alert_hold_service

DEVICE = "11111111-1111-1111-1111-111111111111"
OPEN = ("pending", "active", "acknowledged")


def device_down_rule(**overrides):
    """The seeded "Device Down" rule as it sits on a live appliance."""
    data = dict(
        id=uuid4(), name="Device Down", trigger_on="any", recovery_alert=True,
        severity="critical", device_id=None, group_id=None, device_type=None,
        location=None, scope_tag=None, notify_channels=[], cooldown=0,
        min_duration=0, metric="ping_status", operator="eq", threshold=0,
        conditions=None, condition_logic="AND",
        email_subject=None, email_body=None, sms_template=None,
        recovery_email_subject=None, recovery_email_body=None,
        recovery_sms_template=None, schedule_start=None, schedule_end=None,
        schedule_days=None,
    )
    data.update(overrides)
    return NS(**data)


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class AlertsDB:
    """Just enough of Postgres for the status engine: an alerts table whose
    UPDATEs honour the WHERE clauses the engine actually sends.

    ``statements`` logs every (sql, params) sent; ``monitoring`` is the stored
    system_settings 'monitoring' document; ``channels`` maps channel id ->
    notification_channels row; ``map_hooks`` are manual_maps webhook rows.
    """

    def __init__(self, rules):
        self.rules = rules
        self.alerts: list[dict] = []
        self.statements: list[tuple[str, dict]] = []
        self.monitoring: dict | None = None
        self.channels: dict[str, NS] = {}
        self.map_hooks: list[NS] = []

    def seed(self, rule, *, status="active", new_status="down", minutes_ago=5, **meta):
        self.alerts.append({
            "id": uuid4(), "device_id": DEVICE, "service_check_id": None,
            "rule_id": rule.id if rule is not None else None, "status": status,
            "message": f"{rule.name if rule is not None else 'Notice'}: sw (10.0.0.1) is {new_status.upper()}",
            "triggered_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            "resolved_at": None,
            "metadata": {"old_status": "up", "new_status": new_status,
                         "is_recovery": False, **meta},
        })
        return self.alerts[-1]

    def _cooldown_hits(self, sql, params):
        """_within_cooldown: this rule's announced triggers on this object."""
        since = datetime.now(timezone.utc) - timedelta(seconds=params["cd"])
        col = "device_id" if "device_id = :oid" in sql else "service_check_id"
        hits = []
        for a in self.alerts:
            meta = a["metadata"]
            if str(a["rule_id"]) != params["rid"] or str(a[col]) != str(params["oid"]):
                continue
            if params.get("aid") and str(a["id"]) == params["aid"]:
                continue
            if str(meta.get("is_recovery", False)).lower() == "true":
                continue
            if str(meta.get("notified", True)).lower() != "true":
                continue
            if a["triggered_at"] > since:
                hits.append((1,))
        return hits

    def open_rows(self):
        return [a for a in self.alerts if a["status"] in OPEN]

    def _status_filter(self, sql, row):
        meta = row["metadata"]
        if row["status"] not in OPEN or str(row["device_id"]) != DEVICE:
            return False
        if "service_check_id IS NULL" in sql and row["service_check_id"] is not None:
            return False
        if "is_recovery', 'false') != 'true'" in sql and str(meta.get("is_recovery")).lower() == "true":
            return False
        if "metadata->>'new_status' IS NOT NULL" in sql and meta.get("new_status") is None:
            return False
        if "new_status', '') IN ('down', 'degraded')" in sql and meta.get("new_status") not in ("down", "degraded"):
            return False
        return True

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        self.statements.append((sql, dict(params)))
        if "FROM devices WHERE id" in sql:
            return _Result([NS(device_type="switch", group_id=None, location="", tags=[])])
        if "FROM alert_rules" in sql:
            return _Result(self.rules)
        if "FROM system_settings WHERE key = 'monitoring'" in sql:
            return _Result([(self.monitoring,)] if self.monitoring is not None else [])
        if sql.startswith("SELECT 1 FROM alerts WHERE rule_id = :rid"):
            return _Result(self._cooldown_hits(sql, params))
        if "FROM notification_channels WHERE id = :id" in sql:
            ch = self.channels.get(str(params["id"]))
            return _Result([ch] if ch else [])
        if "FROM manual_maps" in sql:
            return _Result(self.map_hooks)
        if sql.startswith("UPDATE alerts SET status = 'resolved'"):
            hits = [a for a in self.alerts if self._status_filter(sql, a)]
            if "rule_id = :rule_id" in sql:
                hits = [a for a in hits if str(a["rule_id"]) == params["rule_id"]]
            extra = json.loads(params.get("resolution_metadata") or params.get("m") or "{}")
            for a in hits:
                # CASE WHEN status = 'pending' THEN hold_cancelled (old status).
                was_pending = a["status"] == "pending"
                a["status"] = "resolved"
                a["resolved_at"] = params.get("resolved_at") or params.get("now")
                a["metadata"].update(extra)
                if was_pending and "jsonb_build_object('hold_cancelled', true)" in sql:
                    a["metadata"]["hold_cancelled"] = True
            return _Result([NS(id=a["id"], rule_id=a["rule_id"], triggered_at=a["triggered_at"],
                               notified=a["metadata"].get("notified"),
                               new_status=a["metadata"].get("new_status"),
                               hold_cancelled=a["metadata"].get("hold_cancelled")) for a in hits],
                           rowcount=len(hits))
        if sql.startswith("INSERT INTO alerts"):
            row = {"id": uuid4(), "device_id": params["device_id"], "service_check_id": None,
                   "rule_id": params["rule_id"], "status": params.get("status", "resolved"),
                   "message": params["message"], "triggered_at": params["triggered_at"],
                   "resolved_at": params.get("resolved_at"),
                   "metadata": json.loads(params["metadata"])}
            self.alerts.append(row)
            return _Result([(row["id"],)])
        if sql.startswith("UPDATE alerts SET metadata") and "WHERE id = :id" in sql:
            for a in self.alerts:
                if a["id"] == params["id"]:
                    a["metadata"].update(json.loads(params["m"]))
            return _Result()
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(alert_engine, "_device_in_maintenance", AsyncMock(return_value=False))
    monkeypatch.setattr(alert_engine, "_offline_sensor_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "_find_suppressing_dependency", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "get_configured_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", True)

    async def run(db, old, new):
        event = StatusChangeEvent(device_id=DEVICE, hostname="sw", ip_address="10.0.0.1",
                                  old_status=old, new_status=new)
        return await evaluate_status_change(event, db=db)
    return run


@pytest.mark.asyncio
async def test_first_ping_of_a_new_device_raises_nothing(engine):
    """The reported bug: unknown -> up opened "Device Down: sw is UP"."""
    rule = device_down_rule(min_duration=100)
    db = AlertsDB([rule])
    await engine(db, "unknown", "up")
    assert db.alerts == []


@pytest.mark.asyncio
async def test_unknown_to_up_resolves_a_stale_up_alert_without_a_recovery(engine):
    rule = device_down_rule()
    db = AlertsDB([rule])
    stale = db.seed(rule, new_status="up")
    result = await engine(db, "unknown", "up")
    assert stale["status"] == "resolved"
    assert result["resolved_alerts"] == 1
    # Not a recovery: no "recovered" row, no reset notice.
    assert db.open_rows() == []
    assert not any(a["metadata"].get("is_recovery") for a in db.alerts)


@pytest.mark.asyncio
async def test_maintenance_to_up_is_not_a_recovery(engine):
    rule = device_down_rule()
    db = AlertsDB([rule])
    await engine(db, "maintenance", "up")
    assert db.alerts == []


@pytest.mark.asyncio
async def test_up_to_degraded_does_not_fire_device_down(engine):
    db = AlertsDB([device_down_rule()])
    await engine(db, "up", "degraded")
    assert db.alerts == []


@pytest.mark.asyncio
async def test_up_to_down_fires_device_down(engine):
    db = AlertsDB([device_down_rule()])
    await engine(db, "up", "down")
    [row] = db.alerts
    assert row["status"] == "active"
    assert row["metadata"]["new_status"] == "down"
    assert row["message"] == "Device Down: sw (10.0.0.1) is DOWN"


@pytest.mark.asyncio
async def test_up_to_down_with_hold_parks_the_alert_pending(engine):
    db = AlertsDB([device_down_rule(min_duration=100)])
    await engine(db, "up", "down")
    [row] = db.alerts
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_down_to_up_resolves_with_a_recovery(engine):
    rule = device_down_rule()
    db = AlertsDB([rule])
    down = db.seed(rule, new_status="down", notified=True)
    result = await engine(db, "down", "up")
    assert down["status"] == "resolved"
    assert down["metadata"]["resolved_by_recovery"] is True
    assert result["resolved_alerts"] == 1
    [recovery] = [a for a in db.alerts if a is not down]
    assert recovery["status"] == "resolved"
    assert recovery["metadata"]["is_recovery"] is True
    assert "recovered" in recovery["message"]


@pytest.mark.asyncio
async def test_recovery_also_closes_stale_up_rows(engine):
    rule = device_down_rule()
    db = AlertsDB([rule])
    stale = db.seed(rule, new_status="up", minutes_ago=60 * 24 * 30)
    down = db.seed(rule, new_status="down", minutes_ago=4, notified=True)
    await engine(db, "down", "up")
    assert stale["status"] == down["status"] == "resolved"
    [recovery] = [a for a in db.alerts if a["metadata"].get("is_recovery")]
    assert recovery["metadata"]["is_recovery"] is True


@pytest.mark.asyncio
async def test_explicit_up_rule_still_fires_on_up(engine):
    rule = device_down_rule(name="Device came up", trigger_on="up", severity="info")
    db = AlertsDB([rule])
    await engine(db, "unknown", "up")
    [row] = db.alerts
    assert row["metadata"]["new_status"] == "up"


@pytest.mark.asyncio
async def test_down_rule_supersede_closes_any_open_status_row(engine):
    """A pre-fix "is UP" row of the same rule is superseded when it goes down."""
    rule = device_down_rule()
    db = AlertsDB([rule])
    stale = db.seed(rule, new_status="up")
    await engine(db, "up", "down")
    assert stale["status"] == "resolved"
    assert [a["metadata"]["new_status"] for a in db.open_rows()] == ["down"]


# ── ping_status is evaluated (only) on 'any' rules ───────────────────────────

def test_ping_status_condition_selects_the_state_on_any_rules():
    rule = device_down_rule()
    assert _conditions_match(rule, {"ping_status": alert_engine._ping_status_value("down")})
    assert not _conditions_match(rule, {"ping_status": alert_engine._ping_status_value("degraded")})
    assert not _conditions_match(rule, {"ping_status": alert_engine._ping_status_value("up")})
    assert not _conditions_match(rule, {"ping_status": alert_engine._ping_status_value("unknown")})


def test_explicit_triggers_own_the_state_whatever_ping_status_says():
    """The wizard stores ping_status == 0 on every status rule; a "Device
    degraded" or "Device comes up" rule must not be silenced by it."""
    for trigger, status in (("degraded", "degraded"), ("up", "up"), ("down", "down")):
        rule = device_down_rule(trigger_on=trigger)
        assert _conditions_match(rule, {"ping_status": alert_engine._ping_status_value(status)})
        multi = device_down_rule(trigger_on=trigger, conditions=[
            {"metric": "ping_status", "operator": "==", "threshold": 0},
            {"metric": "rtt", "operator": ">", "threshold": 100},
        ])
        assert _conditions_match(multi, {"ping_status": 1.0, "rtt": 250.0})
        assert not _conditions_match(multi, {"ping_status": 1.0, "rtt": 20.0})


# ── Hold sweeper ─────────────────────────────────────────────────────────────

class HoldDB:
    def __init__(self, row):
        self.row = row
        self.settled = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "alert_silences" in sql and "FROM alerts a WHERE a.id" in sql:
            return _Result([(False,)])  # condition not snoozed
        if "FROM alerts a" in sql:
            return _Result([self.row])
        if "SET status = :status" in sql:
            self.settled.append((params["status"], json.loads(params["m"])))
            return _Result(rowcount=1)
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _pending(trigger_on="any", new_status="up", device_status="up"):
    return NS(id=uuid4(), rule_id=uuid4(), device_id=DEVICE, service_check_id=None,
              triggered_at=datetime.now(timezone.utc) - timedelta(minutes=2),
              metadata={"new_status": new_status, "hold_until": "2000-01-01T00:00:00+00:00"},
              alert_severity="critical", message="", rule_enabled=True,
              rule_name="Device Down", rule_severity="critical", cooldown=0,
              min_duration=100, notify_channels=[], metric="ping_status",
              operator="eq", threshold=0, trigger_on=trigger_on, conditions=None,
              condition_logic="AND", email_subject=None, email_body=None,
              sms_template=None, schedule_start=None, schedule_end=None,
              schedule_days=None, device_status=device_status,
              device_hostname="sw", device_ip="10.0.0.1", check_status=None,
              check_name=None, check_type=None, check_target=None)


@pytest.fixture
def sweeper(monkeypatch):
    monkeypatch.setattr(alert_hold_service, "get_configured_timezone", AsyncMock(return_value="UTC"))
    dispatch = AsyncMock(return_value=1)
    monkeypatch.setattr(alert_hold_service, "_dispatch_confirmed", dispatch)
    return dispatch


@pytest.mark.asyncio
async def test_hold_sweeper_never_confirms_a_stray_up_alert(sweeper):
    """The device being up is exactly what an "is UP" row reports, so the
    current == recorded check used to promote it to a Critical."""
    db = HoldDB(_pending())
    out = await alert_hold_service.sweep_pending_holds(db)
    assert out["cancelled"] == 1 and out["confirmed"] == 0
    assert db.settled[0][0] == "resolved"
    sweeper.assert_not_awaited()


@pytest.mark.asyncio
async def test_hold_sweeper_still_confirms_an_explicit_up_rule(sweeper):
    db = HoldDB(_pending(trigger_on="up"))
    out = await alert_hold_service.sweep_pending_holds(db)
    assert out["confirmed"] == 1
    assert db.settled[0][0] == "active"


@pytest.mark.asyncio
async def test_hold_sweeper_confirms_a_held_down(sweeper):
    db = HoldDB(_pending(new_status="down", device_status="down"))
    out = await alert_hold_service.sweep_pending_holds(db)
    assert out["confirmed"] == 1
    sweeper.assert_awaited_once()
