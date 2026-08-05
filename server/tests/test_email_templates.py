"""Email template family (email_render + report_html email body).

Locks in the rendering contract every notification path relies on: severity
styling, resolved state, HTML escaping of untrusted values, graceful handling
of empty contexts, and the plain-text alternatives.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.email_render import (  # noqa: E402
    build_account_email_html, build_account_email_text,
    build_alert_email_html, build_alert_email_text,
    build_notification_email_html, build_notification_email_text,
    severity_hex,
)
from app.services import report_html  # noqa: E402


ALERT_CTX = {
    "severity": "critical",
    "status": "DOWN",
    "title": "Core switch unreachable",
    "hostname": "core-sw-01",
    "ip_address": "10.10.101.201",
    "message": "ICMP checks failed 5 times in a row.",
    "details": [("Group", "Core"), ("Location", "DC-1")],
    "timestamp": "2026-08-05T08:00:00+00:00",
    "action_url": "https://zen.example.com/alerts",
}


# ── alert family ─────────────────────────────────────────────────────────────

def test_alert_html_carries_all_fields():
    h = build_alert_email_html(ALERT_CTX)
    for needle in ("Core switch unreachable", "core-sw-01", "10.10.101.201",
                   "ICMP checks failed", "DC-1", "CRITICAL", "DOWN",
                   "https://zen.example.com/alerts"):
        assert needle in h


def test_alert_severity_accents():
    for sev in ("critical", "warning", "info"):
        h = build_alert_email_html({**ALERT_CTX, "severity": sev})
        assert severity_hex(sev) in h


def test_alert_resolved_state():
    h = build_alert_email_html({**ALERT_CTX, "resolved": True})
    assert "RESOLVED" in h
    assert severity_hex("critical", resolved=True) in h
    # resolved green replaces the critical red accent bar
    assert h.count(severity_hex("critical")) == 0


def test_alert_unknown_severity_defaults_to_warning():
    h = build_alert_email_html({**ALERT_CTX, "severity": "bogus"})
    assert severity_hex("warning") in h


def test_alert_escapes_untrusted_values():
    h = build_alert_email_html({
        "title": "<script>alert(1)</script>",
        "message": "<img src=x onerror=y>",
        "hostname": "a & b <c>",
        "details": [("<b>L</b>", "<i>V</i>")],
    })
    assert "<script>" not in h
    assert "<img" not in h
    assert "&lt;script&gt;" in h
    assert "<i>V</i>" not in h


def test_alert_empty_ctx_renders():
    h = build_alert_email_html({})
    assert "<!DOCTYPE html>" in h and "ZenPlus" in h and "ALERT" in h


def test_alert_text_alternative():
    t = build_alert_email_text(ALERT_CTX)
    assert "[DOWN] Core switch unreachable" in t
    assert "core-sw-01 (10.10.101.201)" in t
    assert "https://zen.example.com/alerts" in t
    assert "<" not in t.replace("<https", "")  # no HTML leakage


# ── notification family ──────────────────────────────────────────────────────

def test_notification_html():
    h = build_notification_email_html({
        "status": "TEST",
        "title": "SMTP configuration verified",
        "message": "Gateway can deliver mail.",
        "details": [("Gateway", "smtp.example.com:587")],
    })
    assert "SMTP configuration verified" in h
    assert "TEST" in h
    assert "smtp.example.com:587" in h


def test_notification_text():
    t = build_notification_email_text({"title": "N", "message": "M",
                                       "details": [("K", "V")]})
    assert t.startswith("N")
    assert "M" in t and "V" in t


# ── account family ───────────────────────────────────────────────────────────

def test_account_html_greeting_and_security_note():
    h = build_account_email_html({
        "title": "Your password was reset",
        "recipient_name": "Khuram",
        "message": "An administrator has reset your password.",
        "details": [("Account", "khuram"), ("Changed by", "admin")],
    })
    assert "Hi Khuram" in h
    assert "Security note" in h
    assert "Your password was reset" in h
    assert "ACCOUNT" in h


def test_account_custom_security_note_and_button():
    h = build_account_email_html({
        "title": "T", "security_note": "Custom note.",
        "action_url": "https://zen.example.com/login",
    })
    assert "Custom note." in h
    assert "https://zen.example.com/login" in h


def test_account_text():
    t = build_account_email_text({"title": "T", "recipient_name": "K",
                                  "message": "M"})
    assert "Hi K," in t and "M" in t


# ── report email body ────────────────────────────────────────────────────────

def test_report_email_html_preheader_and_shell():
    meta = {"title": "Weekly Executive Summary", "period_label": "Last 7 days",
            "generated_at": "2026-08-05T08:00:00+00:00"}
    data = {"kpis": {"availability_pct": 99.2, "incidents_count": 3,
                     "mttr_minutes": 12, "devices_monitored": 42,
                     "active_critical_count": 1, "sla_attained_pct": 99.2,
                     "sla_target_pct": 99.9}}
    h = report_html.build_report_email_html(meta, data,
                                            view_url="https://zen.example.com/r/1",
                                            attached=True)
    assert "Weekly Executive Summary" in h
    assert "Availability 99.2%" in h          # hidden preheader
    assert "View full report" in h
    assert "attached to this email" in h
    assert "SCHEDULED REPORT" in h
