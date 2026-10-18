"""Host-metric alert evaluation.

Device/service alerting is event-driven (the Go poller pushes status changes to
alert_engine). Host metrics live in ClickHouse and arrive continuously, so they
need a *periodic* evaluator instead: every ``EVAL_INTERVAL_S`` this loads the
enabled host alert rules (``alert_rules`` rows whose ``metric`` starts with
``host_``), evaluates each against every in-scope server, and raises/resolves a
server-scoped alert via the shared helpers in server_health_service.

Threshold metrics (CPU, memory, filesystem per mount, disk utilisation) are
judged on their per-sample history through the same contract as the SNMP
evaluator (``network_conditions.evaluate``):

* ``min_duration`` is a breach-persistence hold: every sample for that long
  must breach before an incident opens (it used to widen an averaging window,
  so a 5-minute mean hovering on the threshold re-fired on every crossing).
* An open incident resolves only after its cleared condition held for the
  rule's ``clear_duration`` (NULL = 300 s, 0 = immediate), judged against the
  rule's reset threshold or, for these percent metrics, the automatic one
  2 points on the healthy side. A re-breach during that hold continues the
  same incident; nothing is re-sent.
* A missing or stale sample (older than the agent's telemetry freshness) is
  unknown: it neither raises, nor resolves, nor counts as clear time.

One incident per rule + server (+ mount for filesystems: dedupe
``rule:<id>:mount:<mount>``). Each decision is serialized per rule/server/
dimension with a transaction advisory lock, and the whole pass runs in one
API worker at a time (``alert_worker_lock``).

A rule with ``server_id IS NULL`` applies to every agent server; a rule with a
``server_id`` applies only to that one. Agent-offline is handled by the
staleness sweep.
"""

from __future__ import annotations
from app.core.crypto import encrypt_config, decrypt_config
import ssl

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap
from app.services import alert_notify_state as ns
from app.services.server_health_service import (
    create_server_alert, resolve_server_alerts, server_condition_silenced,
)
from app.services.filesystem_monitoring import ch_capacity_filter
from app.services.host_metric_service import telemetry_freshness_seconds

logger = logging.getLogger("zenplus.host_alerts")

EVAL_INTERVAL_S = 60
MIN_LOOKBACK_S = 900          # history floor so short holds still see context
# CPU, memory and disk utilisation are judged on their rolling mean over
# max(min_duration, AVERAGE_WINDOW_S), as before 1.24.16: a "> 90% for 5 min"
# rule fires on a 5-minute average, so one dip below 90% does not reset it.
AVERAGE_WINDOW_S = 120
AVERAGED_METRICS = frozenset({"host_cpu_pct", "host_memory_pct", "host_disk_util_pct"})
# One agent with a very long upload interval must not stretch every rule's
# history query (and hit HISTORY_ROW_LIMIT, which would skip the rule).
MAX_LOOKBACK_BUDGET_S = 900


def rolling_mean(series: list, window: float) -> list:
    """Each sample replaced by the mean of the samples in (ts - window, ts].

    A missing sample stays missing (a gap is never averaged into a reading),
    so the freshness and unknown-data rules of the evaluator still apply.
    """
    out: list = []
    start = 0
    total = 0.0
    count = 0
    vals = [(ts, v) for ts, v in series]
    for i, (ts, v) in enumerate(vals):
        if v is not None:
            total += v
            count += 1
        while start <= i and vals[start][0] <= ts - window:
            if vals[start][1] is not None:
                total -= vals[start][1]
                count -= 1
            start += 1
        out.append((ts, None if v is None or count == 0 else total / count))
    return out
HISTORY_ROW_LIMIT = 1_000_000

# Threshold metrics evaluated on per-sample history. Each entry is the SQL for
# (server_id, dimension, epoch seconds, value) rows between %(since)s and
# %(until)s, ordered by server, dimension and time.
_HISTORY_SQL = {
    "host_cpu_pct": (
        "SELECT toString(server_id), '', toUnixTimestamp64Milli(timestamp) / 1000, cpu_total_pct "
        "FROM zenplus.host_cpu_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "ORDER BY server_id, timestamp LIMIT {limit}"
    ),
    "host_memory_pct": (
        "SELECT toString(server_id), '', toUnixTimestamp64Milli(timestamp) / 1000, used_pct "
        "FROM zenplus.host_memory_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "ORDER BY server_id, timestamp LIMIT {limit}"
    ),
    # Disk utilisation keeps its server-wide meaning (the mean over the
    # server's disks), now per collection instead of per window.
    "host_disk_util_pct": (
        "SELECT toString(server_id), '', toUnixTimestamp(timestamp) AS ts, avg(util_pct) "
        "FROM zenplus.host_disk_io_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "GROUP BY server_id, ts ORDER BY server_id, ts LIMIT {limit}"
    ),
    "host_filesystem_pct": (
        "SELECT toString(server_id), mount, toUnixTimestamp64Milli(timestamp) / 1000, used_pct "
        "FROM zenplus.host_filesystem_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s AND {capacity} "
        "ORDER BY server_id, mount, timestamp LIMIT {limit}"
    ),
}
THRESHOLD_METRICS = frozenset(_HISTORY_SQL)


