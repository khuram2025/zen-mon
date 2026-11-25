"""The appliance releases offered for download.

The catalogue is data, not code: publishing a new appliance image means
changing ``CATALOG_FILE`` (or ``DEFAULT_CATALOG``) and placing the files, never
editing a template. Sizes and hashes are shown to the visitor and must be the
values of the published, signed checksum files.
"""
import json
from datetime import date

from . import conf

DEFAULT_CATALOG = {
    "current": "1.25.2",
    "releases": {
        "1.25.2": {
            "version": "1.25.2",
            "released": "2026-11-07",
            "requirements": "4 vCPUs, 8 GiB RAM, 40 GiB + 100 GiB disks, x86-64, UEFI Secure Boot",
            "trial": "30 days, 50 devices, 20 service checks, 5 users",
            # Records published next to the images; shown on the release page.
            "evidence": [
                {"label": "Build provenance", "url": "/downloads/zenplus-appliance/1.25.2/provenance.json"},
                {"label": "VMware acceptance record", "url": "/downloads/zenplus-appliance/1.25.2/qualification.json"},
                {"label": "Hyper-V acceptance record", "url": "/downloads/zenplus-appliance/1.25.2/hyperv-qualification.json"},
                {"label": "Third-party notices", "url": "/downloads/zenplus-appliance/1.24.14/third-party-notices.html"},
            ],
            "artifacts": [
                {
                    "key": "vmware",
                    "label": "VMware OVA",
                    "platform": "VMware",
                    "summary": "Single OVA for VMware Workstation 17 or later.",
                    "filename": "zenplus-appliance-1.25.2-vmware.ova",
                    "size_bytes": 1721163264,
                    "sha256": "e1b712274387daa83f835a3e32ad3b4038a12ba9037a6ade544b55b31822c996",
                    "install_url": "/kb/zenplus/installation/#vmware",
                },
                {
                    "key": "hyperv",
                    "label": "Hyper-V VHDX",
                    "platform": "Microsoft Hyper-V",
                    "summary": "ZIP with the VHDX pair and a PowerShell importer. Generation 2.",
                    "filename": "zenplus-appliance-1.25.2-hyperv.zip",
                    "size_bytes": 1724089683,
                    "sha256": "5d01b0fab69f830f287e394a8cab815433ba0e3374b88f3f9d989b4f8864ce34",
                    "install_url": "/kb/zenplus/installation/#hyperv",
                },
            ],
        }
    },
}

_SAFE_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class CatalogError(ValueError):
    pass


def _check(catalog):
    releases = catalog.get("releases") or {}
    if catalog.get("current") not in releases:
        raise CatalogError("catalogue 'current' names no release")
    for version, release in releases.items():
        if release.get("version") != version or not set(version) <= set("0123456789."):
            raise CatalogError("bad release version %r" % (version,))
        try:
            released_on(release)
        except (KeyError, TypeError, ValueError):
            raise CatalogError("%s: released must be a date such as 2026-11-07" % version)
        keys = set()
        for artifact in release.get("artifacts") or []:
            name = artifact.get("filename", "")
            # The filename becomes part of an nginx internal path: no
            # separators, no leading dot, nothing a visitor chose.
            if not name or not set(name) <= _SAFE_NAME or name.startswith("."):
                raise CatalogError("unsafe filename %r" % (name,))
            if len(artifact.get("sha256", "")) != 64:
                raise CatalogError("%s: sha256 must be 64 hex characters" % name)
            if artifact.get("key") in keys or not artifact.get("key", "").isalnum():
                raise CatalogError("%s: artifact key missing, repeated or not alphanumeric" % name)
            keys.add(artifact["key"])
    return catalog


def load():
    path = conf.get("CATALOG_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return _check(json.load(handle))
    return _check(DEFAULT_CATALOG)


def current_release():
    catalog = load()
    return catalog["releases"][catalog["current"]]


def get_release(version):
    return load()["releases"].get(version)


def get_artifact(release, key):
    for artifact in release["artifacts"]:
        if artifact["key"] == key:
            return artifact
    return None


def released_on(release):
    return date.fromisoformat(release["released"])


def human_size(size_bytes):
    return "%.2f GiB" % (size_bytes / float(1024 ** 3))
