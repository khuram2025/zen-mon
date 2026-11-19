"""Alerting audit, notification text (phase 1): T-02 .. T-20.

Pure rendering checks: no database, no transport. Each test names the audit
finding it pins.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from app.services import alert_condition
from app.services import alert_phrasing as ap
from app.services import email_render as er
from app.services import sms_text
from app.services.branding import Branding

RIYADH = Branding(timezone="Asia/Riyadh")
AT = datetime(2026, 11, 8, 11, 44, tzinfo=timezone.utc)         # 14:44 in Riyadh
NOW = datetime(2026, 11, 8, 11, 47, tzinfo=timezone.utc)


# ── T-02 SMS in GSM-7 ────────────────────────────────────────────────────────

def test_sms_text_is_transliterated_to_gsm7():
    raw = "ZenPlus — “core” · 3×4 ≥ 90 ≤ 5 → up… a b – ‘x’ 45°C"
    out = sms_text.to_gsm7(raw)
    assert out == "ZenPlus - \"core\" - 3x4 >= 90 <= 5 -> up... a b - 'x' 45C"
    assert sms_text.to_gsm7(out) == out                       # idempotent
    assert all(ch in sms_text._BASIC or ch in sms_text._EXTENDED for ch in out)


def test_sms_text_keeps_letters_of_other_scripts():
    # An Arabic site name has no ASCII spelling: dropping it would lose it.
    assert sms_text.to_gsm7("موقع — down") == "موقع - down"
    assert sms_text.to_gsm7("Çorlu café") == "Çorlu café"     # Ç and é are GSM
    assert sms_text.to_gsm7("Łódź") == "Łodz"                 # ó -> o; Ł has no GSM form


def test_gsm7_length_counts_extension_characters_twice():
    assert sms_text.gsm7_length("abc") == 3
    assert sms_text.gsm7_length("[a]") == 5


def test_long_sms_logs_a_warning_and_is_not_truncated(caplog):
    with caplog.at_level(logging.WARNING, logger="zenplus.sms"):
        sms_text.warn_if_long("x" * 160)
        assert not caplog.records
        sms_text.warn_if_long("x" * 161)
    assert "161 GSM-7 characters" in caplog.text


@pytest.mark.parametrize("kind", ["device", "service", "trap", "syslog"])
def test_default_sms_templates_are_plain_gsm7(kind):
    for field in ("sms_template", "recovery_sms_template"):
        tpl = ap.default_templates(kind).get(field)
        if tpl:
            assert sms_text.to_gsm7(tpl) == tpl, (kind, field, tpl)
    for tpl in (ap.FLAP_START_SMS, ap.FLAP_STOP_SMS):
        assert sms_text.to_gsm7(tpl) == tpl


def test_old_dash_defaults_are_treated_as_no_template():
    assert ap.is_default_template("ZenPlus {severity} — {rule_name}: {event_sentence}")
    assert ap.effective_template("ZenPlus resolved — {rule_name}: {event_sentence}{duration_suffix}",
                                 ap.DEFAULT_RECOVERY_SMS) == ap.DEFAULT_RECOVERY_SMS


def test_sms_skips_the_stamp_when_the_sentence_states_the_local_time():
    said = "ZenPlus CRITICAL - Down: Router r1 (10.0.0.1) went down at 14:44: it does not answer ping."
    assert ap.sms_with_timestamp(said, AT, RIYADH) == said
    plain = "ZenPlus WARNING - CPU: Host CPU on srv rose to 95% (limit 90%)."
    assert ap.sms_with_timestamp(plain, AT, RIYADH) == plain + "\n2026-11-08 14:44 GMT+3"


def test_sms_with_timestamp_returns_gsm7():
    assert ap.sms_with_timestamp("a — b", AT, RIYADH) == "a - b\n2026-11-08 14:44 GMT+3"


# ── T-03 held alerts word like immediate ones ────────────────────────────────

def _held_row(**meta):
    return NS(metric="ping_status", operator="==", threshold=0, conditions=None,
              condition_logic="AND", device_hostname="sw1", device_ip="10.0.0.1",
              device_type="switch", device_group="Core", device_location="DC-1",
              triggered_at=AT)


def test_held_degraded_alert_has_reading_cause_type_and_time():
    from app.services.alert_hold_service import _device_phrasing
    meta = {"new_status": "degraded", "condition": "Packet loss 33% (limit 10%)",
            "condition_metric": "packet_loss", "condition_value": 33.0, "condition_limit": 10.0}
    v = _device_phrasing(_held_row(), meta, "degraded", RIYADH, NOW)
    assert v["event_sentence"] == ("Switch sw1 (10.0.0.1) has degraded connectivity since 14:44: "
                                   "packet loss 33% (limit 10%).")
    assert v["packet_loss"] == "33%"
    assert v["group"] == "Core" and v["location"] == "DC-1" and v["device_type"] == "switch"


def test_held_down_alert_says_when_it_went_down():
    from app.services.alert_hold_service import _device_phrasing
    meta = {"new_status": "down", "condition": "No response", "rtt_ms": None, "loss_pct": 100.0}
    v = _device_phrasing(_held_row(), meta, "down", RIYADH, NOW)
    assert v["event_sentence"] == "Switch sw1 (10.0.0.1) went down at 14:44: it does not answer ping."
    assert v["rtt"] == "unavailable (no reply)" and v["packet_loss"] == "100%"


def test_held_rtt_rule_leads_with_its_own_reading():
    from app.services.alert_hold_service import _device_phrasing
    row = _held_row()
    row.metric, row.operator, row.threshold = "rtt", ">", 100
    meta = {"new_status": "degraded", "rule_value": 150.0, "rtt_ms": 150.0, "loss_pct": 33.0,
            "condition": "Packet loss 33% (limit 10%) · latency 150 ms (limit 100 ms)"}
    v = _device_phrasing(row, meta, "degraded", RIYADH, NOW)
    assert v["event_sentence"] == ("Round-trip time on Switch sw1 (10.0.0.1) rose to 150 ms "
                                   "(limit 100 ms); status degraded: packet loss 33% (limit 10%).")


# ── T-04 rtt / packet-loss rules lead with their own reading ─────────────────

def test_status_cause_lists_only_what_differs_from_the_rule():
    title = "Packet loss 33% (limit 10%) · latency 412 ms (limit 100 ms)"
    assert alert_condition.status_cause("rtt", "degraded", title) == \
        "status degraded: packet loss 33% (limit 10%)"
    assert alert_condition.status_cause("packet_loss", "degraded", title) == \
        "status degraded: latency 412 ms (limit 100 ms)"
    assert alert_condition.status_cause("rtt", "degraded", "Latency 412 ms (limit 100 ms)") == ""
    assert alert_condition.status_cause("ping_status", "degraded", title) == ""
    assert alert_condition.status_cause("packet_loss", "down", "No response") == ""
    assert alert_condition.status_cause(
        "rtt", "degraded", "Latency or packet loss over limits on recent checks") == \
        "status degraded: latency or packet loss over limits on recent checks"


def test_rule_alert_message_uses_the_rules_own_limit():
    msg = alert_condition.rule_alert_message("rtt", 150, ">", 100, "fw-01", "10.0.0.1",
                                             "status degraded: packet loss 33% (limit 10%)")
    assert msg == ("Round-trip time 150 ms (limit 100 ms) — fw-01 (10.0.0.1); "
                   "status degraded: packet loss 33% (limit 10%)")
    assert alert_condition.rule_alert_message("rtt", None, ">", 100, "fw-01", None) == \
        "Round-trip time unavailable (no reply) (limit 100 ms) — fw-01"


# ── T-05 a degraded recovery is not "Down for" ───────────────────────────────

def _recovery(recovered_from, detail=None):
    return ap.phrasing_variables(metric="ping_status", operator="==", threshold=0,
                                 is_recovery=True, device_status="up", duration="5 minutes",
                                 subject_noun="sw1 (10.0.0.1)", asset_type="Switch",
                                 event_time=AT, branding=RIYADH, now=NOW,
                                 recovered_from=recovered_from, status_detail=detail)


def test_recovery_from_degraded_says_degraded_for():
    v = _recovery("degraded", "Latency and packet loss back within limits")
    assert v["duration_sentence"] == " Degraded for 5 minutes."
    assert v["event_sentence"].endswith("back to normal at 14:44: latency and packet loss back within limits.")


def test_recovery_from_down_says_down_for_and_came_back_up():
    v = _recovery("down")
    assert v["duration_sentence"] == " Down for 5 minutes."
    assert v["event_sentence"] == "Switch sw1 (10.0.0.1) came back up at 14:44: it answers ping again."


def test_recovery_map_remembers_the_statuses_it_closed():
    from app.api.v1.alert_engine import _recovery_map
    rows = [NS(rule_id="r1", notified="true", triggered_at=AT, id="a1", new_status="down"),
            NS(rule_id="r1", notified="true", triggered_at=NOW, id="a2", new_status="degraded")]
    assert _recovery_map(rows)["r1"]["statuses"] == {"down", "degraded"}


# ── T-06 every alert mail links to its incident ──────────────────────────────

def test_mail_ctx_links_the_incident_from_the_alert_id():
    from app.services.host_alert_service import _mail_ctx
    mail = _mail_ctx({"alert_id": "3f2a9c1d-0000-0000-0000-000000000000", "rule_name": "CPU",
                      "action_url": "https://zen.example/alerts"})
    view, ack = ap.incident_links(mail)
    assert view == "https://zen.example/alerts/3f2a9c1d-0000-0000-0000-000000000000"
    assert ack == view + "?ack=1"
    assert "View incident" in er.build_alert_email_html(mail)


def test_default_alerts_link_comes_from_app_base_url_or_company_url(monkeypatch):
    from app.services import host_alert_service as has
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "APP_BASE_URL", "")
    assert has._alerts_url(Branding(base_url="https://zen.example/")) == "https://zen.example/alerts"
    assert has._alerts_url(Branding()) == ""


# ── T-07 / T-12 filesystem and host messages ─────────────────────────────────

def _host_rule(metric="host_filesystem_pct", threshold=85):
    return NS(id="r1", name="Disk space", metric=metric, operator=">", threshold=threshold,
              severity="warning", notify_channels=["c1"], priority=None, target=None,
              conditions=None, condition_logic="AND", recovery_alert=True)


def test_host_detail_reads_like_the_network_family():
    from app.services.host_alert_service import _detail
    assert _detail(_host_rule(), 87.8, "D:") == "Filesystem D: 88% (limit 85%)"
    assert _detail(_host_rule("host_cpu_pct", 90), 93.8, None) == "Host CPU 94% (limit 90%)"
    assert _detail(_host_rule("host_cpu_pct", 95), 95.001, None) == "Host CPU 95.0% (limit 95%)"
    assert _detail(_host_rule("host_cpu_pct", 90), None, None) == "Host CPU: no current reading"


@pytest.mark.asyncio
async def test_filesystem_alert_names_the_mount_in_sentence_and_subject(monkeypatch):
    from app.services import host_alert_service as has
    sent = []

    async def capture(db, channels, ctx):
        sent.append(ctx)
        return 1
    monkeypatch.setattr(has, "dispatch_to_channels", capture)
    await has._notify_rule(None, _host_rule(), "srv-x", value=87.8, ip_address="10.0.0.5",
                           alert_id="3f2a9c1d-0000-0000-0000-000000000000", first_seen=AT,
                           dimension="D:", branding=RIYADH)
    [ctx] = sent
    assert ctx["body"] == "Filesystem D: on Server srv-x (10.0.0.5) rose to 88% (limit 85%)."
    assert ctx["subject"] == "[WARNING] ALERT: Disk space — D: on srv-x (10.0.0.5)"
    assert "Filesystem D: on Server srv-x" in ctx["message"]
    assert ctx["headline_metric"]["label"] == "Filesystem D:"


# ── T-08 escalation text ─────────────────────────────────────────────────────

def _esc_row(**kw):
    base = dict(message="High CPU usage: srv-a (10.0.0.5) — Host CPU 93.8% (limit 90%)",
                rule_name="High CPU usage", metric="host_cpu_pct", operator=">", threshold=90,
                metadata={"trigger_value": 93.8, "threshold": 90.0},
                device_hostname=None, server_hostname="srv-a", check_name=None, device_ip="10.0.0.5")
    base.update(kw)
    return NS(**base)


def test_escalation_names_the_opening_value_once_without_the_rule_name():
    from app.services.alert_escalation_service import _incident_text
    assert _incident_text(_esc_row()) == "Host CPU on srv-a (10.0.0.5) opened at 94% (limit 90%)."


def test_escalation_text_without_an_opening_value_is_the_message_as_a_sentence():
    from app.services.alert_escalation_service import _incident_text
    row = _esc_row(metadata={}, message="High CPU usage: srv-a (10.0.0.5) — Host CPU 93.8% (limit 90%)")
    assert _incident_text(row) == "srv-a (10.0.0.5) — Host CPU 93.8% (limit 90%)."
    row = _esc_row(metric="ping_status", metadata={}, message="No response — edge-1 (10.0.0.1)")
    assert _incident_text(row) == "No response — edge-1 (10.0.0.1)."


# ── T-09 service checks ──────────────────────────────────────────────────────

def test_service_trigger_and_recovery_name_the_check():
    t = ap.default_templates("service")
    assert "{check_name}" in t["email_subject"] and "{check_name}" in t["recovery_email_subject"]
    assert "{rule_name}" not in t["recovery_email_subject"]
    v = {"severity": "CRITICAL", "rule_name": "Web checks", "check_name": "Shop API",
         "target": "https://shop.example", **ap.place_variables(), **ap.service_check_variables(
             "down", "HTTP 503 Service Unavailable returned by upstream gateway after 3 retries and more")}
    sms = t["sms_template"].format_map(v)
    assert sms.startswith("ZenPlus CRITICAL - Web checks: Shop API (https://shop.example) failed: HTTP 503")
    assert sms.endswith("upstream...") and len(sms) <= 160
    assert len(v["error_brief"]) <= ap.SMS_REASON_CHARS + 2
    assert ap.service_check_variables("down", "")["error_brief"] == "."
    v.update(ap.service_check_variables("down", "Connection timed out."))
    assert t["sms_template"].format_map(v).endswith("failed: Connection timed out.")


# ── T-10 one date style ──────────────────────────────────────────────────────

def test_flap_since_uses_the_organisation_format():
    from app.services.device_flap_notice_service import _since_text
    brand = Branding(timezone="Asia/Riyadh", date_format="DD/MM/YYYY", time_format="12h")
    assert _since_text(AT, "UTC", brand) == "08/11/2026 2:44 PM GMT+3"
    assert _since_text(AT, "Asia/Riyadh") == "2026-11-08 14:44 GMT+3"
    assert _since_text(None, "UTC") == "an unknown time"


# ── T-11 operators in words ──────────────────────────────────────────────────

def test_synthetic_assertion_failures_say_what_was_expected():
    from app.services.apm_synthetic_service import _check_assertion
    ok, text_ = _check_assertion({"type": "status_code", "operator": "eq", "value": 200},
                                 status_code=503, body="", body_json=None, latency_ms=10)
    assert not ok and text_ == "status 503, expected 200"
    ok, text_ = _check_assertion({"type": "latency_ms", "operator": "lt", "value": 500},
                                 status_code=200, body="", body_json=None, latency_ms=812)
    assert text_ == "latency 812 ms, expected below 500 ms"


# ── T-13 controller recovery ─────────────────────────────────────────────────

def test_controller_recovery_text_says_what_failed_and_what_to_do():
    from app.services import controller_recovery as cr
    state = cr.Recovery(0.0, None, 1800.0, 0.0, "x", "controller restart or monitoring interruption")
    msg = cr.failed_message(state)
    assert "exceeded its deadline" not in msg
    assert msg.startswith("Monitoring has not recovered within 30 minutes of a controller restart")
    assert "on hold" in msg and "API and poller services" in msg
    assert "did not report ready" in cr.failed_body(state)


# ── T-15 template metrics ────────────────────────────────────────────────────

def test_template_metric_uses_its_display_name_and_unit():
    ap.register_template_metrics([{"name": "UPS", "metrics": [
        {"key": "ups_load_pct", "name": "UPS load", "unit": "%"},
        {"key": "ups_temp_c", "name": "battery temperature", "unit": "°C"},
        {"key": "ups_runtime_min", "name": "Runtime", "unit": "min"}]}])
    v = ap.phrasing_variables(metric="tpl_ups_load_pct", operator=">", threshold=80, value=88.4,
                              subject_noun="ups-01 (10.0.0.9)", asset_type="UPS")
    assert v["event_sentence"] == "UPS load on UPS ups-01 (10.0.0.9) rose to 88% (limit 80%)."
    assert ap.metric_noun("tpl_ups_temp_c") == "Battery temperature"
    assert ap.format_reading("tpl_ups_temp_c", 41.26, ">", 40) == "41.3°C"
    assert ap.format_threshold("tpl_ups_runtime_min", 10) == "10 min"
    # A key no template registered keeps the readable fallback.
    assert ap.metric_noun("tpl_never_seen_metric") == "Never seen metric"


# ── T-18 syslog and trap SMS ─────────────────────────────────────────────────

def test_syslog_sms_has_product_severity_word_and_capped_text():
    tpl = ap.default_templates("syslog")["sms_template"]
    v = {"severity": "WARNING", "rule_name": "Core syslog", "hostname": "fw-01",
         **ap.syslog_variables(3, "x" * 400), **ap.place_variables()}
    sms = tpl.format_map(v)
    assert sms.startswith("ZenPlus WARNING - Core syslog: fw-01 syslog error: ")
    assert sms.endswith("...") and len(sms) < 160
    assert ap.syslog_severity_label("notice") == "notice"


def test_trap_sms_names_the_trap():
    tpl = ap.default_templates("trap")["sms_template"]
    v = {"severity": "WARNING", "rule_name": "Link traps", "hostname": "sw1",
         **ap.trap_variables("linkDown", "1.3.6.1.6.3.1.1.5.3", "Interface port3 is down"),
         **ap.place_variables()}
    assert tpl.format_map(v) == ("ZenPlus WARNING - Link traps: sw1 sent trap linkDown. "
                                 "Interface port3 is down")
    v.update(ap.trap_variables(None, "1.3.6.1.4.1.9", None))
    assert tpl.format_map(v) == "ZenPlus WARNING - Link traps: sw1 sent trap 1.3.6.1.4.1.9."


# ── T-19 recovery subjects ───────────────────────────────────────────────────

@pytest.mark.parametrize("subject,ctx,expected", [
    ("[CRITICAL] Resolved: Router down — r1", {"resolved": True, "priority": "P2"},
     "[RESOLVED] [P2] Router down — r1"),
    ("[WARNING] Resolved: CPU — srv", {"lifecycle": "recovered", "lifecycle_tag": "Recovered",
                                       "incident_ref": "INC-1"},
     "[RESOLVED] CPU — srv [INC-1]"),
    ("[WARNING] ALERT: CPU — srv", {}, "[WARNING] ALERT: CPU — srv"),
])
def test_recovery_subject_leads_with_resolved(subject, ctx, expected):
    assert er.alert_subject(subject, ctx) == expected
    assert er.alert_subject(expected, ctx) == expected


# ── T-20 test sends ──────────────────────────────────────────────────────────

def test_default_recovery_subjects_lead_with_resolved():
    assert ap.DEFAULT_RECOVERY_EMAIL_SUBJECT.startswith("[RESOLVED] ")
    assert ap.default_templates("service")["recovery_email_subject"] == "[RESOLVED] {check_name}"
    # Rules that stored the old default get the new one.
    assert ap.effective_template("[{severity}] Resolved: {rule_name}{host_suffix}",
                                 ap.DEFAULT_RECOVERY_EMAIL_SUBJECT) == ap.DEFAULT_RECOVERY_EMAIL_SUBJECT


def test_test_mail_condition_is_the_label_not_the_storage_form():
    from app.api.v1.alert_rules import _condition_text
    assert _condition_text({"metric": "host_cpu_pct", "operator": ">", "threshold": 90.0}) == \
        "Host CPU above 90%"
