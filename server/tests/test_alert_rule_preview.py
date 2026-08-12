"""POST /alert-rules/{id}/preview — rendered mail, and unsaved template edits.

The preview dialog on /alert-rules renders whatever this endpoint returns, so
the contract that matters is: the real HTML email comes back, and templates
posted in the body win over the ones stored on the rule without touching it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app

from conftest import make_user

RULE_ID = uuid4()


def _rule_row(**overrides):
    row = dict(
        id=RULE_ID,
        name="Core router down",
        description=None,
        enabled=True,
        metric="ping_status",
        operator="==",
        threshold=0,
        duration=0,
        device_id=None,
        group_id=None,
        service_check_id=None,
        service_check_group_id=None,
        severity="critical",
        notify_channels=[],
        cooldown=300,
        device_type="router",
        location="DC-1",
        trigger_on="down",
        recovery_alert=True,
        min_duration=0,
        max_repeat=0,
        schedule_start=None,
        schedule_end=None,
        schedule_days=[],
        email_subject=None,
        email_body=None,
        sms_template=None,
        recovery_email_subject=None,
        recovery_email_body=None,
        recovery_sms_template=None,
        conditions=None,
        condition_logic="AND",
        trap_oid=None,
        target=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        created_by=None,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


class RuleDB:
    """Serves one alert rule; everything else answers empty."""

    def __init__(self, row):
        self.row = row
        self.writes = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "UPDATE" in sql or "INSERT" in sql or "DELETE" in sql:
            self.writes.append(sql)
        if "FROM alert_rules WHERE id" in sql:
            return SimpleNamespace(first=lambda: self.row)
        return SimpleNamespace(first=lambda: None, scalar=lambda: None)

    async def commit(self):
        return None


@pytest.fixture
def preview_client():
    db = RuleDB(_rule_row())

    async def fake_db():
        yield db

    async def fake_user():
        return make_user("admin")

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = fake_user
    with TestClient(app) as client:
        yield client, db
    app.dependency_overrides.clear()


def test_preview_returns_rendered_email(preview_client):
    client, _db = preview_client
    resp = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    alert = body["alert"]
    assert alert["subject"] == "[CRITICAL] DOWN: Core router down"
    # The HTML mail, not just the template sentence.
    assert alert["email_html"].lstrip().startswith("<!DOCTYPE html>")
    assert "Core router down" in alert["email_html"]
    assert "core-router-01" in alert["email_text"]
    assert alert["sms_body"].startswith("ZenPlus CRITICAL — Core router down:")

    # recovery_alert is on, so the resolved mail renders too.
    assert body["recovery"]["subject"] == "[CRITICAL] Resolved: Core router down"

    # The editor needs both what is stored (all defaults here) and the text
    # those defaults resolve to.
    assert body["templates"]["stored"]["email_body"] is None
    assert "{event_sentence}" in body["templates"]["effective"]["email_body"]


def test_preview_bodies_read_as_english(preview_client):
    """No raw `metric operator threshold` anywhere a person reads."""
    client, db = preview_client
    db.row = _rule_row(name="MOBILY high utilization", metric="if_util_pct",
                       operator=">", threshold=80.0, severity="warning",
                       trigger_on="any")

    body = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()

    alert = body["alert"]
    assert alert["email_body"] == (
        "Interface utilisation on core-router-01 rose above 80% (currently 87%)."
    )
    # The stored metric key never reaches the reader — in any channel.
    for text_ in (alert["email_body"], alert["sms_body"], alert["email_text"],
                  alert["email_html"]):
        assert "if_util_pct" not in text_
    assert "Interface utilisation above 80%" in alert["email_html"]


def test_recovery_preview_states_how_long_the_condition_held(preview_client):
    client, db = preview_client
    db.row = _rule_row(name="MOBILY high utilization", metric="if_util_pct",
                       operator=">", threshold=80.0, trigger_on="any")

    recovery = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()["recovery"]

    assert "is back below 80%" in recovery["email_body"]
    assert "active for 3 minutes 25 seconds" in recovery["email_body"]
    # And as its own row in the details table, not only buried in the prose.
    assert "Active for" in recovery["email_html"]
    assert "3 minutes 25 seconds" in recovery["sms_body"]


LEGACY_WIZARD_BODY = (
    "{status_intro}\n\nRule: {rule_name}\nSeverity: {severity}\nDevice: {hostname} "
    "({ip_address})\nStatus: {status}\nMetric: {metric} {operator} {threshold}\n"
    "Time: {timestamp}\n\n--\nZenPlus Network Monitoring"
)


def test_wizard_seeded_template_is_treated_as_no_template(preview_client):
    """Rules created before this wording existed still get the new mail.

    The wizard used to pre-fill every new rule with a field dump. Nobody typed
    it, so it is a default, not a customisation.
    """
    client, db = preview_client
    db.row = _rule_row(email_body=LEGACY_WIZARD_BODY,
                       sms_template="[ZenPlus {severity}] {hostname} ({ip_address}) "
                                    "is {status}. Rule: {rule_name}")

    alert = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()["alert"]

    assert alert["email_body"] == "core-router-01 is not responding to ping."
    assert "Metric: ping_status" not in alert["email_body"]
    assert alert["sms_body"].startswith("ZenPlus CRITICAL — Core router down:")


def test_hand_written_template_is_left_alone(preview_client):
    client, db = preview_client
    db.row = _rule_row(email_body="Site down: {hostname}. Call the NOC.")

    alert = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()["alert"]
    assert alert["email_body"] == "Site down: core-router-01. Call the NOC."


def test_recovery_never_falls_back_to_the_trigger_body(preview_client):
    """A custom "is DOWN" body must not be reused for the resolved notice."""
    client, db = preview_client
    db.row = _rule_row(email_body="{hostname} is DOWN.", recovery_email_body=None)

    body = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()
    assert body["alert"]["email_body"] == "core-router-01 is DOWN."
    assert "is DOWN" not in body["recovery"]["email_body"]
    assert body["recovery"]["email_body"].startswith("core-router-01 is responding to ping again.")


def test_preview_uses_posted_templates_without_saving(preview_client):
    client, db = preview_client
    resp = client.post(
        f"/api/v1/alert-rules/{RULE_ID}/preview",
        json={"email_subject": "PING FAIL {hostname}", "email_body": "{hostname} at {location} is {status}."},
    )
    assert resp.status_code == 200, resp.text
    alert = resp.json()["alert"]

    assert alert["subject"] == "PING FAIL core-router-01"
    assert alert["email_body"] == "core-router-01 at DC-1 is DOWN."
    assert "core-router-01 at DC-1 is DOWN." in alert["email_html"]
    # Previewing an edit must not write it to the rule.
    assert db.writes == []
    assert resp.json()["templates"]["stored"]["email_subject"] is None


def test_preview_blank_override_falls_back_to_default(preview_client):
    """Clearing a field in the editor means "use the built-in default"."""
    client, db = preview_client
    db.row = _rule_row(email_subject="CUSTOM {rule_name}")

    stored = client.post(f"/api/v1/alert-rules/{RULE_ID}/preview").json()
    assert stored["alert"]["subject"] == "CUSTOM Core router down"

    cleared = client.post(
        f"/api/v1/alert-rules/{RULE_ID}/preview", json={"email_subject": ""},
    ).json()
    assert cleared["alert"]["subject"] == "[CRITICAL] DOWN: Core router down"


def test_preview_unknown_rule_is_404(preview_client):
    client, db = preview_client
    db.row = None
    resp = client.post(f"/api/v1/alert-rules/{uuid4()}/preview")
    assert resp.status_code == 404
