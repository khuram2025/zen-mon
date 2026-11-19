"""Server inventory conditions, the pure parts: which names are loaded and how
the "inventory" condition type evaluates (unknown is never "does not have")."""
from __future__ import annotations

import pytest

from app.services import server_inventory_facts as sif
from app.services.discovery_rules import conditions


def test_needles_collect_exact_and_contains_per_field():
    tree = {"op": "any", "conditions": [
        {"field": "services", "operator": "has", "value": ["MSSQLSERVER", " W3SVC "]},
        {"op": "none", "conditions": [{"field": "software", "operator": "has_none_containing", "value": "Oracle"}]},
        {"field": "running_services", "operator": "reported"},
        {"field": "vendor", "operator": "equals", "value": "x"}]}
    got = sif.needles([tree])
    assert got == {"services": {"exact": {"mssqlserver", "w3svc"}, "contains": set()},
                   "software": {"exact": set(), "contains": {"oracle"}},
                   "running_services": {"exact": set(), "contains": set()}}
    assert sif.merge(got, {"services": {"exact": {"a"}, "contains": {"b"}}})["services"] == {
        "exact": {"mssqlserver", "w3svc", "a"}, "contains": {"b"}}


@pytest.mark.parametrize("op,have,want,expect", [
    ("has", ["MSSQLSERVER", "SQL Server (MSSQLSERVER)"], ["mssqlserver"], True),
    ("has", [], ["MSSQLSERVER"], False),
    ("has_none", [], ["MSSQLSERVER"], True),
    ("has_containing", ["Microsoft SQL Server 2019 (64-bit)"], "sql server", True),
    ("has_none_containing", ["IIS 10.0 Express"], "sql server", True),
    ("reported", [], None, True),
    ("not_reported", [], None, False),
    # not reported: unknown, every test fails except "is not reported"
    ("has", None, ["x"], False),
    ("has_none", None, ["x"], False),
    ("has_none_containing", None, "x", False),
    ("reported", None, None, False),
    ("not_reported", None, None, True),
])
def test_inventory_operators(op, have, want, expect):
    node = conditions.validate({"op": "all", "conditions": [{"field": "services", "operator": op, "value": want}]})
    assert conditions.evaluate(node, {"services": have}) is expect


def test_inventory_fields_are_in_both_catalogs():
    from app.services.discovery_rules.facts import FIELD_SOURCES, FIELDS
    from app.services.device_groups.facts import BASE_FIELDS
    for key in sif.FIELDS:
        assert FIELDS[key][0] == "inventory" and FIELD_SOURCES[key] == ["server"]
        assert BASE_FIELDS[key][0] == "inventory"
    with pytest.raises(conditions.ConditionError):
        conditions.validate({"op": "all", "conditions": [{"field": "software", "operator": "has_containing"}]})
