"""Reset notifications follow the fate of their trigger.

Quiet hours gated the trigger but deliberately exempted the recovery, so a
breach starting inside a business-hours schedule produced a lone "resolved"
mail about an incident nobody was paged for -- and, because the alert row
already existed, the trigger was never sent even once the window opened.

alerts.metadata->>'notified' records what happened to the trigger; these pin
how that flag is read, including the upgrade case where it is absent.
"""
import json

import pytest

from app.services import alert_notify_state as ns


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row

    def scalar(self):
        return self._row[0] if self._row else None


class FakeDB:
    """Answers the metadata lookups; records the UPDATEs stamp() issues."""

    def __init__(self, flag="__absent__"):
        self.flag = flag
        self.updates = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append(json.loads(params["m"]))
            return FakeResult()
        if "metadata->>'notified'" in sql:
            if self.flag == "__absent__":
                return FakeResult((None,))
            return FakeResult((self.flag,))
        if "SELECT id FROM alerts" in sql:
            return FakeResult(("alert-1",))
        return FakeResult()


# ── stamp ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stamp_records_both_outcomes():
    db = FakeDB()
    await ns.stamp(db, "alert-1", True)
    await ns.stamp(db, "alert-1", False)
    assert db.updates == [{"notified": True}, {"notified": False}]


@pytest.mark.asyncio
async def test_stamp_on_a_missing_alert_is_a_no_op():
    """_raise returns None if the insert produced no row; don't blow up."""
    db = FakeDB()
    await ns.stamp(db, None, True)
    assert db.updates == []


# ── reading the flag ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_is_sent_when_the_trigger_was_sent():
    assert await ns.was_notified(FakeDB("true"), "alert-1") is True


@pytest.mark.asyncio
async def test_recovery_is_suppressed_when_the_trigger_was_suppressed():
    """The reported bug: a reset mail for a trigger that never went out."""
    assert await ns.was_notified(FakeDB("false"), "alert-1") is False


@pytest.mark.asyncio
async def test_alerts_predating_the_flag_still_get_their_recovery():
    """Upgrade safety: in-flight incidents must not lose their all-clear."""
    assert await ns.was_notified(FakeDB(), "alert-1") is True


@pytest.mark.asyncio
async def test_pending_is_true_only_for_an_explicitly_suppressed_trigger():
    assert await ns.is_pending(FakeDB("false"), "alert-1") is True   # owed a send
    assert await ns.is_pending(FakeDB("true"), "alert-1") is False   # already sent
    assert await ns.is_pending(FakeDB(), "alert-1") is False         # legacy, leave alone
