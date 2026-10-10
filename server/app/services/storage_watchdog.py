"""Independent free-space watchdog (systemd: zenplus-storage-watchdog.service).

Protects the appliance from filling its disk even while the API or PostgreSQL
is down — a full disk is exactly what stops PostgreSQL. It therefore depends
only on the standard library, ``clickhouse_connect`` and a state directory:

  policy.json    last valid pressure policy, written by the API (defaults if absent)
  state.json     heartbeat, filesystems, pressure, pending reclamation, samples,
                 ClickHouse breakdown and growth — read by the Storage page
  audit.jsonl    bounded local audit trail; the API imports it into storage_events
  ingest-paused  present while free space is critical and nothing more can be
                 reclaimed; ingestion endpoints answer 503 until it is removed

Every pass is bounded (``storage_policy.MAX_ACTIONS_PER_PASS``) and counts
bytes it scheduled for deletion as pending until ClickHouse has had time to
remove them, so one pressure episode never cascades into deleting more than
needed. Scheduled bytes and measured reclamation are recorded separately.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from app.services import storage_policy as P

logger = logging.getLogger("zenplus.storage_watchdog")

STATE_DIR = os.environ.get("ZENPLUS_STORAGE_STATE_DIR", "/var/lib/zenplus-storage")
MOUNTS = ("/", "/data")
INTERVAL_S = 30
BREAKDOWN_EVERY_S = 300
LEGACY_SWEEP_EVERY_S = 3600
SAMPLE_KEEP_S = 2 * 3600
HISTORY_EVERY_S = 300
HISTORY_KEEP_S = 48 * 3600
AUDIT_MAX_BYTES = 512 * 1024
REPEAT_EVENT_EVERY_S = 3600
STATE_RANK = {"ok": 0, "growth": 1, "pressure": 2, "critical": 3}


def _path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def _atomic_write(name: str, data: Any) -> None:
    tmp = _path(f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str)
    os.chmod(tmp, 0o640)
    os.replace(tmp, _path(name))


def _read_json(name: str) -> Optional[dict]:
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def append_audit(record_type: str, details: dict, now: float) -> dict:
    record = {"id": str(uuid.uuid4()), "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
              "type": record_type, "details": details}
    path = _path("audit.jsonl")
    try:
        if os.path.exists(path) and os.path.getsize(path) > AUDIT_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.exception("could not write the storage audit trail")
    logger.warning("storage watchdog: %s %s", record_type, json.dumps(details, default=str)[:500])
    return record


def read_audit(limit: int = 200) -> list[dict]:
    """Newest-last audit records from the current and rotated files."""
    lines: list[str] = []
    for name in ("audit.jsonl.1", "audit.jsonl"):
        try:
            with open(_path(name), "r", encoding="utf-8") as f:
                lines.extend(f.readlines())
        except OSError:
            continue
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def sample_filesystems() -> list[P.FsSample]:
    out = []
    for mount in MOUNTS:
        try:
            st = os.statvfs(mount)
            dev = os.stat(mount).st_dev
        except OSError:
            continue
        out.append(P.FsSample(
            mount=mount, device=dev,
            total_bytes=st.f_blocks * st.f_frsize,
            used_bytes=(st.f_blocks - st.f_bfree) * st.f_frsize,
            avail_bytes=st.f_bavail * st.f_frsize,
        ))
    return P.dedupe_filesystems(out)


def clickhouse_client():
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        connect_timeout=5, send_receive_timeout=120,
        # The watchdog's own queries must not add to the diagnostic logs.
        settings={"log_queries": 0},
    )


# ─── ClickHouse reads ────────────────────────────────────────────────────────

def ch_partitions(client) -> list[dict]:
    rows = client.query(
        "SELECT database, table, partition_id, sum(bytes_on_disk), max(max_date), max(max_time) "
        "FROM system.parts WHERE active AND database IN ('system', %(db)s) "
        "GROUP BY database, table, partition_id",
        parameters={"db": P.CH_DATABASE},
    ).result_rows
    return [{"database": r[0], "table": r[1], "partition_id": r[2], "bytes": int(r[3]),
             "newest": P.newest_date(r[4], r[5])} for r in rows]


def ch_breakdown(client) -> dict:
    """Active/inactive bytes per table and detached bytes, for the Storage page."""
    rows = client.query(
        "SELECT database, table, active, sum(bytes_on_disk), count() FROM system.parts "
        "GROUP BY database, table, active"
    ).result_rows
    tables: dict[str, dict] = {}
    for db, table, active, size, parts in rows:
        t = tables.setdefault(f"{db}.{table}", {"database": db, "table": table,
                                                "active_bytes": 0, "inactive_bytes": 0, "parts": 0})
        t["active_bytes" if active else "inactive_bytes"] += int(size)
        if active:
            t["parts"] += int(parts)
    detached = 0
    try:
        detached = int(client.query(
            "SELECT sum(bytes_on_disk) FROM system.detached_parts").result_rows[0][0] or 0)
    except Exception:
        pass  # older servers have no bytes_on_disk in detached_parts
    return {"tables": tables, "detached_bytes": detached}


def summarize_breakdown(bd: dict) -> dict:
    app_active = diag_active = inactive = 0
    for t in bd["tables"].values():
        inactive += t["inactive_bytes"]
        if t["database"] == "system" and P.diagnostic_log(t["table"]):
            diag_active += t["active_bytes"]
        else:
            app_active += t["active_bytes"]
    return {"application_bytes": app_active, "diagnostic_bytes": diag_active,
            "inactive_bytes": inactive, "detached_bytes": bd["detached_bytes"]}


# ─── The watchdog ────────────────────────────────────────────────────────────

class Watchdog:
    def __init__(self,
                 sampler: Callable[[], list[P.FsSample]] = sample_filesystems,
                 client_factory: Callable[[], Any] = clickhouse_client,
                 clock: Callable[[], float] = time.time):
        self.sampler = sampler
        self.client_factory = client_factory
        self.clock = clock
        self._client = None
        prior = _read_json("state.json") or {}
        self.pending: list[dict] = prior.get("pending") or []
        self.passes: dict[str, dict] = prior.get("open_passes") or {}
        self.samples: dict[str, list] = prior.get("samples") or {}
        self.history: list = prior.get("history") or []
        self.snapshots: list = prior.get("table_snapshots") or []
        self.breakdown: Optional[dict] = prior.get("breakdown")
        self.last_breakdown_at = 0.0
        self.last_legacy_sweep_at = 0.0
        self.last_event_at: dict[str, float] = prior.get("last_event_at") or {}
        self.last_pass: Optional[dict] = prior.get("last_pass")
        self.last_error: Optional[dict] = prior.get("last_error")
        self.paused: bool = os.path.exists(_path("ingest-paused"))

    # ClickHouse access tolerates outages: every tick reconnects as needed.
    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def _ch(self, fn, *args):
        try:
            return fn(self.client(), *args)
        except Exception:
            self._client = None
            raise

    def _event_once(self, key: str, record_type: str, details: dict, now: float) -> None:
        if now - self.last_event_at.get(key, 0) >= REPEAT_EVENT_EVERY_S:
            append_audit(record_type, details, now)
            self.last_event_at[key] = now

    def policy(self) -> dict:
        return P.normalize_policy((_read_json("policy.json") or {}).get("policy"))

    def _record_samples(self, fss: list[P.FsSample], now: float) -> None:
        for fs in fss:
            key = str(fs.device)
            series = [s for s in self.samples.get(key, []) if now - s[0] <= SAMPLE_KEEP_S]
            series.append([now, fs.used_bytes])
            self.samples[key] = series
        if not self.history or now - self.history[-1]["t"] >= HISTORY_EVERY_S:
            self.history.append({"t": now, "fs": {str(f.device): [f.used_bytes, f.avail_bytes]
                                                  for f in fss}})
            self.history = [h for h in self.history if now - h["t"] <= HISTORY_KEEP_S]

    def _settle_passes(self, fss: list[P.FsSample], now: float) -> None:
        """Record measured reclamation once a pass's deletions had time to land."""
        self.pending = [p for p in self.pending if now - p["at"] < P.PENDING_RECLAIM_WINDOW_S]
        by_dev = {str(f.device): f for f in fss}
        for pass_id, info in list(self.passes.items()):
            if now - info["at"] < P.PENDING_RECLAIM_WINDOW_S:
                continue
            fs = by_dev.get(info["device"])
            measured = (fs.avail_bytes - info["avail_before"]) if fs else None
            append_audit("watchdog_reclaim", {
                "pass_id": pass_id, "scheduled_bytes": info["scheduled"],
                "measured_free_change_bytes": measured,
                "note": "measured change includes concurrent writes",
            }, now)
            del self.passes[pass_id]

    def _maybe_breakdown(self, now: float) -> None:
        if now - self.last_breakdown_at < BREAKDOWN_EVERY_S:
            return
        self.last_breakdown_at = now
        bd = self._ch(ch_breakdown)
        sizes = {k: v["active_bytes"] + v["inactive_bytes"] for k, v in bd["tables"].items()}
        self.snapshots = [s for s in self.snapshots if now - s["t"] <= 26 * 3600]
        if not self.snapshots or now - self.snapshots[-1]["t"] >= 3600:
            self.snapshots.append({"t": now, "sizes": sizes})
        growth = []
        hour_ago = next((s for s in reversed(self.snapshots) if now - s["t"] >= 3000), None)
        if hour_ago:
            span_h = (now - hour_ago["t"]) / 3600
            for k, size in sizes.items():
                delta = size - hour_ago["sizes"].get(k, 0)
                if delta > 0:
                    growth.append({"table": k, "bytes_per_hour": int(delta / span_h), "bytes": size})
            growth.sort(key=lambda g: -g["bytes_per_hour"])
        top = sorted(({"table": k, **v} for k, v in bd["tables"].items()),
                     key=lambda t: -(t["active_bytes"] + t["inactive_bytes"]))[:25]
        self.breakdown = {"at": now, **summarize_breakdown(bd), "top_tables": top,
                          "growth": growth[:10]}

    def _sweep_legacy(self, today: date, now: float) -> None:
        if now - self.last_legacy_sweep_at < LEGACY_SWEEP_EVERY_S:
            return
        self.last_legacy_sweep_at = now
        candidates = P.build_candidates(self._ch(ch_partitions), today, P.DEFAULT_POLICY)
        expired = P.legacy_expired(candidates, today)
        if expired:
            self._execute(expired, None, "legacy_cleanup", now)

    def _execute(self, plan: list[P.Candidate], fs: Optional[P.FsSample], reason: Any,
                 now: float) -> dict:
        pass_id = str(uuid.uuid4())
        done, errors, scheduled = [], [], 0
        for c in plan:
            try:
                self._ch(lambda cl, sql: cl.command(sql), c.sql())
            except Exception as exc:
                errors.append(f"{c.database}.{c.table}: {str(exc)[:200]}")
                continue
            scheduled += c.bytes
            done.append({"tier": c.tier, "action": c.action, "table": f"{c.database}.{c.table}",
                         "partition_id": c.partition_id, "bytes": c.bytes,
                         "newest": c.newest.isoformat() if c.newest else None})
            if not c.reclaims_immediately:
                self.pending.append({"at": now, "bytes": c.bytes, "pass_id": pass_id})
        record = {"pass_id": pass_id, "reason": reason, "actions": done, "errors": errors,
                  "scheduled_bytes": scheduled,
                  "avail_before": fs.avail_bytes if fs else None,
                  "usage_pct_before": fs.usage_pct if fs else None}
        append_audit("watchdog_purge", record, now)
        if fs is not None and scheduled:
            self.passes[pass_id] = {"at": now, "device": str(fs.device),
                                    "avail_before": fs.avail_bytes, "scheduled": scheduled}
        self.last_pass = {"at": now, **{k: v for k, v in record.items() if k != "actions"},
                          "action_count": len(done)}
        return record

    def _set_paused(self, paused: bool, reason: str, now: float) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            with open(_path("ingest-paused"), "w", encoding="utf-8") as f:
                json.dump({"since": now, "reason": reason}, f)
            os.chmod(_path("ingest-paused"), 0o644)
            append_audit("watchdog_ingest_paused", {"reason": reason}, now)
        else:
            try:
                os.unlink(_path("ingest-paused"))
            except FileNotFoundError:
                pass
            append_audit("watchdog_ingest_resumed", {"reason": reason}, now)

    def tick(self) -> dict:
        now = self.clock()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        policy = self.policy()
        # Deduplicate here, not only in the default sampler: evaluating a shared
        # / and /data twice would run two purge passes for one filesystem.
        fss = P.dedupe_filesystems(self.sampler())
        self._record_samples(fss, now)
        self._settle_passes(fss, now)
        error = None
        try:
            self._maybe_breakdown(now)
            self._sweep_legacy(today, now)
        except Exception as exc:
            error = f"ClickHouse: {str(exc)[:300]}"

        evaluations = []
        worst = "ok"
        for fs in fss:
            pending = P.pending_bytes(self.pending, now)
            rate = P.growth_rate([tuple(s) for s in self.samples.get(str(fs.device), [])])
            pressure = P.evaluate_pressure(fs, policy, pending, rate)
            plan: list[P.Candidate] = []
            exhausted = False
            if pressure.need_bytes > 0 and policy["enabled"]:
                try:
                    candidates = P.build_candidates(self._ch(ch_partitions), today, policy)
                    allow_app = P.application_allowed(candidates, pressure, fs.usage_pct)
                    plan = P.plan_pass(candidates, pressure, allow_application=allow_app)
                    if "application" in pressure.tiers and not allow_app:
                        self._event_once(f"futile:{fs.device}", "watchdog_application_spared", {
                            "mount": fs.mount, "usage_pct": fs.usage_pct,
                            "need_bytes": pressure.need_bytes,
                            "note": "deleting all eligible monitoring data could not reach the "
                                    "target; the space is used by other files. Monitoring data "
                                    f"is only deleted from {P.APPLICATION_LAST_RESORT_PCT:.0f}% usage.",
                        }, now)
                    if plan:
                        self._execute(plan, fs, pressure.reasons, now)
                    else:
                        exhausted = True
                        self._event_once(f"exhausted:{fs.device}", "watchdog_exhausted", {
                            "mount": fs.mount, "usage_pct": fs.usage_pct,
                            "need_bytes": pressure.need_bytes, "reasons": pressure.reasons,
                            "note": "no eligible data left to delete within policy "
                                    f"(minimum keep {policy['min_keep_days']} days)",
                        }, now)
                except Exception as exc:
                    error = f"ClickHouse: {str(exc)[:300]}"
            if pressure.state == "critical" and (exhausted or error or not policy["enabled"]):
                self._set_paused(True, f"{fs.mount}: {', '.join(pressure.reasons)}", now)
            evaluations.append({
                "mount": fs.mount, "device": fs.device, "total_bytes": fs.total_bytes,
                "used_bytes": fs.used_bytes, "avail_bytes": fs.avail_bytes,
                "reserved_bytes": fs.reserved_bytes, "usage_pct": fs.usage_pct,
                "state": pressure.state, "reasons": pressure.reasons,
                "need_bytes": pressure.need_bytes,
                "runway_hours": pressure.runway_hours,
                "growth_bytes_per_hour": pressure.rate_bytes_per_hour,
                "exhausted": exhausted, "planned_actions": len(plan),
            })
            if STATE_RANK[pressure.state] > STATE_RANK[worst]:
                worst = pressure.state

        # Resume only with margin above the free-space floor (hysteresis).
        if self.paused and worst in ("ok", "growth") and all(
                e["avail_bytes"] >= policy["min_free_gib"] * P.GIB for e in evaluations):
            self._set_paused(False, "free space recovered", now)

        if error:
            self.last_error = {"at": now, "message": error}
            self._event_once("error", "watchdog_error", {"message": error}, now)

        state = {
            "version": 1,
            "heartbeat": now,
            "pid": os.getpid(),
            "interval_s": INTERVAL_S,
            "policy": policy,
            "state": worst,
            "filesystems": evaluations,
            "pending_bytes": P.pending_bytes(self.pending, now),
            "pending": self.pending,
            "open_passes": self.passes,
            "last_pass": self.last_pass,
            "last_error": self.last_error,
            "ingest_paused": self.paused,
            "breakdown": self.breakdown,
            "samples": self.samples,
            "history": self.history,
            "table_snapshots": self.snapshots,
            "last_event_at": self.last_event_at,
        }
        _atomic_write("state.json", state)
        return state


def run_tick_once(client_factory: Callable[[], Any]) -> Optional[dict]:
    """One tick under the service lock (API fallback when the service is absent).

    Returns None when the watchdog service holds the lock.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_path("watchdog.lock"), "w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        return Watchdog(client_factory=client_factory).tick()


def ingest_paused() -> Optional[dict]:
    """The pause marker's contents while free space is critical, else None."""
    try:
        with open(_path("ingest-paused"), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"reason": "free space critically low"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(_path("watchdog.lock"), "w", encoding="ascii")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("another storage watchdog is running")
        return 1
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    dog = Watchdog()
    while not stop["flag"]:
        started = time.monotonic()
        try:
            dog.tick()
        except Exception:
            logger.exception("storage watchdog tick failed")
        remaining = INTERVAL_S - (time.monotonic() - started)
        while remaining > 0 and not stop["flag"]:
            time.sleep(min(1.0, remaining))
            remaining -= 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
