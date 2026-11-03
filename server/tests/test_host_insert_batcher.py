"""Group commit for host telemetry: fewer, larger inserts with durable acks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.services import host_insert_batcher as B
from app.services import host_metric_service as metrics


class FakeClickHouse:
    def __init__(self, fail_times=0, land_despite_failure=False,engine='MergeTree',dependencies=0):
        self.inserts: list[dict] = []
        self.describes = 0
        self.fail_times = fail_times
        self.land_despite_failure = land_despite_failure
        self.engine=engine
        self.dependencies=dependencies
        self.stored: list[tuple] = []

    class _R:
        def __init__(self, rows):
            self.result_rows = rows

    def query(self, sql, parameters=None):
        if sql.startswith('SELECT engine,length(dependencies_table)'):
            return self._R([[self.engine,self.dependencies]])
        if sql.startswith("DESCRIBE"):
            self.describes += 1
            return self._R([["server_id", "String"], ["agent_id", "String"],
                            ["timestamp", "DateTime64(3)"], ["v", "Float64"]])
        assert "toDateTime64" in sql and "'UTC'" in sql
        a, ts = parameters["a"], set(parameters["ts"])
        return self._R([[sum(1 for r in self.stored if r[1] == a and B._ts_literal(r[2]) in ts)]])

    def insert(self, table, rows, column_names=None, column_type_names=None, settings=None):
        assert column_type_names, "column types must be passed (no per-insert DESCRIBE)"
        if self.fail_times:
            self.fail_times -= 1
            if self.land_despite_failure:
                self.stored.extend(tuple(r) for r in rows)
            raise ConnectionError("response lost")
        self.inserts.append({"table": table, "rows": list(rows), "token":
                             settings["insert_deduplication_token"]})
        self.stored.extend(tuple(r) for r in rows)


COLS = ["server_id", "agent_id", "timestamp", "v"]
TS = datetime(2026, 10, 10, tzinfo=timezone.utc)


def row(agent, i=0):
    return ["srv", agent, TS.replace(second=i), 1.0]


@pytest.fixture(autouse=True)
def _fresh_type_cache(monkeypatch):
    monkeypatch.setattr(B, "RETRY_WINDOW_S", 0.3)
    B._column_types.clear()
    yield
    B._column_types.clear()


def run(coro):
    return asyncio.run(coro)


def test_concurrent_uploads_become_one_insert_and_ack_after_it():
    ch = FakeClickHouse()

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.05)
        await asyncio.gather(*(b.insert("host_cpu_metrics", COLS, [row(f"a{i}")], f"tok{i}")
                               for i in range(50)))
        return b
    b = run(main())
    assert len(ch.inserts) == 1 and len(ch.inserts[0]["rows"]) == 50
    assert ch.describes == 1
    assert b.stats["members"] == 50 and b.stats["inserts"] == 1


def test_single_upload_keeps_its_own_token():
    ch = FakeClickHouse()

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        await b.insert("host_cpu_metrics", COLS, [row("a")], "tok-a")
    run(main())
    assert ch.inserts[0]["token"] == "tok-a"


def test_size_threshold_flushes_without_waiting():
    ch = FakeClickHouse()

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_rows=10, max_delay_s=60)
        await asyncio.wait_for(asyncio.gather(*(
            b.insert("t", COLS, [row(f"a{i}", j) for j in range(5)], f"tok{i}") for i in range(2))), 2)
    run(main())
    assert len(ch.inserts) == 1 and len(ch.inserts[0]["rows"]) == 10


def test_transient_failure_retries_the_same_block_and_token():
    ch = FakeClickHouse(fail_times=1)

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        await asyncio.gather(*(b.insert("t", COLS, [row(f"a{i}")], f"tok{i}") for i in range(3)))
    run(main())
    assert len(ch.inserts) == 1 and len(ch.inserts[0]["rows"]) == 3


def test_persistent_failure_fails_every_upload_for_replay():
    ch = FakeClickHouse(fail_times=10)

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        return await asyncio.gather(*(b.insert("t", COLS, [row(f"a{i}")], f"tok{i}")
                                      for i in range(3)), return_exceptions=True)
    results = run(main())
    assert all(isinstance(r, ConnectionError) for r in results)


def test_lost_response_acknowledges_only_uploads_whose_rows_are_stored():
    # The block landed although every attempt reported failure.
    ch = FakeClickHouse(fail_times=10, land_despite_failure=True)
    ch.stored.clear()

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        return await asyncio.gather(*(b.insert("t", COLS, [row(f"a{i}")], f"tok{i}")
                                      for i in range(3)), return_exceptions=True)
    assert run(main()) == [None, None, None]


@pytest.mark.parametrize('engine,dependencies',[
    ('ReplicatedMergeTree',0),('ReplicatedMergeTree',1),('MergeTree',1)])
def test_unknown_quorum_or_view_outcome_is_not_acknowledged_from_raw_rows(engine,dependencies):
    ch=FakeClickHouse(fail_times=100,land_despite_failure=True,
                     engine=engine,dependencies=dependencies)
    async def main():
        b=B.HostInsertBatcher(lambda:ch,max_delay_s=0.01)
        with pytest.raises(ConnectionError):
            await b.insert('host_cpu_metrics',COLS,[row('a')],'same-upload-token')
        assert ch.stored,'Raw rows must exist to exercise the false acknowledgement'
        assert b.stats['verified']==0
    run(main())


def test_presence_confirmation_is_bound_to_the_actual_database_and_unknown_metadata_refuses():
    class Client:
        def query(self,sql,parameters):
            assert parameters=={'database':'zenplus','name':'host_cpu_metrics'}
            return FakeClickHouse._R([])
    assert not B.presence_can_confirm(Client(),'zenplus.host_cpu_metrics')
    assert not B.presence_can_confirm(Client(),'host_cpu_metrics; SELECT 1')


def test_previously_stored_row_does_not_vouch_for_other_uploads():
    ch = FakeClickHouse(fail_times=10)
    ch.stored.append(tuple(row("a0")))          # a0 was stored by an earlier upload

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        return await asyncio.gather(*(b.insert("t", COLS, [row(f"a{i}")], f"tok{i}")
                                      for i in range(3)), return_exceptions=True)
    results = run(main())
    assert results[0] is None
    assert isinstance(results[1], ConnectionError) and isinstance(results[2], ConnectionError)


def test_cancelled_caller_does_not_orphan_the_write_and_replay_joins_it():
    ch = FakeClickHouse()

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.05)
        first = asyncio.create_task(b.insert("t", COLS, [row("a")], "tok-a"))
        await asyncio.sleep(0.01)
        first.cancel()                         # HTTP deadline
        with pytest.raises(asyncio.CancelledError):
            await first
        await b.insert("t", COLS, [row("a")], "tok-a")   # agent replays the same upload
        await asyncio.sleep(0.1)
        return b
    b = run(main())
    assert len(ch.inserts) == 1 and len(ch.stored) == 1
    assert b.stats["replays_joined"] == 1


def test_failed_token_may_be_written_again():
    ch = FakeClickHouse(fail_times=3)

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        with pytest.raises(ConnectionError):
            await b.insert("t", COLS, [row("a")], "tok-a")
        await b.insert("t", COLS, [row("a")], "tok-a")
    run(main())
    assert len(ch.inserts) == 1


def test_host_metric_ingest_batches_across_uploads(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(metrics, "get_clickhouse_client", lambda: ch)
    monkeypatch.setattr(B, "MAX_DELAY_S", 0.05)
    monkeypatch.setattr(B, "column_type_names", lambda client, table, columns: ["String"] * len(columns))

    async def main():
        B._batchers.clear()
        return await asyncio.gather(*(
            metrics._persist_metric_groups(f"agent{i}", "srv", f"batch{i}", {
                "cpu": [{"timestamp": TS, "cpu_total_pct": 5}],
                "memory": [{"timestamp": TS, "total_bytes": 10, "used_bytes": 5}],
            }) for i in range(20)))
    accepted = run(main())
    assert accepted == [2] * 20
    assert sorted(i["table"] for i in ch.inserts) == ["host_cpu_metrics", "host_memory_metrics"]
    assert all(len(i["rows"]) == 20 for i in ch.inserts)


def test_host_metric_ingest_failure_names_the_kind(monkeypatch):
    ch = FakeClickHouse(fail_times=100)
    monkeypatch.setattr(metrics, "get_clickhouse_client", lambda: ch)
    monkeypatch.setattr(B, "column_type_names", lambda client, table, columns: ["String"] * len(columns))

    async def main():
        B._batchers.clear()
        await metrics._persist_metric_groups("a", "srv", "b", {"cpu": [{"timestamp": TS}]})
    with pytest.raises(metrics.HostMetricStorageError) as exc:
        run(main())
    assert exc.value.part == "cpu"


def test_schema_change_is_described_again(monkeypatch):
    ch = FakeClickHouse()
    calls = {"n": 0}
    real = B.column_type_names

    def stale_then_real(client, table, columns):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("v")       # column added by a migration, not in the cache
        return real(client, table, columns)
    monkeypatch.setattr(B, "column_type_names", stale_then_real)
    B.insert_rows(ch, "t", COLS, [row("a")], "tok")
    assert len(ch.inserts) == 1 and ch.describes == 1


def test_verification_uses_milliseconds_and_all_rows():
    # Clock-skew correction leaves sub-second timestamps; the probe must match them.
    ms = TS.replace(microsecond=123000)
    assert B._ts_literal(ms) == "2026-10-10 00:00:00.123"
    ch = FakeClickHouse()
    ch.stored.append(("srv", "a", ms, 1.0))
    assert B.rows_present(ch, "t", COLS, [["srv", "a", ms, 1.0]])
    # Only one of two rows stored (an insert that failed half-way): not acknowledged.
    assert not B.rows_present(ch, "t", COLS, [["srv", "a", ms, 1.0], ["srv", "a", TS, 2.0]])


def test_identical_block_is_retried_through_a_longer_outage():
    ch = FakeClickHouse(fail_times=3)        # more than the old 2 retries

    async def main():
        b = B.HostInsertBatcher(lambda: ch, max_delay_s=0.01)
        await asyncio.gather(*(b.insert("t", COLS, [row(f"a{i}")], f"tok{i}") for i in range(3)))
    B.RETRY_WINDOW_S = 5
    run(main())
    assert len(ch.inserts) == 1 and len(ch.stored) == 3
