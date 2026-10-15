from app.services.notification_transport import NotificationHTTPClient
from app.services.sms_transport import send_sms_request
from app.core.crypto import encrypt_config, decrypt_config
import ssl
import json
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.services import alert_rule_scope as rs
from app.services.audit_service import write_audit_log

# Rule and routing changes are policy administration, separate from triage
# (Alerts review A05): alerts.manage, plus the tag-scope guard per rule.
require_rule_manager = require_permission("alerts.manage")
from app.models.user import User
from app.services import alert_phrasing as ap
from app.services import netflow_alert_service as nfa

router = APIRouter(prefix="/alert-rules", tags=["Alert Rules"])


def _validate_network_contract(metric, conditions):
    from app.services.network_conditions import supported, UNMAPPED_METRICS, validate_conditions
    items = conditions or [dict(metric=metric, operator='>', threshold=0)]
    if any(c.get('reset_threshold') is not None and not supported(c.get('metric')) for c in items):
        raise HTTPException(status_code=422, detail='Reset thresholds require a network metric')
    if any(supported(c.get('metric')) or c.get('metric') in UNMAPPED_METRICS for c in items):
        try:
            validate_conditions(items)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def _merge_condition_update(current, fields):
    """A partial flat edit must not leave a different compound rule active."""
    merged = dict(fields)
    conditions = merged.get('conditions', current.get('conditions'))
    if isinstance(conditions, str):
        conditions = json.loads(conditions)
    if conditions:
        conditions = [dict(c) for c in conditions]
        if 'conditions' not in merged:
            for key in ('metric', 'operator', 'threshold'):
                if key in merged:
                    conditions[0][key] = merged[key]
        merged.update({key: conditions[0][key] for key in ('metric', 'operator', 'threshold')})
        merged['conditions'] = conditions
    metric = merged.get('metric', current.get('metric'))
    _validate_network_contract(metric, conditions)
    if metric == 'syslog':
        threshold = merged.get('threshold', current.get('threshold', 0))
        if threshold is None or not 0 <= threshold <= 7 or int(threshold) != threshold:
            raise HTTPException(status_code=422, detail='Syslog severity must be an integer from 0 to 7')
        merged['recovery_alert'] = False
        merged['min_duration'] = 0
    return merged


# NetFlow rules keep their scope in flow_scope; the device-family scope
# columns mean nothing for them and would confuse the tag-scope guard.
_NETFLOW_CLEARED = ("device_id", "group_id", "service_check_id", "service_check_group_id",
                    "device_type", "location", "scope_tag", "trap_oid", "target")


async def _netflow_definition(metric, operator, threshold, flow_scope, flow_params):
    """(operator, threshold, canonical scope, stored params) for a NetFlow rule;
    422 with the reason otherwise. Finding rules are on/off: == 1."""
    if metric == nfa.FINDING:
        operator, threshold = "==", 1.0
    try:
        scope, params = await nfa.validate_definition(metric, operator, threshold, flow_scope, flow_params)
    except nfa.RuleDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ap.normalize_operator(operator), float(threshold), scope, params


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

# Network-device (SNMP) metric keys, evaluated by network_alert_service.
_NETWORK_METRICS = (
    "cpu|memory|uptime_reset|temperature|fan_state|psu_state|"
    "if_in_bps|if_out_bps|if_util_pct|if_errors|if_discards|if_oper_status|"
    "session_count|vpn_tunnel_state|ha_state|bgp_neighbor_down"
)
# APM and browser-RUM metric keys. Server RED keys are scoped by service; RUM
# keys are scoped by browser application ID. Rates are fractions in [0,1].
_APM_METRICS = (
    "apm_latency_p50|apm_latency_p95|apm_latency_p99|"
    "apm_error_rate|apm_throughput|apm_apdex|"
    "apm_rum_lcp_p75|apm_rum_inp_p75|apm_rum_cls_p75|"
    "apm_rum_error_session_rate|apm_rum_resource_failure_rate|apm_rum_new_error_groups|"
    "apm_slo_burn|apm_synthetic_down|apm_anomaly"
)
# User Device Tracker metric keys (migrate-060 CHECK), evaluated by
# udt_alert_service. The first four are event-driven (threshold acts as an
# on/off toggle); udt_port_capacity_pct is a numeric percentage with
# raise/resolve.
_UDT_METRICS = (
    "udt_new_endpoint|udt_rogue_endpoint|udt_watch_endpoint|"
    "udt_endpoint_moved|udt_port_capacity_pct"
)
# NetPath (WAN/Internet path) metric keys (migrate-076 CHECK), evaluated by
# netpath_alert_service. netpath_rtt / netpath_loss / netpath_hop_count are
# numeric with raise/resolve; netpath_path_change / netpath_unreachable are
# event-driven (threshold acts as an on/off toggle). Scope: `target` = probe
# name (empty = all probes).
_NETPATH_METRICS = (
    "netpath_rtt|netpath_loss|netpath_hop_count|"
    "netpath_path_change|netpath_unreachable"
)
# Monitoring-template metric keys (migrate-062 CHECK allows the tpl_ prefix):
# any series a template emits (tpl_<metric key>), evaluated per device by
# network_alert_service._eval_template against ALL matching instance series.
_TPL_METRICS = r"tpl_[a-z0-9_]{1,60}"
# NetFlow metric keys (migrate-143 CHECK), evaluated by netflow_alert_service.
# Scope = alert_rules.flow_scope (FlowFilter, canonical keys); parameters
# (window, band, hold/clear evaluations, flap suppression, per-metric options)
# = alert_rules.flow_params. One condition per rule (no compound conditions).
_NETFLOW_METRICS = nfa.METRIC_PATTERN
_CONDITION_METRICS = f"ping_status|rtt|packet_loss|jitter|service_status|{_NETWORK_METRICS}|{_APM_METRICS}|{_UDT_METRICS}|{_NETPATH_METRICS}|{_TPL_METRICS}|{_NETFLOW_METRICS}"


class ConditionItem(BaseModel):
    metric: str = Field(..., pattern=f"^({_CONDITION_METRICS})$")
    operator: str = Field(..., pattern="^(>|<|>=|<=|==|!=)$")
    threshold: float = Field(..., allow_inf_nan=False)
    reset_threshold: Optional[float] = Field(None, allow_inf_nan=False)


class EscalationLevel(BaseModel):
    """One SLA escalation tier: if the alert is still active and unacknowledged
    ``after_minutes`` after triggering, page ``notify_channels``. An optional
    ``repeat_every_minutes`` re-pages this tier on that cadence until
    ack/resolve (capped by the rule's max_repeat; 0 = unlimited)."""
    after_minutes: int = Field(..., ge=1, le=10080)
    notify_channels: list[str] = Field(..., min_length=1, max_length=20)
    repeat_every_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