def fetch_host_history(metric: str, since: float, until: float) -> dict[str, dict[str, list]]:
    """{server_id: {dimension: [(epoch s, value), ...]}} for a threshold metric.

    The dimension is the mount for filesystems and "" otherwise. Raises when
    the history cannot be read; the caller then leaves every incident as it is.
    """
    from app.core.database import get_clickhouse_client

    sql = _HISTORY_SQL[metric].format(limit=HISTORY_ROW_LIMIT, capacity=ch_capacity_filter())
    rows = get_clickhouse_client().query(sql, parameters={
        "since": datetime.fromtimestamp(since, timezone.utc),
        "until": datetime.fromtimestamp(until, timezone.utc),
    }).result_rows
    if len(rows) >= HISTORY_ROW_LIMIT:
        raise RuntimeError(f"host alert history limit reached for {metric}")
    out: dict[str, dict[str, list]] = {}
    for sid, dim, stamp, value in rows:
        out.setdefault(str(sid), {}).setdefault(str(dim or ""), []).append(
            (float(stamp), None if value is None else float(value)))
    return out


async def _freshness_budgets(db: AsyncSession) -> dict[str, int]:
    """{server_id: seconds a sample stays current} from each agent's policy."""
    rows = (await db.execute(text(
        """SELECT DISTINCT ON (a.server_id) a.server_id, p.metric_interval_s, p.upload_interval_s
           FROM agents a
           LEFT JOIN agent_policies p ON p.id = a.policy_id
           WHERE a.server_id IS NOT NULL
           ORDER BY a.server_id, a.last_heartbeat_at DESC NULLS LAST, a.created_at DESC"""
    ))).all()
    return {str(r[0]): telemetry_freshness_seconds(r[1] if r[1] is not None else 30,
                                                   r[2] if r[2] is not None else 60)
            for r in rows}


async def _servers(db: AsyncSession) -> dict[str, str]:
    """{server_id: hostname} for agent servers that can be alerted on."""
    rows = (await db.execute(text(
        "SELECT id, hostname FROM servers "
        "WHERE collection_mode = 'agent' AND status <> 'disabled'"
    ))).all()
    return {str(r[0]): (r[1] or "server") for r in rows}


async def _gateway_config(db: AsyncSession, ch, gw_type: str) -> dict | None:
    gw_id = ch.gateway_id or (decrypt_config(ch.config)).get("gateway_id")
    if gw_id:
        row = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways WHERE id = :id"),
            {"id": gw_id})).first()
    else:
        row = (await db.execute(text(
            "SELECT config, enabled FROM notification_gateways "
            "WHERE type = :t AND is_default = true LIMIT 1"
        ), {"t": gw_type})).first()
    if not row:
        return None
    # `enabled` is the column the Gateways page writes; config.enabled is only
    # whatever was in the blob when it was last saved. Honouring the column is
    # what makes "disable this gateway" actually stop delivery.
    if not row.enabled:
        return None
    return decrypt_config(row.config)


# ── Notification dispatch ────────────────────────────────────────────────────

_PHONE_NOISE = re.compile(r"[\s().\-]")


