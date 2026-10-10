"""Regression tests of the Phase 1 review for the export and the settings write (be-b fix round).

Each test names the finding it pins: F1 (an export or a settings change by a role whose name is longer than 20
characters was silently not audited; the audit write now fails closed), F3 (a client that disconnects mid-export left a
PostgreSQL backend idle in its transaction), F8 (no cap on concurrent exports), BN-08 (extreme since / until values),
F11 / BN-07 (the settings write is a locked read-modify-write; see also the concurrency test at the end).

The disconnect tests cancel the consumer of the stream at several moments, the way Starlette cancels a response
generator when the client goes away, and then look at ``pg_stat_activity`` of the test database: a leaked backend would
still be "idle in transaction" holding its snapshot.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from app.services import inventory_export as export
from app.services import inventory_query as iq
from app.services import inventory_trends_settings as its
from inventory_fleet import (
    FIXED_NOW, aid, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
EXPORT = f"{API}/export"
ALL_PERMS = ["inventory.view", "inventory.export", "inventory.manage"]
LONG_ROLE = "regional_security_operations"                       # 28 characters: what the role editor stores for that display name
LONGEST_ROLE = "r" * 50                                           # roles.name allows 50
MIGRATION_159 = Path(__file__).resolve().parents[2] / "scripts" / "migrate-159-audit-actor-role-width.sql"


def audit_rows(db, action: str) -> list[dict[str, Any]]:
    return [{"actor": r[0], "role": r[1], "type": r[2], "id": r[3], "metadata": r[4]} for r in db.query(
        "SELECT actor_username, actor_role, resource_type, resource_id, metadata FROM audit_logs WHERE action = $1 "
        "ORDER BY created_at, id", action)]


def leaked_backends(db) -> list[tuple]:
    """Backends of the test database that sit in a transaction doing nothing (what a leaked export leaves behind)."""
    return db.query(
        "SELECT pid, state FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend' AND state LIKE 'idle in transaction%'")


def make_audit_fail(db) -> None:
    db.psql("""
        CREATE OR REPLACE FUNCTION f1_audit_down() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'audit store is down'; END $$ LANGUAGE plpgsql;
        CREATE TRIGGER f1_audit_down BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION f1_audit_down();""")


def make_audit_work(db) -> None:
    db.psql("DROP TRIGGER IF EXISTS f1_audit_down ON audit_logs")


@pytest.fixture
def fresh_limiter(monkeypatch):
    """A limiter of the production size that no other test shares."""
    lim = export.ExportLimiter()
    monkeypatch.setattr(export, "limiter", lim)
    return lim


def seed_events(db, count: int) -> None:
    """``count`` software change events inside the default seven-day window (one per second, newest first)."""
    sid = db.scalar("SELECT id FROM servers ORDER BY display_name LIMIT 1")
    db.execute(
        "INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, asset_name, category, action, subject, "
        "subject_key, old_value, new_value) SELECT $1::timestamptz - (g || ' seconds')::interval, 'server', $2, 'seeded', "
        "'software', 'changed', 'App ' || g, 'app-' || g, '1.0', '2.0' FROM generate_series(1, $3) g", FIXED_NOW, sid, count)


# ════════════════════════════════════════════════════════════════════
# F1: the audit entry is part of the action
# ════════════════════════════════════════════════════════════════════

def test_f1_audit_logs_actor_role_is_as_wide_as_every_role_name(fleet_db):
    width = lambda table, column: fleet_db.scalar(      # noqa: E731
        "SELECT character_maximum_length FROM information_schema.columns WHERE table_name = $1 AND column_name = $2", table, column)
    assert width("audit_logs", "actor_role") >= max(width("roles", "name"), width("users", "role")) == 50


def test_f1_migration_159_widens_a_narrow_column_and_a_replay_changes_nothing(fleet_db_fresh):
    db = fleet_db_fresh
    db.execute("INSERT INTO audit_logs (actor_username, actor_role, action, resource_type) VALUES ('keep', 'viewer', 'x.test', 't')")
    db.execute("ALTER TABLE audit_logs ALTER COLUMN actor_role TYPE VARCHAR(20)")
    for _ in range(2):                                   # the second run is the replay
        db.psql_file(MIGRATION_159)
        assert db.scalar("SELECT character_maximum_length FROM information_schema.columns "
                         "WHERE table_name = 'audit_logs' AND column_name = 'actor_role'") == 50
        assert db.scalar("SELECT actor_role FROM audit_logs WHERE actor_username = 'keep'") == "viewer"
    db.execute("INSERT INTO audit_logs (actor_username, actor_role, action, resource_type) VALUES ('long', $1, 'x.test', 't')", LONGEST_ROLE)


@pytest.mark.parametrize("role", [LONG_ROLE, LONGEST_ROLE])
def test_f1_an_export_by_a_long_named_role_is_audited(fleet_db_fresh, pinned_clock, fresh_limiter, role):
    with api_client(fleet_db_fresh, role=role, permissions=ALL_PERMS, username="long.role", persist_user=True) as c:
        resp = c.get(EXPORT, params={"dataset": "assets", "kind": "server"})
        assert resp.status_code == 200 and resp.headers["X-Export-Rows"] != "0"
    [row] = audit_rows(fleet_db_fresh, "inventory.export")
    assert (row["actor"], row["role"], row["type"], row["id"]) == ("long.role", role, "inventory_export", "assets")
    assert row["metadata"]["rows"] == int(resp.headers["X-Export-Rows"])


@pytest.mark.parametrize("role", [LONG_ROLE, LONGEST_ROLE])
def test_f1_a_settings_change_by_a_long_named_role_is_audited(fleet_db_fresh, role):
    with api_client(fleet_db_fresh, role=role, permissions=ALL_PERMS, username="long.role", persist_user=True) as c:
        assert c.put(f"{API}/settings", json={"stale_days": 21}).status_code == 200
    [row] = audit_rows(fleet_db_fresh, "inventory.settings.update")
    assert (row["actor"], row["role"]) == ("long.role", role)
    assert row["metadata"] == {"changed": {"stale_days": {"from": 7, "to": 21}}}


def test_f1_an_export_that_cannot_be_audited_is_refused_before_the_first_byte(fleet_db_fresh, pinned_clock, fresh_limiter):
    make_audit_fail(fleet_db_fresh)
    with api_client(fleet_db_fresh, role="t_f1_export", permissions=ALL_PERMS, persist_user=True) as c:
        resp = c.get(EXPORT, params={"dataset": "assets"})
        assert resp.status_code == 503 and "audit" in resp.json()["detail"].lower()
        assert "attachment" not in resp.headers.get("content-disposition", "") and not resp.content.startswith(b"\xef\xbb\xbf")
        assert fresh_limiter.running == 0                                  # the slot of the refused export is back
        make_audit_work(fleet_db_fresh)
        assert c.get(EXPORT, params={"dataset": "assets"}).status_code == 200      # and the same request works once the log does
    assert len(audit_rows(fleet_db_fresh, "inventory.export")) == 1


def test_f1_a_settings_change_that_cannot_be_audited_is_not_saved(fleet_db_fresh):
    make_audit_fail(fleet_db_fresh)
    with api_client(fleet_db_fresh, role="t_f1_manage", permissions=ALL_PERMS, persist_user=True) as c:
        resp = c.put(f"{API}/settings", json={"stale_days": 30})
        assert resp.status_code == 503
        assert fleet_db_fresh.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory'") == 0     # not reported, not saved
        assert c.get(f"{API}/settings").json()["settings"]["stale_days"] == 7
        make_audit_work(fleet_db_fresh)
        assert c.put(f"{API}/settings", json={"stale_days": 30}).status_code == 200            # the advisory lock was released
    assert fleet_db_fresh.scalar("SELECT value->>'stale_days' FROM system_settings WHERE key = 'inventory'") == "30"
    assert len(audit_rows(fleet_db_fresh, "inventory.settings.update")) == 1


# ════════════════════════════════════════════════════════════════════
# BN-08: extreme dates are a 422 on the export too, and nothing is audited or kept
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("params", [
    {"since": "0001-01-01T00:00:00Z"}, {"until": "9999-12-31T23:59:59Z"}, {"since": "1969-12-31T23:59:59Z"},
    {"since": "2100-01-01T00:00:01Z"},
])
def test_bn08_the_changes_export_rejects_dates_outside_1970_to_2100(fleet_db_fresh, fresh_limiter, params):
    with api_client(fleet_db_fresh, role="t_bn08", permissions=ALL_PERMS, persist_user=True) as c:
        resp = c.get(EXPORT, params={"dataset": "changes", **params})
        assert resp.status_code == 422 and "must be between 1970-01-01 and 2100-01-01" in resp.json()["detail"]
    assert audit_rows(fleet_db_fresh, "inventory.export") == [] and fresh_limiter.running == 0


# ════════════════════════════════════════════════════════════════════
# F8: how many exports run at once
# ════════════════════════════════════════════════════════════════════

def test_f8_the_limiter_admits_two_in_all_and_one_per_user():
    lim = export.ExportLimiter()
    a1 = lim.acquire("a")
    with pytest.raises(export.ExportBusy) as same_user:
        lim.acquire("a")
    assert same_user.value.per_user is True
    b1 = lim.acquire("b")
    with pytest.raises(export.ExportBusy) as full:
        lim.acquire("c")
    assert full.value.per_user is False and lim.running == 2
    a1.release()
    a1.release()                                        # idempotent: one export gives back one slot
    assert lim.running == 1
    c1 = lim.acquire("c")
    b1.release(), c1.release()
    assert lim.running == 0 and not lim._by_user


def test_f8_the_limiter_holds_under_threads():
    lim = export.ExportLimiter(total=3, per_user=1)
    admitted, refused = [], []

    def worker(user):
        try:
            admitted.append(lim.acquire(user))
        except export.ExportBusy:
            refused.append(user)

    threads = [threading.Thread(target=worker, args=(f"u{i}",)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(admitted) == 3 and len(refused) == 37 and lim.running == 3


def test_f8_a_second_export_of_the_same_user_gets_429_with_retry_after(fleet_db_fresh, pinned_clock, fresh_limiter):
    with api_client(fleet_db_fresh, role="t_f8", permissions=ALL_PERMS, username="f8.user", persist_user=True) as c:
        held = fresh_limiter.acquire(aid("user", "f8.user"))
        resp = c.get(EXPORT, params={"dataset": "assets"})
        assert resp.status_code == 429 and resp.headers["Retry-After"] == str(export.RETRY_AFTER_S)
        assert "already have an export" in resp.json()["detail"]
        held.release()
        assert c.get(EXPORT, params={"dataset": "assets"}).status_code == 200
    assert len(audit_rows(fleet_db_fresh, "inventory.export")) == 1       # a refused request is not an export


def test_f8_a_third_export_in_the_process_gets_429(fleet_db_fresh, pinned_clock, fresh_limiter):
    with api_client(fleet_db_fresh, role="t_f8", permissions=ALL_PERMS, username="f8.third", persist_user=True) as c:
        busy = [fresh_limiter.acquire("someone"), fresh_limiter.acquire("someone-else")]
        resp = c.get(EXPORT, params={"dataset": "software"})
        assert resp.status_code == 429 and "too many exports" in resp.json()["detail"] and "Retry-After" in resp.headers
        busy[0].release()
        assert c.get(EXPORT, params={"dataset": "software"}).status_code == 200
        busy[1].release()
    assert fresh_limiter.running == 0


def test_f8_every_way_an_export_can_end_gives_its_slot_back(fleet_db_fresh, pinned_clock, fresh_limiter):
    with api_client(fleet_db_fresh, role="t_f8", permissions=ALL_PERMS, username="f8.paths", persist_user=True) as c:
        assert c.get(EXPORT, params={"dataset": "assets"}).status_code == 200                          # streamed to the end
        assert c.get(EXPORT, params={"dataset": "installations"}).status_code == 422                   # invalid request
        assert c.get(EXPORT, params={"dataset": "installations", "product": "no-such-product"}).status_code == 404
        assert c.get(EXPORT, params={"dataset": "changes", "category": ["os", "software"]}).status_code == 422
        make_audit_fail(fleet_db_fresh)
        assert c.get(EXPORT, params={"dataset": "assets"}).status_code == 503                          # audit down
        assert fresh_limiter.running == 0


# ════════════════════════════════════════════════════════════════════
# F3: a client that goes away never leaves a backend behind
# ════════════════════════════════════════════════════════════════════

CHANGES_EVENTS = 20_000                                # 40 batches of 500


async def leave_after(run, delay: float) -> None:
    """Read ``run`` until ``delay`` seconds have passed, then cancel the reader through an anyio cancel scope: that is how
    Starlette ends a response when the client disconnects (the cancellation is delivered again at every await until the
    scope is left, which is what breaks the clean-up of a connection cancelled mid-query)."""
    import anyio

    async def reader():
        with anyio.CancelScope() as scope:
            holder.append(scope)
            async for _chunk in run.chunks():
                await asyncio.sleep(0)

    holder: list[Any] = []
    task = asyncio.ensure_future(reader())
    await asyncio.sleep(delay)
    if holder:
        holder[0].cancel()
    await task


async def start_run(db, **plan_args):
    """``(engine-bound run, plan, close)`` of a changes export of the seeded events."""
    params = export.ExportParams(dataset="changes", changes=iq.ChangeFilters(**plan_args))
    cm = sessions(db)
    sm = await cm.__aenter__()
    async with sm() as s:
        plan = await export.plan_export(s, params, scope=None)
        bind = s.bind
    return bind, plan, cm


async def settle(run, db, *, timeout=30.0):
    """Wait for the producer to end the way it must: without an exception, and with its connection back."""
    await asyncio.wait_for(asyncio.shield(run.task), timeout)
    assert run.task.exception() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.0, 0.01, 0.03, 0.08, 0.2])
async def test_f3_a_client_that_leaves_mid_export_leaves_no_backend_behind(fleet_db_fresh, pinned_clock, fresh_limiter, delay):
    """The consumer is cancelled at different moments, as Starlette does when the client disconnects. The export used
    to run its queries inside that cancellable generator: a cancel landing inside a round trip left the connection's
    backend idle in transaction for the life of the worker."""
    seed_events(fleet_db_fresh, CHANGES_EVENTS)
    bind, plan, cm = await start_run(fleet_db_fresh)
    try:
        assert plan.total >= CHANGES_EVENTS
        slot = fresh_limiter.acquire("u")
        run = export.ExportRun(bind, plan, release=slot.release)

        await leave_after(run, delay)
        await settle(run, fleet_db_fresh)
        assert fresh_limiter.running == 0                                          # the slot came back
        assert leaked_backends(fleet_db_fresh) == []
        assert run.batches_read < plan.rows // export.BATCH                        # and it stopped early, not at the end of the file
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_f3_a_query_is_never_cancelled_in_flight(fleet_db_fresh, pinned_clock, fresh_limiter):
    """The consumer's cancel must not reach the producer: record any CancelledError inside the dataset's own reads."""
    seed_events(fleet_db_fresh, 5_000)
    bind, plan, cm = await start_run(fleet_db_fresh)
    inner = plan.batches
    log: list[str] = []

    def batches(s):
        async def gen():
            try:
                async for b in inner(s):
                    await asyncio.sleep(0.01)                 # a slow read: the cancel below lands while the producer is busy
                    yield b
            except asyncio.CancelledError:
                log.append("cancelled")
                raise
        return gen()

    plan.batches = batches
    try:
        run = export.ExportRun(bind, plan)
        await leave_after(run, 0.05)
        await settle(run, fleet_db_fresh)
        assert log == [] and leaked_backends(fleet_db_fresh) == []
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_f3_the_whole_file_reaches_a_client_that_stays(fleet_db_fresh, pinned_clock, fresh_limiter):
    seed_events(fleet_db_fresh, 3_000)
    bind, plan, cm = await start_run(fleet_db_fresh)
    try:
        slot = fresh_limiter.acquire("u")
        body = b"".join([c async for c in export.ExportRun(bind, plan, release=slot.release).chunks()]).decode("utf-8-sig")
        lines = [ln for ln in body.split("\r\n") if ln]
        assert len(lines) == plan.rows + 1 and fresh_limiter.running == 0
        legacy = b"".join([c async for c in export.stream_csv(bind, plan)]).decode("utf-8-sig")
        assert legacy == body                                                      # the wrapper serves the same bytes
        assert leaked_backends(fleet_db_fresh) == []
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_f3_the_snapshot_transaction_is_read_only_with_an_idle_timeout_backstop(fleet_db_fresh, pinned_clock):
    seen: dict[str, Any] = {}
    plan = export.ExportPlan(dataset="probe", columns=["a"], filters={}, total=1, rows=1, truncated=False, batches=None)  # type: ignore[arg-type]

    def batches(s):
        async def gen():
            for name in ("idle_in_transaction_session_timeout", "transaction_isolation", "transaction_read_only"):
                seen[name] = (await s.execute(__import__("sqlalchemy").text(f"SHOW {name}"))).scalar()
            yield [["x"]]
        return gen()

    plan.batches = batches
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            bind = s.bind
        body = b"".join([c async for c in export.stream_csv(bind, plan)]).decode("utf-8-sig")
    assert body == "a\r\nx\r\n"
    assert seen == {"idle_in_transaction_session_timeout": f"{export.IDLE_IN_TRANSACTION_S // 60}min"
                    if export.IDLE_IN_TRANSACTION_S % 60 == 0 else f"{export.IDLE_IN_TRANSACTION_S}s",
                    "transaction_isolation": "repeatable read", "transaction_read_only": "on"}


@pytest.mark.asyncio
async def test_f3_a_client_that_stays_connected_but_stops_reading_is_abandoned(fleet_db_fresh, pinned_clock, fresh_limiter, monkeypatch):
    """A stalled client must not hold a connection and a snapshot for as long as it sleeps; when it comes back it gets an
    error, never a short file that looks complete."""
    monkeypatch.setattr(export, "CONSUMER_STALL_S", 0.3)
    monkeypatch.setattr(export, "QUEUE_DEPTH", 2)
    seed_events(fleet_db_fresh, 10_000)
    bind, plan, cm = await start_run(fleet_db_fresh)
    try:
        slot = fresh_limiter.acquire("u")
        run = export.ExportRun(bind, plan, release=slot.release)
        chunks = run.chunks()
        header = await chunks.__anext__()                                           # the client reads the header ... and falls asleep
        assert header.startswith(b"\xef\xbb\xbf")
        await settle(run, fleet_db_fresh, timeout=10)
        assert fresh_limiter.running == 0 and leaked_backends(fleet_db_fresh) == []
        with pytest.raises(export.ExportStalled):
            while True:
                await chunks.__anext__()
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_f3_a_response_that_is_never_iterated_does_not_hold_its_slot_forever(fleet_db_fresh, pinned_clock, fresh_limiter, monkeypatch):
    monkeypatch.setattr(export, "CONSUMER_STALL_S", 0.3)
    monkeypatch.setattr(export, "QUEUE_DEPTH", 1)
    seed_events(fleet_db_fresh, 5_000)
    bind, plan, cm = await start_run(fleet_db_fresh)
    try:
        slot = fresh_limiter.acquire("u")
        run = export.ExportRun(bind, plan, release=slot.release)
        await settle(run, fleet_db_fresh, timeout=10)
        assert fresh_limiter.running == 0 and leaked_backends(fleet_db_fresh) == []
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_f3_a_failure_in_the_middle_ends_the_stream_in_an_error_and_returns_the_connection(fleet_db_fresh, pinned_clock, fresh_limiter):
    boom = RuntimeError("read failed")
    plan = export.ExportPlan(dataset="probe", columns=["a"], filters={}, total=3, rows=3, truncated=False, batches=None)  # type: ignore[arg-type]

    def batches(s):
        async def gen():
            yield [["first"]]
            raise boom
        return gen()

    plan.batches = batches
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            bind = s.bind
        slot = fresh_limiter.acquire("u")
        got: list[bytes] = []
        with pytest.raises(RuntimeError, match="read failed"):
            async for chunk in export.ExportRun(bind, plan, release=slot.release).chunks():
                got.append(chunk)
        assert got[-1] == b"first\r\n" and fresh_limiter.running == 0
        assert leaked_backends(fleet_db_fresh) == []


# -- the same through the real route, with the disconnect delivered the way uvicorn delivers it --

async def asgi_get(app, path: str, params: dict[str, Any], *, leave_after: int | None = None):
    """One GET through the ASGI app. With ``leave_after`` the client disconnects once that many body bytes arrived
    (ASGI spec 2.3 as uvicorn 0.34 reports it: Starlette cancels the response through an anyio cancel scope)."""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": urlencode(params, doseq=True).encode(),
             "root_path": "", "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    gone, asked = asyncio.Event(), False
    status, body = None, bytearray()

    async def receive():
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if leave_after is not None and len(body) >= leave_after:
                gone.set()          # (send never suspends, as uvicorn's rarely does: the cancel lands in the generator's own awaits)

    await app(scope, receive, send)
    return status, bytes(body)


@pytest.mark.parametrize("leave_after", [1, 40_000, 400_000])
def test_f3_through_the_route_a_disconnect_leaves_no_backend_and_frees_the_slot(fleet_db_fresh, pinned_clock, fresh_limiter, leave_after):
    seed_events(fleet_db_fresh, 12_000)
    with api_client(fleet_db_fresh, role="t_f3", permissions=ALL_PERMS, username="f3.user", persist_user=True) as c:
        status, body = asyncio.run(asgi_get(c.app, EXPORT, {"dataset": "changes"}, leave_after=leave_after))
        assert status == 200 and len(body) < 12_000 * 60                              # the client left before the end
        for _ in range(300):                                                           # the producer ends at its next batch
            if fresh_limiter.running == 0:
                break
            threading.Event().wait(0.1)
        assert fresh_limiter.running == 0
        threading.Event().wait(0.3)
        assert leaked_backends(fleet_db_fresh) == []
        again = c.get(EXPORT, params={"dataset": "changes"})                           # and the user may export again at once
        assert again.status_code == 200 and again.headers["X-Export-Rows"] == str(again.content.count(b"\r\n") - 1)


# ════════════════════════════════════════════════════════════════════
# F11 / BN-07: the settings write is a locked read-modify-write
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_f11_bn07_two_administrators_saving_different_keys_both_keep_their_change(fleet_db_fresh, monkeypatch):
    """Each writer reads the current settings, merges only its own key and stores the whole blob. Without a lock both read
    the same blob and the second store reverts the first change (both were audited, one was lost). The read is slowed down
    so that the two writers overlap for certain."""
    from app.services import inventory_service as svc
    from inventory_fleet import ensure_role, ensure_user, make_user

    real = svc.get_inventory_settings

    async def slow_read(db):
        found = await real(db)
        await asyncio.sleep(0.3)
        return found

    monkeypatch.setattr(svc, "get_inventory_settings", slow_read)
    ensure_role(fleet_db_fresh, "t_f11", ["inventory.manage"])
    admins = []
    for name in ("admin.a", "admin.b"):
        user = make_user("t_f11", username=name, user_id=aid("user", name))
        ensure_user(fleet_db_fresh, user.id, name, "t_f11")
        admins.append(user)

    async with sessions(fleet_db_fresh) as sm:
        async def save(user, body):
            async with sm() as s:
                return await its.update_settings(s, user, body)

        first, second = await asyncio.gather(save(admins[0], {"stale_days": 30}), save(admins[1], {"snapshot_hour": 5}))
    stored = fleet_db_fresh.scalar("SELECT value FROM system_settings WHERE key = 'inventory'")
    assert stored["stale_days"] == 30 and stored["snapshot_hour"] == 5                  # neither change was lost
    rows = audit_rows(fleet_db_fresh, "inventory.settings.update")
    assert len(rows) == 2
    changes = {r["actor"]: r["metadata"]["changed"] for r in rows}
    assert changes == {"admin.a": {"stale_days": {"from": 7, "to": 30}}, "admin.b": {"snapshot_hour": {"from": 0, "to": 5}}}
    # whoever went second saw the first one's result: the response of the later writer carries both
    both = [r["settings"] for r in (first, second) if r["settings"]["stale_days"] == 30 and r["settings"]["snapshot_hour"] == 5]
    assert len(both) == 1


def test_bn07_lowering_stale_days_reports_the_fresh_hours_it_capped(fleet_db_fresh):
    """stale_days 3 caps fresh_hours.server (24 h default is fine, a stored 600 h is not): the response said changed
    fresh_hours but adjusted [] although nobody asked for that number."""
    with api_client(fleet_db_fresh, role="t_bn07", permissions=ALL_PERMS, persist_user=True) as c:
        assert c.put(f"{API}/settings", json={"stale_days": 30, "fresh_hours": {"server": 600}}).status_code == 200
        body = c.put(f"{API}/settings", json={"stale_days": 3}).json()
    assert body["settings"]["stale_days"] == 3 and body["settings"]["fresh_hours"]["server"] == 72
    assert set(body["changed"]) == {"stale_days", "fresh_hours"} and body["adjusted"] == ["fresh_hours.server"]