def _validated_escalation(levels: Optional[list[EscalationLevel]]) -> Optional[list[dict]]:
    """Normalise and sanity-check escalation tiers; None when empty.

    Tiers are stored sorted by deadline and must not share one — two tiers due
    at the same minute is one tier with more channels, and the sweeper's
    "highest due level wins" rule would silently swallow the lower one.
    """
    if not levels:
        return None
    if len(levels) > 5:
        raise HTTPException(status_code=422, detail="At most 5 escalation levels")
    out = sorted((lv.model_dump() for lv in levels), key=lambda lv: lv["after_minutes"])
    deadlines = [lv["after_minutes"] for lv in out]
    if len(set(deadlines)) != len(deadlines):
        raise HTTPException(status_code=422,
                            detail="Escalation levels must have distinct delays")
    return out


class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True

    metric: str = Field(..., pattern=f"^({_CONDITION_METRICS}|trap|syslog)$")
    operator: str = Field(..., pattern="^(>|<|>=|<=|==|!=)$")
    threshold: float = Field(..., allow_inf_nan=False)

    # E2: optional trap-OID filter for metric='trap' rules (empty = any trap).
    trap_oid: Optional[str] = None

    # Per-interface scope for network metrics (if_*): interface name/descr/alias
    # substring or exact if_index. Empty = all monitored interfaces on the device.
    target: Optional[str] = None

    # E1: optional compound conditions. When non-empty these are the source of
    # truth (combined by condition_logic); the flat metric/operator/threshold
    # above are kept in sync with conditions[0] for backward compatibility.
    conditions: Optional[list[ConditionItem]] = None
    condition_logic: str = Field(default="AND", pattern="^(AND|OR)$")

    trigger_on: str = Field(default="any", pattern="^(any|down|up|degraded)$")
    recovery_alert: bool = False

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    service_check_id: Optional[UUID] = None
    service_check_group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None
    scope_tag: Optional[str] = Field(default=None, max_length=120)

    severity: str = Field(default="warning", pattern="^(info|warning|critical)$")
    notify_channels: list[str] = Field(default_factory=list)
    cooldown: int = Field(default=300, ge=0)
    min_duration: int = Field(default=0, ge=0)
    max_repeat: int = Field(default=0, ge=0)
    # Device status rules: notify at once even while the device is flapping
    # (by default a flapping device's triggers wait for its status to settle).
    notify_while_flapping: bool = False

    # SLA escalation tiers (see EscalationLevel). None/empty = no escalation.
    escalation_levels: Optional[list[EscalationLevel]] = None

    # NetFlow rules only (metric netflow_*): FlowFilter scope and parameters.
    flow_scope: Optional[dict] = None
    flow_params: Optional[dict] = None

    schedule_start: Optional[str] = None  # HH:MM
    schedule_end: Optional[str] = None    # HH:MM
    schedule_days: Optional[list[int]] = None  # 1-7 (Mon-Sun)

    # Message templates
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None

    metric: Optional[str] = Field(None, pattern=f"^({_CONDITION_METRICS}|trap|syslog)$")
    operator: Optional[str] = Field(None, pattern="^(>|<|>=|<=|==|!=|eq|neq|gt|lt|gte|lte)$")
    threshold: Optional[float] = Field(None, allow_inf_nan=False)
    trap_oid: Optional[str] = None
    target: Optional[str] = None

    conditions: Optional[list[ConditionItem]] = None
    condition_logic: Optional[str] = Field(None, pattern="^(AND|OR)$")

    trigger_on: Optional[str] = Field(None, pattern="^(any|down|up|degraded)$")
    recovery_alert: Optional[bool] = None

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    service_check_id: Optional[UUID] = None
    service_check_group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None
    scope_tag: Optional[str] = Field(default=None, max_length=120)

    severity: Optional[str] = Field(None, pattern="^(info|warning|critical)$")
    notify_channels: Optional[list[str]] = None
    cooldown: Optional[int] = Field(None, ge=0)
    min_duration: Optional[int] = Field(None, ge=0)
    max_repeat: Optional[int] = Field(None, ge=0)
    notify_while_flapping: Optional[bool] = None

    escalation_levels: Optional[list[EscalationLevel]] = None

    flow_scope: Optional[dict] = None
    flow_params: Optional[dict] = None

    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    schedule_days: Optional[list[int]] = None

    # Message templates
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


# The six message templates, in the order the editor shows them.
_TEMPLATE_FIELDS = (
    "email_subject", "email_body", "sms_template",
    "recovery_email_subject", "recovery_email_body", "recovery_sms_template",
)


class AlertRuleDraft(BaseModel):
    """A rule still being composed — enough of one to render its notifications.

    Everything is optional and unknown keys are ignored, so the wizard can post
    its whole in-progress payload (scope ids, channels, schedule) and get back
    the mail those settings would produce without saving anything.
    """
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    severity: str = "warning"
    metric: Optional[str] = "ping_status"
    operator: Optional[str] = None
    threshold: Optional[float] = None
    conditions: Optional[list] = None
    condition_logic: Optional[str] = "AND"
    trigger_on: Optional[str] = None
    recovery_alert: bool = False
    device_type: Optional[str] = None
    location: Optional[str] = None
    scope_tag: Optional[str] = None
    group_id: Optional[str] = None
    service_check_id: Optional[str] = None
    service_check_group_id: Optional[str] = None
    target: Optional[str] = None
    trap_oid: Optional[str] = None
    flow_scope: Optional[dict] = None
    flow_params: Optional[dict] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


class AlertRulePreview(BaseModel):
    """Unsaved template edits to render the preview with (all optional)."""
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Columns to SELECT in list / get queries
_RULE_COLUMNS = (
    "id, name, description, enabled, metric, operator, threshold, duration, "
    "device_id, group_id, service_check_id, service_check_group_id, "
    "severity, notify_channels, cooldown, "
    "device_type, location, scope_tag, trigger_on, recovery_alert, "
    "min_duration, max_repeat, notify_while_flapping, escalation_levels, "
    "schedule_start, schedule_end, schedule_days, "
    "email_subject, email_body, sms_template, "
    "recovery_email_subject, recovery_email_body, recovery_sms_template, "
    "conditions, condition_logic, trap_oid, target, flow_scope, flow_params, "
    "created_at, updated_at, created_by"
)


