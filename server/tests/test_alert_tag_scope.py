"""Tag-scoped alert rules: devices.tags matching.

Scope by tag is the one scope dimension whose value operators edit by hand, so
these cover the shapes the column actually arrives in (NULL, JSON string, mixed
casing) rather than only the happy path.
"""
from types import SimpleNamespace

from app.services.network_alert_service import _device_in_scope
from app.services.tag_service import tag_set


def _rule(**over):
    base = dict(device_id=None, group_id=None, device_type=None,
                location=None, scope_tag=None)
    base.update(over)
    return SimpleNamespace(**base)


def _dev(**over):
    base = dict(hostname="sw01", device_type="switch", location="DC-1",
                group_id=None, tags=set())
    base.update(over)
    return base


# ── tag_set normalisation ───────────────────────────────────────────────────

def test_tag_set_handles_every_shape_the_column_arrives_in():
    assert tag_set(None) == set()
    assert tag_set([]) == set()
    assert tag_set(["Core", "Edge"]) == {"core", "edge"}
    # asyncpg can hand back an undecoded JSON string
    assert tag_set('["Core", "Edge"]') == {"core", "edge"}
    # whitespace and case are normalised, blanks dropped
    assert tag_set(["  Core  ", "", "CORE"]) == {"core"}


def test_tag_set_never_invents_a_none_tag():
    """A NULL element must not stringify into a tag literally named "none"."""
    assert tag_set([None, "core"]) == {"core"}
    assert "none" not in tag_set([None])


def test_tag_set_rejects_non_list_values():
    assert tag_set("not json") == set()
    assert tag_set(42) == set()
    assert tag_set({"core": True}) == set()


# ── scope matching ──────────────────────────────────────────────────────────

def test_untagged_rule_still_matches_every_device():
    """Rules that predate tag scope must be unaffected."""
    assert _device_in_scope(_rule(), "d1", _dev()) is True
    assert _device_in_scope(_rule(), "d1", _dev(tags={"core"})) is True


def test_tag_scoped_rule_matches_only_tagged_devices():
    rule = _rule(scope_tag="core")
    assert _device_in_scope(rule, "d1", _dev(tags={"core", "edge"})) is True
    assert _device_in_scope(rule, "d1", _dev(tags={"edge"})) is False
    assert _device_in_scope(rule, "d1", _dev(tags=set())) is False


def test_tag_match_is_case_and_whitespace_insensitive():
    """The registry canonicalises spelling, but older assignments may not."""
    assert _device_in_scope(_rule(scope_tag="Core"), "d1", _dev(tags={"core"})) is True
    assert _device_in_scope(_rule(scope_tag="  core "), "d1", _dev(tags={"core"})) is True


def test_tag_scope_combines_with_other_scope_dimensions():
    """Tag narrows a rule; it never widens one another dimension excluded."""
    rule = _rule(scope_tag="core", device_type="switch")
    assert _device_in_scope(rule, "d1", _dev(tags={"core"})) is True
    # right tag, wrong type
    assert _device_in_scope(rule, "d1", _dev(tags={"core"}, device_type="router")) is False
    # right type, wrong tag
    assert _device_in_scope(rule, "d1", _dev(tags={"edge"})) is False


def test_device_missing_the_tags_key_is_not_a_crash():
    """_snmp_devices always supplies tags, but a stale caller must not 500."""
    dev = {"hostname": "sw01", "device_type": "switch", "location": "DC-1", "group_id": None}
    assert _device_in_scope(_rule(scope_tag="core"), "d1", dev) is False
    assert _device_in_scope(_rule(), "d1", dev) is True
