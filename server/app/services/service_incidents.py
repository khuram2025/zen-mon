"""Server-side incident reconstruction from service_status_log (B14/B15).

Incidents are built from recorded state transitions, INCLUDING the transition
that precedes the window (the entering state), and every transition inside
the window is read by keyset paging — no fixed 500/1000-event cap.

Rules:
- down -> a "down" incident; warning/degraded -> a "warning" incident;
- a change of kind (warning -> down) closes one incident and opens the next,
  so Warning -> Down -> Up yields separate warning and down intervals that
  share one severity_path;
- up closes the incident;
- unknown / paused / anything else is an evidence gap: it closes the incident
  at that transition and is never drawn as a warning.
Maintenance is reported separately; incidents inside maintenance are kept.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

PAGE = 5000
MAX_TRANSITIONS = 200_000

DOWN_STATES = {"down", "critical", "offline"}
WARNING_STATES = {"warning", "degraded"}


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fmt(ts: datetime) -> str:
    # DateTime64(3) accepts milliseconds; keeps keyset paging exact.
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def kind_of(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s in DOWN_STATES:
        return "down"
    if s in WARNING_STATES:
        return "warning"
    if s == "up":
        return "up"
    return "gap"


def read_transitions(ch, check_id, start: datetime, end: datetime):
    """(entering, transitions): the last transition before `start` and every
    transition in [start, end), paged by timestamp (no fixed cap)."""
    params = {"id": str(check_id), "f": _fmt(start), "t": _fmt(end)}
    prior = ch.query("""
        SELECT timestamp, new_status, reason FROM zenplus.service_status_log
        WHERE service_check_id = %(id)s AND timestamp < %(f)s
        ORDER BY timestamp DESC LIMIT 1
    """, parameters=params).result_rows
    entering = None
    if prior:
        ts, status, reason = prior[0][0], prior[0][1], (prior[0][2] if len(prior[0]) > 2 else "")
        entering = (_aware(ts), status, reason)
    out = []
    cursor = start
    first = True
    while len(out) < MAX_TRANSITIONS:
        op = ">=" if first else ">"
        rows = ch.query(f"""
            SELECT timestamp, new_status, reason FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s AND timestamp {op} %(c)s AND timestamp < %(t)s
            ORDER BY timestamp LIMIT {PAGE}
        """, parameters={**params, "c": _fmt(cursor)}).result_rows
        out.extend((_aware(r[0]), r[1], r[2] if len(r) > 2 else "") for r in rows)
        if len(rows) < PAGE:
            break
        cursor = _aware(rows[-1][0])
        first = False
    return entering, out


def next_transition_after(ch, check_id, ts: datetime):
    rows = ch.query("""
        SELECT timestamp, new_status, reason FROM zenplus.service_status_log
        WHERE service_check_id = %(id)s AND timestamp >= %(f)s
        ORDER BY timestamp LIMIT 1
    """, parameters={"id": str(check_id), "f": _fmt(ts)}).result_rows
    if not rows:
        return None
    return _aware(rows[0][0]), rows[0][1], rows[0][2] if len(rows[0]) > 2 else ""


def build_incidents(check_id, entering, transitions, start: datetime, end: datetime,
                    now: datetime, closing=None) -> list[dict]:
    """Pure reconstruction. `closing` is the first transition at/after `end`
    (to report the real end of an incident still open at the window end)."""
    events = []
    if entering is not None:
        events.append(entering)
    events.extend(transitions)
    incidents: list[dict] = []
    open_inc = None
    episode: list[dict] = []

    def close(ts, reason):
        nonlocal open_inc
        if open_inc is not None:
            open_inc["end"] = ts
            open_inc["reason_end"] = reason
            incidents.append(open_inc)
            open_inc = None

    def end_episode():
        path = [i["kind"] for i in episode]
        for i in episode:
            i["severity_path"] = path
        episode.clear()

    for ts, status, reason in events:
        kind = kind_of(status)
        if open_inc is not None and kind == open_inc["kind"]:
            continue  # repeated state, same incident
        close(ts, reason)
        if kind in ("down", "warning"):
            open_inc = {"kind": kind, "start": ts, "end": None, "reason_start": reason, "reason_end": None}
            episode.append(open_inc)
        else:
            end_episode()
    if open_inc is not None:
        if closing is not None and kind_of(closing[1]) != open_inc["kind"]:
            close(closing[0], closing[2])
        else:
            incidents.append(open_inc)
            open_inc = None
    end_episode()

    out = []
    for inc in incidents:
        lo, hi = inc["start"], inc["end"]
        eff_hi = hi or now
        in_window = max(0.0, (min(eff_hi, end, now) - max(lo, start)).total_seconds())
        if hi is not None and hi <= start:
            continue  # closed before the window (entering state was not an incident)
        if lo >= end:
            continue
        out.append({
            "id": f"{check_id}:{lo.isoformat()}",
            "kind": inc["kind"],
            "start": lo.isoformat(),
            "end": hi.isoformat() if hi else None,
            # Open with no later transition at all: still in this state now.
            "ongoing": hi is None and closing is None,
            "starts_before_window": lo < start,
            "ends_after_window": hi is None or hi > end,
            "duration_sec": (hi - lo).total_seconds() if hi else None,
            "in_window_sec": in_window,
            "reason_start": inc.get("reason_start") or None,
            "reason_end": inc.get("reason_end") or None,
            "severity_path": inc.get("severity_path") or [inc["kind"]],
        })
    return out


def get_incidents(ch, check, start: datetime, end: datetime, skip: int = 0, limit: int = 100,
                  now: datetime | None = None) -> dict:
    """Paged incidents (newest first) plus coverage for the window."""
    from app.services.service_availability import read_segments, summarize

    now = now or datetime.now(timezone.utc)
    start, end = _aware(start), _aware(end)
    entering, transitions = read_transitions(ch, check.id, start, end)
    closing = next_transition_after(ch, check.id, end) if end < now else None
    incidents = build_incidents(check.id, entering, transitions, start, end, now, closing)
    incidents.sort(key=lambda i: i["start"], reverse=True)

    created = getattr(check, "created_at", None)
    observed_start = max(start, _aware(created)) if created else start
    observed_end = min(end, now)
    coverage_pct, sample_count = None, 0
    if observed_end > observed_start:
        segments, sample_count = read_segments(ch, check.id, observed_start, observed_end,
                                               getattr(check, "check_interval", None) or 60)
        coverage_pct = summarize(segments, observed_start, observed_end)["coverage_pct"]
    return {
        "data": incidents[skip:skip + limit],
        "meta": {"total": len(incidents), "skip": skip, "limit": limit,
                 "transitions_read": len(transitions)},
        "entering_state": ({"status": entering[1], "since": entering[0].isoformat(),
                            "reason": entering[2] or None} if entering else None),
        "coverage_pct": coverage_pct,
        "sample_count": sample_count,
        "server_time": now.isoformat(),
        "from": start.isoformat(),
        "to": end.isoformat(),
    }
