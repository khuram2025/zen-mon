"""What every alert raise and send path tells the shared dispatcher (1.24.17).

All alert notifications go through ``host_alert_service.dispatch_to_channels``
so recipient dedupe, durable delivery keys, grouping and per-recipient budgets
apply to every source alike. The evaluators share three small facts here:

* the incident priority stamped on ``alerts.priority`` when it is raised
  (``alert_priority.priority_for``: a rule's explicit priority, else derived
  from severity);
* whether a send may be held for a digest (``groupable``): only a warning or
  info TRIGGER whose priority is P3/P4. Critical and P1/P2 pages, recoveries,
  escalations and reviews always go out at once;
* whether the database has the priority columns yet (migrate-164). Until it
  does, raise paths simply leave them out — a code update that lands before
  its migration must not stop alerting.
"""

from __future__ import annotations

from sqlalchemy import text

from app.services import alert_priority

_PRIORITY_SUPPORTED = False

_PRIORITY_COLUMNS_SQL = """
    SELECT count(*) = 2 FROM pg_attribute
    WHERE attrelid IN ('alerts'::regclass, 'alert_rules'::regclass)
      AND attname = 'priority' AND NOT attisdropped
"""


async def priority_supported(db) -> bool:
    """Whether alerts.priority and alert_rules.priority exist (migrate-164).

    A positive answer is cached; a negative one is asked again, so an
    evaluator picks the migration up without a restart.
    """
    global _PRIORITY_SUPPORTED
    if not _PRIORITY_SUPPORTED:
        row = (await db.execute(text(_PRIORITY_COLUMNS_SQL))).first()
        try:
            _PRIORITY_SUPPORTED = row is not None and row[0] is True
        except (TypeError, IndexError, KeyError):
            _PRIORITY_SUPPORTED = False
    return _PRIORITY_SUPPORTED


async def rule_priority_column(db) -> str:
    """The SELECT expression for a rule's explicit priority."""
    return "priority" if await priority_supported(db) else "NULL AS priority"


def priority_for(rule_priority, severity) -> str:
    """The priority a newly raised alert is stamped with."""
    return alert_priority.priority_for(rule_priority, severity)


def priority_insert(supported: bool, rule_priority, severity) -> tuple[str, str, dict]:
    """(", priority", ", :priority", {"priority": "P3"}) to splice into an
    ``INSERT INTO alerts`` column list, VALUES list and parameters; three
    empty values when ``supported`` (:func:`priority_supported`) is False."""
    if not supported:
        return "", "", {}
    return ", priority", ", :priority", {"priority": priority_for(rule_priority, severity)}


async def stamp_priority(db, alert_id, rule_priority, severity) -> None:
    """Stamp the priority on an alert row another helper inserted."""
    if alert_id is None or not await priority_supported(db):
        return
    await db.execute(text("UPDATE alerts SET priority = :priority WHERE id = :id AND priority IS NULL"),
                     {"id": alert_id, "priority": priority_for(rule_priority, severity)})


def notification_fields(severity, rule_priority=None, *, trigger: bool) -> dict:
    """The dispatch ctx keys every sender passes: ``priority`` and
    ``groupable`` (True only for a warning/info trigger of priority P3/P4)."""
    priority = priority_for(rule_priority, severity)
    sev = str(severity or "warning").strip().lower()
    groupable = (bool(trigger) and sev in ("warning", "info")
                 and not alert_priority.is_urgent(priority))
    return {"priority": priority, "groupable": groupable}


def reset_for_tests() -> None:
    global _PRIORITY_SUPPORTED
    _PRIORITY_SUPPORTED = False
