"""Alert Center queries: list, summary metrics, trend and export.

One filter builder (``AlertFilters.where``) feeds every endpoint, so the list,
the summary cards, the trend chart and the CSV export always describe the
same population (Alerts review A01/A11/A15). All clock arithmetic happens in
SQL with NOW(): a rolling "last 24 h" is the appliance's last 24 hours on
every refetch, whatever the browser clock says (A13).

Populations:
* incidents — alert rows that are not notices (see alert_workflow.notice_sql);
* notices   — recovery notices, dependency-suppressed and hold-cancelled rows,
  device flapping start/stop notices.
The Alert Center defaults to incidents; legacy callers that pass ``status``
without ``view`` keep the historic unfiltered behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.schemas.alert import AlertStats
from app.services import alert_workflow as wf

OPEN = "('pending', 'active', 'acknowledged')"
VIEWS = ("active", "suppressed", "pending", "acknowledged", "resolved", "open", "all")
SORTS = ("newest", "oldest", "severity", "closed")
BASES = {"triggered": "a.triggered_at", "resolved": "a.resolved_at",
         "acknowledged": "a.acknowledged_at"}
ENTITY_KINDS = ("device", "server", "service_check", "sensor", "apm", "system")
AGES = {"gt1h": 1, "gt24h": 24, "gt7d": 168}
EXPORT_CAP = 50_000

_ID_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")


def _like(value: str) -> str:
    esc = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


ENTITY_KIND_SQL = (
    "CASE WHEN a.service_check_id IS NOT NULL THEN 'service_check'"
    " WHEN a.device_id IS NOT NULL THEN 'device'"
    " WHEN a.server_id IS NOT NULL THEN 'server'"
    " WHEN a.sensor_id IS NOT NULL THEN 'sensor'"
    " WHEN COALESCE(a.metadata->>'source', '') LIKE 'apm%' OR COALESCE(r.metric, '') LIKE 'apm\\_%'"
    " THEN 'apm' ELSE 'system' END"
)

RULE_MISSING_SQL = (
    "(r.id IS NULL AND (jsonb_exists(COALESCE(a.metadata, '{}'::jsonb), 'policy_snapshot')"
    " OR jsonb_exists(COALESCE(a.metadata, '{}'::jsonb), 'rule_id')"
    " OR jsonb_exists(COALESCE(a.metadata, '{}'::jsonb), 'new_status')))"
)


def base_from(workflow: bool) -> str:
    """FROM clause shared by list/summary/export (aliases: a, d, s, sc, sen, r)."""
    return (
        "FROM alerts a"
        " LEFT JOIN devices d ON d.id = a.device_id"
        " LEFT JOIN servers s ON s.id = a.server_id"
        " LEFT JOIN service_checks sc ON sc.id = a.service_check_id"
        " LEFT JOIN sensors sen ON sen.id = a.sensor_id"
        f" LEFT JOIN alert_rules r ON r.id = {wf.effective_rule_sql('a')}"
    )


@dataclass
class AlertFilters:
    view: str | None = None
    status: str | None = None
    severity: list[str] = field(default_factory=list)
    device_id: UUID | None = None
    server_id: UUID | None = None
    service_check_id: UUID | None = None
    sensor_id: UUID | None = None
    rule_id: UUID | None = None
    entity_kind: str | None = None
    assignee: str | None = None
    population: str | None = None
    hours: int | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    basis: str = "triggered"
    age: str | None = None
    rule_state: str | None = None
    search: str | None = None
    delivery: str | None = None
    as_of: datetime | None = None
    ids: list[str] | None = None
    visible_tags: list[str] | None = None
    user_id: Any = None
    # The caller lacks netflow.view: a search must not match a NetFlow
    # alert's (withheld) message text (X-01).
    hide_netflow_text: bool = False

    def describe(self) -> list[str]:
        """Human labels of the population-narrowing filters (for 'Filtered: …')."""
        out = []
        if self.severity:
            out.append("Severity: " + ", ".join(self.severity))
        for label, val in (("Device", self.device_id), ("Server", self.server_id),
                           ("Service check", self.service_check_id), ("Sensor", self.sensor_id),
                           ("Rule", self.rule_id)):
            if val:
                out.append(f"{label} scoped")
        if self.entity_kind:
            out.append(f"Entity: {self.entity_kind.replace('_', ' ')}")
        if self.assignee:
            out.append({"me": "Assigned to me", "unassigned": "Unassigned"}.get(
                self.assignee, "Assignee scoped"))
        if self.age:
            out.append(f"Open longer than {self.age[2:]}")
        if self.rule_state:
            out.append(f"Rule {self.rule_state}")
        if self.delivery:
            out.append({"failed": "Delivery failed", "none": "No delivery recorded"}.get(
                self.delivery, "Delivery"))
        if self.search:
            out.append(f"Search: {self.search}")
        return out

    def where(self, *, include_view: bool = True, include_window: bool = True,
              include_population: bool = True) -> tuple[list[str], dict]:
        w: list[str] = []
        p: dict = {}
        if self.visible_tags is not None:
            w.append(scoping.alert_visible("a"))
            p[scoping.SCOPE_PARAM] = self.visible_tags
        if self.ids is not None:
            w.append("a.id = ANY(CAST(:ids AS uuid[]))")
            p["ids"] = [str(i) for i in self.ids]
        sup = wf.suppressed_sql("a")
        if include_view and self.view:
            w.append({
                "active": f"a.status = 'active' AND NOT {sup}",
                "suppressed": f"a.status IN ('active', 'acknowledged') AND {sup}",
                "pending": "a.status = 'pending'",
                "acknowledged": f"a.status = 'acknowledged' AND NOT {sup}",
                "resolved": "a.status = 'resolved'",
                "open": f"a.status IN {OPEN}",
                "all": "TRUE",
            }[self.view])
        elif include_view and self.status:
            w.append("a.status = :status")
            p["status"] = self.status
        if self.severity:
            w.append("a.severity = ANY(CAST(:sev AS text[]))")
            p["sev"] = self.severity
        for col, val in (("device_id", self.device_id), ("server_id", self.server_id),
                         ("service_check_id", self.service_check_id),
                         ("sensor_id", self.sensor_id)):
            if val:
                w.append(f"a.{col} = :{col}")
                p[col] = val
        if self.rule_id:
            w.append(f"{wf.effective_rule_sql('a')} = :rule_id")
            p["rule_id"] = self.rule_id
        if self.entity_kind:
            w.append(f"({ENTITY_KIND_SQL}) = :ekind")
            p["ekind"] = self.entity_kind
        if self.assignee:
            if self.assignee == "unassigned":
                w.append("a.assigned_to IS NULL")
            elif self.assignee == "me":
                w.append("a.assigned_to = :assignee")
                p["assignee"] = self.user_id
            else:
                w.append("a.assigned_to = CAST(:assignee AS uuid)")
                p["assignee"] = self.assignee
        pop = self.population or ("incidents" if self.view else "all")
        if include_population and pop == "incidents":
            w.append(f"NOT {wf.notice_sql('a')}")
        elif include_population and pop == "notices":
            w.append(wf.notice_sql("a"))
        if include_window:
            col = BASES.get(self.basis, "a.triggered_at")
            if self.from_time or self.to_time:
                if self.from_time:
                    w.append(f"{col} >= :from_time")
                    p["from_time"] = self.from_time
                if self.to_time:
                    w.append(f"{col} <= :to_time")
                    p["to_time"] = self.to_time
            elif self.hours:
                w.append(f"{col} >= NOW() - make_interval(hours => :hours)")
                p["hours"] = int(self.hours)
        if self.as_of is not None:
            # Snapshot anchor for paging: rows that arrive after the first
            # page was read cannot shift later pages (A01).
            w.append("a.triggered_at <= :as_of")
            p["as_of"] = self.as_of
        if self.delivery == "failed":
            w.append("EXISTS (SELECT 1 FROM alert_deliveries _dl WHERE _dl.alert_id = a.id"
                     " AND _dl.outcome = 'failed')")
        elif self.delivery == "none":
            w.append("NOT EXISTS (SELECT 1 FROM alert_deliveries _dl WHERE _dl.alert_id = a.id)")
        if self.age in AGES:
            w.append("a.triggered_at < NOW() - make_interval(hours => :age_h)")
            p["age_h"] = AGES[self.age]
        if self.rule_state == "disabled":
            w.append("r.enabled = false")
        elif self.rule_state == "missing":
            w.append(RULE_MISSING_SQL)
        if self.search and self.search.strip():
            q = self.search.strip()
            p["q"] = _like(q)
            message = ("(a.message ILIKE :q AND COALESCE(a.metadata->>'source', '') <> 'netflow')"
                       if self.hide_netflow_text else "a.message ILIKE :q")
            ors = [
                message, "d.hostname ILIKE :q", "host(d.ip_address) ILIKE :q",
                "sc.name ILIKE :q", "s.hostname ILIKE :q", "s.display_name ILIKE :q",
                "host(s.primary_ip) ILIKE :q", "sen.name ILIKE :q", "r.name ILIKE :q",
                "a.metadata->>'monitor_name' ILIKE :q", "a.metadata->>'slo_name' ILIKE :q",
                "a.metadata->>'service' ILIKE :q",
            ]
            if _ID_PREFIX_RE.match(q):
                ors.append("a.id::text LIKE :idp")
                p["idp"] = q.lower() + "%"
            w.append("(" + " OR ".join(ors) + ")")
        return w, p


def order_sql(sort: str) -> str:
    return {
        "oldest": "a.triggered_at ASC, a.id ASC",
        "severity": ("CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
                     " a.triggered_at ASC, a.id ASC"),
        "closed": "a.resolved_at DESC NULLS LAST, a.id DESC",
    }.get(sort, "a.triggered_at DESC, a.id DESC")


def _where(clauses: list[str]) -> str:
    return (" WHERE " + " AND ".join(f"({c})" for c in clauses)) if clauses else ""


async def list_rows(db: AsyncSession, f: AlertFilters, *, sort: str = "newest",
                    skip: int = 0, limit: int = 50, count: bool = True):
    """(rows, total). Rows carry every column the serializer needs."""
    workflow = await wf.supported(db)
    clauses, params = f.where()
    frm = base_from(workflow)
    total = None
    if count:
        total = (await db.execute(text(f"SELECT count(*) {frm}{_where(clauses)}"), params)).scalar() or 0
    owner_cols = (", a.assigned_to, a.assigned_at, ug.username AS assignee_username,"
                  " ug.full_name AS assignee_full_name") if workflow else ""
    owner_join = " LEFT JOIN users ug ON ug.id = a.assigned_to" if workflow else ""
    sql = (
        "SELECT a.id, a.status, a.severity, a.message, a.triggered_at, a.acknowledged_at,"
        " a.resolved_at, a.rule_id AS raw_rule_id, r.id AS rule_id, r.name AS rule_name,"
        " r.enabled AS rule_enabled, r.metric AS rule_metric, r.notify_channels,"
        " a.device_id, a.server_id, a.service_check_id, a.sensor_id, a.metadata,"
        " a.acknowledged_by, ua.username AS ack_username, ua.full_name AS ack_full_name,"
        " d.hostname AS device_hostname, host(d.ip_address) AS device_ip,"
        " s.hostname AS server_hostname, s.display_name AS server_display,"
        " host(s.primary_ip) AS server_ip, sc.name AS service_check_name,"
        " sen.name AS sensor_name,"
        f" {wf.suppressed_sql('a')} AS suppressed"
        f"{owner_cols} {frm}"
        " LEFT JOIN users ua ON ua.id = a.acknowledged_by"
        f"{owner_join}{_where(clauses)} ORDER BY {order_sql(sort)}"
        " OFFSET :skip LIMIT :limit"
    )
    rows = (await db.execute(text(sql), {**params, "skip": skip, "limit": limit})).fetchall()
    return rows, total


def _stats(values: list[float]) -> dict:
    vals = sorted(v for v in values if v is not None and v >= 0)
    if not vals:
        return {"count": 0, "mean_s": None, "p50_s": None, "p95_s": None}

    def pct(q: float) -> float:
        if len(vals) == 1:
            return vals[0]
        k = (len(vals) - 1) * q
        lo = int(k)
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

    return {"count": len(vals), "mean_s": round(sum(vals) / len(vals), 1),
            "p50_s": round(pct(0.5), 1), "p95_s": round(pct(0.95), 1)}


async def summary(db: AsyncSession, f: AlertFilters, *, window_hours: int = 24,
                  targets: dict | None = None) -> dict:
    """Open-backlog counts and closed-incident metrics over complete populations."""
    workflow = await wf.supported(db)
    targets = targets or dict(wf.DEFAULT_RESPONSE_TARGETS)
    frm = base_from(workflow)
    now = (await db.execute(text("SELECT NOW()"))).scalar()

    # ── Open backlog (incidents only; window filters do not apply to "now") ──
    clauses, params = f.where(include_view=False, include_window=False, include_population=False)
    clauses = clauses + [f"a.status IN {OPEN}", f"NOT {wf.notice_sql('a')}"]
    owner = "a.assigned_to IS NULL" if workflow else "TRUE"
    tgt_sql = []
    for sev in ("critical", "warning", "info"):
        if targets.get(sev):
            tgt_sql.append(f"(x.severity = '{sev}' AND x.ack_at < NOW() - make_interval(mins => :t_{sev}))")
            params[f"t_{sev}"] = int(targets[sev])
    overdue = "(" + " OR ".join(tgt_sql) + ")" if tgt_sql else "FALSE"
    delivery_failed = ("EXISTS (SELECT 1 FROM alert_deliveries _dl WHERE _dl.alert_id = a.id"
                       " AND _dl.outcome = 'failed')") if workflow else "FALSE"
    open_row = (await db.execute(text(
        "SELECT"
        " count(*) AS total,"
        " count(*) FILTER (WHERE x.status = 'active' AND NOT x.sup) AS active,"
        " count(*) FILTER (WHERE x.status = 'acknowledged' AND NOT x.sup) AS acknowledged,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND x.sup) AS suppressed,"
        " count(*) FILTER (WHERE x.status = 'pending') AS pending,"
        " count(*) FILTER (WHERE x.status = 'pending' AND x.flap_held) AS flap_held,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.severity = 'critical') AS critical,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.severity = 'warning') AS warning,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.severity = 'info') AS info,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.severity = 'critical' AND x.unowned) AS unassigned_critical,"
        f" count(*) FILTER (WHERE x.status = 'active' AND NOT x.sup AND {overdue}) AS ack_overdue,"
        " min(x.triggered_at) FILTER (WHERE x.status IN ('active','acknowledged')) AS oldest,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.triggered_at >= NOW() - interval '1 hour') AS lt_1h,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.triggered_at < NOW() - interval '1 hour' AND x.triggered_at >= NOW() - interval '24 hours') AS h1_24,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.triggered_at < NOW() - interval '24 hours' AND x.triggered_at >= NOW() - interval '7 days') AS d1_7,"
        " count(*) FILTER (WHERE x.status IN ('active','acknowledged') AND NOT x.sup AND x.triggered_at < NOW() - interval '7 days') AS gt_7d,"
        " count(*) FILTER (WHERE x.rule_disabled) AS rule_disabled,"
        " count(*) FILTER (WHERE x.rule_missing) AS rule_missing,"
        " count(*) FILTER (WHERE x.triggered_at < NOW() - interval '7 days') AS older_7d,"
        " count(*) FILTER (WHERE x.delivery_failed) AS delivery_failed"
        f" FROM (SELECT a.status, a.severity, a.triggered_at, {wf.suppressed_sql('a')} AS sup,"
        f" {wf.ack_anchor_sql('a')} AS ack_at,"
        f" COALESCE(a.metadata->>'flap_held', 'false') = 'true' AS flap_held,"
        f" {delivery_failed} AS delivery_failed,"
        f" {owner} AS unowned, COALESCE(r.enabled = false, false) AS rule_disabled,"
        f" {RULE_MISSING_SQL} AS rule_missing"
        f" {frm}{_where(clauses)}) x"
    ), params)).first()

    # ── Closed-incident window metrics ──
    wclauses, wparams = f.where(include_view=False, include_window=False, include_population=False)
    if f.from_time or f.to_time:
        w_from = f.from_time or (now - timedelta(hours=window_hours))
        w_to = f.to_time or now
    else:
        w_to = now
        w_from = now - timedelta(hours=max(1, int(window_hours)))
    wparams.update({"w_from": w_from, "w_to": w_to})
    in_w = "BETWEEN :w_from AND :w_to"
    wclauses = wclauses + [f"(a.triggered_at {in_w} OR a.resolved_at {in_w} OR a.acknowledged_at {in_w})"]
    wrows = (await db.execute(text(
        f"SELECT a.status, a.triggered_at, a.acknowledged_at, a.resolved_at,"
        f" {wf.closure_reason_sql('a')} AS reason, {wf.notice_sql('a')} AS is_notice"
        f" {frm}{_where(wclauses)}"
    ), wparams)).fetchall()

    def within(ts):
        return ts is not None and w_from <= ts <= w_to

    opened = closed = notices = anomalies = 0
    by_reason: dict[str, int] = {"recovered": 0, "manual": 0, "suppressed": 0,
                                 "superseded": 0, "administrative": 0}
    ttr, ttc, tta = [], [], []
    for r in wrows:
        if r.is_notice:
            if within(r.resolved_at):
                notices += 1
            continue
        if within(r.triggered_at):
            opened += 1
        if r.status == "resolved" and within(r.resolved_at):
            closed += 1
            by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
            secs = (r.resolved_at - r.triggered_at).total_seconds() if r.triggered_at else None
            if secs is not None and secs < 0:
                anomalies += 1
            elif secs is not None:
                ttc.append(secs)
                if r.reason == "recovered":
                    ttr.append(secs)
        if within(r.acknowledged_at) and r.triggered_at:
            secs = (r.acknowledged_at - r.triggered_at).total_seconds()
            if secs >= 0:
                tta.append(secs)
            else:
                anomalies += 1

    delivery = {"attempts": 0, "accepted": 0, "failed": 0, "skipped": 0, "alerts_with_failures": 0}
    if workflow:
        dclauses, dparams = f.where(include_view=False, include_window=False, include_population=False)
        dparams.update({"w_from": w_from, "w_to": w_to})
        drow = (await db.execute(text(
            "SELECT count(*) AS attempts,"
            " count(*) FILTER (WHERE dl.outcome = 'accepted') AS accepted,"
            " count(*) FILTER (WHERE dl.outcome = 'failed') AS failed,"
            " count(*) FILTER (WHERE dl.outcome = 'skipped') AS skipped,"
            " count(DISTINCT dl.alert_id) FILTER (WHERE dl.outcome = 'failed') AS alerts_with_failures"
            f" FROM alert_deliveries dl JOIN ({'SELECT a.id ' + frm + _where(dclauses)}) fa ON fa.id = dl.alert_id"
            " WHERE dl.attempted_at BETWEEN :w_from AND :w_to"
        ), dparams)).first()
        delivery = {k: int(getattr(drow, k) or 0) for k in delivery}

    o = open_row
    return {
        "server_time": now,
        "filtered": bool(f.describe()),
        "filters": f.describe(),
        "open": {
            "total": o.total, "active": o.active, "acknowledged": o.acknowledged,
            "suppressed": o.suppressed, "pending": o.pending,
            # Pending because the device is flapping (part of ``pending``).
            "flap_held": o.flap_held,
            "by_severity": {"critical": o.critical, "warning": o.warning, "info": o.info},
            "unassigned_critical": o.unassigned_critical if workflow else None,
            "ack_overdue": o.ack_overdue,
            "oldest_triggered_at": o.oldest,
            "aging": {"lt_1h": o.lt_1h, "h1_24": o.h1_24, "d1_7": o.d1_7, "gt_7d": o.gt_7d},
        },
        "window": {
            "from": w_from, "to": w_to, "basis": "resolved",
            "opened": opened, "closed": closed, "by_reason": by_reason,
            "notices": notices, "clock_anomalies": anomalies,
            "time_to_recover": _stats(ttr), "time_to_close": _stats(ttc),
            "time_to_ack": _stats(tta),
            "delivery": delivery,
        },
        "diagnostics": {
            "open_rule_disabled": o.rule_disabled, "open_rule_missing": o.rule_missing,
            "open_older_7d": o.older_7d,
            "open_delivery_failed": o.delivery_failed,
        },
        "response_targets": targets,
    }


async def trend(db: AsyncSession, f: AlertFilters, *, hours: int = 168,
                bucket: str | None = None, tz: str = "UTC") -> dict:
    """Opened vs closed incidents per bucket, plus top rules and entities."""
    hours = max(1, min(int(hours), 2160))
    unit = bucket if bucket in ("hour", "day") else ("hour" if hours <= 48 else "day")
    workflow = await wf.supported(db)
    frm = base_from(workflow)
    now = (await db.execute(text("SELECT NOW()"))).scalar()
    start = now - timedelta(hours=hours)
    clauses, params = f.where(include_view=False, include_window=False, include_population=False)
    clauses = clauses + [f"NOT {wf.notice_sql('a')}"]
    params.update({"start": start, "unit": unit, "tz": tz})
    opened = (await db.execute(text(
        f"SELECT date_trunc(:unit, a.triggered_at AT TIME ZONE :tz) AS bucket_at, count(*) AS n"
        f" {frm}{_where(clauses + ['a.triggered_at >= :start'])} GROUP BY 1"
    ), params)).fetchall()
    closed_where = _where(clauses + ["a.resolved_at >= :start", "a.status = 'resolved'"])
    closed = (await db.execute(text(
        f"SELECT date_trunc(:unit, a.resolved_at AT TIME ZONE :tz) AS bucket_at,"
        f" {wf.closure_reason_sql('a')} AS reason, count(*) AS n"
        f" {frm}{closed_where} GROUP BY 1, 2"
    ), params)).fetchall()
    top_rules = (await db.execute(text(
        f"SELECT r.id, r.name, count(*) AS n {frm}"
        f"{_where(clauses + ['a.triggered_at >= :start', 'r.id IS NOT NULL'])}"
        f" GROUP BY r.id, r.name ORDER BY n DESC LIMIT 8"
    ), params)).fetchall()
    top_entities = (await db.execute(text(
        f"SELECT {ENTITY_KIND_SQL} AS kind,"
        f" COALESCE(a.service_check_id, a.device_id, a.server_id, a.sensor_id)::text AS id,"
        f" COALESCE(sc.name, d.hostname, s.display_name, s.hostname, sen.name,"
        f"          a.metadata->>'monitor_name', a.metadata->>'slo_name', a.metadata->>'service', 'System') AS name,"
        f" count(*) AS n {frm}{_where(clauses + ['a.triggered_at >= :start'])}"
        f" GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 8"
    ), params)).fetchall()
    # Local-time bucket series with zeros, generated from the DB clock.
    local_now = (await db.execute(text("SELECT date_trunc(:unit, NOW() AT TIME ZONE :tz)"),
                                  {"unit": unit, "tz": tz})).scalar()
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    n_buckets = hours if unit == "hour" else max(1, (hours + 23) // 24)
    series_keys = [local_now - step * i for i in range(n_buckets - 1, -1, -1)]
    series = {k: {"t": k.isoformat(), "opened": 0, "recovered": 0, "closed_other": 0}
              for k in series_keys}
    for r in opened:
        if r.bucket_at in series:
            series[r.bucket_at]["opened"] += int(r.n)
    for r in closed:
        if r.bucket_at in series:
            key = "recovered" if r.reason == "recovered" else "closed_other"
            series[r.bucket_at][key] += int(r.n)
    return {
        "server_time": now, "bucket": unit, "timezone": tz, "hours": hours,
        "series": [series[k] for k in series_keys],
        "top_rules": [{"rule_id": str(r.id), "name": r.name, "count": int(r.n)} for r in top_rules],
        "top_entities": [{"kind": r.kind, "id": r.id, "name": r.name, "count": int(r.n)}
                         for r in top_entities],
    }


async def get_alert_stats(db: AsyncSession, visible_tags: list[str] | None = None) -> AlertStats:
    """Legacy counters for the sidebar/dashboard badges. ``active`` and the
    severity counts exclude suppressed conditions (what needs attention);
    ``resolved_today`` counts incidents, not recovery notices."""
    scope = f"AND {scoping.alert_visible('a')}" if visible_tags is not None else ""
    params = {scoping.SCOPE_PARAM: visible_tags} if visible_tags is not None else {}
    sup = wf.suppressed_sql("a")
    row = (await db.execute(text(
        "SELECT"
        f" count(*) FILTER (WHERE a.status = 'active' AND NOT {sup}) AS active,"
        f" count(*) FILTER (WHERE a.status = 'acknowledged') AS acknowledged,"
        f" count(*) FILTER (WHERE a.status IN ('active','acknowledged') AND {sup}) AS suppressed,"
        " count(*) FILTER (WHERE a.status = 'pending') AS pending,"
        f" count(*) FILTER (WHERE a.status = 'active' AND NOT {sup} AND a.severity = 'critical') AS critical,"
        f" count(*) FILTER (WHERE a.status = 'active' AND NOT {sup} AND a.severity = 'warning') AS warning,"
        f" count(*) FILTER (WHERE a.status = 'active' AND NOT {sup} AND a.severity = 'info') AS info,"
        " count(*) FILTER (WHERE a.status = 'resolved' AND a.resolved_at >= date_trunc('day', NOW())"
        f"   AND NOT {wf.notice_sql('a')}) AS resolved_today,"
        " NOW() AS server_time"
        f" FROM alerts a WHERE a.status IN ('pending','active','acknowledged','resolved') {scope}"
        "   AND (a.status <> 'resolved' OR a.resolved_at >= date_trunc('day', NOW()))"
    ), params)).first()
    return AlertStats(
        active=row.active, acknowledged=row.acknowledged, resolved_today=row.resolved_today,
        critical=row.critical, warning=row.warning, info=row.info,
        suppressed=row.suppressed, pending=row.pending, server_time=row.server_time,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
