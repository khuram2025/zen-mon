from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from app.api.v1.apm_ingest import (
    SPAN_COLUMNS,
    decode_otlp_traces_json,
    decode_otlp_traces_protobuf,
)


def test_decode_otlp_json_accepts_canonical_enum_names():
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
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "1001000000",
                "status": {"code": "STATUS_CODE_OK"},
            }]}],
        }],
    }

    spans, resources, exceptions, rejected = decode_otlp_traces_json(payload, "prod")

    assert rejected == 0
    assert len(spans) == 1
    assert len(resources) == 1
    assert exceptions == []
    row = dict(zip(SPAN_COLUMNS, spans[0]))
    assert row["span_kind"] == 2
    assert row["span_kind_str"] == "SERVER"
    assert row["status_code"] == "OK"


def test_decode_otlp_protobuf_uses_the_same_row_pipeline():
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
    span.start_time_unix_nano = 1_000_000_000
    span.end_time_unix_nano = 1_005_000_000
    span.status.code = 1

    payload = decode_otlp_traces_protobuf(request.SerializeToString())
    spans, resources, exceptions, rejected = decode_otlp_traces_json(payload, "test")

    assert rejected == 0
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
