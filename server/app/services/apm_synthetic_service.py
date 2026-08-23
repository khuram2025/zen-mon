"""APM synthetic monitoring — scripted user scenarios (AM-E9).

Executes multi-step HTTP scenarios ("user journeys") defined in
``apm_synthetic_monitors`` (Postgres, shipped in migrate-039 as forward
schema; this service is its first consumer). Each run walks the scenario's
steps in order inside one HTTP session (cookies carry over), substitutes
``{{variables}}`` — both scenario-level and values extracted from earlier
responses — checks per-step assertions, and writes a result row to
``zenplus.apm_synthetic_results`` (ClickHouse, migrate-058).

Scenario ``config`` JSONB shape:

    {
      "steps": [{
        "name": "Login",
        "method": "POST",
        "url": "https://shop.example.com/api/login",
        "headers": {"Content-Type": "application/json"},
        "body": "{\\"user\\":\\"demo\\",\\"pass\\":\\"{{password}}\\"}",
        "extract": [{"var": "token", "from": "json", "path": "access_token"}],
        "assertions": [
          {"type": "status_code", "operator": "eq", "value": 200},
          {"type": "json_path", "path": "user.id", "operator": "exists"},
          {"type": "body_contains", "value": "welcome"},
          {"type": "latency_ms", "operator": "lt", "value": 2000}
        ]
      }, ...],
      "variables": {"password": "..."},
      "verify_tls": true,
      "notify_channels": ["email"]
    }

A step with no assertions passes when its HTTP status is < 400. A monitor
whose run fails (after ``retry_count`` retries) transitions to ``down``,
raises a deduped row in ``alerts`` (source ``apm_synthetic``, metric key
``apm_synthetic_down`` — the key whitelisted since migrate-039 that never
had a producer) and notifies the scenario's channels; recovery resolves it.

The scheduler loop runs in every uvicorn worker; a Postgres advisory lock
elects one runner per tick (same idiom as the storage sweeper).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.host_alert_service import dispatch_to_channels

logger = logging.getLogger("zenplus.apm_synthetics")

TICK_INTERVAL_S = 15
SYNTHETIC_SWEEP_ADVISORY_LOCK = 1515074391
MAX_STEPS = 20
MAX_BODY_CAPTURE = 512          # response snippet kept per step for the UI
DEFAULT_LOCATION = "appliance"

_VAR_RX = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")

_OPS = {
    "eq": lambda a, b: a == b, "neq": lambda a, b: a != b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "contains": lambda a, b: str(b) in str(a),
}


def _substitute(template: str, variables: dict[str, Any]) -> str:
    def repl(m: re.Match) -> str:
        return str(variables.get(m.group(1), m.group(0)))
    return _VAR_RX.sub(repl, template or "")


def _json_path(data: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dot/bracket path like ``user.roles[0].name``. Returns
    (found, value)."""
    cur = data
    for part in re.split(r"\.", path or ""):
        if not part:
            continue
        m = re.fullmatch(r"([^\[\]]*)((\[\d+\])*)", part)
        key, idxs = (m.group(1), m.group(2)) if m else (part, "")
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return False, None
            cur = cur[key]
        for i in re.findall(r"\[(\d+)\]", idxs or ""):
            if not isinstance(cur, list) or int(i) >= len(cur):
                return False, None
            cur = cur[int(i)]
    return True, cur


def _check_assertion(a: dict, *, status_code: int, body: str,
                     body_json: Any, latency_ms: float) -> tuple[bool, str]:
    typ = a.get("type") or "status_code"
    op = a.get("operator") or "eq"
    want = a.get("value")
    if typ == "status_code":
        ok = _OPS.get(op, _OPS["eq"])(status_code, int(want or 200))
        return ok, f"status {status_code} {op} {want}"
    if typ == "latency_ms":
        ok = _OPS.get(op, _OPS["lt"])(latency_ms, float(want or 0))
        return ok, f"latency {latency_ms:.0f}ms {op} {want}ms"
    if typ == "body_contains":
        ok = str(want or "") in body
        return ok, f"body contains {str(want)[:40]!r}"
    if typ == "json_path":
        found, val = _json_path(body_json, a.get("path") or "")
        if op == "exists":
            return found, f"json {a.get('path')} exists"
        if not found:
            return False, f"json {a.get('path')} missing"
        cmp_val: Any = val
        try:
            if isinstance(want, (int, float)) or (
                isinstance(want, str) and re.fullmatch(r"-?\d+(\.\d+)?", want or "")
            ):
                cmp_val, want = float(val), float(want)
        except (TypeError, ValueError):
            cmp_val = str(val)
        ok = _OPS.get(op, _OPS["eq"])(cmp_val, want)
        return ok, f"json {a.get('path')}={str(val)[:40]} {op} {want}"
    return False, f"unknown assertion type {typ!r}"


