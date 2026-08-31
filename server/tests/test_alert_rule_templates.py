"""The message text a rule will send is knowable without sending one.

Almost every rule stores NULL in its six template columns — that is how a rule
tracks the built-in wording instead of freezing a copy of it. But NULL is not
something an editor can display, so both rule editors showed six empty boxes
behind grey placeholders: the operator could not read what their alerts say,
let alone adjust a word of it.

`effective_templates` resolves what will actually be sent, per rule kind, and
`normalize_stored_template` is the other half of the bargain: text posted back
unchanged is stored as NULL again, so displaying a default never bakes a copy
of it onto the rule.
"""

from app.services import alert_phrasing as ap


# ── Which default set applies ────────────────────────────────────────────────

def test_kind_follows_the_engine_routing():
    assert ap.template_kind({"metric": "trap"}) == "trap"
    assert ap.template_kind({"metric": "service_status"}) == "service"
    assert ap.template_kind({"metric": "rtt", "service_check_id": "abc"}) == "service"
    assert ap.template_kind({"metric": "ping_status"}) == "device"
    assert ap.template_kind({"metric": "apm_error_rate"}) == "device"


def test_a_service_rule_is_not_offered_device_wording():
    """The reported bug's cousin: advertising text that would never be sent."""
    service = ap.default_templates("service")
    device = ap.default_templates("device")
    assert service["email_body"] != device["email_body"]
    assert "{check_name}" in service["email_body"]


def test_traps_have_no_recovery_wording():
    """An event never clears, so there is no reset message to word."""
    trap = ap.default_templates("trap")
    assert trap["recovery_email_subject"] is None
    assert trap["recovery_email_body"] is None
    assert "{trap_message}" in trap["email_body"]


# ── What a rule will actually send ───────────────────────────────────────────

def test_a_rule_storing_nothing_reports_the_builtin_text():
    """The reported bug: the editor had nothing to show for a NULL column."""
    eff = ap.effective_templates({"metric": "ping_status"})
    assert eff["email_subject"] == ap.DEFAULT_EMAIL_SUBJECT
    assert eff["email_body"] == ap.DEFAULT_EMAIL_BODY
    assert eff["recovery_sms_template"] == ap.DEFAULT_RECOVERY_SMS


def test_a_customised_template_is_reported_as_written():
    eff = ap.effective_templates({"metric": "ping_status", "email_body": "Custom body"})
    assert eff["email_body"] == "Custom body"


def test_effective_text_matches_the_kind_the_rule_will_be_sent_as():
    eff = ap.effective_templates({"metric": "service_status"})
    assert eff["email_body"] == ap.default_templates("service")["email_body"]


def test_a_legacy_prefilled_template_reports_the_current_wording():
    """Rules seeded by the old wizard must not show their stale field dump."""
    eff = ap.effective_templates({
        "metric": "ping_status",
        "email_subject": "[{severity}] {status}: {rule_name}",
    })
    assert eff["email_subject"] == ap.DEFAULT_EMAIL_SUBJECT


def test_recovery_text_is_readable_before_recovery_is_switched_on():
    """The reset wording has to be editable to decide whether to enable it."""
    eff = ap.effective_templates({"metric": "ping_status", "recovery_alert": False})
    assert eff["recovery_email_body"] == ap.DEFAULT_RECOVERY_EMAIL_BODY


# ── Displaying a default must not store a copy of it ─────────────────────────

def test_text_equal_to_the_default_is_stored_as_null():
    assert ap.normalize_stored_template(
        ap.DEFAULT_EMAIL_BODY, ap.DEFAULT_EMAIL_BODY) is None


def test_surrounding_whitespace_does_not_make_it_custom():
    assert ap.normalize_stored_template(
        f"  {ap.DEFAULT_EMAIL_BODY}  ", ap.DEFAULT_EMAIL_BODY) is None


def test_blank_is_stored_as_null():
    assert ap.normalize_stored_template("   ", ap.DEFAULT_EMAIL_BODY) is None
    assert ap.normalize_stored_template(None, ap.DEFAULT_EMAIL_BODY) is None


def test_genuinely_edited_text_is_kept_verbatim():
    assert ap.normalize_stored_template(
        "Router down — call the NOC", ap.DEFAULT_EMAIL_BODY
    ) == "Router down — call the NOC"


def test_service_text_equal_to_the_service_default_is_stored_as_null():
    svc = ap.default_templates("service")
    assert ap.normalize_stored_template(svc["email_body"], svc["email_body"]) is None
    # ...but the same text on a device rule is a real customisation.
    assert ap.normalize_stored_template(
        svc["email_body"], ap.DEFAULT_EMAIL_BODY) == svc["email_body"]


def test_round_trip_display_then_save_leaves_the_rule_tracking_the_default():
    """Open the editor, change nothing, save: the column stays NULL."""
    rule = {"metric": "ping_status"}
    shown = ap.effective_templates(rule)
    defaults = ap.default_templates("device")
    stored = {
        name: ap.normalize_stored_template(shown[name], defaults[name])
        for name in ap.TEMPLATE_FIELDS
    }
    assert set(stored.values()) == {None}


# ── A trap announces what happened, not the opposite ─────────────────────────

def test_a_trap_alert_says_the_trap_arrived():
    """The reported wording bug, found by rendering a trap rule's own mail.

    A trap rule matches an OID; it has no threshold, so the wizard stores a
    placeholder `== 0` in the operator/threshold columns. Probing that
    placeholder concluded the rule watched the device's *healthy* state, so
    every trap notification announced "has cleared the trap condition" — the
    opposite of the event that had just fired it.
    """
    v = ap.rule_phrasing({"metric": "trap", "operator": "==", "threshold": 0},
                         hostname="core-router-01")
    assert v["event_sentence"] == "core-router-01 has sent a matching SNMP trap."
    assert v["condition_label"] == "Matching SNMP trap received"


def test_the_trap_placeholder_threshold_does_not_change_the_wording():
    """Whatever sits in those columns, a trap fires on arrival."""
    for threshold in (0, 1, None):
        v = ap.rule_phrasing({"metric": "trap", "operator": "==", "threshold": threshold},
                             hostname="host")
        assert "has sent a matching SNMP trap" in v["event_sentence"]


def test_thresholded_state_metrics_keep_their_wording():
    """The fix is scoped to event metrics: real conditions still read correctly."""
    cases = {
        ("ping_status", "==", 0): "is not responding to ping",
        ("if_oper_status", "eq", 2): "is operationally down",
        ("uptime_reset", "eq", 1): "has rebooted",
        ("udt_rogue_endpoint", ">=", 1): "has seen an unauthorised endpoint",
        ("service_status", "==", 0): "is failing its check",
    }
    for (metric, operator, threshold), expected in cases.items():
        v = ap.rule_phrasing({"metric": metric, "operator": operator, "threshold": threshold},
                             hostname="host")
        assert expected in v["event_sentence"], metric