def _row_to_dict(row) -> dict:
    schedule_start = row.schedule_start
    schedule_end = row.schedule_end
    if schedule_start is not None and not isinstance(schedule_start, str):
        schedule_start = schedule_start.strftime("%H:%M")
    if schedule_end is not None and not isinstance(schedule_end, str):
        schedule_end = schedule_end.strftime("%H:%M")

    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "metric": row.metric,
        "operator": row.operator,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "duration": row.duration,
        "device_id": str(row.device_id) if row.device_id else None,
        "group_id": str(row.group_id) if row.group_id else None,
        "service_check_id": str(row.service_check_id) if row.service_check_id else None,
        "service_check_group_id": str(row.service_check_group_id) if row.service_check_group_id else None,
        "severity": row.severity,
        "notify_channels": row.notify_channels if row.notify_channels else [],
        "cooldown": row.cooldown,
        "device_type": row.device_type,
        "location": row.location,
        "scope_tag": row.scope_tag,
        "trigger_on": row.trigger_on,
        "recovery_alert": row.recovery_alert,
        "min_duration": row.min_duration,
        "max_repeat": row.max_repeat,
        "notify_while_flapping": bool(getattr(row, "notify_while_flapping", False)),
        "escalation_levels": getattr(row, "escalation_levels", None) or [],
        "schedule_start": schedule_start,
        "schedule_end": schedule_end,
        "schedule_days": row.schedule_days if row.schedule_days else [],
        "email_subject": row.email_subject,
        "email_body": row.email_body,
        "sms_template": row.sms_template,
        "recovery_email_subject": row.recovery_email_subject,
        "recovery_email_body": row.recovery_email_body,
        "recovery_sms_template": row.recovery_sms_template,
        "conditions": getattr(row, "conditions", None) or None,
        "condition_logic": getattr(row, "condition_logic", None) or "AND",
        "trap_oid": getattr(row, "trap_oid", None),
        "target": getattr(row, "target", None),
        "flow_scope": getattr(row, "flow_scope", None),
        "flow_params": getattr(row, "flow_params", None),
        "family": rs.family({"metric": row.metric,
                             "service_check_id": row.service_check_id,
                             "service_check_group_id": row.service_check_group_id}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
    }


def _normalized_templates(submitted: dict, kind: str) -> dict:
    """Submitted template fields, with "same as the default" reduced to NULL."""
    defaults = ap.default_templates(kind)
    return {
        name: ap.normalize_stored_template(submitted.get(name), defaults.get(name))
        for name in _TEMPLATE_FIELDS
    }


def _with_templates(rule: dict) -> dict:
    """Attach the message text this rule actually sends.

    The six template columns are NULL on almost every rule — that is how a rule
    tracks the built-in wording instead of freezing a copy of it. But a NULL is
    not something an editor can show, so the rule carries its resolved text
    alongside: ``template_kind`` names the default set, ``effective_templates``
    is what will be sent per field, and ``default_templates`` is what "reset to
    default" restores. The stored columns stay exactly as they are, so the
    editor can still tell customised from default.
    """
    kind = ap.template_kind(rule)
    rule["template_kind"] = kind
    rule["default_templates"] = ap.default_templates(kind)
    rule["effective_templates"] = ap.effective_templates(rule, kind)
    return rule


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/template-defaults")
async def get_template_defaults(user: User = Depends(get_current_user)):
    """The built-in message templates, per rule kind.

    A rule being created has no id to preview against, and the wizard switches
    kind as the user picks a source — so it reads the whole map once and shows
    the set matching the current source. Keeping the text here rather than in
    the client is what stops the editor advertising device wording on a
    service-check rule.
    """
    return {kind: ap.default_templates(kind) for kind in ("device", "service", "trap", "syslog")}


# ---------------------------------------------------------------------------
# NetFlow rule family (CR-07)
# ---------------------------------------------------------------------------

@router.get("/netflow/catalog")
async def netflow_rule_catalog(user: User = Depends(get_current_user)):
    """What the wizard offers for NetFlow rules: metrics with their units,
    operators, defaults and which FlowFilter keys each may be scoped by, plus
    the application and IP-group names a scope may name."""
    from app.services import netflow_classify, netflow_groups

    applications: list[str] = []
    groups: list[str] = []
    try:
        await netflow_classify.load_rules()
        await netflow_groups.load_groups()
        applications = sorted(netflow_classify.known_applications())
        groups = sorted(netflow_groups.group_names())
    except Exception:  # names are suggestions; the server still validates
        pass
    return {
        "applications": applications,
        "groups": groups,
        "metrics": [
            {"metric": key, **info, "scope_keys": sorted(nfa._SCOPE_KEYS_BY_METRIC[key])}
            for key, info in nfa.METRIC_INFO.items()
        ],
        "finding_severities": list(nfa.FINDING_SEVERITIES),
        "finding_confidences": list(nfa.FINDING_CONFIDENCES),
        "eval_interval_s": nfa.EVAL_INTERVAL_S,
    }


class NetflowRuleDraft(BaseModel):
    """A NetFlow rule condition to test against live flow data."""
    model_config = ConfigDict(extra="ignore")

    metric: str = Field(..., pattern=f"^({nfa.METRIC_PATTERN})$")
    operator: str = Field(">", pattern="^(>|<|>=|<=|==|!=|eq|neq|gt|lt|gte|lte)$")
    threshold: float = Field(0, allow_inf_nan=False)
    flow_scope: Optional[dict] = None
    flow_params: Optional[dict] = None


async def _netflow_visibility(db: AsyncSession, user: User):
    from app.api.v1.netflow.access import VIEW_PERMISSION, visibility_for_user

    vis = await visibility_for_user(db, user)
    if not vis.can(VIEW_PERMISSION):
        raise HTTPException(status_code=403, detail="Viewing NetFlow data needs the netflow.view permission")
    return vis


