from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core import database


def test_legacy_clickhouse_accessor_is_thread_local(monkeypatch):
    created = []
    create_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def factory(**_kwargs):
        client = object()
        with create_lock:
            created.append(client)
        return client

    monkeypatch.setattr(database.clickhouse_connect, "get_client", factory)
    monkeypatch.setattr(database, "_ch_threadlocal", threading.local())

    def use_client_twice():
        barrier.wait(timeout=2)
        first = database.get_clickhouse_client()
        second = database.get_clickhouse_client()
        return first, second

    with ThreadPoolExecutor(max_workers=2) as pool:
        pairs = list(pool.map(lambda _index: use_client_twice(), range(2)))

    assert len(created) == 2
    assert all(first is second for first, second in pairs)
    assert pairs[0][0] is not pairs[1][0]


def test_both_clickhouse_accessors_share_one_session_within_a_thread(monkeypatch):
    client = object()
    monkeypatch.setattr(
        database.clickhouse_connect, "get_client", lambda **_kwargs: client,
    )
    monkeypatch.setattr(database, "_ch_threadlocal", threading.local())

    assert database.get_clickhouse_client() is client
    assert database.get_ch_client() is client