def _recipients(raw, kind: str) -> list[tuple[str, str]]:
    """[(dedupe key, address as configured)] for a channel's recipient list,
    in order and without repeats. Email keys are case-insensitive; phone keys
    ignore spacing and punctuation."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace(";", ",").split(",")
    for item in items:
        addr = str(item or "").strip()
        if not addr:
            continue
        key = addr.lower() if kind == "email" else _PHONE_NOISE.sub("", addr)
        if key and key not in seen:
            seen.add(key)
            out.append((key, addr))
    return out


def _unique_channels(channel_ids) -> list[str]:
    """Channel ids in order, each once (a rule listing a channel twice, or a
    caller merging two lists, would otherwise send it twice)."""
    return list(dict.fromkeys(str(c) for c in (channel_ids or []) if c not in (None, "")))


def _key_context(ctx: dict):
    """(alert_id, event) when this dispatch is keyed, else None."""
    from app.services import alert_delivery_log as dl

    alert_id = dl._uuid_or_none(ctx.get("alert_id"))
    event = str(ctx.get("delivery_event") or "").strip()
    if not alert_id or not event or not dl.keys_enabled():
        return None
    return alert_id, event[:64]


# Direct-to-person deliveries (ctx["recipients"], e.g. escalation targets
# resolved to a user's contact methods) are keyed and logged against this
# constant channel id; equal to notification_grouping.DIRECT_CHANNEL_ID.
DIRECT_CHANNEL_ID = "00000000-0000-0000-0000-0000000000e1"


def _direct_targets(ctx: dict) -> dict[str, list[tuple[str, str, dict]]]:
    """``ctx['recipients']`` by kind: {"email": [(key, address, entry)], "sms": [...]},
    in order, each address once (same normalization as channel recipients)."""
    out: dict[str, list[tuple[str, str, dict]]] = {"email": [], "sms": []}
    seen: set[tuple[str, str]] = set()
    for entry in ctx.get("recipients") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in out:
            continue
        for key, addr in _recipients([entry.get("address")], kind):
            if (kind, key) not in seen:
                seen.add((kind, key))
                out[kind].append((key, addr, entry))
    return out


async def _default_gateway(db: AsyncSession, gw_type: str) -> dict | None:
    """The enabled default gateway of a type ("smtp" / "sms"), or None."""
    row = (await db.execute(text(
        "SELECT config, enabled FROM notification_gateways "
        "WHERE type = :t AND is_default = true LIMIT 1"), {"t": gw_type})).first()
    if not row or not row.enabled:
        return None
    return decrypt_config(row.config)


def _mail_ctx(ctx: dict) -> dict:
    """The email_render context of an alert notification."""
    from app.api.v1.alert_engine import _clean_details

    mail_ctx = {
        "severity": ctx.get("severity"),
        "status": ctx.get("status"),
        "resolved": bool(ctx.get("resolved")) or (ctx.get("status") == "RESOLVED"),
        "title": ctx.get("rule_name"),
        "hostname": ctx.get("hostname"),
        "ip_address": ctx.get("ip_address"),
        # `body` is the prose written for mail; `message` is the
        # SMS line. Reading them the wrong way round put a 140-
        # character telegram in the callout of a full-width mail.
        "message": ctx.get("body") or ctx.get("message"),
        "notice": ctx.get("notice") or "",
        "details": _clean_details(ctx.get("details") or []),
        "headline_metric": ctx.get("headline_metric") or {},
        "action_url": ctx.get("action_url") or "",
        "timestamp": ctx.get("triggered_at"),
    }
    # Incident identity and lifecycle (alert_phrasing.incident_context).
    mail_ctx.update({k: ctx[k] for k in ap.INCIDENT_KEYS if ctx.get(k)})
    # A sender whose mail differs from its webhook payload
    # (the service path's mail has no IP line) says so here.
    mail_ctx.update(ctx.get("email_overrides") or {})
    return mail_ctx


async def dispatch_to_channels(db: AsyncSession, channel_ids: list, ctx: dict) -> int:
    """Send an alert to notification channels. Best-effort; returns how many
    channels (and direct recipients) accepted it — or hold it in a digest.
    Every outcome is written to the delivery log (accepted / failed / skipped
    / held) against ``ctx['alert_id']`` if given.

    Channel ids are deduplicated, and so are recipients across the channels of
    one dispatch (an address on two email channels gets one mail;
    case-insensitive).

    Durable delivery (migrate-162): when ``ctx`` carries ``alert_id`` *and*
    ``delivery_event`` ("trigger", "recovery", "escalation:L2:r1", ...), each
    (channel, recipient) is claimed in ``alert_delivery_keys`` and the claim is
    committed before anything is sent, so this function COMMITS the caller's
    session. A delivery another worker, retry or earlier pass already claimed,
    accepted or left uncertain is skipped (and counted as delivered); a failed
    one is retried. The key records accepted / failed / uncertain — an error
    that may have come after the provider took the message is uncertain and is
    never re-sent. Without both keys this behaves as it always has.

    Further ``ctx`` keys (1.24.17):

    * ``recipients``: [{"kind": "email"|"sms", "address", "user_id", "label"}]
      delivered one by one through the default SMTP / SMS gateway, with the
      same cross-channel dedupe and delivery keys (channel id
      :data:`DIRECT_CHANNEL_ID`).
    * ``priority`` (P1..P4) and ``groupable`` (default False): a groupable
      P3/P4 trigger may be held into a digest by notification_grouping
      (grouping wait or per-recipient budget). P1/P2/critical, recoveries,
      escalations and reviews are never held. A held send counts as handled.
      A recovery whose trigger still waits in an open digest is listed there
      as already recovered instead of being sent on its own.
    * ``only_recipients``: restrict channels to these recipient keys (digest
      release); ``html_body``: a pre-rendered mail body.
    """
    from app.api.v1.alert_engine import _send_email, _send_sms, _dispatch_channel_result
    from app.services import alert_delivery_log as dl
    from app.services import notification_grouping as ng
    from app.services.email_render import alert_subject, build_alert_email_html

    kind = dl.kind_from_ctx(ctx)
    keyed = _key_context(ctx)
    # Recipients this dispatch has already handled (or found already
    # delivered for this incident event), by dedupe key.
    covered: set[str] = set()
    # ... of which these were delivered (or claimed) by an earlier dispatch.
    prior = await dl.delivered_recipients(db, *keyed) if keyed else {}
    covered.update(prior)
    raw_only = ctx.get("only_recipients")
    only = None if raw_only is None else {str(k) for k in raw_only}
    gate = await ng.gate_for(db, ctx, keyed)
    # Channels / direct recipients that accepted (or hold) this notification.
    credited: set[str] = set()

    class _Route:
        """One channel, or one direct recipient, of this dispatch."""

        def __init__(self, ch_id: str, credit_key: str, name=None, ctype=None):
            self.id, self.credit_key, self.name, self.type = ch_id, credit_key, name, ctype
            self.timer = dl.Timer()
            self.claimed: list[str] = []
            self.in_flight: list[str] = []

        async def log(self, outcome: str, error=None):
            await dl.record(
                db, alert_id=ctx.get("alert_id"), rule_id=ctx.get("rule_id"),
                channel_id=self.id, channel_name=self.name, channel_type=self.type, kind=kind,
                outcome=outcome, error=error,
                latency_ms=self.timer.ms if outcome not in ("skipped", "held") else None)

        async def claim(self, targets: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
            """Claim targets: (targets to send to, any already delivered).
            Unkeyed dispatches send to every target."""
            if not keyed or not dl._uuid_or_none(self.id):
                return targets, False
            fresh, already = [], False
            for key, addr in targets:
                key_id, state = await dl.claim_key(db, keyed[0], keyed[1], self.id, key)
                if key_id:
                    self.claimed.append(key_id)
                    fresh.append((key, addr))
                elif state is None:
                    fresh.append((key, addr))      # keys unavailable: send as before
                else:
                    # Another dispatch owns (or delivered) this recipient for
                    # this event: no other channel of ours sends it again.
                    covered.add(key)
                    already = already or state in dl.DELIVERED_STATES
            if self.claimed:
                await db.commit()                  # the claim is durable before the send
            return fresh, already

        async def finish(self, state: str, error=None):
            if self.claimed:
                await dl.finish_keys(db, self.claimed, state, error)
                await db.commit()

        async def gated(self, targets: list[tuple[str, str]], direct_kind: str | None = None):
            """Targets still to send now after the grouping/budget gate held
            (or folded) the rest; held ones count as handled."""
            if not gate:
                return targets
            rest, routed = await gate.route(db, channel_id=self.id, channel_name=self.name,
                                            channel_type=self.type or "", targets=targets,
                                            direct_kind=direct_kind)
            for r in routed:
                covered.update(k for k in r.keys if k)
                await self.log(r.log_outcome, r.log_text)
            if routed:
                credited.add(self.credit_key)
            return rest

        async def send_addresses(self, gw: dict, ctype: str, rcpt: str, targets) -> None:
            self.in_flight = [k for k, _ in targets]
            if ctype == "email":
                mail_ctx = _mail_ctx(ctx)
                ok = await _send_email(gw, rcpt, alert_subject(ctx["subject"], ctx),
                                       mail_ctx["message"],
                                       html_body=ctx.get("html_body") or build_alert_email_html(mail_ctx))
                skip_reason = "config: SMTP gateway has no host"
            else:
                # Gateway template placeholders the sender offers
                # (device path: {hostname}, {ip_address}, {status}).
                ok = await _send_sms(gw, rcpt, ctx["message"],
                                     values=ctx.get("sms_template_values"))
                skip_reason = "config: SMS gateway is not a usable HTTP gateway"
            # Log first: the key's commit then makes both durable.
            if ok:
                credited.add(self.credit_key)
                covered.update(self.in_flight)
                await self.log("accepted")
                await self.finish("accepted")
            else:
                await self.log("skipped", skip_reason)
                await self.finish("failed", skip_reason)

        async def failed(self, exc: BaseException):
            logger.warning("alert notify: channel %s failed: %s", self.name or self.id, exc)
            outcome = dl.outcome_for_exception(exc)
            if outcome == "uncertain":
                covered.update(self.in_flight)   # it may have arrived: never twice
            await self.log("failed", exc if outcome == "failed"
                           else f"uncertain: {dl.sanitize_error(exc)}")
            if self.claimed:
                try:
                    await self.finish(outcome, exc)
                except Exception:
                    logger.warning("alert notify: could not record outcome for channel %s", self.id)

    for ch_id in _unique_channels(channel_ids):
        route = _Route(ch_id, ch_id)
        try:
            ch = (await db.execute(text(
                "SELECT name, type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"
            ), {"id": ch_id})).first()
            if not ch:
                await route.log("skipped", "config: channel no longer exists")
                continue
            route.name, route.type = ch.name, ch.type
            if not ch.enabled:
                await route.log("skipped", "config: channel disabled")
                continue
            cfg = decrypt_config(ch.config)
            if ch.type in ("email", "sms"):
                field = "recipients" if ch.type == "email" else "phone_numbers"
                listed = _recipients(cfg.get(field, ""), ch.type)
                if only is not None:
                    listed = [(k, a) for k, a in listed if k in only]
                    if not listed:
                        await route.log("skipped", "config: the held recipients are no longer on this channel")
                        continue
                gw = await _gateway_config(db, ch, "smtp" if ch.type == "email" else "sms")
                if not listed or not gw:
                    what = "recipients" if ch.type == "email" else "phone numbers"
                    await route.log("skipped", f"config: no {what}" if not listed
                                    else f"config: no enabled {'SMTP' if ch.type == 'email' else 'SMS'} gateway")
                    continue
                # A recipient counts as covered only once a channel accepted
                # the message (or possibly did): a backup channel on another
                # gateway still reaches them when this one fails.
                targets = [(k, a) for k, a in listed if k not in covered]
                if not targets:
                    if any(k in prior for k, _ in listed):
                        credited.add(ch_id)     # an earlier dispatch of this event reached them
                        await route.log("skipped", "duplicate: already delivered for this event")
                    else:
                        await route.log("skipped", "duplicate: every recipient was already notified "
                                                   "for this event by another channel")
                    continue
                targets = await route.gated(targets)
                if not targets:
                    continue
                targets, already = await route.claim(targets)
                if not targets:
                    if already:
                        credited.add(ch_id)
                    await route.log("skipped", "duplicate: already delivered for this event")
                    continue
                if ch.type == "email":
                    rcpt = ", ".join(a for _, a in targets)
                elif len(targets) == len(listed) and only is None and isinstance(cfg.get(field), str):
                    rcpt = cfg[field]     # SMS gateways substitute the list verbatim
                else:
                    rcpt = ",".join(a for _, a in targets)
                await route.send_addresses(gw, ch.type, rcpt, targets)
            elif ch.type in ("webhook", "slack", "teams", "discord", "pagerduty"):
                if not await route.gated([("", "")]):
                    continue
                targets, already = await route.claim([("", "")])
                if not targets:
                    if already:
                        credited.add(ch_id)
                    await route.log("skipped", "duplicate: already delivered for this event")
                    continue
                ok, err = await _dispatch_channel_result(ch.type, cfg, ctx)
                if ok:
                    credited.add(ch_id)
                    await route.log("accepted")
                    await route.finish("accepted")
                elif err is not None:
                    outcome = dl.outcome_for_exception(err)
                    await route.log("failed", err if outcome == "failed"
                                    else f"uncertain: {dl.sanitize_error(err)}")
                    await route.finish(outcome, err)
                else:
                    await route.log("skipped", "config: no URL or routing key")
                    await route.finish("failed", "config: no URL or routing key")
            else:
                await route.log("skipped", f"config: unsupported channel type {ch.type}")
        except Exception as exc:
            await route.failed(exc)

    # Direct to person: one message per recipient through the default gateway.
    direct = _direct_targets(ctx)
    for dkind in ("email", "sms"):
        gw: dict | None | bool = None
        for key, addr, entry in direct[dkind]:
            route = _Route(DIRECT_CHANNEL_ID, f"direct:{dkind}:{key}",
                           name=str(entry.get("label") or ng.DIRECT_CHANNEL_NAME)[:255], ctype=dkind)
            try:
                if key in covered:
                    if key in prior:
                        credited.add(route.credit_key)
                        await route.log("skipped", "duplicate: already delivered for this event")
                    else:
                        await route.log("skipped", "duplicate: already notified for this event "
                                                   "by another channel")
                    continue
                if gw is None:
                    gw = await _default_gateway(db, "smtp" if dkind == "email" else "sms") or False
                if not gw:
                    await route.log("skipped", "config: no enabled default "
                                               f"{'SMTP' if dkind == 'email' else 'SMS'} gateway")
                    continue
                targets = await route.gated([(key, addr)], direct_kind=dkind)
                if not targets:
                    continue
                targets, already = await route.claim(targets)
                if not targets:
                    if already:
                        credited.add(route.credit_key)
                    await route.log("skipped", "duplicate: already delivered for this event")
                    continue
                await route.send_addresses(gw, dkind, addr, targets)
            except Exception as exc:
                await route.failed(exc)
    if gate is not None and gate.changed:
        await db.commit()
    return len(credited)


# ── Evaluation ───────────────────────────────────────────────────────────────

async def _current_value(db: AsyncSession, rule, server_id: str, ch_fleets: dict | None = None,
                         fs_worst: dict | None = None) -> tuple[bool, float, str] | None:
    """(breach, value, detail) for a service/process rule against one server,
    or None to skip. Threshold metrics are judged on history instead."""
    metric = rule.metric

    if metric == "host_service_down":
        if not rule.target:
            return None
        row = (await db.execute(text(
            """SELECT count(*), max(service_name) FROM server_service_inventory
               WHERE server_id = :sid
                 AND service_name = :target
                 AND lower(state) IN ('stopped','stop_pending','dead','failed')"""
        ), {"sid": server_id, "target": rule.target})).first()
        n = int(row[0] or 0)
        return n > 0, float(n), (f"{rule.target} stopped" if n else f"{rule.target} running")

    if metric == "host_process_down":
        if not rule.target:
            return None
        policy = (await db.execute(text(
            """SELECT p.metric_interval_s, p.upload_interval_s
               FROM agents a
               LEFT JOIN agent_policies p ON p.id = a.policy_id
               WHERE a.server_id = :sid
               ORDER BY a.last_heartbeat_at DESC NULLS LAST, a.created_at DESC
               LIMIT 1"""
        ), {"sid": server_id})).first()
        freshness_s = telemetry_freshness_seconds(
            policy[0] if policy else 30,
            policy[1] if policy else 60,
        )
        row = (await db.execute(text(
            """SELECT count(*) FROM server_process_inventory
               WHERE server_id = :sid AND lower(name) = lower(:target)
                 AND running = TRUE
                 AND updated_at >= NOW() - make_interval(secs => :ttl)"""
        ), {"sid": server_id, "target": rule.target, "ttl": freshness_s})).first()
        running = int(row[0] or 0) > 0
        return (not running), (0.0 if running else 1.0), (f"{rule.target} {'running' if running else 'not running'}")

    return None


def _message(rule, hostname: str, detail: str) -> str:
    return f"{rule.name}: {hostname} — {detail}"


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(rule, dimension: str | None) -> str:
    """The incident identity: one per rule + server (+ mount)."""
    return f"rule:{rule.id}" + (f":mount:{dimension}" if dimension else "")


def _rule_conditions(rule) -> list[dict]:
    """The rule's condition for its metric, with any reset_threshold."""
    conditions = rule.conditions
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except ValueError:
            conditions = None
    # The rule's own threshold/operator columns are authoritative (a generic
    # /alert-rules edit does not rebuild ``conditions``); the stored condition
    # only contributes its reset level.
    for c in conditions or []:
        if isinstance(c, dict) and c.get("metric") == rule.metric:
            return [{"metric": rule.metric, "operator": rule.operator or c.get("operator"),
                     "threshold": float(rule.threshold if rule.threshold is not None
                                        else (c.get("threshold") or 0)),
                     "reset_threshold": c.get("reset_threshold")}]
    return [{"metric": rule.metric, "operator": rule.operator,
             "threshold": float(rule.threshold or 0)}]


