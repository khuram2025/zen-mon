"""Exact partition census reuse for the flow-history reconciler.

Only unchanged active parts may reuse a census. Inserts, merges, mutations,
TTL deletion, partition replacement and schema changes invalidate it. This
cache is an optimization; the underlying raw records remain authoritative.
"""
from __future__ import annotations

import hashlib
import json
from typing import Awaitable, Callable

FORMAT = 1
MAX_CACHE_BYTES = 2 * 1024 * 1024
MAX_PARTITIONS_PER_QUERY = 16
Query = Callable[[str, dict | None], Awaitable[list[tuple]]]


class RawCensusCache:
    def __init__(self, query: Query, database: str, stored: dict | None = None):
        self.query = query
        self.database = database
        stored = stored if isinstance(stored, dict) else {}
        self.stored = stored.get('partitions', {}) if stored.get('format') == FORMAT else {}
        if not isinstance(self.stored, dict):
            self.stored = {}
        self.entries: dict = {}
        self.rows: dict[str, dict[str, list]] = {}
        self.hits = 0
        self.scanned = 0

    async def fingerprints(self) -> dict[str, str]:
        table = await self.query(
            "SELECT toString(uuid), create_table_query FROM system.tables "
            "WHERE database = %(db)s AND name = 'flow_records'", {'db': self.database})
        identity = repr(table)
        parts = await self.query(
            "SELECT partition_id, name, rows, data_version, toString(hash_of_all_files) "
            "FROM system.parts WHERE database = %(db)s AND table = 'flow_records' AND active "
            "ORDER BY partition_id, name", {'db': self.database})
        grouped: dict[str, list] = {}
        for partition, *part in parts:
            grouped.setdefault(str(partition), []).append(part)
        return {partition: hashlib.sha256((identity + repr(parts)).encode()).hexdigest()
                for partition, parts in grouped.items()}

    async def _read(self, partitions: list[str], sql: dict[str, str]) -> None:
        # Batch cold/changed partitions so first startup does not pay two HTTP
        # round trips for every historical day. Each batch remains bounded.
        for offset in range(0, len(partitions), MAX_PARTITIONS_PER_QUERY):
            batch = partitions[offset:offset + MAX_PARTITIONS_PER_QUERY]
            self.scanned += len(batch)
            for partition in batch:
                self.rows[partition] = {clock: [] for clock in sql}
            for clock, statement in sql.items():
                for partition, *row in await self.query(statement, {'partitions': batch}):
                    self.rows[str(partition)][clock].append(list(row))

    @staticmethod
    def _valid(entry) -> bool:
        if not isinstance(entry, dict) or not isinstance(entry.get('fingerprint'), str):
            return False
        rows = entry.get('rows')
        if not isinstance(rows, dict):
            return False
        for clock, width in (('flow', 8), ('received', 7)):
            if not isinstance(rows.get(clock), list):
                return False
            for row in rows[clock]:
                if not isinstance(row, list) or len(row) != width:
                    return False
                if any(type(value) is not int or value < 0 for value in row):
                    return False
        return True

    async def prepare(self, sql: dict[str, str]) -> None:
        before = await self.fingerprints()
        dirty = []
        for partition, fingerprint in before.items():
            old = self.stored.get(partition)
            if self._valid(old) and old['fingerprint'] == fingerprint:
                self.rows[partition] = old['rows']
                self.hits += 1
            else:
                dirty.append(partition)
        await self._read(dirty, sql)
        after = await self.fingerprints()
        # A changed partition is reread once. If it keeps changing, its current
        # census can be used this pass, but must not be persisted as reusable.
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        await self._read(sorted(changed & after.keys()), sql)
        for partition in changed - after.keys():
            self.rows.pop(partition, None)
        final = await self.fingerprints() if changed else after
        for partition, rows in self.rows.items():
            stable = (after.get(partition) == final.get(partition) if partition in changed
                      else before.get(partition) == final.get(partition))
            if stable and final.get(partition):
                self.entries[partition] = {'fingerprint': final[partition], 'rows': rows}

    def census(self, clock: str, lo_s: int, hi_s: int) -> dict[int, dict[str, int]]:
        names = (('flows', 'if_sides', 'bytes', 'v5_sampled', 'total', 'ts_min_ms', 'unmasked')
                 if clock == 'flow' else
                 ('counted', 'total', 'bytes', 'ts_min_ms', 'ts_max_ms', 'unmasked'))
        # Received hours may span several flow-timestamp partitions.
        merged: dict[int, dict[str, int]] = {}
        for rows in self.rows.values():
            for hour, *values in rows[clock]:
                if not lo_s <= hour < hi_s:
                    continue
                values = dict(zip(names, values))
                if hour not in merged:
                    merged[hour] = values
                    continue
                for name, value in values.items():
                    if name == 'ts_min_ms':
                        merged[hour][name] = min(merged[hour][name], value)
                    elif name == 'ts_max_ms':
                        merged[hour][name] = max(merged[hour][name], value)
                    else:
                        # Match ClickHouse's UInt64 SUM overflow semantics.
                        merged[hour][name] = (merged[hour][name] + value) % (2**64)
        return merged

    def export(self) -> dict:
        # Bound persisted state on appliances with unusually long retention.
        # A dropped entry is simply rescanned next time; it never affects data.
        chosen = {}
        size = 64
        for partition, entry in sorted(self.entries.items(), reverse=True):
            encoded = json.dumps({partition: entry}, separators=(',', ':'))
            if size + len(encoded.encode()) > MAX_CACHE_BYTES:
                continue
            chosen[partition] = entry
            size += len(encoded.encode())
        return {'format': FORMAT, 'partitions': chosen}