async def run_scenario(monitor: dict) -> dict:
    """Execute one scenario run. Returns the result dict (not yet persisted)."""
    cfg = monitor.get("config") or {}
    steps_cfg = (cfg.get("steps") or [])[:MAX_STEPS]
    variables: dict[str, Any] = dict(cfg.get("variables") or {})
    verify_tls = cfg.get("verify_tls", True)
    timeout = float(monitor.get("timeout") or 30)

    step_results: list[dict] = []
    failed_step = ""
    error = ""
    t_start = time.monotonic()

    if not steps_cfg and monitor.get("target_url"):
        # Zero-config monitor: a single GET against target_url.
        steps_cfg = [{"name": "GET", "method": "GET", "url": monitor["target_url"]}]

    async with httpx.AsyncClient(
        timeout=timeout, verify=verify_tls, follow_redirects=True,
        headers={"User-Agent": "ZenPlus-Synthetics/1.0"},
    ) as client:
        for step in steps_cfg:
            name = step.get("name") or step.get("url") or f"step {len(step_results) + 1}"
            method = (step.get("method") or "GET").upper()
            url = _substitute(step.get("url") or "", variables)
            headers = {k: _substitute(v, variables)
                       for k, v in (step.get("headers") or {}).items()}
            body = _substitute(step.get("body") or "", variables) or None

            s_start = time.monotonic()
            status_code, resp_body, body_json = 0, "", None
            step_error = ""
            try:
                resp = await client.request(method, url, headers=headers, content=body)
                status_code = resp.status_code
                resp_body = resp.text[:65536]
                try:
                    body_json = resp.json()
                except (json.JSONDecodeError, ValueError):
                    body_json = None
            except httpx.TimeoutException:
                step_error = f"timeout after {timeout:.0f}s"
            except httpx.HTTPError as e:
                step_error = f"{type(e).__name__}: {e}"
            latency_ms = (time.monotonic() - s_start) * 1000

            asserts_out: list[dict] = []
            if step_error:
                ok = False
            else:
                assertions = step.get("assertions") or []
                if not assertions:
                    ok = status_code < 400
                    if not ok:
                        step_error = f"HTTP {status_code}"
                else:
                    ok = True
                    for a in assertions:
                        a_ok, detail = _check_assertion(
                            a, status_code=status_code, body=resp_body,
                            body_json=body_json, latency_ms=latency_ms,
                        )
                        asserts_out.append({"type": a.get("type"), "ok": a_ok,
                                            "detail": detail})
                        if not a_ok:
                            ok = False
                if ok:
                    for ex in step.get("extract") or []:
                        var = ex.get("var")
                        if not var:
                            continue
                        if (ex.get("from") or "json") == "json":
                            found, val = _json_path(body_json, ex.get("path") or "")
                            if found:
                                variables[var] = val
                        elif ex.get("from") == "header":
                            variables[var] = resp.headers.get(ex.get("path") or "", "")
                        elif ex.get("from") == "regex":
                            m = re.search(ex.get("path") or "", resp_body)
                            if m:
                                variables[var] = m.group(1) if m.groups() else m.group(0)

            step_results.append({
                "name": name, "method": method, "url": url, "ok": ok,
                "status_code": status_code, "ms": round(latency_ms, 1),
                "error": step_error, "asserts": asserts_out,
                "body_snippet": "" if ok else resp_body[:MAX_BODY_CAPTURE],
            })
            if not ok:
                failed_step = name
                error = step_error or next(
                    (a["detail"] for a in asserts_out if not a["ok"]), "assertion failed")
                break

    total_ms = (time.monotonic() - t_start) * 1000
    passed = sum(1 for s in step_results if s["ok"])
    success = bool(step_results) and passed == len(steps_cfg)
    if not steps_cfg:
        success, error = False, "scenario has no steps"
    return {
        "success": success,
        "status": "up" if success else "down",
        "total_ms": round(total_ms, 1),
        "steps_total": len(steps_cfg),
        "steps_passed": passed,
        "failed_step": failed_step,
        "error": error[:500],
        "steps": step_results,
    }


async def run_monitor_with_retries(monitor: dict) -> dict:
    """Run the scenario, retrying failures ``retry_count`` times."""
    retries = max(0, int(monitor.get("retry_count") or 0))
    result = await run_scenario(monitor)
    attempt = 0
    while not result["success"] and attempt < retries:
        attempt += 1
        await asyncio.sleep(1)
        result = await run_scenario(monitor)
    result["attempts"] = attempt + 1
    return result


# ─── Persistence + alerting ──────────────────────────────────────────────────

def _insert_result_sync(monitor_id: str, result: dict) -> None:
    from app.core.database import get_ch_client
    get_ch_client().insert(
        "apm_synthetic_results",
        [[uuid_mod.UUID(monitor_id),
          datetime.now(timezone.utc).replace(tzinfo=None),
          result["status"], 1 if result["success"] else 0,
          int(result["total_ms"]), result["steps_total"], result["steps_passed"],
          result["failed_step"], result["error"],
          json.dumps(result["steps"], separators=(",", ":")),
          DEFAULT_LOCATION]],
        column_names=["monitor_id", "timestamp", "status", "success", "total_ms",
                      "steps_total", "steps_passed", "failed_step", "error",
                      "steps_json", "location"],
        database="zenplus",
    )


