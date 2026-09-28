"""Regression tests for the Cisco onboarding assessment (ZP-06, ZP-10)."""

from datetime import datetime, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from app.api.v1 import devices as devices_api
from app.api.v1.snmp_credentials import _row_to_response


def _cred_row(**over):
    now = datetime.now(timezone.utc)
    row = {"id": uuid4(), "name": "lab", "snmp_version": "2c", "community": "enc:xyz",
           "created_at": now, "updated_at": now}
    row.update(over)
    return row


def test_credential_reports_configured_community_without_revealing_it():
    out = _row_to_response(_cred_row())
    assert out.community is None
    assert out.has_community is True
    assert _row_to_response(_cred_row(community=None)).has_community is False


STACK_TEMPLATE = [
    {"key": "stack", "name": "Stack", "kind": "scalar", "metrics": [
        {"key": "cisco_stack_ring", "name": "Stack Ring", "type": "enum",
         "labels": {"1": {"text": "Redundant", "sev": "ok"}, "2": {"text": "Open Ring", "sev": "warn"}}},
    ]},
    {"key": "stack_members", "name": "Stack Members", "kind": "table", "metrics": [
        {"key": "cisco_stack_state", "name": "State", "type": "enum"},
    ]},
]


class _Result:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class _DB:
    def __init__(self, members):
        now = datetime.now(timezone.utc)
        self.vals = [dict(group_key="stack", metric_key="cisco_stack_ring", instance="", series_key="tpl_cisco_stack_ring",
                          label=None, unit=None, value_num=2.0, value_text=None, updated_at=now)]
        for m in members:
            self.vals.append(dict(group_key="stack_members", metric_key="cisco_stack_state", instance=m,
                                  series_key=f"tpl_cisco_stack_state_{m}", label=f"Switch {m}", unit=None,
                                  value_num=4.0, value_text=None, updated_at=now))
        self.calls = 0

    async def execute(self, sql, params=None):
        self.calls += 1
        if "FROM devices d" in str(sql):
            return _Result([{"profile_id": uuid4(), "promote_managed": False, "name": "Cisco", "vendor": "Cisco",
                             "description": None, "builtin": True, "oid_groups": STACK_TEMPLATE}])
        return _Result(self.vals)


def _ring(out):
    stack = next(g for g in out["groups"] if g["key"] == "stack")
    return stack, stack["metrics"][0]


@pytest.mark.asyncio
async def test_open_ring_on_standalone_switch_is_informational():
    out = await devices_api.device_template_insights(uuid4(), _DB(["1001"]), NS(id=uuid4()))
    group, metric = _ring(out)
    assert metric["status"] == "info" and group["status"] == "info"
    assert "Standalone" in metric["text"]


@pytest.mark.asyncio
async def test_open_ring_on_real_stack_still_warns():
    out = await devices_api.device_template_insights(uuid4(), _DB(["1001", "2001"]), NS(id=uuid4()))
    group, metric = _ring(out)
    assert metric["status"] == "warn" and metric["text"] == "Open Ring"
