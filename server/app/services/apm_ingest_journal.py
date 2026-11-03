"""Commissioned OTLP trace admission before asynchronous analytics recovery.

Span identities are scoped to the authenticated ingest key. Related spans,
resources and exceptions commit together. Retries may regroup immutable spans;
their first admitted payload and original analytics block remain authoritative.
"""
import hashlib
import os
import re

from app.core.controller_admission import permitted
from app.services.host_ingest_journal import HostIngestJournal, JournalUnavailable


def configured():
    return os.environ.get('APM_INSERT_JOURNAL_ENABLED') == '1'


def validate_configuration():
    if configured() and (os.environ.get('HOST_INSERT_JOURNAL_ENABLED') != '1'
                         or not os.environ.get('ZENPLUS_CONTROLLER_GRANT')):
        raise RuntimeError('APM journal requires host recovery and commissioned controller admission')


def _span_token(key_id, trace_id, span_id):
    if (not re.fullmatch('[0-9a-f]{32}', trace_id) or int(trace_id, 16) == 0
            or not re.fullmatch('[0-9a-f]{16}', span_id) or int(span_id, 16) == 0):
        raise ValueError('APM journal requires valid nonzero trace and span identities')
    return 'otlp-span-v1-' + hashlib.sha256(
        (str(key_id) + '\0' + trace_id + '\0' + span_id).encode()).hexdigest()


async def admit(engine, key_id, raw_request, spans, resources, exceptions):
    from app.api.v1.apm_ingest import SPAN_COLUMNS, RESOURCE_COLUMNS, EXC_COLUMNS
    validate_configuration()
    if not configured() or not permitted():
        raise JournalUnavailable('APM controller admission unavailable')
    members = {}
    for row in spans:
        token = _span_token(key_id, row[1], row[2])
        if token in members:
            raise ValueError('Duplicate span identity in one OTLP request')
        members[token] = [row]
    if not members:
        return {}
    exception_members = {token: [] for token in members}
    for row in exceptions:
        token = _span_token(key_id, row[3], row[4])
        if token not in exception_members:
            raise ValueError('Exception has no span in this OTLP request')
        exception_members[token].append(row)
    request_token = 'otlp-resource-v1-' + hashlib.sha256(
        str(key_id).encode() + b'\0' + raw_request).hexdigest()
    blocks = [
        ('apm_spans', SPAN_COLUMNS, list(members.items())),
        # Receipt-only members freeze an admitted span's absence of errors.
        ('apm_exceptions', EXC_COLUMNS, list(exception_members.items())),
    ]
    if resources:
        blocks.append(('apm_traces_resource', RESOURCE_COLUMNS,
                       [(request_token, list(resources.values()))]))
    return await HostIngestJournal(engine, None).stage_many(blocks)
