"""Support-bundle worker entry point.

Invoked by the unprivileged queue dispatcher. Reads the request JSON the API
process dropped under ``/opt/zenplus/support/requests/``, runs every registered
collector, writes a redacted tarball to
``/opt/zenplus/support/bundles/<uuid>.tar.gz`` and updates
``/opt/zenplus/support/jobs/<uuid>.json`` so the dashboard can poll status.

Failure isolation: one broken collector must not abort the bundle. The worker
catches every exception per collector and continues; only a fatal error in
the worker harness itself can mark the whole bundle ``failed``.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import BUNDLE_SCHEMA_VERSION, __version__
from . import job_state as js
from .archiver import (
    DEFAULT_PER_FILE_CAP,
    DEFAULT_TOTAL_CAP,
    EXTENDED_PER_FILE_CAP,
    BundleArchive,
)
from .collectors import CollectorContext, all_collectors, run_collector
from .manifest import BundleRequest, build_manifest
from .redaction import Redactor


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s zenplus.support: %(message)s",
)
logger = logging.getLogger("zenplus.support")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ZenPlus support-bundle worker")
    parser.add_argument("--job", required=True, help="UUID job id passed by systemd")
    args = parser.parse_args(argv)

    job_id = args.job
    if not js.is_valid_job_id(job_id):
        logger.error("refusing non-UUID job id: %r", job_id)
        return 2

    req_path = js.request_path(job_id)
    job_path = js.job_path(job_id)
    bundle_path = js.bundle_path(job_id)

    if not req_path.exists():
        _mark_failed(job_path, job_id, f"missing request file: {req_path}")
        return 2

    # Read request, validate, kick off.
    try:
        raw_request = js.read_json_safe(req_path, max_bytes=64 * 1024)
        # The pathname is authoritative. Never let a forged JSON field put a
        # different job identifier into the bundle metadata.
        request = BundleRequest.from_json({**raw_request, "job_id": job_id})
        errors = request.validate()
        if errors:
            _mark_failed(job_path, job_id, "; ".join(errors))
            return 2
    except Exception as exc:  # noqa: BLE001
        _mark_failed(job_path, job_id, f"cannot parse request: {exc!r}")
        return 2

    try:
        state = js.read_json_safe(job_path, max_bytes=256 * 1024)
    except (OSError, ValueError):
        state = js.initial_job_state(job_id, request.requested_by)
    state.update({"status": js.STATUS_RUNNING, "phase": "running"})
    js.write_atomic(job_path, state)

    started = datetime.now(timezone.utc)
    ctx = CollectorContext(
        job_id=job_id,
        time_range=request.time_range,
        include_extended_logs=request.include_extended_logs,
    )

    redactor = Redactor()
    section_summaries: dict[str, dict] = {}
    skipped_total: list[str] = []
    truncated_total: list[str] = []

    js.BUNDLES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        per_file_cap = (
            EXTENDED_PER_FILE_CAP if request.include_extended_logs else DEFAULT_PER_FILE_CAP
        )
        with BundleArchive(output_path=bundle_path, per_file_cap=per_file_cap) as archive:
            for name, fn in all_collectors():
                state["phase"] = name
                js.write_atomic(job_path, state)
                logger.info("running collector: %s", name)
                result = run_collector(name, fn, ctx)
                # Redact and add every file the collector produced.
                for arc_name, body in result.files.items():
                    redacted = redactor.apply_bytes(body)
                    try:
                        add_result = archive.add(arc_name, redacted)
                    except ValueError as exc:
                        result.skipped_files.append(arc_name)
                        result.warn(f"{arc_name} rejected by archive policy: {exc}")
                        continue
                    if add_result.skipped:
                        result.skipped_files.append(arc_name)
                        result.warn(f"{arc_name} skipped because bundle data cap was reached")
                    else:
                        result.archived_files.append(arc_name)
                    if add_result.truncated and not add_result.skipped:
                        result.truncated_files.append(arc_name)
                        result.notes.append(f"{arc_name} truncated to fit per-file cap")
                section_summaries[name] = result.summary()

            # Top-of-bundle metadata.
            state["phase"] = "package"
            js.write_atomic(job_path, state)

            appliance_id, hostname, version = _identity()
            completed = datetime.now(timezone.utc)
            manifest = build_manifest(
                request,
                appliance_id=appliance_id,
                hostname=hostname,
                version=version,
                started_at=started,
                completed_at=completed,
                section_summaries=section_summaries,
            )
            import json as _json
            manifest["archive"] = {
                "per_file_cap_bytes": per_file_cap,
                "total_cap_bytes": archive.total_cap,
                "skipped_files": sorted(archive.skipped),
                "truncated_files": sorted(
                    item.arcname for item in archive.added if item.truncated
                ),
            }
            _add_required(
                archive,
                "manifest.json",
                _json.dumps(
                    redactor.apply_structure(manifest), indent=2, sort_keys=True
                ) + "\n",
            )
            _add_required(
                archive,
                "README.txt",
                redactor.apply_bytes(_readme_text(appliance_id, version, request).encode("utf-8")),
            )
            # Generate the report only after every other text item (including
            # request metadata and collector notes in manifest.json) has been
            # scrubbed, otherwise its counts under-report actual redactions.
            _add_required(
                archive,
                "redaction-report.json",
                _json.dumps({"counts": redactor.report()}, indent=2, sort_keys=True) + "\n",
            )
            archive.finalize_checksums()
            skipped_total = list(archive.skipped)
            truncated_total = sorted(item.arcname for item in archive.added if item.truncated)
    except Exception as exc:  # noqa: BLE001
        logger.exception("bundle worker crashed")
        _unlink_quietly(bundle_path)
        _mark_failed(job_path, job_id, f"worker crash: {exc!r}\n{traceback.format_exc()[:2000]}")
        return 3

    # Finalize: hash and stat the bundle, write final job state.
    sha256 = _sha256_file(bundle_path)
    size = bundle_path.stat().st_size
    if size > DEFAULT_TOTAL_CAP:
        _unlink_quietly(bundle_path)
        _mark_failed(
            job_path,
            job_id,
            f"packaged bundle exceeded hard {DEFAULT_TOTAL_CAP}-byte output limit",
        )
        return 3
    try:
        os.chmod(bundle_path, 0o640)
    except OSError:
        pass

    state.update({
        "status": js.STATUS_READY,
        "phase": "done",
        "completed_at": js.now_iso(),
        "size_bytes": size,
        "sha256": sha256,
        "filename": _bundle_filename(appliance_id, started),
        "skipped_files": skipped_total,
        "truncated_files": truncated_total,
        "collector_failures": sorted(
            name for name, summary in section_summaries.items()
            if summary.get("status") == "failed"
        ),
        "collector_warnings": sorted(
            name for name, summary in section_summaries.items()
            if summary.get("status") == "warning"
        ),
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "worker_version": __version__,
    })
    js.write_atomic(job_path, state)
    logger.info("bundle ready: %s (%d bytes)", bundle_path, size)

    # Best-effort: remove the request file once we're done so /opt/zenplus
    # doesn't accumulate dead requests.
    try:
        os.unlink(req_path)
    except OSError:
        pass

    return 0


def _identity() -> tuple[str, str, str]:
    """Return (appliance_id, hostname, version) — best-effort, never raises."""
    import platform
    hostname = platform.node()
    appliance_id = ""
    version = "unknown"
    try:
        conf = Path("/opt/zenplus/updater/config/agent.conf")
        if conf.exists():
            for line in conf.read_text(errors="replace").splitlines():
                s = line.strip()
                if s.startswith("id") and "=" in s:
                    appliance_id = s.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        v = Path("/opt/zenplus/.version")
        if v.exists():
            version = v.read_text(errors="replace").strip().splitlines()[0]
    except OSError:
        pass
    return appliance_id, hostname, version


def _bundle_filename(appliance_id: str, when: datetime) -> str:
    import re

    # This value becomes a Content-Disposition filename. Keep it deliberately
    # narrow even though appliance IDs are normally UUIDs.
    short = re.sub(r"[^A-Za-z0-9]", "", appliance_id or "")[:8] or "appliance"
    ts = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"zenplus-support-{short}-{ts}.tar.gz"


def _readme_text(appliance_id: str, version: str, request: BundleRequest) -> str:
    return (
        "ZenPlus Tech Support Bundle\n"
        f"Bundle schema version: {BUNDLE_SCHEMA_VERSION}\n"
        f"Worker version:        {__version__}\n"
        f"Appliance ID:          {appliance_id or '(unregistered)'}\n"
        f"ZenPlus version:       {version}\n"
        f"Time range collected:  {request.time_range}\n"
        f"Issue category:        {request.issue_category}\n"
        "\n"
        "This archive contains diagnostic data about a ZenPlus appliance.\n"
        "Secrets (passwords, API keys, license keys, credentials) have been\n"
        "redacted before packaging. redaction-report.json shows how many of\n"
        "each kind of secret were masked.\n"
        "\n"
        "manifest.json describes which collectors ran, in what order, and\n"
        "their status. Sections marked 'failed' or 'warning' show what went\n"
        "wrong in the corresponding section's notes field.\n"
        "\n"
        "Hand this file to support@zentryc.com along with your case number.\n"
    )


def _mark_failed(path: Path, job_id: str, reason: str) -> None:
    state = js.initial_job_state(job_id, "")
    state["status"] = js.STATUS_FAILED
    state["phase"] = "failed"
    state["error"] = Redactor().apply(reason)[:2000]
    state["completed_at"] = js.now_iso()
    try:
        js.write_atomic(path, state)
    except Exception:
        logger.exception("could not write failure state to %s", path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _add_required(archive: BundleArchive, arcname: str, body: bytes | str) -> None:
    result = archive.add(arcname, body, required=True)
    if result.skipped:
        raise RuntimeError(f"required bundle metadata did not fit: {arcname}")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("could not remove incomplete bundle %s", path)


if __name__ == "__main__":
    sys.exit(main())
