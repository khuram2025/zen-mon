"""Device reboot alerts (sysUpTime went back): an event with a hold.

The field complaint: "Device reboot detected: sw (ip) reported an uptime reset
(possible reboot) (currently 1)" and a resolve "(now 0). Active for 5 minutes
34 seconds" that said nothing about the switch. The rule is stored as
``uptime_reset == 1``, a 0/1 flag that is 1 for exactly one poll, so its
operator, reset level and breach hold never meant anything.

Now a reboot raises one incident per rule + device saying when it rebooted
and the uptime now; a further reboot updates the same incident ("rebooted
again") and restarts the hold; the incident closes itself after the hold
(``clear_duration``, NULL = 15 min) with how long the device has stayed up —
or that there is no current uptime reading; a reboot already announced is
never paged twice, even after a manual resolve.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import network_alert_service as worker
from app.services import network_history

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0
DAY = 86400.0


def test_reboot_events_find_drops_and_ignore_the_timeticks_wrap():
    up = [(T0, 40 * DAY), (T0 + 60, 40 * DAY + 60), (T0 + 120, 30), (T0 + 180, 90)]
    [ev] = worker.reboot_events(up)
    assert ev["detected_at"] == T0 + 120 and ev["rebooted_at"] == T0 + 90
    assert ev["uptime_s"] == 30 and ev["previous_uptime_s"] == 40 * DAY + 60
    # A small dip (counter jitter) is not a reboot.
    assert worker.reboot_events([(T0, 1000), (T0 + 60, 990)]) == []
    # sysUpTime is 32-bit TimeTicks: it wraps after ~497 days without a reboot.
    wrap = 2 ** 32 / 100.0
    assert worker.reboot_events([(T0, wrap - 30), (T0 + 60, 30)]) == []
    assert worker.event_hold_seconds(None) == worker.EVENT_HOLD_DEFAULT_S == 900
    assert worker.event_hold_seconds(300) == 300 and worker.event_hold_seconds(0) == 0


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def reboot(pg_db, monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    device = str(pg_db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, snmp_enabled, snmp_poll_interval, device_type)"
        " VALUES ('New_ACC_AdminB_SW09', '192.168.24.34', 'up', true, 60, 'switch') RETURNING id"))
    points: list = []
    sent: list[dict] = []

    def fetch_history(metrics, since, until):
        assert "uptime_reset" in metrics
        return {device: {"uptime": [(t, v) for t, v in points if since <= t <= until]}}, {}

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(network_history, "fetch_history", fetch_history)
    monkeypatch.setattr(worker, "dispatch_to_channels", dispatch)
    # The seeded rule (migrate-037): uptime_reset eq 1, no hold, recovery on.
    rule = str(pg_db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " recovery_alert, notify_channels, cooldown) VALUES ('Device reboot detected', 'uptime_reset',"
        " 'eq', 1, 'warning', 0, true, $1::jsonb, 0) RETURNING id",
        ["00000000-0000-0000-0000-00000000c0de"]))
    return NS(db=pg_db, device=device, points=points, sent=sent, rule=rule)


async def replay(n, uptimes, *, start, step=60):
    """One uptime sample per step (None = no sample), evaluated 5 s later."""
    async with session(n.db) as s:
        for i, value in enumerate(uptimes):
            t = start + i * step
            if value is not None:
                n.points.append((t, value))
            await worker.evaluate_network_rules(s, now=t + 5)


def alerts(n):
    return n.db.query("SELECT id, status, message, metadata FROM alerts WHERE device_id = $1"
                      " ORDER BY triggered_at, id", n.device)


def climbing(start_uptime, count, step=60):
    return [start_uptime + i * step for i in range(count)]


@pytest.mark.asyncio
async def test_a_reboot_pages_once_with_when_and_uptime_then_clears_after_the_hold(reboot):
    n = reboot
    await replay(n, climbing(41 * DAY, 3), start=T0)
    assert alerts(n) == [] and n.sent == []
    # Rebooted: uptime 120 s at T0+180.
    await replay(n, climbing(120, 5), start=T0 + 180)
    [(alert_id, status, message, meta)] = alerts(n)
    assert status == "active" and meta["reboot_count"] == 1
    assert message.startswith("Switch New_ACC_AdminB_SW09 (192.168.24.34) rebooted at ")
    assert "uptime is now 2 min (was 41 days)" in message
    assert len(n.sent) == 1
    sms = n.sent[0]["message"]
    for raw in ("currently 1", "(now 0)", "uptime reset", "possible reboot"):
        assert raw not in sms
    assert sms.startswith("ZenPlus WARNING — Device reboot detected: Switch New_ACC_AdminB_SW09"
                          " (192.168.24.34) rebooted at ")
    assert n.sent[0]["headline_metric"] is None          # no "1" as a big number
    # Still open just inside the 15-minute hold from the reboot's detection.
    await replay(n, climbing(420, 10), start=T0 + 480)
    assert alerts(n)[0][1] == "active" and len(n.sent) == 1
    await replay(n, climbing(1020, 3), start=T0 + 1080)
    [(_, status, _, _)] = alerts(n)
    assert status == "resolved"
    assert [c["is_recovery"] for c in n.sent] == [False, True]
    clear = n.sent[1]
    assert "has stayed up for" in clear["body"] and "no further reboots" in clear["body"]
    assert "Active for" not in clear["message"] and "(now 0)" not in clear["message"]
    assert clear["delivery_event"] == "recovery" and clear["alert_id"] == str(alert_id)


@pytest.mark.asyncio
async def test_rebooting_again_updates_the_same_incident_and_restarts_the_hold(reboot):
    n = reboot
    n.db.execute("UPDATE alert_rules SET clear_duration = 300 WHERE id = $1", n.rule)
    await replay(n, [10 * DAY, 10 * DAY + 60, 30, 90], start=T0)
    assert len(alerts(n)) == 1 and len(n.sent) == 1
    # Second reboot 3 minutes later: same incident, a "changed" notice.
    await replay(n, [150, 20, 80], start=T0 + 240)
    [(alert_id, status, message, meta)] = alerts(n)
    assert status == "active" and meta["reboot_count"] == 2
    assert "rebooted again" in message and "2 reboots since" in message
    assert len(n.sent) == 2 and n.sent[1]["lifecycle"] == "changed"
    assert n.sent[1]["delivery_event"].startswith("reboot:") and "rebooted again" in n.sent[1]["body"]
    # The 300 s hold runs from the second reboot (detected at T0+300).
    await replay(n, [140, 200, 260], start=T0 + 420)
    assert alerts(n)[0][1] == "active"
    await replay(n, [320], start=T0 + 600)
    assert alerts(n)[0][1] == "resolved" and len(alerts(n)) == 1
    assert "(2 in total)" in n.sent[-1]["body"]


@pytest.mark.asyncio
async def test_a_manual_resolve_inside_the_hold_is_not_paged_again(reboot):
    n = reboot
    await replay(n, [5 * DAY, 30, 90], start=T0)
    assert len(n.sent) == 1
    n.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE device_id = $1", n.device)
    await replay(n, [150, 210], start=T0 + 180)
    assert len(alerts(n)) == 1 and len(n.sent) == 1


@pytest.mark.asyncio
async def test_without_a_current_uptime_the_clear_says_the_state_is_unknown(reboot):
    n = reboot
    n.db.execute("UPDATE alert_rules SET clear_duration = 300 WHERE id = $1", n.rule)
    await replay(n, [5 * DAY, 30], start=T0)
    # The switch stops answering SNMP right after the reboot.
    await replay(n, [None] * 8, start=T0 + 120)
    [(_, status, _, _)] = alerts(n)
    assert status == "resolved"
    assert "no uptime reading since" in n.sent[-1]["body"] and "current state unknown" in n.sent[-1]["body"]


@pytest.mark.asyncio
async def test_an_operator_or_threshold_on_the_flag_no_longer_matters(reboot):
    """Rules saved with odd comparisons on the 0/1 flag (or a hold that made
    them never fire) keep their stored values and alert on reboots."""
    n = reboot
    n.db.execute("UPDATE alert_rules SET operator = '>', threshold = 0.5, min_duration = 600"
                 " WHERE id = $1", n.rule)
    await replay(n, [5 * DAY, 30, 90], start=T0)
    assert len(alerts(n)) == 1 and len(n.sent) == 1
    stored = n.db.query("SELECT operator, threshold, min_duration FROM alert_rules WHERE id = $1", n.rule)
    assert [tuple(r) for r in stored] == [(">", 0.5, 600)]