async def _open_alert_started(db: AsyncSession, server_id: str, dedupe: str):
    """When the open alert with this dedupe key was raised, or None."""
    return (await db.execute(text(
        """SELECT MIN(triggered_at) FROM alerts
           WHERE server_id = :sid AND status IN ('active', 'acknowledged')
             AND metadata->>'dedupe' = :dedupe"""
    ), {"sid": server_id, "dedupe": dedupe})).scalar()


async def _notify_rule(db: AsyncSession, rule, hostname: str, *, value: float | None = None,
                       detail: str = "", is_recovery: bool = False,
                       duration: str = "", alert_id=None, first_seen=None,
                       event: str | None = None) -> int:
    """Send a host metric alert, or its all-clear, in the shared house style.
    Returns how many channels accepted it."""
    sev = rule.severity or "warning"
    threshold = _float(rule.threshold)
    numeric = value is not None and rule.metric in THRESHOLD_METRICS
    reading = ap.format_reading(rule.metric, value, rule.operator, threshold) if numeric else ""
    now = datetime.now(timezone.utc)
    incident = ap.incident_context(alert_id=alert_id, first_seen=first_seen, now=now,
                                   lifecycle="recovered" if is_recovery else "new") if alert_id else {}
    v = ap.rule_phrasing(rule, hostname=hostname, is_recovery=is_recovery,
                         reading=reading, duration=duration,
                         value=value if numeric else None)
    v.update({"rule_name": rule.name or "Alert", "hostname": hostname,
              "ip_address": "", "severity": sev.upper(),
              "status": "RESOLVED" if is_recovery else "ALERT"})
    v.update(incident)
    return await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": _render(ap.DEFAULT_RECOVERY_EMAIL_SUBJECT if is_recovery
                           else ap.DEFAULT_EMAIL_SUBJECT, v),
        "body": _render(ap.DEFAULT_RECOVERY_EMAIL_BODY if is_recovery
                        else ap.DEFAULT_EMAIL_BODY, v),
        "message": _render(ap.DEFAULT_RECOVERY_SMS if is_recovery else ap.DEFAULT_SMS, v),
        "hostname": hostname, "ip_address": "",
        "status": "RESOLVED" if is_recovery else "ALERT",
        "severity": sev, "resolved": is_recovery,
        "headline_metric": ap.headline_metric(rule.metric, value, rule.operator, threshold)
                           if numeric else None,
        "details": [("Alert rule", rule.name),
                    ("Condition", v.get("condition_label")),
                    # `detail` carries what the evaluator measured — which
                    # filesystem, which service — and nothing else knows it.
                    ("Measured", detail),
                    ("Active for", duration if is_recovery else None)],
        "triggered_at": now.isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": event or ("recovery" if is_recovery else "trigger"),
        "is_recovery": is_recovery,
        **_funnel_fields(sev, getattr(rule, "priority", None), trigger=not is_recovery),
        **incident,
    })


