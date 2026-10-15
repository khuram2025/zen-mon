"""GET/PUT /settings/monitoring: the device status policy document (DESIGN "Policy").

The real routes run against the private PostgreSQL cluster from ``pg_fixture`` (never the appliance
database) through a plain ``TestClient`` (no startup hooks) with the session and the signed-in user
overridden. PUT merges into the stored document and validates the MERGED result; GET returns the
stored values over the defaults plus ``effective``, the shared loader's output.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import device_status_machine as dsm
from pg_fixture import PgDatabase, pg_cluster, pg_db  # noqa: F401

URL = "/api/v1/settings/monitoring"
POLICY_KEYS = set(dsm.DEFAULTS)


def add_user(db: PgDatabase, role: str = "admin") -> SimpleNamespace:
    name = f"{role}-{uuid.uuid4().hex[:8]}"
    user_id = db.scalar("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, 'x', $3) "
                        "RETURNING id", name, f"{name}@example.test", "admin" if role == "admin" else "viewer")
    return SimpleNamespace(id=user_id, username=name, email=f"{name}@example.test", full_name=name,
                           role=role, is_active=True, last_login=None, password_hash="")


@pytest.fixture
def api(pg_db):
    admin = add_user(pg_db)
    current = {"user": admin}

    async def session():
        engine = create_async_engine(pg_db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                yield db
        finally:
            await engine.dispose()

    async def signed_in():
        return current["user"]

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield SimpleNamespace(client=TestClient(app), db=pg_db, admin=admin, current=current)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def stored(db: PgDatabase):
    rows = db.query("SELECT value, updated_at, updated_by FROM system_settings WHERE key = 'monitoring'")
    return rows[0] if rows else None


def audit(db: PgDatabase) -> list:
    return db.query("SELECT actor_id, metadata FROM audit_logs WHERE action = 'settings.monitoring.update' "
                    "ORDER BY created_at")


def test_get_without_a_stored_document_returns_the_defaults_and_the_effective_policy(api):
    body = api.client.get(URL).json()
    assert {k: body[k] for k in POLICY_KEYS} == dsm.DEFAULTS
    assert body["degraded_rtt_clear_ms"] is None and body["degraded_loss_clear_pct"] is None  # "derived"
    assert body["effective"] == dsm.effective_policy_dict(dsm.Policy())
    assert body["effective"]["degraded_rtt_clear_ms"] == 80.0 and body["effective"]["degraded_loss_clear_pct"] == 8.0


def test_a_partial_put_merges_and_keeps_every_other_stored_key(api):
    api.db.execute("INSERT INTO system_settings (key, value) VALUES ('monitoring', $1)",
                   {"degraded_rtt_ms": 150, "degraded_confirm_checks": 4, "degraded_window_checks": 6,
                    "owned_by_someone_else": "kept"})
    before = stored(api.db)[1]

    # An older client sends only the two limits; the confirmation settings must survive.
    response = api.client.put(URL, json={"degraded_rtt_ms": 200, "degraded_loss_pct": 20})
    assert response.status_code == 200, response.text
    assert "within about a minute" in response.json()["message"]
    assert response.json()["effective"]["degraded_rtt_clear_ms"] == 160.0

    value, updated_at, updated_by = stored(api.db)
    assert value == {"degraded_rtt_ms": 200.0, "degraded_loss_pct": 20.0, "degraded_confirm_checks": 4,
                     "degraded_window_checks": 6, "owned_by_someone_else": "kept"}
    assert updated_at > before and updated_by == api.admin.id
    [(actor, metadata)] = audit(api.db)
    assert actor == api.admin.id and metadata == {"degraded_rtt_ms": 200.0, "degraded_loss_pct": 20.0}

    body = api.client.get(URL).json()
    assert (body["degraded_rtt_ms"], body["degraded_confirm_checks"], body["degraded_window_checks"]) == (200, 4, 6)
    assert body["effective"]["degraded_confirm_checks"] == 4
    assert body["effective"]["degraded_loss_clear_pct"] == 16.0
    assert "owned_by_someone_else" not in body


def test_the_full_document_round_trips(api):
    document = {**dsm.DEFAULTS, "degraded_rtt_ms": 250.5, "degraded_rtt_clear_ms": 180, "degraded_confirm_checks": 2,
                "degraded_window_checks": 4, "down_confirm_checks": 5, "sensor_down_confirm_checks": 2,
                "up_confirm_checks": 2, "flap_detection_enabled": False, "flap_window_minutes": 60,
                "flap_start_changes": 6, "flap_stop_changes": 2, "flap_stable_minutes": 20}
    assert api.client.put(URL, json=document).status_code == 200
    body = api.client.get(URL).json()
    assert {k: body[k] for k in POLICY_KEYS} == document
    assert body["effective"] == dsm.effective_policy_dict(dsm.policy_from_settings(document))

    # null puts a recovery limit back to "derived".
    assert api.client.put(URL, json={"degraded_rtt_clear_ms": None}).status_code == 200
    body = api.client.get(URL).json()
    assert body["degraded_rtt_clear_ms"] is None and body["effective"]["degraded_rtt_clear_ms"] == 200.4


@pytest.mark.parametrize("stored_doc, incoming, message", [
    ({"degraded_window_checks": 5}, {"degraded_confirm_checks": 6},
     "degraded_window_checks (5) must be at least degraded_confirm_checks (6)"),
    ({}, {"degraded_confirm_checks": 7},                      # the default window (5) counts too
     "degraded_window_checks (5) must be at least degraded_confirm_checks (7)"),
    ({"degraded_rtt_clear_ms": 90}, {"degraded_rtt_ms": 80},  # lowering a limit under a stored override
     "degraded_rtt_clear_ms (90 ms) cannot be above degraded_rtt_ms (80 ms)"),
    ({}, {"degraded_loss_pct": 5, "degraded_loss_clear_pct": 6},
     "degraded_loss_clear_pct (6%) cannot be above degraded_loss_pct (5%)"),
    ({"flap_start_changes": 5}, {"flap_stop_changes": 5},
     "flap_stop_changes (5) must be below flap_start_changes (5)"),
    ({"flap_window_minutes": 15}, {"flap_stable_minutes": 20},
     "flap_stable_minutes (20) cannot exceed flap_window_minutes (15)"),
])
def test_cross_field_rules_are_checked_on_the_merged_document(api, stored_doc, incoming, message):
    if stored_doc:
        api.db.execute("INSERT INTO system_settings (key, value) VALUES ('monitoring', $1)", stored_doc)
    response = api.client.put(URL, json=incoming)
    assert response.status_code == 422
    assert response.json()["detail"] == f"Invalid monitoring settings: {message}"
    assert (stored(api.db) or [None])[0] == (stored_doc or None)  # nothing written
    assert audit(api.db) == []


@pytest.mark.parametrize("incoming", [
    {"degraded_confirm_checks": 0}, {"degraded_confirm_checks": 21}, {"degraded_window_checks": 2.5},
    {"down_confirm_checks": 11}, {"sensor_down_confirm_checks": 0}, {"up_confirm_checks": 11},
    {"flap_window_minutes": 4}, {"flap_start_changes": 2}, {"flap_stable_minutes": 0},
    {"degraded_rtt_ms": 0}, {"degraded_rtt_ms": 10001}, {"degraded_loss_pct": 0.5},
    {"degraded_rtt_clear_ms": 0}, {"degraded_loss_clear_pct": -1},
    {"degraded_confirm_checks": "3"}, {"degraded_rtt_ms": True}, {"flap_detection_enabled": 1},
    {"flap_detection_enabled": "true"}, {"degraded_confirm_checks": None},
])
def test_out_of_range_or_mistyped_values_are_rejected(api, incoming):
    response = api.client.put(URL, json=incoming)
    assert response.status_code == 422, response.text
    assert stored(api.db) is None


def test_an_integral_float_is_an_integer_and_zero_loss_recovery_is_allowed(api):
    assert api.client.put(URL, json={"degraded_clear_checks": 4.0, "degraded_loss_clear_pct": 0}).status_code == 200
    assert stored(api.db)[0] == {"degraded_clear_checks": 4, "degraded_loss_clear_pct": 0.0}


def test_a_stored_invalid_value_is_shown_as_its_default_and_must_be_fixed_by_the_next_put(api):
    api.db.execute("INSERT INTO system_settings (key, value) VALUES ('monitoring', $1)",
                   {"degraded_rtt_ms": 0, "degraded_confirm_checks": "4", "flap_start_changes": 99})
    body = api.client.get(URL).json()
    assert (body["degraded_rtt_ms"], body["degraded_confirm_checks"], body["flap_start_changes"]) == (100.0, 3, 4)
    # what the loaders apply: 0 means unset, strings are ignored, out of range clamps
    assert (body["effective"]["degraded_rtt_ms"], body["effective"]["degraded_confirm_checks"],
            body["effective"]["flap_start_changes"]) == (100.0, 3, 64)

    response = api.client.put(URL, json={"degraded_loss_pct": 12})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("Invalid monitoring settings: ")
    assert "stored degraded_rtt_ms" in detail and "stored degraded_confirm_checks: must be a number" in detail
    assert "stored flap_start_changes" in detail

    fixed = {k: body[k] for k in POLICY_KEYS}               # the UI sends the full document back
    assert api.client.put(URL, json={**fixed, "degraded_loss_pct": 12}).status_code == 200
    assert api.client.get(URL).json()["effective"]["flap_start_changes"] == 4


def test_only_settings_managers_may_change_the_policy(api):
    api.current["user"] = add_user(api.db, "operator")
    assert api.client.get(URL).status_code == 200
    assert api.client.put(URL, json={"degraded_rtt_ms": 120}).status_code == 403
    api.current["user"] = add_user(api.db, "viewer")
    assert api.client.get(URL).status_code == 403
    assert stored(api.db) is None