@router.post("/netflow/evaluate")
async def evaluate_netflow_rule_draft(
    draft: NetflowRuleDraft,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dry-run a NetFlow condition against live flow data -- the readings the
    evaluator would see now, restricted to the caller's flow visibility.
    Nothing is stored and no alert is raised."""
    vis = await _netflow_visibility(db, user)
    op, thr, scope, params = await _netflow_definition(
        draft.metric, draft.operator, draft.threshold, draft.flow_scope, draft.flow_params)
    for key in ("exporter",):
        if scope.get(key) and not vis.allows(scope[key]):
            raise HTTPException(status_code=404, detail="Exporter not found")
    iface = scope.get("iface") or ""
    if "@" in iface and not vis.allows(iface.split("@", 1)[1]):
        raise HTTPException(status_code=404, detail="Exporter not found")
    rule = nfa.rule_from_row({"id": "draft", "name": "Draft", "metric": draft.metric, "operator": op,
                              "threshold": thr, "flow_scope": scope, "flow_params": params})
    ev = await nfa.evaluate(rule, vis=vis, db=db)
    return nfa.evaluation_payload(rule, ev)


@router.get("")
async def list_alert_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules ORDER BY created_at DESC")
    )
    rows = result.fetchall()
    return {"data": [_with_templates(_row_to_dict(r)) for r in rows]}


@router.post("", status_code=201)
async def create_alert_rule(
    data: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    now = datetime.now(timezone.utc)
    await rs.guard_rule_change(db, user, data.model_dump())

    # E1: when compound conditions are supplied, they are authoritative and the
    # flat metric/operator/threshold columns mirror conditions[0] (those columns
    # are NOT NULL and keep older clients / the legacy engine path working).
    conditions = [c.model_dump() for c in data.conditions] if data.conditions else None
    metric, operator, threshold = data.metric, data.operator, data.threshold
    if conditions:
        metric = conditions[0]["metric"]
        operator = conditions[0]["operator"]
        threshold = conditions[0]["threshold"]

    _validate_network_contract(metric, conditions)
    if metric == 'syslog':
        _merge_condition_update({'metric': metric, 'threshold': threshold}, {})

    flow_scope = flow_params = None
    cleared: set[str] = set()
    if nfa.is_netflow_metric(metric) or any(nfa.is_netflow_metric(c["metric"]) for c in conditions or []):
        if conditions:
            raise HTTPException(status_code=422,
                                detail="NetFlow rules take a single condition (no compound conditions)")
        operator, threshold, flow_scope, flow_params = await _netflow_definition(
            metric, operator, threshold, data.flow_scope, data.flow_params)
        cleared = set(_NETFLOW_CLEARED)
    elif data.flow_scope or data.flow_params:
        raise HTTPException(status_code=422, detail="flow_scope and flow_params only apply to NetFlow metrics")

    params: dict = {
        "name": data.name,
        "description": data.description,
        "enabled": data.enabled,
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
        "conditions": json.dumps(conditions) if conditions else None,
        "condition_logic": data.condition_logic or "AND",
        "trap_oid": (data.trap_oid or None),
        "target": (data.target or None),
        "flow_scope": json.dumps(flow_scope) if flow_scope is not None else None,
        "flow_params": json.dumps(flow_params) if flow_params is not None else None,
        "duration": 0 if metric == 'syslog' else data.min_duration,
        "device_id": data.device_id,
        "group_id": data.group_id,
        "service_check_id": data.service_check_id,
        "service_check_group_id": data.service_check_group_id,
        "severity": data.severity,
        "notify_channels": json.dumps(data.notify_channels),
        "cooldown": data.cooldown,
        "device_type": data.device_type,
        "location": data.location,
        "scope_tag": (data.scope_tag or "").strip() or None,
        "trigger_on": data.trigger_on,
        "recovery_alert": False if metric == 'syslog' else data.recovery_alert,
        "min_duration": 0 if metric == 'syslog' else data.min_duration,
        "max_repeat": data.max_repeat,
        "notify_while_flapping": data.notify_while_flapping,
        "escalation_levels": json.dumps(_validated_escalation(data.escalation_levels))
                             if data.escalation_levels else None,
        "schedule_start": data.schedule_start,
        "schedule_end": data.schedule_end,
        "schedule_days": json.dumps(data.schedule_days) if data.schedule_days else None,
        # The editors show the default as real, editable text, so text that
        # matches it is posted back untouched — store NULL for those so the
        # rule keeps tracking the built-in wording (see normalize_stored_template).
        **_normalized_templates(
            {f: getattr(data, f, None) for f in _TEMPLATE_FIELDS},
            ap.template_kind({"metric": metric,
                              "service_check_id": data.service_check_id,
                              "service_check_group_id": data.service_check_group_id}),
        ),
        "created_at": now,
        "updated_at": now,
        "created_by": user.id,
    }
    for key in cleared:
        params[key] = None
    if cleared:
        params["trigger_on"] = "any"

    result = await db.execute(
        text(
            "INSERT INTO alert_rules "
            "(name, description, enabled, metric, operator, threshold, duration, "
            "conditions, condition_logic, trap_oid, target, flow_scope, flow_params, "
            "device_id, group_id, service_check_id, service_check_group_id, "
            "severity, notify_channels, cooldown, "
            "device_type, location, scope_tag, trigger_on, recovery_alert, "
            "min_duration, max_repeat, notify_while_flapping, escalation_levels, "
            "schedule_start, schedule_end, schedule_days, "
            "email_subject, email_body, sms_template, "
            "recovery_email_subject, recovery_email_body, recovery_sms_template, "
            "created_at, updated_at, created_by) "
            "VALUES "
            "(:name, :description, :enabled, :metric, :operator, :threshold, :duration, "
            "CAST(:conditions AS jsonb), :condition_logic, :trap_oid, :target, "
            "CAST(:flow_scope AS jsonb), CAST(:flow_params AS jsonb), "
            ":device_id, :group_id, :service_check_id, :service_check_group_id, "
            ":severity, CAST(:notify_channels AS jsonb), :cooldown, "
            ":device_type, :location, :scope_tag, :trigger_on, :recovery_alert, "
            ":min_duration, :max_repeat, :notify_while_flapping, CAST(:escalation_levels AS jsonb), "
            "CAST(:schedule_start AS time), CAST(:schedule_end AS time), "
            "CAST(:schedule_days AS jsonb), "
            ":email_subject, :email_body, :sms_template, "
            ":recovery_email_subject, :recovery_email_body, :recovery_sms_template, "
            ":created_at, :updated_at, :created_by) "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create alert rule")
    created = _row_to_dict(row)
    await write_audit_log(db, actor=user, action="alert_rule.create", resource_type="alert_rule",
                          resource_id=created["id"],
                          metadata={"name": created["name"],
                                    "scope": rs.describe_scope(created)["label"],
                                    "fields": rs.rule_changes({}, created)})
    await db.commit()
    return _with_templates(created)


@router.get("/{rule_id}")
async def get_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _with_templates(_row_to_dict(row))


@router.put("/{rule_id}")
async def update_alert_rule(
    rule_id: UUID,
    data: AlertRuleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    fields = data.model_dump(exclude_unset=True)
    # NOT NULL column: an explicit null means "leave it as it is".
    if fields.get("notify_while_flapping", False) is None:
        del fields["notify_while_flapping"]
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    before = await rs.rule_row(db, rule_id)
    if not before:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    await rs.guard_rule_change(db, user, before, {**before, **fields})

    # E1: keep flat metric/operator/threshold mirrored to conditions[0] when a
    # non-empty conditions array is supplied (empty array reverts to legacy).
    if fields.get("conditions"):
        c0 = fields["conditions"][0]
        fields["metric"] = c0["metric"]
        fields["operator"] = c0["operator"]
        fields["threshold"] = c0["threshold"]

    if any(k in fields for k in ('metric', 'operator', 'threshold', 'conditions')):
        current_rule = (await db.execute(text(
            'SELECT metric, operator, threshold, conditions FROM alert_rules WHERE id = :id'),
            {'id': rule_id})).mappings().first()
        if not current_rule:
            raise HTTPException(status_code=404, detail='Alert rule not found')
        fields = _merge_condition_update(current_rule, fields)

    # NetFlow rules: validate the merged definition; a changed definition
    # makes the old subjects meaningless, so their state and open alerts go.
    netflow_reset = False
    merged_metric = fields.get("metric", before.get("metric"))
    was_netflow = nfa.is_netflow_metric(before.get("metric"))
    if nfa.is_netflow_metric(merged_metric):
        if fields.get("conditions"):
            raise HTTPException(status_code=422,
                                detail="NetFlow rules take a single condition (no compound conditions)")
        op, thr, scope, nf_params = await _netflow_definition(
            merged_metric,
            fields.get("operator", before.get("operator")),
            fields.get("threshold", before.get("threshold")),
            fields["flow_scope"] if "flow_scope" in fields else _json_value(before.get("flow_scope")),
            fields["flow_params"] if "flow_params" in fields else _json_value(before.get("flow_params")),
        )
        new_def = (merged_metric, op, thr, scope, nf_params)
        old_def = (before.get("metric"), ap.normalize_operator(before.get("operator")),
                   float(before.get("threshold") or 0), _json_value(before.get("flow_scope")) or {},
                   _json_value(before.get("flow_params")) or {})
        fields.update({"metric": merged_metric, "operator": op, "threshold": thr,
                       "flow_scope": scope, "flow_params": nf_params, "conditions": None,
                       "trigger_on": "any"})
        for key in _NETFLOW_CLEARED:
            fields[key] = None
        netflow_reset = was_netflow and new_def != old_def
        if fields.get("enabled") is False and before.get("enabled"):
            netflow_reset = True
    else:
        if fields.get("flow_scope") or fields.get("flow_params"):
            raise HTTPException(status_code=422,
                                detail="flow_scope and flow_params only apply to NetFlow metrics")
        if was_netflow:
            fields["flow_scope"] = None
            fields["flow_params"] = None
            netflow_reset = True

    # Templates equal to the built-in default are stored as NULL, so a rule
    # keeps tracking the wording rather than freezing a copy the editor merely
    # displayed. Which default applies depends on the rule's kind, which this
    # request may itself be changing — so classify the merged rule.
    if any(f in fields for f in _TEMPLATE_FIELDS):
        current = (await db.execute(
            text("SELECT metric, service_check_id, service_check_group_id "
                 "FROM alert_rules WHERE id = :id"),
            {"id": rule_id},
        )).first()
        if not current:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        kind = ap.template_kind({
            "metric": fields.get("metric", current.metric),
            "service_check_id": fields.get("service_check_id", current.service_check_id),
            "service_check_group_id": fields.get(
                "service_check_group_id", current.service_check_group_id),
        })
        defaults = ap.default_templates(kind)
        for name in _TEMPLATE_FIELDS:
            if name in fields:
                fields[name] = ap.normalize_stored_template(fields[name], defaults.get(name))

    set_parts = []
    params: dict = {"id": rule_id, "updated_at": datetime.now(timezone.utc)}

    # Special-case columns that need casts
    _jsonb_cols = {"notify_channels", "schedule_days", "flow_scope", "flow_params"}
    _time_cols = {"schedule_start", "schedule_end"}

    for key, value in fields.items():
        if key == "conditions":
            set_parts.append("conditions = CAST(:conditions AS jsonb)")
            params["conditions"] = json.dumps(value) if value else None
        elif key == "escalation_levels":
            # Empty list clears escalation; validation re-parses because
            # exclude_unset dumps gave us plain dicts.
            levels = [EscalationLevel(**lv) for lv in (value or [])]
            set_parts.append("escalation_levels = CAST(:escalation_levels AS jsonb)")
            validated = _validated_escalation(levels)
            params["escalation_levels"] = json.dumps(validated) if validated else None
        elif key in _jsonb_cols:
            set_parts.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(value) if value is not None else None
        elif key in _time_cols:
            set_parts.append(f"{key} = CAST(:{key} AS time)")
            params[key] = value
        elif key == "device_id" or key == "group_id":
            set_parts.append(f"{key} = :{key}")
            params[key] = value
        elif key == "scope_tag":
            # "" means the user cleared tag scope; store NULL so the engine's
            # `if rule.scope_tag` guard treats it as unscoped rather than
            # matching devices against an empty tag name.
            set_parts.append(f"{key} = :{key}")
            params[key] = (value or "").strip() or None
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    # Keep legacy duration column in sync with min_duration
    if "min_duration" in fields:
        set_parts.append("duration = :min_duration_dup")
        params["min_duration_dup"] = fields["min_duration"]

    set_parts.append("updated_at = :updated_at")
    set_clause = ", ".join(set_parts)

    result = await db.execute(
        text(
            f"UPDATE alert_rules SET {set_clause} "
            f"WHERE id = :id "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    closed = await nfa.reset_rule(db, str(rule_id)) if netflow_reset else 0
    after = await rs.rule_row(db, rule_id)
    await write_audit_log(db, actor=user, action="alert_rule.update", resource_type="alert_rule",
                          resource_id=str(rule_id),
                          metadata={"name": after.get("name") if after else None,
                                    "changes": rs.rule_changes(before, after),
                                    "affected": await rs.affected(db, after or before),
                                    **({"netflow_alerts_closed": closed} if netflow_reset else {})})
    await db.commit()
    return _with_templates(_row_to_dict(row))


@router.delete("/{rule_id}", status_code=204)
async def delete_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    before = await rs.rule_row(db, rule_id)
    if not before:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    await rs.guard_rule_change(db, user, before)
    affected = await rs.affected(db, before)
    if nfa.is_netflow_metric(before.get("metric")):
        # Nothing would ever evaluate (and so resolve) them once the rule is gone.
        await nfa.reset_rule(db, str(rule_id), reason="rule_deleted")
    result = await db.execute(
        text("DELETE FROM alert_rules WHERE id = :id RETURNING id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    await write_audit_log(db, actor=user, action="alert_rule.delete", resource_type="alert_rule",
                          resource_id=str(rule_id),
                          metadata={"name": before.get("name"), "affected": affected,
                                    "rule": rs.rule_changes({}, before)})
    await db.commit()


class ToggleBody(BaseModel):
    """Explicit target state (a stale page cannot flip a rule the wrong way)
    and an optional reason for the audit trail."""
    enabled: Optional[bool] = None
    reason: Optional[str] = Field(default=None, max_length=500)


@router.get("/{rule_id}/impact")
async def alert_rule_impact(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Who a change to this rule affects: scope, target count, open alerts."""
    rule = await rs.rule_row(db, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return await rs.impact(db, rule)


@router.get("/{rule_id}/netflow-status")
async def netflow_rule_status(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Last evaluation of a NetFlow rule per subject (value, streaks, flapping,
    unknown reasons), restricted to the caller's flow visibility."""
    rule = await rs.rule_row(db, rule_id)
    if not rule or not nfa.is_netflow_metric(rule.get("metric")):
        raise HTTPException(status_code=404, detail="NetFlow alert rule not found")
    vis = await _netflow_visibility(db, user)
    return await nfa.rule_status(db, str(rule_id), vis)


@router.post("/{rule_id}/toggle")
async def toggle_alert_rule(
    rule_id: UUID,
    body: Optional[ToggleBody] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    before = await rs.rule_row(db, rule_id)
    if not before:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    await rs.guard_rule_change(db, user, before)
    target = (not before["enabled"]) if body is None or body.enabled is None else body.enabled
    result = await db.execute(
        text(
            "UPDATE alert_rules SET enabled = :enabled, updated_at = :now "
            "WHERE id = :id "
            "RETURNING id, name, enabled"
        ),
        {"id": rule_id, "now": datetime.now(timezone.utc), "enabled": target},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    if not row.enabled and before["enabled"] and nfa.is_netflow_metric(before.get("metric")):
        await nfa.reset_rule(db, str(rule_id), reason="rule_disabled")
    if bool(before["enabled"]) != bool(row.enabled):
        await write_audit_log(db, actor=user, action="alert_rule.toggle", resource_type="alert_rule",
                              resource_id=str(rule_id),
                              metadata={"name": row.name, "enabled": row.enabled,
                                        "reason": body.reason if body else None,
                                        "affected": await rs.affected(db, before)})
    await db.commit()
    return {
        "id": str(row.id),
        "name": row.name,
        "enabled": row.enabled,
        "message": f"Rule '{ row.name }' {'enabled' if row.enabled else 'disabled'}",
    }


def _render_template(template: str, variables: dict) -> str:
    """Replace {var_name} placeholders with values."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _build_alert_message(rule_dict: dict, is_recovery: bool = False) -> dict:
    """Build a preview/simulation alert message from a rule.

    Uses the same phrasing helpers and the same default templates as the live
    evaluators, so what an operator approves in the preview dialog is what the
    recipient gets.
    """
    now = datetime.now(timezone.utc)
    trigger = rule_dict.get("trigger_on", "any")
    metric = rule_dict.get("metric") or "ping_status"
    operator = rule_dict.get("operator") or "=="
    threshold = rule_dict.get("threshold", 0)

    if is_recovery:
        device_status = "UP"
        status_intro = "The following alert has been resolved:"
    else:
        device_status = "DOWN" if trigger == "down" else "DEGRADED" if trigger == "degraded" else "UP" if trigger == "up" else "ALERT"
        status_intro = "An alert has been triggered:"

    # Preview against something the rule could plausibly fire on: an APM rule
    # previewed against "core-router-01" teaches the reader nothing about the
    # mail their service team will get.
    if metric.startswith("apm_"):
        hostname = "checkout-api"
    elif metric.startswith("host_"):
        hostname = "app-server-01"
    elif metric == "service_status" or rule_dict.get("service_check_id"):
        hostname = "Dashboard HTTPS"
    elif metric.startswith("netflow_"):
        hostname = "edge-router-01"
    else:
        hostname = "core-router-01"
    reading = ap.sample_reading(metric, operator, threshold, recovered=is_recovery)
    # A worked example beats a placeholder: the recovery preview has to show
    # the "how long was it broken" line, so the sample carries a duration.
    sample_duration = ap.humanize_duration(3 * 60 + 25) if is_recovery else ""

    variables = {
        "severity": (rule_dict.get("severity") or "warning").upper(),
        "rule_name": rule_dict.get("name", "Alert Rule"),
        "hostname": hostname,
        "ip_address": "10.0.0.1",
        "status": device_status,
        "status_intro": status_intro,
        "device_type": rule_dict.get("device_type") or "router",
        "group": "Core Network",
        "location": rule_dict.get("location") or "DC-1 Rack A1",
        # "Now" rather than a frozen date: a preview/test mail stamped months in
        # the past reads as stale data rather than as a sample.
        "timestamp": now.strftime("%H:%M UTC on %d %b %Y"),
        "rtt": "45.2ms",
        "packet_loss": "15%",
        "check_name": rule_dict.get("name") or "Service check",
        "check_type": "HTTP",
        "target": "https://service.example.com",
        "error": "" if is_recovery else "connection timed out after 5s",
        "error_sentence": "" if is_recovery else " Connection timed out after 5s.",
        **ap.rule_phrasing(rule_dict, hostname=hostname, is_recovery=is_recovery,
                           reading=reading, duration=sample_duration),
    }

    # NetFlow rules phrase their sample reading exactly as the evaluator does
    # (bands, windows, exporter silence vs device reachability).
    nf_vars = nfa.preview_phrasing(rule_dict, is_recovery=is_recovery, duration=sample_duration)
    if nf_vars:
        variables.update(nf_vars)

    if metric == 'syslog':
        variables.update(event_message='Interface uplink changed state', syslog_severity=int(threshold or 0),
                         event_sentence=f'{hostname} sent a matching syslog event.', status='SYSLOG')
        device_status = 'SYSLOG'

    # `effective_template` also treats the wizard's old pre-filled templates as
    # "no template", so a rule created before this wording existed previews
    # (and sends) the current text rather than the field dump it was seeded with.
    if is_recovery:
        subject_tpl = ap.effective_template(rule_dict.get("recovery_email_subject"),
                                            ap.DEFAULT_RECOVERY_EMAIL_SUBJECT)
        body_tpl = ap.effective_template(rule_dict.get("recovery_email_body"),
                                         ap.DEFAULT_RECOVERY_EMAIL_BODY)
        sms_tpl = ap.effective_template(rule_dict.get("recovery_sms_template"),
                                        ap.DEFAULT_RECOVERY_SMS)
    else:
        subject_tpl = ap.effective_template(rule_dict.get("email_subject"), ap.DEFAULT_EMAIL_SUBJECT)
        body_tpl = ap.effective_template(rule_dict.get("email_body"), ap.DEFAULT_EMAIL_BODY)
        sms_tpl = ap.effective_template(rule_dict.get("sms_template"), ap.DEFAULT_SMS)
        if metric == 'syslog':
            defaults = ap.default_templates('syslog')
            subject_tpl = rule_dict.get('email_subject') or defaults['email_subject']
            body_tpl = rule_dict.get('email_body') or defaults['email_body']
            sms_tpl = rule_dict.get('sms_template') or defaults['sms_template']

    return {
        "subject": _render_template(subject_tpl, variables),
        "email_body": _render_template(body_tpl, variables),
        "sms_body": _render_template(sms_tpl, variables),
        "sim_hostname": variables["hostname"],
        "sim_ip": variables["ip_address"],
        "device_status": device_status,
        "severity": variables["severity"],
        # Return raw templates for editing
        "email_subject_template": subject_tpl,
        "email_body_template": body_tpl,
        "sms_template": sms_tpl,
        # Sample values behind every {placeholder}, so the editor can show what
        # each variable resolves to instead of just naming it.
        "variables": variables,
        "iso_timestamp": now.isoformat(),
    }


# Rules whose readings the alert mail leads with (round-trip time / packet loss);
# every other metric family has no sample reading worth inventing.
_PING_METRICS = {"ping_status", "rtt", "packet_loss", "jitter"}


def _preview_email_ctx(rule_dict: dict, msg: dict, is_recovery: bool,
                       action_url: str = "") -> dict:
    """Context for the *real* alert email, filled with the preview's samples.

    Deliberately mirrors the ctx alert_engine builds when a rule actually
    fires — same headline metric, same details rows — so the mail shown in the
    preview dialog is the mail the recipient will get, not an approximation.
    """
    from app.api.v1.alert_engine import _clean_details

    variables = msg.get("variables") or {}
    metric = rule_dict.get("metric") or ""
    is_ping = metric in _PING_METRICS

    ctx = {
        "product_name": "ZenPlus",
        "severity": rule_dict.get("severity") or "warning",
        "resolved": is_recovery,
        "status": msg["device_status"],
        "title": rule_dict.get("name") or "Alert",
        "hostname": msg["sim_hostname"],
        "ip_address": msg["sim_ip"],
        "message": msg["email_body"],
        "notice": "Preview — the device, readings and time below are sample "
                  "values. No alert has been raised and nothing was sent.",
        "details": _clean_details([
            ("Alert rule", rule_dict.get("name")),
            ("Condition", variables.get("condition_label")),
            ("Active for", variables.get("duration") if is_recovery else None),
            ("Round-trip time", variables.get("rtt") if is_ping else None),
            ("Packet loss", variables.get("packet_loss") if is_ping else None),
            ("Group", rule_dict.get("group_id") and variables.get("group")),
            ("Location", rule_dict.get("location")),
            ("Device type", rule_dict.get("device_type")),
            ("Tag", rule_dict.get("scope_tag")),
            ("Flow scope", variables.get("flow_scope_label")),
        ]),
        "action_url": action_url,
        "timestamp": msg.get("iso_timestamp"),
    }
    if is_ping:
        ctx["headline_metric"] = {
            "label": "Round-trip time",
            "value": variables.get("rtt"),
            "secondary_label": "Packet loss",
            "secondary_value": variables.get("packet_loss"),
        }
    elif variables.get("reading"):
        # Every other metric family leads with the reading that fired the rule,
        # next to the threshold it crossed — the two numbers a reader compares.
        ctx["headline_metric"] = {
            "label": ap.metric_noun(metric),
            "value": variables["reading"],
            "secondary_label": "Threshold",
            "secondary_value": variables.get("threshold_value"),
        }
    return ctx


async def _preview_payload(db: AsyncSession, rule: dict, stored: dict) -> dict:
    """Render a rule's trigger and reset notifications as the engine builds them.

    Shared by the saved-rule preview and the draft preview the wizard uses
    while a rule is still being composed, so what an editor shows is never a
    second implementation of what the sender does.
    """
    from app.api.v1.alert_engine import _dashboard_url
    from app.services.email_render import (
        build_alert_email_html, build_alert_email_text,
    )
    action_url = await _dashboard_url(db, "/alerts")

    def _render(is_recovery: bool) -> dict:
        msg = _build_alert_message(rule, is_recovery=is_recovery)
        ctx = _preview_email_ctx(rule, msg, is_recovery, action_url)
        msg["email_html"] = build_alert_email_html(ctx)
        msg["email_text"] = build_alert_email_text(ctx)
        return msg

    alert_msg = _render(False)
    # The reset mail is rendered whenever the rule has reset wording at all —
    # not only once recovery notifications are switched on. Deciding whether to
    # switch them on means reading what they would say.
    kind = ap.template_kind(rule)
    recovery_msg = _render(True) if ap.default_templates(kind)["recovery_email_body"] else None

    return {
        "alert": alert_msg,
        "recovery": recovery_msg,
        "recovery_enabled": bool(rule.get("recovery_alert")),
        "templates": {
            "stored": stored,
            "kind": kind,
            "defaults": ap.default_templates(kind),
            "effective": ap.effective_templates(rule, kind),
        },
    }


@router.post("/preview")
async def preview_alert_rule_draft(
    draft: AlertRuleDraft,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render the notifications for a rule that has not been saved yet.

    The wizard previews as its templates are typed, and a rule that does not
    exist has no id to preview against. It used to create a disabled throwaway
    rule and delete it again for every preview — one write, one delete and a
    leaked rule whenever the delete failed. This renders from the draft alone
    and touches nothing.
    """
    rule = draft.model_dump()
    return await _preview_payload(db, rule, {k: rule.get(k) for k in _TEMPLATE_FIELDS})


@router.post("/{rule_id}/preview")
async def preview_alert_rule(
    rule_id: UUID,
    overrides: Optional[AlertRulePreview] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render what this rule's notifications will look like.

    Returns the rendered HTML/plain-text email exactly as the alert engine
    builds it, plus the SMS body and the raw templates. Any template supplied
    in the body is used instead of the stored one, so the editor can preview
    unsaved edits; a blank string means "fall back to the built-in default".
    """
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")

    rule = _row_to_dict(row)
    stored = {k: rule.get(k) for k in _TEMPLATE_FIELDS}
    if overrides is not None:
        for key, value in overrides.model_dump(exclude_unset=True).items():
            rule[key] = value or None

    return await _preview_payload(db, rule, stored)


@router.post("/{rule_id}/simulate")
async def simulate_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_rule_manager),
):
    import httpx
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    # Get rule
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")

    rule = _row_to_dict(row)
    alert_msg = _build_alert_message(rule)
    notify_channels = rule.get("notify_channels", [])
    results = []

    if not notify_channels:
        return {"message": "No notification channels configured for this rule", "results": []}

    for ch_id in notify_channels:
        ch_result = await db.execute(
            text("SELECT id, name, type, config, enabled, gateway_id FROM notification_channels WHERE id = :id"),
            {"id": ch_id},
        )
        ch_row = ch_result.first()
        if not ch_row:
            results.append({"channel": ch_id, "status": "error", "detail": "Channel not found"})
            continue
        if not ch_row.enabled:
            results.append({"channel": ch_row.name, "status": "skipped", "detail": "Channel disabled"})
            continue

        ch_config = decrypt_config(ch_row.config)

        try:
            if ch_row.type == "sms":
                phones = ch_config.get("phone_numbers", "")
                if not phones:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No phone numbers"})
                    continue

                # Get SMS gateway
                gw_id = ch_row.gateway_id or ch_config.get("gateway_id")
                if gw_id:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                else:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'sms' AND is_default = true LIMIT 1"))
                gw_row = gw_res.first()
                if not gw_row:
                    gw_raw = await db.execute(text("SELECT value FROM system_settings WHERE key = 'sms'"))
                    gw_row2 = gw_raw.first()
                    gw_cfg = decrypt_config(gw_row2[0]) if gw_row2 else None
                else:
                    gw_cfg = decrypt_config(gw_row.config)

                if not gw_cfg:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No SMS gateway"})
                    continue

                if gw_cfg.get("provider") == "custom_http" and gw_cfg.get("api_url"):
                    resp = await send_sms_request(gw_cfg, {
                        'recipients': phones, 'message': alert_msg['sms_body'],
                        'sender': gw_cfg.get('sender_name') or 'ZenPlus',
                        'hostname': alert_msg['sim_hostname'], 'ip_address': alert_msg['sim_ip'],
                        'status': alert_msg['device_status'],
                    }, client_factory=NotificationHTTPClient)

                    results.append({"channel": ch_row.name, "type": "sms", "status": "sent", "detail": f"Status {resp.status_code}", "recipients": phones})
                else:
                    results.append({"channel": ch_row.name, "type": "sms", "status": "skipped", "detail": f"Provider {gw_cfg.get('provider')} not supported for simulation"})

            elif ch_row.type == "email":
                recipients = ch_config.get("recipients", "")
                if not recipients:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No recipients"})
                    continue

                gw_id = ch_row.gateway_id or ch_config.get("gateway_id")
                if gw_id:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                else:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'smtp' AND is_default = true LIMIT 1"))
                gw_row = gw_res.first()
                if not gw_row:
                    gw_raw = await db.execute(text("SELECT value FROM system_settings WHERE key = 'smtp'"))
                    gw_row2 = gw_raw.first()
                    gw_cfg = decrypt_config(gw_row2[0]) if gw_row2 else None
                else:
                    gw_cfg = decrypt_config(gw_row.config)

                if not gw_cfg or not gw_cfg.get("host"):
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No SMTP gateway configured"})
                    continue

                recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]

                # Render the same styled email a real alert produces, so what
                # the operator receives when testing is what their users will
                # actually get. This used to send the raw template text as a
                # plain-text part only, which arrived looking like a debug dump
                # and told them nothing about how the real alert would look.
                from app.services.email_render import (
                    build_alert_email_html, build_alert_email_text,
                )
                sim_ctx = {
                    "product_name": "ZenPlus",
                    "severity": (rule.get("severity") or "warning"),
                    "resolved": False,
                    "status": f"TEST · {alert_msg['device_status']}",
                    "title": rule.get("name") or "Alert rule test",
                    "hostname": alert_msg["sim_hostname"],
                    "ip_address": alert_msg["sim_ip"],
                    "message": alert_msg["email_body"],
                    # No headline value: a test has no measurement, and showing
                    # the bare condition ("== 0") in the slot reserved for a
                    # reading reads as a broken number rather than a threshold.
                    "notice": "Test notification — this is what the alert will look like when it "
                              "fires. The device and readings below are sample values; no alert "
                              "has been raised.",
                    "details": [
                        ("Alert rule", rule.get("name")),
                        ("Condition", " ".join(str(x) for x in (
                            rule.get("metric"), rule.get("operator"), rule.get("threshold")) if x not in (None, ""))),
                        ("Sample device", f"{alert_msg['sim_hostname']} ({alert_msg['sim_ip']})"),
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                html_body = build_alert_email_html(sim_ctx)
                text_body = build_alert_email_text(sim_ctx)

                msg = MIMEMultipart("alternative")
                msg["From"] = f"{gw_cfg.get('from_name', 'ZenPlus')} <{gw_cfg.get('from_email', '')}>"
                msg["To"] = ", ".join(recipient_list)
                msg["Subject"] = f"[TEST] {alert_msg['subject']}"
                # Plain part first: last attachment wins in multipart/alternative.
                msg.attach(MIMEText(text_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))

                enc = gw_cfg.get("encryption", "tls")
                if enc == "ssl":
                    server = smtplib.SMTP_SSL(gw_cfg["host"], gw_cfg.get("port", 465), timeout=10, context=ssl.create_default_context())
                else:
                    server = smtplib.SMTP(gw_cfg["host"], gw_cfg.get("port", 587), timeout=10)
                    server.starttls(context=ssl.create_default_context())
                if gw_cfg.get("username"):
                    server.login(gw_cfg["username"], gw_cfg.get("password", ""))
                server.sendmail(gw_cfg.get("from_email", ""), recipient_list, msg.as_string())
                server.quit()
                results.append({"channel": ch_row.name, "type": "email", "status": "sent", "detail": f"Sent to {recipients}"})

            else:
                results.append({"channel": ch_row.name, "type": ch_row.type, "status": "skipped", "detail": "Simulation not supported for this type"})

        except Exception as e:
            results.append({"channel": ch_row.name, "status": "error", "detail": str(e)[:200]})

    sent = sum(1 for r in results if r["status"] == "sent")
    return {
        "message": f"Simulation complete: {sent}/{len(results)} notifications sent",
        "alert_preview": alert_msg,
        "results": results,
    }