def _funnel_fields(severity, rule_priority, *, trigger: bool) -> dict:
    from app.services import alert_funnel
    return alert_funnel.notification_fields(severity, rule_priority, trigger=trigger)


def _detail(rule, value: float | None, dimension: str | None) -> str:
    """What was measured, for the alert message and the Measured row."""
    if value is None:
        return "no current reading"
    shown = ap.format_reading(rule.metric, value, rule.operator, _float(rule.threshold))
    return f"{dimension} at {shown}" if dimension else shown


async def _stamp_state(db: AsyncSession, alert_id, meta: dict) -> None:
    await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) "
                          "|| CAST(:meta AS jsonb) WHERE id = :id"),
                     {"id": alert_id, "meta": json.dumps(meta)})


@dataclass
class _Open:
    """An open incident as the pass read it."""
    id: object
    notified: str | None          # raw metadata->>'notified'
    triggered_at: object
    status: str


async def _open_incidents(db: AsyncSession, rule) -> dict[tuple[str, str], _Open]:
    """{(server_id, dedupe): open incident} for one rule, read once per pass:
    ``rule:<id>`` and, for filesystems, every ``rule:<id>:mount:<m>``."""
    base = _dedupe(rule, None)
    rows = (await db.execute(text(
        """SELECT id, server_id::text, metadata->>'dedupe', metadata->>'notified',
                  triggered_at, status
           FROM alerts
           WHERE server_id IS NOT NULL AND status IN ('active', 'acknowledged')
             AND (metadata->>'dedupe' = :base OR starts_with(metadata->>'dedupe', :mounts))
           ORDER BY triggered_at"""
    ), {"base": base, "mounts": base + ":mount:"})).all()
    return {(r[1], r[2]): _Open(r[0], r[3], r[4], r[5]) for r in rows}


