from __future__ import annotations

from app.services.external_auth import (
    RADIUS_DEFAULTS, _ldap_cn, explain_ldap_role, explain_radius_role, map_ldap_role, map_radius_role,
)


def test_ldap_cn_is_escape_aware():                       # UA-12
    assert _ldap_cn(r"CN=Ops\, East,OU=Groups,DC=invalid") == "ops, east"
    assert _ldap_cn(r"CN=Ops\2C East,OU=Groups,DC=invalid") == "ops, east"
    assert _ldap_cn("CN=Net Admins,OU=Groups,DC=corp") == "net admins"
    assert _ldap_cn("plainname") == "plainname"


def test_ldap_rule_matching_and_explanation():
    cfg = {"group_mappings": [{"group": "Ops, East", "role": "viewer"},
                              {"group": "CN=NetAdmins,OU=G,DC=c", "role": "admin"}], "default_role": ""}
    assert map_ldap_role(cfg, [r"CN=Ops\, East,OU=Groups,DC=invalid"]) == "viewer"
    role, rule = explain_ldap_role(cfg, ["cn=netadmins,ou=g,dc=c"])
    assert (role, rule["index"], rule["kind"]) == ("admin", 1, "mapping")
    assert explain_ldap_role(cfg, ["CN=Other,DC=c"]) == (None, None)
    cfg["default_role"] = "viewer"
    assert explain_ldap_role(cfg, ["CN=Other,DC=c"]) == ("viewer", {"kind": "default"})


def test_radius_attribute_specific_rules():
    cfg = {"class_mappings": [{"value": "netadmins", "attribute": "Class", "role": "admin"},
                              {"value": "netadmins", "attribute": "", "role": "viewer"}], "default_role": ""}
    attrs = [("Reply-Message", "NetAdmins")]
    assert map_radius_role(cfg, ["NetAdmins"], attrs) == "viewer"        # Reply-Message cannot match the Class rule
    assert map_radius_role(cfg, ["netadmins"], [("Class", "netadmins")]) == "admin"
    assert explain_radius_role(cfg, [], []) == (None, None)


def test_radius_default_role_is_deny_by_default():           # UA-01
    assert RADIUS_DEFAULTS["default_role"] == ""
