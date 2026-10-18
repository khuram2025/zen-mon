"""Incident priority (P1..P4) and the mail surface that shows it.

* Severity derives priority (critical -> P2, warning -> P3, info -> P4); only a
  rule chooses P1. A stamped alert priority wins over the derivation.
* Response targets read P-keys and still honour the severity keys saved by
  earlier releases.
* Alert mail subjects carry the priority after the severity tag, and every
  alert mail offers "View incident" and "Acknowledge" links to the incident
  (the acknowledge page confirms; the link itself changes nothing).
"""
from app.services import alert_phrasing as ap
from app.services import alert_priority as prio
from app.services import email_render as er

AID = "3f2a9c1d-1111-2222-3333-444455556666"


def test_priority_is_derived_from_severity_and_p1_is_never_derived():
    assert prio.priority_for(None, "critical") == "P2"
    assert prio.priority_for(None, "warning") == "P3"
    assert prio.priority_for(None, "info") == "P4"
    assert prio.priority_for(None, "unknown") == "P3"
    assert prio.priority_for("P1", "warning") == "P1"
    assert prio.priority_for("p4", "critical") == "P4"
    assert prio.priority_for("P9", "critical") == "P2"          # invalid rule value: derived
    assert prio.effective_priority("P1", "P3", "info") == "P1"   # stamp wins
    assert prio.effective_priority(None, "P3", "info") == "P3"
    assert prio.effective_priority(None, None, "info") == "P4"


def test_labels_rank_and_urgency():
    assert prio.priority_label("P2") == "P2 High"
    assert prio.priority_label(None) == ""
    assert sorted(["P3", "P1", "P4", "P2"], key=prio.priority_rank) == ["P1", "P2", "P3", "P4"]
    assert prio.is_urgent("P1") and prio.is_urgent("P2") and not prio.is_urgent("P3")


def test_response_targets_read_p_keys_then_legacy_severity_keys():
    assert prio.response_target_minutes({"P1": 5, "P3": 45}, "P3") == 45
    assert prio.response_target_minutes({"warning": 30}, "P3") == 30
    assert prio.response_target_minutes({"critical": 10}, "P2") == 10
    # P1 has no legacy key: its default applies.
    assert prio.response_target_minutes({"critical": 10}, "P1") == 5
    assert prio.response_target_minutes({"P4": None}, "P4") is None
    assert prio.clean_response_targets({"critical": 10, "P2": 20}) == {"P1": 5, "P2": 20, "P3": 60, "P4": None}


def test_priority_sql_twin():
    sql = prio.priority_sql("a", "r")
    assert sql.startswith("COALESCE(a.priority, r.priority,") and "'critical' THEN 'P2'" in sql
    assert "a.priority" not in prio.priority_sql("a", "r", columns=False)


def test_subject_gets_the_priority_after_the_severity():
    ctx = {"priority": "P3", "lifecycle": "new", "lifecycle_tag": "New", "incident_ref": "INC-3f2a9c1d"}
    out = er.alert_subject("[WARNING] ALERT: Memory high — srv-a", ctx)
    assert out == "[WARNING] [P3] [New] Memory high — srv-a [INC-3f2a9c1d]"
    assert er.alert_subject(out, ctx) == out                       # idempotent
    assert ap.priority_subject("Custom subject", "P1") == "[P1] Custom subject"
    assert ap.priority_subject("[WARNING] x", None) == "[WARNING] x"


def test_incident_links_from_the_dashboard_url():
    assert ap.dashboard_base("https://zp.example/alerts") == "https://zp.example"
    assert ap.dashboard_base("https://zp.example/zen/services") == "https://zp.example/zen"
    assert ap.dashboard_base("https://zp.example/devices/abc") == "https://zp.example"
    assert ap.dashboard_base("/alerts") == ""
    view, ack = ap.incident_links({"action_url": "https://zp.example/alerts", "incident_id": AID})
    assert view == f"https://zp.example/alerts/{AID}" and ack == f"{view}?ack=1"
    assert ap.incident_links({"incident_url": "https://x/alerts/1"}) == ("https://x/alerts/1",
                                                                         "https://x/alerts/1?ack=1")
    assert ap.incident_links({"action_url": "", "incident_id": AID}) == ("", "")


def test_alert_mail_has_view_and_acknowledge_buttons_but_resolved_mail_does_not_ack():
    ctx = {"severity": "warning", "priority": "P3", "incident_id": AID, "incident_ref": "INC-3f2a9c1d",
           "lifecycle": "new", "lifecycle_label": "New", "action_url": "https://zp.example/alerts",
           "title": "Memory high", "message": "Memory is high."}
    html = er.build_alert_email_html(ctx)
    assert "View incident" in html and f"/alerts/{AID}?ack=1" in html and "Acknowledge" in html
    assert "P3 Warning" in html
    text = er.build_alert_email_text(ctx)
    assert f"View incident: https://zp.example/alerts/{AID}" in text and "?ack=1" in text
    done = er.build_alert_email_html({**ctx, "resolved": True})
    assert "View incident" in done and "?ack=1" not in done
    # No incident id: the plain dashboard button as before.
    plain = er.build_alert_email_html({"severity": "info", "action_url": "https://zp.example/alerts"})
    assert "View in ZenPlus" in plain and "Acknowledge" not in plain
