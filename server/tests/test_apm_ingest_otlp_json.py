import asyncio
import time
from datetime import datetime, timezone

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from app.api.v1.apm_ingest import (
    SPAN_COLUMNS,
    decode_otlp_traces_json,
    decode_otlp_traces_protobuf,
    decode_content_encoding,
)
from app.api.v1 import apm_ingest

import gzip
import pytest


def test_otlp_gzip_content_encoding_round_trip():
    payload = b"standard-otlp-compressed-body"
    assert decode_content_encoding(gzip.compress(payload), "gzip") == payload
    assert decode_content_encoding(payload, "identity") == payload


def test_otlp_content_encoding_rejects_invalid_or_unknown_encoding():
    with pytest.raises(ValueError, match="Invalid gzip"):
        decode_content_encoding(b"not-gzip", "gzip")
    with pytest.raises(LookupError, match="Unsupported"):
        decode_content_encoding(b"body", "br")


def test_decode_otlp_json_accepts_canonical_enum_names():
    start_ns = time.time_ns()
    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [{
                "key": "service.name", "value": {"stringValue": "web"},
            }]},
            "scopeSpans": [{"spans": [{
                "traceId": "01" * 16,
                "spanId": "02" * 8,
                "name": "GET /",
                "kind": "SPAN_KIND_SERVER",
                "startTimeUnixNano": str(start_ns),
                "endTimeUnixNano": str(start_ns + 1_000_000),
                "status": {"code": "STATUS_CODE_OK"},
            }]}],
        }],
    }

    spans, resources, exceptions, rejected, skewed = decode_otlp_traces_json(payload, "prod")

    assert rejected == 0
    assert skewed == 0
    assert len(spans) == 1
    assert len(resources) == 1
    assert exceptions == []
    row = dict(zip(SPAN_COLUMNS, spans[0]))
    assert row["span_kind"] == 2
    assert row["span_kind_str"] == "SERVER"
    assert row["status_code"] == "OK"


def test_decode_otlp_protobuf_uses_the_same_row_pipeline():
    start_ns = time.time_ns()
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    attribute = resource_spans.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "iis-protobuf"
    span = resource_spans.scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex("01" * 16)
    span.span_id = bytes.fromhex("02" * 8)
    span.name = "GET /orders"
    span.kind = 2
    span.start_time_unix_nano = start_ns
    span.end_time_unix_nano = start_ns + 5_000_000
    span.status.code = 1

    payload = decode_otlp_traces_protobuf(request.SerializeToString())
    spans, resources, exceptions, rejected, skewed = decode_otlp_traces_json(payload, "test")

    assert rejected == 0
    assert skewed == 0
    assert len(spans) == 1
    assert len(resources) == 1
    assert exceptions == []
    row = dict(zip(SPAN_COLUMNS, spans[0]))
    assert row["trace_id"] == "01" * 16
    assert row["span_id"] == "02" * 8
    assert row["service_name"] == "iis-protobuf"
    assert row["env"] == "test"
    assert row["span_kind"] == 2
    assert row["duration_nano"] == 5_000_000


def test_runtime_status_reports_writer_readiness_and_queue_pressure(monkeypatch):
    class RunningTask:
        @staticmethod
        def done():
            return False

    checked_at = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    queue = asyncio.Queue(maxsize=apm_ingest.QUEUE_MAXSIZE)
    monkeypatch.setattr(apm_ingest, "_writer_task", RunningTask())
    monkeypatch.setattr(apm_ingest, "_queue", queue)
    monkeypatch.setattr(apm_ingest, "_last_received_at", checked_at)

    status = apm_ingest.runtime_status(checked_at=checked_at)

    assert status["available"] is True
    assert status["state"] == "active"
    assert status["queue_capacity"] == apm_ingest.QUEUE_MAXSIZE
    assert status["last_received_at"] == checked_at


def test_runtime_status_reports_stopped_writer(monkeypatch):
    monkeypatch.setattr(apm_ingest, "_writer_task", None)
    monkeypatch.setattr(apm_ingest, "_queue", None)

    status = apm_ingest.runtime_status()

    assert status["available"] is False
    assert status["state"] == "unavailable"


@pytest.mark.asyncio
async def test_writer_acknowledges_only_after_storage_commit(monkeypatch):
    entered_flush = asyncio.Event()
    release_flush = asyncio.Event()

    async def delayed_flush(*_args):
        entered_flush.set()
        await release_flush.wait()

    async def no_stats():
        return None

    monkeypatch.setattr(apm_ingest, "_flush", delayed_flush)
    monkeypatch.setattr(apm_ingest, "_persist_pending_stats", no_stats)
    apm_ingest._queue = asyncio.Queue()
    acknowledgement = asyncio.get_running_loop().create_future()
    writer = asyncio.create_task(apm_ingest._writer_loop())
    try:
        await apm_ingest._queue.put(([["span"]], {}, [], acknowledgement))
        await asyncio.wait_for(entered_flush.wait(), timeout=1)
        assert not acknowledgement.done()
        release_flush.set()
        await asyncio.wait_for(asyncio.shield(acknowledgement), timeout=1)
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        apm_ingest._queue = None


@pytest.mark.asyncio
async def test_writer_surfaces_storage_failure_to_request(monkeypatch):
    failure = RuntimeError("ClickHouse unavailable")

    async def failed_flush(*_args):
        raise failure

    async def no_stats():
        return None

    monkeypatch.setattr(apm_ingest, "_flush", failed_flush)
    monkeypatch.setattr(apm_ingest, "_persist_pending_stats", no_stats)
    apm_ingest._queue = asyncio.Queue()
    acknowledgement = asyncio.get_running_loop().create_future()
    writer = asyncio.create_task(apm_ingest._writer_loop())
    try:
        await apm_ingest._queue.put(([["span"]], {}, [], acknowledgement))
        with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
            await asyncio.wait_for(asyncio.shield(acknowledgement), timeout=1)
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        apm_ingest._queue = None
