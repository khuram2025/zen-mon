"""Wording of the evaluators that build their own sentences: NCM, Device
Tracker (UDT), NetPath, service checks and the SNMP stored message."""
from __future__ import annotations

from types import SimpleNamespace as NS

from app.services import ncm_events, netpath_alert_service as netpath, udt_alert_service as udt
from app.services import network_alert_service as network

SW = NS(hostname="sw-01", ip="10.0.0.2", device_type="switch")


def test_ncm_config_change_names_the_device_what_changed_and_who():
    summary, sentence = ncm_events.describe("config.changed", SW, {
        "config_type": "running", "added": 5, "removed": 2, "reported_user": "admin"}, at="14:44")
    assert summary == "Running configuration changed"
    assert sentence == ("The running configuration of Switch sw-01 (10.0.0.2) changed at 14:44:"
                        " 5 lines added, 2 removed, by admin.")
    _, secret = ncm_events.describe("config.changed", SW, {"config_type": "startup", "added": 1,
                                                           "removed": 0, "sensitive_changed": True})
    assert secret == ("The startup configuration of Switch sw-01 (10.0.0.2) changed: 1 line added,"
                      " 0 removed, including credential or key lines.")


def test_ncm_backup_failure_and_recovery_state_the_cause_without_raw_errors():
    summary, failed = ncm_events.describe("backup.failed", SW, {
        "failure_category": "authentication", "last_message": "Traceback: secret=abc"}, at="02:10")
    assert summary == "Configuration backup failed"
    assert failed == ("Configuration backup of Switch sw-01 (10.0.0.2) failed at 02:10: login was"
                      " refused. The last saved configuration is still available.")
    assert "secret" not in failed and "Traceback" not in failed
    _, back = ncm_events.describe("backup.recovered", SW, {"occurrences": 3}, at="03:00")
    assert back == "Configuration backup of Switch sw-01 (10.0.0.2) is working again at 03:00 after 3 failed attempts."
    payload = ncm_events._payload(SW, "d1", "k", "backup.failed", summary, sentence=failed)
    assert payload["subject"] == "NCM: Configuration backup failed - sw-01 (10.0.0.2)"
    assert payload["body"] == failed and payload["sms_body"].startswith("ZenPlus WARNING — Configuration backup of")


def test_udt_events_name_endpoint_switch_and_port():
    ep = {"hostname": "laptop-7", "ip": "10.1.2.3", "mac": "aa:bb:cc:dd:ee:ff"}
    dev = {"hostname": "acc-sw-3", "ip_address": "10.0.0.9", "device_type": "switch"}
    assert udt.endpoint_message("rogue_detected", ep, dev, "Gi1/0/12") == (
        "Endpoint laptop-7 (10.1.2.3, MAC aa:bb:cc:dd:ee:ff) is not authorised and was seen on"
        " Switch acc-sw-3 (10.0.0.9) port Gi1/0/12.")
    assert udt.endpoint_message("new_endpoint", {"mac": "aa:bb"}, None, None) == (
        "New endpoint MAC aa:bb connected for the first time.")
    assert udt.endpoint_message("endpoint_moved", ep, dev, "Gi1/0/3").startswith(
        "Endpoint laptop-7 (10.1.2.3, MAC aa:bb:cc:dd:ee:ff) moved to Switch acc-sw-3")


def test_netpath_names_the_path_and_target():
    assert netpath._path_label({"name": "Office 365", "target_ip": "40.97.1.1"}) == "Path to Office 365 (40.97.1.1)"
    assert netpath._path_label({"name": "dns", "target_host": "dns"}) == "Path to dns"


def test_snmp_stored_message_is_words_not_metric_keys():
    rule = NS(metric="cpu", name="High CPU")
    text_ = network._detail_text(rule, [dict(metric="cpu", operator=">", threshold=90)], {"cpu": 97.0})
    assert text_ == "Device CPU 97% (limit 90%)"
    oper = NS(metric="if_oper_status", name="Interface down")
    assert network._detail_text(oper, [dict(metric="if_oper_status", operator="eq", threshold=2)],
                                {"if_oper_status": 2.0}) == "The interface is down (link not operational)"
    assert "=" not in network._detail_text(rule, [dict(metric="cpu", operator=">", threshold=90)], {"cpu": None})
