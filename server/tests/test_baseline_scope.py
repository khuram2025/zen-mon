"""Baseline scope matching shared by the evaluator and the compliance read path."""
import json

from app.services.baseline_service import baseline_in_scope


def _b(**over):
    base = dict(enabled=True, os_type=None, site_id=None, match_tags=[])
    base.update(over)
    return base


def _s(**over):
    base = dict(os_type="linux", site_id="site-1", tags=["prod"], status="online")
    base.update(over)
    return base


def test_unscoped_enabled_baseline_applies():
    assert baseline_in_scope(_b(), _s())


def test_disabled_baseline_never_applies():
    assert not baseline_in_scope(_b(enabled=False), _s())


def test_disabled_server_gets_no_baselines():
    assert not baseline_in_scope(_b(), _s(status="disabled"))


def test_os_scope_mismatch_and_missing_server_os():
    assert not baseline_in_scope(_b(os_type="windows"), _s())
    assert not baseline_in_scope(_b(os_type="linux"), _s(os_type=None))
    assert baseline_in_scope(_b(os_type="linux"), _s())


def test_site_scope_compares_as_strings():
    assert baseline_in_scope(_b(site_id="site-1"), _s())
    assert not baseline_in_scope(_b(site_id="site-2"), _s())
    assert not baseline_in_scope(_b(site_id="site-1"), _s(site_id=None))


def test_tag_scope_accepts_json_string_and_requires_subset():
    assert baseline_in_scope(_b(match_tags=json.dumps(["prod"])), _s())
    assert not baseline_in_scope(_b(match_tags=["prod", "pci"]), _s())
    assert not baseline_in_scope(_b(match_tags=["prod"]), _s(tags=None))
