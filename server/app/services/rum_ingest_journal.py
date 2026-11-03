"""One persisted RUM operation covers raw insertion and its derived rollup."""
import hashlib
import os
import re
import time

from app.core.controller_admission import permitted
from app.services.host_ingest_journal import HostIngestJournal, JournalUnavailable
from app.services.rum_journal_contract_v1 import COLUMNS_V1, ROLLUP_SQL_V1

WORK_KIND = 'rum_raw_rollup_v1'


def configured():
    return os.environ.get('RUM_INSERT_JOURNAL_ENABLED') == '1'


def validate_configuration():
    if configured() and (os.environ.get('HOST_INSERT_JOURNAL_ENABLED') != '1'
                         or not os.environ.get('ZENPLUS_CONTROLLER_GRANT')):
        raise RuntimeError('RUM journal requires host recovery and commissioned controller admission')


async def admit(engine, identities, rows):
    validate_configuration()
    if not configured() or not permitted():
        raise JournalUnavailable('RUM controller admission unavailable')
    if len(identities) != len(rows):
        raise ValueError('RUM journal identity count mismatch')
    members = {}
    for identity, row in zip(identities, rows, strict=True):
        digest = hashlib.sha256(identity.encode()).hexdigest()
        if len(row) != len(COLUMNS_V1) or row[-1] != digest:
            raise ValueError('RUM journal row does not match its immutable identity')
        members.setdefault(identity, [row])
    _, fresh = await HostIngestJournal(engine, None).stage_with_receipts(
        WORK_KIND, COLUMNS_V1, list(members.items()))
    return fresh


def write(client_factory, columns, rows, token):
    """Retry both steps with their original tokens, regardless of raw presence.

    This contract is versioned. Add another work kind for an incompatible
    schema or rollup operation; preserve this reader for pending v1 work.
    """
    from app.services.host_insert_batcher import HostInsertBatcher, RETRY_WINDOW_S
    if columns != COLUMNS_V1 or not rows or any(len(r) != len(columns) for r in rows):
        raise ValueError('Unsupported RUM journal operation')
    ids = [r[-1] for r in rows]
    if (len(set(ids)) != len(ids)
            or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in ids)):
        raise ValueError('Invalid RUM journal event identities')
    bounds = [int(r[0].timestamp()*1000) for r in rows]
    # The raw step and derived step are retried independently. A successful
    # raw deduplication response never substitutes for the derived step.
    HostInsertBatcher(client_factory)._write('apm_rum_events', columns, rows, token)
    params = {'dedupe_ids': ids, 'batch_from_ms': min(bounds), 'batch_to_ms': max(bounds)}
    settings = {'insert_deduplicate': 1, 'insert_deduplication_token': 'rollup-'+token}
    deadline = time.monotonic()+RETRY_WINDOW_S
    delay = .25
    while True:
        try:
            client_factory().command(ROLLUP_SQL_V1, parameters=params, settings=settings)
            return
        except Exception:
            if time.monotonic()+delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay*2, 4)
