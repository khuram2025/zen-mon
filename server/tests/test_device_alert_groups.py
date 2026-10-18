"""Repeated alerts on the device page (field review D-09): the grouping key and
label replay scripts/dev/device-alert-groups/vectors.json, the same cases the
dashboard's eventsModel.ts replays. Presentation only; no database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import device_insights as di

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "scripts/dev/device-alert-groups/vectors.json")
                     .read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["key_cases"], ids=lambda c: c["name"])
def test_group_key(case):
    assert di.alert_group_key(case["rule_id"], case["rule_name"], case["metadata"]) == case["expect"]


@pytest.mark.parametrize("case", VECTORS["label_cases"], ids=lambda c: c["name"])
def test_group_label(case):
    assert di.alert_group_label(case["titles"], case["rule_name"], case["metadata"]) == case["expect"]


def _row(i, at, *, rule="r-1", status="active", severity="warning", title="Packet loss 33% (limit 10%)",
         metric="packet_loss", new_status="degraded"):
    return {"id": f"a{i}", "severity": severity, "status": status, "title": title, "triggered_at": at,
            "rule_id": rule, "rule_name": "High Latency",
            "metadata": {"condition_metric": metric, "new_status": new_status, "if_index": None}}


def test_repeats_collapse_worst_first_then_oldest():
    rows = [
        _row(1, "2026-10-15T08:00:00+00:00"),
        _row(2, "2026-10-17T09:00:00+00:00", title="Packet loss 41% (limit 10%)", status="acknowledged"),
        _row(3, "2026-10-16T10:00:00+00:00", severity="critical"),
        _row(4, "2026-10-17T12:00:00+00:00", metric="rtt", title="Latency 412 ms (limit 100 ms)"),
        {"id": "x1", "severity": "critical", "status": "active", "title": "Tunnel down",
         "triggered_at": "2026-10-18T01:00:00+00:00", "rule_id": None, "rule_name": None, "metadata": None},
        {"id": "x2", "severity": "critical", "status": "active", "title": "Tunnel down",
         "triggered_at": "2026-10-18T02:00:00+00:00", "rule_id": None, "rule_name": None, "metadata": None},
    ]
    groups = di.group_device_alerts(rows)
    assert [(g["label"], g["count"]) for g in groups] == [
        ("Degraded (packet loss)", 3),        # critical (a3), first at 15 Oct
        ("Tunnel down", 1), ("Tunnel down", 1),  # no rule: never grouped
        ("Latency 412 ms (limit 100 ms)", 1),
    ]
    loss = groups[0]
    assert loss["severity"] == "critical", "the worst member"
    assert [m["id"] for m in loss["members"]] == ["a2", "a3", "a1"], "newest first"
    assert loss["members"][0] == {"id": "a2", "title": "Packet loss 41% (limit 10%)", "status": "acknowledged",
                                  "severity": "warning", "triggered_at": "2026-10-17T09:00:00+00:00"}
    assert (loss["first_at"], loss["last_at"]) == ("2026-10-15T08:00:00+00:00", "2026-10-17T09:00:00+00:00")
    assert loss["latest_title"] == "Packet loss 41% (limit 10%)"
    assert (loss["active"], loss["acknowledged"]) == (2, 1)
    assert loss["rule_id"] == "r-1"
    assert di.group_device_alerts([]) == []