async def _lock(db: AsyncSession, rule, sid: str, dimension: str | None) -> None:
    """Serialize one rule/server/dimension across workers (released on commit)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"host:{rule.id}:{sid}:{dimension or ''}"})


async def _apply_state(db: AsyncSession, rule, sid: str, hostname: str, dimension: str | None,
                       breach: bool, value: float | None, detail: str, *, allowed: bool,
                       counts: dict, opened: _Open | None, extra: dict | None = None) -> None:
    """Raise, continue or resolve one rule/server/dimension incident.

    ``opened`` is the open incident the pass read (None = none). A new one is
    only inserted under the dimension's advisory lock, and create_server_alert
    re-checks for an open row under it; every send happens after a commit.
    """
    dedupe = _dedupe(rule, dimension)
    if breach:
        if opened is None:
            await _lock(db, rule, sid, dimension)
            meta = {"rule_id": str(rule.id), "metric": rule.metric,
                    "value": round(value, 2) if value is not None else None,
                    "threshold": float(rule.threshold or 0),
                    # A send in flight is neither pending nor notified, so no
                    # other pass announces it again (alert_notify_state).
                    **ns.sending_metadata(), "data_state": "current", **(extra or {})}
            if dimension:
                meta["mount"] = dimension
            new_id = await create_server_alert(
                db, sid, severity=rule.severity or "warning",
                message=_message(rule, hostname, detail),
                source="host_metric", dedupe=dedupe, metadata=meta,
            )
            if new_id:
                # The incident's priority is fixed when it is raised.
                from app.services import alert_funnel
                await alert_funnel.stamp_priority(db, new_id, getattr(rule, "priority", None),
                                                  rule.severity or "warning")
            await db.commit()
            if not new_id:
                return  # silenced, or another evaluator opened it first
            counts["raised"] += 1
            # Quiet hours: the alert is recorded above; only the outbound
            # notification waits for the rule's schedule window.
            if allowed:
                await _notify_rule(db, rule, hostname, value=value, detail=detail,
                                   alert_id=new_id,
                                   first_seen=await _open_alert_started(db, sid, dedupe))
            await ns.stamp(db, new_id, allowed)
            await db.commit()
            return
        await _stamp_state(db, opened.id, {"value": round(value, 2) if value is not None else None,
                                           "data_state": "current", **(extra or {})})
        if (opened.status == "active" and allowed and await ns.is_pending(db, opened.id)
                and not await server_condition_silenced(db, sid, dedupe)
                and await ns.claim_pending(db, opened.id)):
            # Trigger was suppressed by quiet hours and the host is still
            # breaching — announce it now the window has opened, otherwise the
            # breach is never reported at all. A snoozed condition stays quiet:
            # snoozing no longer closes the row. The claim (false -> sending)
            # is committed first, so no other pass sends it as well.
            await db.commit()
            await _notify_rule(db, rule, hostname, value=value, detail=detail,
                               alert_id=opened.id, first_seen=opened.triggered_at)
            await ns.stamp(db, opened.id, True)
            await db.commit()
        return
    if opened is None:
        return
    n = await resolve_server_alerts(db, sid, dedupe)
    counts["resolved"] += n
    await db.commit()
    # An all-clear for a page nobody received is noise; it follows the
    # trigger's fate, and says how long the host sat over the threshold.
    if n and ns.flag_means_notified(opened.notified) and getattr(rule, "recovery_alert", False):
        await _notify_rule(db, rule, hostname, value=value, detail=detail,
                           is_recovery=True, duration=ap.duration_between(opened.triggered_at),
                           alert_id=opened.id, first_seen=opened.triggered_at)


async def _adopt_legacy_mount_incident(db: AsyncSession, rule, sid: str,
                                       latest: dict[str, float], opened: dict) -> None:
    """Filesystem incidents used to be one per server (``rule:<id>``, the
    worst mount). Re-key an open one to the mount that is worst now, so an
    upgrade continues it instead of announcing that mount again."""
    base = _dedupe(rule, None)
    legacy = opened.get((sid, base))
    if legacy is None or not latest:
        return
    mount = max(latest, key=lambda m: latest[m])
    key = _dedupe(rule, mount)
    if (sid, key) in opened:
        return
    await _stamp_state(db, legacy.id, {"dedupe": key, "mount": mount, "legacy_dedupe": base})
    opened[(sid, key)] = opened.pop((sid, base))


_RULES_SQL = (
    "SELECT id, name, metric, operator, threshold, severity, min_duration, {clear}, "
    "notify_channels, server_id, target, recovery_alert, conditions, condition_logic, "
    "schedule_start, schedule_end, schedule_days, {prio} "
    "FROM alert_rules WHERE enabled = true AND metric LIKE 'host\\_%'"
)
_warned_missing_clear_duration = False


async def _load_rules(db: AsyncSession) -> list:
    """Enabled host rules. Before migrate-162 the clear hold is the default."""
    global _warned_missing_clear_duration
    from app.services import alert_funnel
    prio = await alert_funnel.rule_priority_column(db)
    try:
        return (await db.execute(text(_RULES_SQL.format(clear="clear_duration", prio=prio)))).all()
    except Exception as exc:
        if "clear_duration" not in str(exc):
            raise
        await db.rollback()
        if not _warned_missing_clear_duration:
            logger.warning("alert_rules.clear_duration missing — run migrate-162; "
                           "using the default clear hold")
            _warned_missing_clear_duration = True
        return (await db.execute(text(_RULES_SQL.format(clear="NULL AS clear_duration", prio=prio)))).all()


async def evaluate_host_rules(db: AsyncSession, *, now: float | None = None) -> dict[str, int]:
    rules = await _load_rules(db)
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    from app.services.network_conditions import clear_hold_seconds, evaluate, validate_conditions
    _tz = await get_configured_timezone(db)
    servers = await _servers(db)
    budgets = await _freshness_budgets(db)
    default_budget = telemetry_freshness_seconds(30, 60)
    max_budget = max([default_budget, *budgets.values()])
    now = datetime.now(timezone.utc).timestamp() if now is None else now

    history_cache: dict[tuple[str, int], dict | None] = {}
    counts = {"raised": 0, "resolved": 0}
    for rule in rules:
        allowed = notifications_allowed(
            getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
            getattr(rule, "schedule_days", None), _tz,
        )
        targets = [str(rule.server_id)] if rule.server_id else list(servers)

        opened = await _open_incidents(db, rule)

        if rule.metric not in THRESHOLD_METRICS:
            for sid in targets:
                res = await _current_value(db, rule, sid)
                if res is None:
                    continue
                breach, value, detail = res
                await _apply_state(db, rule, sid, servers.get(sid, "server"), None,
                                   breach, value, detail, allowed=allowed, counts=counts,
                                   opened=opened.get((sid, _dedupe(rule, None))))
            await db.commit()
            continue

        conditions = _rule_conditions(rule)
        try:
            validate_conditions(conditions)
        except ValueError as exc:
            logger.error("host rule %s skipped: %s", rule.id, exc)
            continue
        averaged = rule.metric in AVERAGED_METRICS
        window = max(int(rule.min_duration or 0), AVERAGE_WINDOW_S) if averaged else 0
        # Averaged metrics already carry their persistence in the window;
        # filesystem readings are judged per sample with min_duration as hold.
        hold = 0 if averaged else int(rule.min_duration or 0)
        clear_hold = clear_hold_seconds(getattr(rule, "clear_duration", None))
        lookback = max(window + max(hold, clear_hold) + 2 * min(max_budget, MAX_LOOKBACK_BUDGET_S),
                       MIN_LOOKBACK_S)
        cache_key = (rule.metric, lookback)
        if cache_key not in history_cache:
            try:
                history_cache[cache_key] = await asyncio.to_thread(
                    fetch_host_history, rule.metric, now - lookback, now)
            except Exception:
                logger.exception("host rule %s history unavailable; preserving alerts", rule.id)
                history_cache[cache_key] = None
        history = history_cache[cache_key]
        if history is None:
            continue

        for sid in targets:
            hostname = servers.get(sid, "server")
            fresh = budgets.get(sid, default_budget)
            series_by_dim = history.get(sid, {})
            if rule.metric == "host_filesystem_pct":
                if rule.target:
                    series_by_dim = {m: s for m, s in series_by_dim.items() if m == rule.target}
                latest = {m: s[-1][1] for m, s in series_by_dim.items()
                          if s and s[-1][1] is not None and s[-1][0] + fresh > now}
                await _adopt_legacy_mount_incident(db, rule, sid, latest, opened)
                # Mounts with samples, plus any with an open incident whose
                # samples stopped (they are stamped unknown, never resolved).
                dims = sorted(set(series_by_dim) | {
                    d.split(":mount:", 1)[1] for (osid, d) in opened
                    if osid == sid and ":mount:" in d})
            else:
                dims = [None]
            for dim in dims:
                series = series_by_dim.get(dim or "", [])
                if averaged:
                    series = rolling_mean(series, window)
                current = opened.get((sid, _dedupe(rule, dim)))
                result = evaluate(conditions, "AND", {rule.metric: series}, now=now, hold=hold,
                                  max_gap=fresh, active=current is not None,
                                  clear_hold=clear_hold)
                value = result.values.get(rule.metric)
                if result.breach is None:
                    # Unknown, a breach still inside its hold, or a clear
                    # still inside its clear hold: neither a trigger nor a
                    # recovery. An open incident keeps going.
                    if current is not None:
                        await _stamp_state(db, current.id, {
                            "data_state": result.reason,
                            **({"value": round(value, 2)} if value is not None else {}),
                            "cleared_seconds": round(result.cleared_seconds)})
                    continue
                await _apply_state(db, rule, sid, hostname, dim, result.breach, value,
                                   _detail(rule, value, dim), allowed=allowed, counts=counts,
                                   opened=current,
                                   extra={"held_seconds": round(result.held_seconds)})
        await db.commit()

    if counts["raised"] or counts["resolved"]:
        logger.info("host alert eval: %d rules, %d raised, %d resolved",
                    len(rules), counts["raised"], counts["resolved"])
    return {"rules": len(rules), **counts}


async def run_host_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import HOST_ALERT_PASS, run_pass
    return await run_pass(HOST_ALERT_PASS, evaluate_host_rules, session_factory)


async def host_alert_evaluator_loop() -> None:
    await asyncio.sleep(20)  # let the app boot and first metrics land
    while True:
        try:
            await run_host_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("host alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
