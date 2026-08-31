"""Per-user tag visibility scoping (migrate-104).

The scoping layer has one rule — an entity is visible when its tags share a
name, case-insensitively, with the user's scope — enforced through a single
module. These tests pin the semantics that make it an *authorization*
feature rather than a filter: empty scope means unrestricted, system.admin
always bypasses, untagged entities are hidden from scoped users, and every
tagged surface is declared for rename/delete propagation (a missed surface
is silent authorization drift).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core import scoping
from app.services.tag_service import (
    TAG_ARRAY_SURFACES, TAG_RENAME_ONLY_SURFACES,
)
from app.api.v1.tags import _rename_sql, _strip_sql


def _user(scope=None, role="viewer"):
    return SimpleNamespace(scope_tags=scope or [], role=role)


# ── entity_visible: the matching rule ───────────────────────────────────────

def test_unscoped_user_sees_everything():
    assert scoping.entity_visible([], None)
    assert scoping.entity_visible(["core"], None)
    assert scoping.entity_visible(None, None)


def test_scoped_user_needs_a_shared_tag():
    assert scoping.entity_visible(["core", "edge"], ["core"])
    assert not scoping.entity_visible(["edge"], ["core"])


def test_untagged_entities_are_hidden_from_scoped_users():
    """Fail-closed: an unclassified device belongs to no team's view."""
    assert not scoping.entity_visible([], ["core"])
    assert not scoping.entity_visible(None, ["core"])


def test_matching_is_case_insensitive_and_tolerates_json_strings():
    assert scoping.entity_visible(["Core"], ["core"])
    # asyncpg can hand JSONB back as an undecoded string
    assert scoping.entity_visible('["Core"]', ["core"])


# ── visible_tags: who is scoped at all ──────────────────────────────────────

def _visible(monkeypatch, user, perms):
    async def fake_perms(db, role):
        return perms
    monkeypatch.setattr(scoping, "get_role_permissions", fake_perms)
    return asyncio.run(scoping.visible_tags(None, user))


def test_empty_scope_is_unrestricted(monkeypatch):
    assert _visible(monkeypatch, _user([]), ["devices.view"]) is None
    assert _visible(monkeypatch, _user(None), ["devices.view"]) is None


def test_scope_is_lowercased_and_sorted(monkeypatch):
    assert _visible(monkeypatch, _user(["Riyadh", "core "]), ["devices.view"]) \
        == ["core", "riyadh"]


def test_system_admin_is_never_scoped(monkeypatch):
    """An admin cannot lock themselves out of the fleet."""
    assert _visible(monkeypatch, _user(["core"]), ["system.admin"]) is None


def test_user_without_scope_attr_is_unrestricted(monkeypatch):
    """Rows predating migrate-104 (or test fakes) must not break auth."""
    assert _visible(monkeypatch, SimpleNamespace(role="viewer"), []) is None


# ── SQL predicate builders ──────────────────────────────────────────────────

def test_jsonb_predicate_is_case_insensitive_and_null_safe():
    sql = scoping.jsonb_tags_visible("d.tags")
    assert "jsonb_array_elements_text" in sql
    assert "COALESCE(d.tags, '[]'::jsonb)" in sql
    assert "LOWER(_vt) = ANY(CAST(:vis_tags AS text[]))" in sql


def test_text_array_predicate_is_case_insensitive_and_null_safe():
    sql = scoping.text_tags_visible("service_checks.tags")
    assert "unnest(COALESCE(service_checks.tags, ARRAY[]::text[]))" in sql
    assert "LOWER(_vt) = ANY(CAST(:vis_tags AS text[]))" in sql


def test_alert_predicate_covers_all_three_entity_kinds():
    sql = scoping.alert_visible("alerts")
    for fragment in ("alerts.device_id", "alerts.server_id",
                     "alerts.service_check_id", "FROM devices",
                     "FROM servers", "FROM service_checks"):
        assert fragment in sql
    # service_checks.tags is TEXT[], the others JSONB — both shapes present
    assert "unnest" in sql and "jsonb_array_elements_text" in sql


# ── surface registry: propagation completeness ──────────────────────────────

def test_every_scoped_entity_surface_is_declared_for_propagation():
    """The four entity types the visibility scope filters on must be in the
    rename/delete propagation list, or a tag rename silently changes who
    can see what."""
    tables = {t for t, _c, _k in TAG_ARRAY_SURFACES}
    for required in ("devices", "servers", "service_checks", "apm_services"):
        assert required in tables


def test_user_scope_column_renames_but_never_strips():
    assert ("users", "scope_tags", "jsonb") in TAG_RENAME_ONLY_SURFACES
    tables = {t for t, _c, _k in TAG_ARRAY_SURFACES}
    assert "users" not in tables  # deletes must not touch scopes (fail-closed)


def test_declared_kinds_match_the_live_schema_shapes():
    kinds = {(t, k) for t, _c, k in TAG_ARRAY_SURFACES}
    assert ("service_checks", "text[]") in kinds
    assert ("devices", "jsonb") in kinds
    assert ("apm_services", "jsonb") in kinds


# ── rename/strip SQL speaks both column shapes ──────────────────────────────

def test_rename_sql_shapes():
    jsonb = _rename_sql("devices", "tags", "jsonb")
    assert "jsonb_agg" in jsonb and "jsonb_array_elements_text" in jsonb
    arr = _rename_sql("service_checks", "tags", "text[]")
    assert "array_agg" in arr and "unnest" in arr and "jsonb" not in arr


def test_strip_sql_shapes():
    jsonb = _strip_sql("users", "scope_tags", "jsonb")
    assert "users.scope_tags" in jsonb and "jsonb_agg" in jsonb
    arr = _strip_sql("service_checks", "tags", "text[]")
    assert "array_agg" in arr and "unnest" in arr
