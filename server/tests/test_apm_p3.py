import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.apm_ingest import obfuscate_db_statement
from app.api.v1.apm_p3 import RumEvent, _RUM_SDK, _assert_allowed_origin, _origin


def test_database_statement_obfuscation_preserves_shape_not_literals():
    source = "SELECT * FROM users WHERE email='admin@example.com' AND tenant_id=413 AND score > -12.5"
    scrubbed = obfuscate_db_statement(source)
    assert scrubbed == "SELECT * FROM users WHERE email=? AND tenant_id=? AND score > ?"
    assert "admin@example.com" not in scrubbed
    assert "413" not in scrubbed


def test_rum_origins_are_exact_and_canonical():
    assert _origin("HTTPS://Portal.Example.com:443") == "https://portal.example.com"
    assert _origin("http://192.168.8.19:8080") == "http://192.168.8.19:8080"
    assert _assert_allowed_origin("https://portal.example.com", ["https://portal.example.com/"]) == "https://portal.example.com"
    with pytest.raises(HTTPException) as exc:
        _assert_allowed_origin("https://evil.example.com", ["https://portal.example.com"])
    assert exc.value.status_code == 403


def test_rum_event_rejects_bad_trace_id_and_unbounded_attributes():
    common = {"client_token": "zpr_123456789012", "application_id": "portal", "session_id": "s1"}
    with pytest.raises(ValidationError):
        RumEvent(**common, backend_trace_id="not-a-trace")
    with pytest.raises(ValidationError):
        RumEvent(**common, attributes={str(i): "v" for i in range(33)})


def test_controller_hosted_sdk_avoids_recursive_beacons_and_limits_trace_headers():
    assert 'endpoint.searchParams.set("key",key)' in _RUM_SDK
    assert "originalFetch(endpoint" in _RUM_SDK
    assert "target.origin!==location.origin" in _RUM_SDK
    assert "sendBeacon" not in _RUM_SDK