async def _active_down_alert(db: AsyncSession, monitor_id: str):
    row = (await db.execute(text(
        "SELECT id FROM alerts WHERE status IN ('active','acknowledged') "
        "AND metadata->>'dedupe' = :d ORDER BY triggered_at DESC LIMIT 1"
    ), {"d": f"synthetic:{monitor_id}"})).first()
    return row[0] if row else None


async def _raise_down(db: AsyncSession, monitor: dict, result: dict) -> None:
    msg = (f"Synthetic scenario DOWN: {monitor['name']} — "
           f"failed at step '{result['failed_step'] or 'n/a'}': {result['error']}")
    await db.execute(text(
        "INSERT INTO alerts (status, severity, message, triggered_at, metadata) "
        "VALUES ('active', 'critical', :msg, :ts, CAST(:meta AS jsonb))"
    ), {
        "msg": msg, "ts": datetime.now(timezone.utc),
        "meta": json.dumps({
            "source": "apm_synthetic", "metric": "apm_synthetic_down",
            "dedupe": f"synthetic:{monitor['id']}",
            "monitor_id": str(monitor["id"]), "monitor_name": monitor["name"],
            "failed_step": result["failed_step"], "error": result["error"],
        }),
    })
    channels = (monitor.get("config") or {}).get("notify_channels") or []
    if channels:
        await dispatch_to_channels(db, channels, {
            "subject": f"[CRITICAL] Synthetic scenario down — {monitor['name']}",
            "body": msg, "message": msg,
            "hostname": monitor["name"], "ip_address": "",
            "status": "ALERT", "severity": "critical",
            "details": [("Scenario", monitor["name"]),
                        ("Failed step", result["failed_step"] or "n/a"),
                        ("Error", result["error"]),
                        ("Steps passed", f"{result['steps_passed']}/{result['steps_total']}")],
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "rule_id": str(monitor["id"]), "rule_name": monitor["name"],
        })


async def _resolve_down(db: AsyncSession, alert_id, monitor: dict) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id,
        "m": json.dumps({"resolved_by": "apm_synthetic", "resolved_at": now.isoformat()})})
    channels = (monitor.get("config") or {}).get("notify_channels") or []
    if channels:
        msg = f"Synthetic scenario recovered: {monitor['name']}"
        await dispatch_to_channels(db, channels, {
            "subject": f"[RESOLVED] Synthetic scenario recovered — {monitor['name']}",
            "body": msg, "message": msg,
            "hostname": monitor["name"], "ip_address": "",
            "status": "RESOLVED", "severity": "info",
            "details": [("Scenario", monitor["name"])],
            "triggered_at": now.isoformat(),
            "rule_id": str(monitor["id"]), "rule_name": monitor["name"],
        })


async def execute_and_record(db: AsyncSession, monitor: dict) -> dict:
    """Run a monitor, persist the result, update status, raise/resolve alerts."""
    result = await run_monitor_with_retries(monitor)
    try:
        await asyncio.to_thread(_insert_result_sync, str(monitor["id"]), result)
    except Exception:
        logger.exception("synthetic result insert failed for %s", monitor["name"])

    await db.execute(text(
        "UPDATE apm_synthetic_monitors SET status = :st, last_check_at = NOW(), "
        "updated_at = NOW() WHERE id = :id"
    ), {"st": result["status"], "id": str(monitor["id"])})

    existing = await _active_down_alert(db, str(monitor["id"]))
    if not result["success"] and existing is None:
        await _raise_down(db, monitor, result)
        logger.warning("synthetic %s DOWN: %s", monitor["name"], result["error"])
    elif result["success"] and existing is not None:
        await _resolve_down(db, existing, monitor)
        logger.info("synthetic %s recovered", monitor["name"])
    await db.commit()
    return result


# ─── Scheduler loop ──────────────────────────────────────────────────────────

async def synthetic_tick(db: AsyncSession) -> int:
    locked = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"),
        {"k": SYNTHETIC_SWEEP_ADVISORY_LOCK},
    )).scalar()
    if not locked:
        return 0

    due = (await db.execute(text("""
        SELECT id, name, monitor_type, target_url, config, check_interval,
               timeout, retry_count, status
        FROM apm_synthetic_monitors
        WHERE enabled = TRUE
          AND (last_check_at IS NULL
               OR last_check_at < NOW() - make_interval(secs => check_interval))
        ORDER BY last_check_at ASC NULLS FIRST
        LIMIT 20
    """))).mappings().all()

    ran = 0
    for row in due:
        monitor = dict(row)
        try:
            await execute_and_record(db, monitor)
            ran += 1
        except Exception:
            logger.exception("synthetic run failed: %s", monitor.get("name"))
    return ran


async def apm_synthetic_runner_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(25)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await synthetic_tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("synthetic runner tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)
