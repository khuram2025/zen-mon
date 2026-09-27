from __future__ import annotations

import pytest

from app.services.agent_package_names import (
    FLAVOR_CORE,
    FLAVOR_FULL,
    download_path,
    parse_package_name,
    version_key,
)


@pytest.mark.parametrize(
    ("name", "flavor", "version", "ext"),
    [
        ("zenplus-agent-1.13.0.exe", FLAVOR_FULL, "1.13.0", "exe"),
        ("zenplus-agent-1.13.0.msi", FLAVOR_FULL, "1.13.0", "msi"),
        ("zenplus-agent-core-1.13.0.exe", FLAVOR_CORE, "1.13.0", "exe"),
        ("zenplus-agent-core-1.13.0.msi", FLAVOR_CORE, "1.13.0", "msi"),
        ("zenplus-agent_1.2.0.tar.gz", FLAVOR_FULL, "1.2.0", "tar.gz"),
        ("zenplus-agent-1.12.10-rc1.exe", FLAVOR_FULL, "1.12.10-rc1", "exe"),
    ],
)
def test_parses_both_flavours(name, flavor, version, ext):
    parsed = parse_package_name(name)
    assert parsed is not None
    assert (parsed.flavor, parsed.version, parsed.ext) == (flavor, version, ext)


@pytest.mark.parametrize(
    "name",
    [
        "zenplus-agent-1.12.4.msi.pre-update",
        "zenplus-agent-1.8.0.manifest.json",
        "zenplus-agent-apm-1.13.0.exe",
        "zenplus-agent-core.exe",
        "ZenPlusAgentSetup-x64.exe",
        "zenplus-agent-core-1.13.exe",
    ],
)
def test_ignores_other_files(name):
    assert parse_package_name(name) is None


def test_version_order_is_numeric():
    assert version_key("1.12.10") > version_key("1.12.9")
    assert version_key("1.13.0") > version_key("1.12.10")


def test_full_keeps_the_historical_download_path():
    # Agents before 1.13 were given this exact URL; it must keep serving full.
    assert download_path("windows", FLAVOR_FULL) == "/api/v1/agents/packages/windows/latest"
    assert download_path("windows", FLAVOR_CORE) == "/api/v1/agents/packages/windows/latest?flavor=core"
