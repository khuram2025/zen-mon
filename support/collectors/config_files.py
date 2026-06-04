"""Configuration file collector — every input runs through the redactor.

We never include raw config files in the archive. The collector reads each
known config, hands the bytes to the central ``Redactor`` (via the worker
loop that wraps each collector's output), and writes a ``.redacted`` copy.

Files we already know how to handle:
- /opt/zenplus/.env                                 main env (DB passwords etc)
- /opt/zenplus/updater/config/agent.conf            updater registration
- /opt/zenplus/updater/config/subscription.json     cached subscription
- /opt/zenplus/docker-compose.yml                   service definitions
- /etc/nginx/conf.d/zenplus.conf                    reverse proxy
- /etc/systemd/system/zenplus-*.service{,.timer}    unit files
- /opt/zenplus/scripts/migrations.lock              migration checksums
- /opt/zenplus/.version                             current version line
"""

from __future__ import annotations

from pathlib import Path

from . import CollectorContext, CollectorResult


CONFIG_TARGETS: tuple[tuple[str, Path, str], ...] = (
    ("config/zenplus.env.redacted", Path("/opt/zenplus/.env"), "text"),
    ("config/agent.conf.redacted", Path("/opt/zenplus/updater/config/agent.conf"), "text"),
    ("config/subscription.json.redacted", Path("/opt/zenplus/updater/config/subscription.json"), "text"),
    ("config/docker-compose.yml.redacted", Path("/opt/zenplus/docker-compose.yml"), "text"),
    ("config/nginx-zenplus.conf.redacted", Path("/etc/nginx/conf.d/zenplus.conf"), "text"),
    ("config/version.txt", Path("/opt/zenplus/.version"), "text"),
    ("config/migrations.lock", Path("/opt/zenplus/scripts/migrations.lock"), "text"),
)

SYSTEMD_GLOB = (
    "/etc/systemd/system/zenplus-*.service",
    "/etc/systemd/system/zenplus-*.timer",
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="config_files")

    for arcname, src, kind in CONFIG_TARGETS:
        if not src.exists():
            result.notes.append(f"missing config: {src}")
            continue
        try:
            data = src.read_bytes()
        except OSError as exc:
            result.warn(f"cannot read {src}: {exc}")
            continue
        result.files[arcname] = data

    # systemd units are short; include them all to make timer schedules and
    # ExecStart lines visible at a glance.
    from glob import glob
    for pattern in SYSTEMD_GLOB:
        for path_s in sorted(glob(pattern)):
            p = Path(path_s)
            try:
                result.files[f"config/systemd/{p.name}"] = p.read_bytes()
            except OSError as exc:
                result.warn(f"cannot read {p}: {exc}")

    if not result.files:
        result.fail("no config files collected")
    return result
