"""Signed code-payload inventory and exact-tree reconciliation.

Release payloads are full snapshots of the appliance code roots.  Copying the
snapshot as an overlay is insufficient: a Python module removed by a newer
release remains importable on the appliance forever.  This module records the
files in the signed payload and removes only absent files inside explicitly
managed roots, while preserving appliance-local runtime state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable


PAYLOAD_MANIFEST = ".zenplus-code-manifest.json"
PAYLOAD_MANIFEST_VERSION = 1

# These are code snapshots in every OTA package.  Runtime artifacts and built
# binaries are deliberately managed by their own manifest steps.
DEFAULT_MANAGED_ROOTS = (
    "server",
    "poller",
    "scripts",
    "support",
    "docker",
    "dashboard",
    "updater",
)
DEFAULT_MANAGED_FILES = (
    ".version",
    "docker-compose.yml",
    PAYLOAD_MANIFEST,
)

# Paths populated on an appliance rather than owned by a release.  Matching is
# prefix based, so (for example) every file below updater/config is preserved.
DEFAULT_PRESERVE_PATHS = (
    "server/venv",
    "server/.venv",
    "dashboard/node_modules",
    "dashboard/dist",
    "dashboard/build",
    "support/requests",
    "support/jobs",
    "support/bundles",
    "updater/logs",
    "updater/backups",
    "updater/config",
    "updater/keys",
)


def _normalise_relative(value: str) -> str:
    """Return one safe POSIX relative path or raise before any deletion."""
    raw = str(value or "").replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe code inventory path: {value!r}")
    return path.as_posix()


def _normalise_many(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_normalise_relative(value) for value in values))


def _payload_files(code_path: Path) -> set[str]:
    files: set[str] = set()
    for path in code_path.rglob("*"):
        if path.is_file() or path.is_symlink():
            files.add(path.relative_to(code_path).as_posix())
    return files


def build_payload_manifest(
    code_path: Path,
    *,
    managed_roots: Iterable[str] = DEFAULT_MANAGED_ROOTS,
    managed_files: Iterable[str] = DEFAULT_MANAGED_FILES,
    preserve_paths: Iterable[str] = DEFAULT_PRESERVE_PATHS,
) -> dict:
    """Build the inventory embedded in, and covered by, a release signature."""
    roots = _normalise_many(managed_roots)
    root_files = list(_normalise_many(managed_files))
    if PAYLOAD_MANIFEST not in root_files:
        root_files.append(PAYLOAD_MANIFEST)

    files = _payload_files(code_path)
    files.add(PAYLOAD_MANIFEST)
    return {
        "format_version": PAYLOAD_MANIFEST_VERSION,
        "managed_roots": list(roots),
        "managed_files": root_files,
        "preserve_paths": list(_normalise_many(preserve_paths)),
        "files": sorted(files),
    }


def write_payload_manifest(
    code_path: Path,
    *,
    managed_roots: Iterable[str] = DEFAULT_MANAGED_ROOTS,
    managed_files: Iterable[str] = DEFAULT_MANAGED_FILES,
    preserve_paths: Iterable[str] = DEFAULT_PRESERVE_PATHS,
) -> Path:
    manifest = build_payload_manifest(
        code_path,
        managed_roots=managed_roots,
        managed_files=managed_files,
        preserve_paths=preserve_paths,
    )
    destination = code_path / PAYLOAD_MANIFEST
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_payload_manifest(code_path, manifest)
    return destination


def load_payload_manifest(code_path: Path) -> dict | None:
    path = code_path / PAYLOAD_MANIFEST
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != PAYLOAD_MANIFEST_VERSION:
        raise ValueError(
            f"unsupported code inventory version: {manifest.get('format_version')!r}"
        )
    return manifest


def _is_preserved(relative: str, preserve_paths: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in preserve_paths
    )


def validate_payload_manifest(code_path: Path, manifest: dict) -> dict:
    """Validate inventory scope and require an exact payload/file match."""
    roots = _normalise_many(manifest.get("managed_roots") or ())
    root_files = _normalise_many(manifest.get("managed_files") or ())
    preserve = _normalise_many(manifest.get("preserve_paths") or ())
    files = set(_normalise_many(manifest.get("files") or ()))

    if not roots:
        raise ValueError("code inventory has no managed roots")
    if PAYLOAD_MANIFEST not in root_files or PAYLOAD_MANIFEST not in files:
        raise ValueError("code inventory does not own its manifest")

    allowed = set(root_files)
    for relative in files:
        if relative in allowed:
            continue
        if not any(relative == root or relative.startswith(root + "/") for root in roots):
            raise ValueError(f"code inventory file is outside managed scope: {relative}")

    actual = _payload_files(code_path)
    symlinks = sorted(
        path.relative_to(code_path).as_posix()
        for path in code_path.rglob("*")
        if path.is_symlink()
    )
    if symlinks:
        raise ValueError(f"code payload contains symlinks: {symlinks[:5]}")
    if actual != files:
        missing = sorted(files - actual)[:5]
        unexpected = sorted(actual - files)[:5]
        raise ValueError(
            "code inventory does not match payload "
            f"(missing={missing}, unexpected={unexpected})"
        )

    return {
        "managed_roots": roots,
        "managed_files": root_files,
        "preserve_paths": preserve,
        "files": files,
    }


def remove_stale_managed_files(
    destination: Path,
    *,
    incoming_files: Iterable[str],
    managed_roots: Iterable[str],
    managed_files: Iterable[str],
    preserve_paths: Iterable[str],
) -> int:
    """Remove files absent from an incoming exact snapshot.

    Every path is validated before scanning or deleting.  Directory symlinks at
    a managed root are refused so reconciliation can never escape the appliance
    code tree.
    """
    incoming = set(_normalise_many(incoming_files))
    roots = _normalise_many(managed_roots)
    root_files = _normalise_many(managed_files)
    preserve = _normalise_many(preserve_paths)
    removed = 0

    for root_name in roots:
        root = destination / Path(*PurePosixPath(root_name).parts)
        if root.is_symlink():
            raise ValueError(f"managed code root is a symlink: {root_name}")
        if not root.exists():
            continue

        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)

            # Do not descend into appliance-local state.  A symlinked directory
            # outside a preserved path is treated as a stale entry, never
            # followed.
            for dirname in list(dirnames):
                child = current_path / dirname
                relative = child.relative_to(destination).as_posix()
                if _is_preserved(relative, preserve):
                    dirnames.remove(dirname)
                elif child.is_symlink():
                    # Even a path present in the payload must become a real
                    # directory before copy2 writes below it.
                    child.unlink()
                    removed += 1
                    dirnames.remove(dirname)

            for filename in filenames:
                child = current_path / filename
                relative = child.relative_to(destination).as_posix()
                if _is_preserved(relative, preserve):
                    continue
                # Never copy through a destination symlink, including when its
                # pathname is expected by the incoming snapshot.
                if child.is_symlink():
                    child.unlink()
                    removed += 1
                    continue
                if relative in incoming:
                    continue
                child.unlink()
                removed += 1

        # Remove empty stale package directories, deepest first. Runtime paths
        # were pruned above and therefore keep their ancestors non-empty.
        directories = [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            relative = directory.relative_to(destination).as_posix()
            if _is_preserved(relative, preserve):
                continue
            try:
                directory.rmdir()
            except OSError:
                pass

    for file_name in root_files:
        if _is_preserved(file_name, preserve):
            continue
        path = destination / Path(*PurePosixPath(file_name).parts)
        if path.is_symlink():
            path.unlink()
            removed += 1
        elif file_name in incoming:
            continue
        elif path.is_file():
            path.unlink()
            removed += 1
        elif path.exists():
            raise ValueError(f"managed root file is a directory: {file_name}")

    return removed


def reconcile_code_tree(code_path: Path, destination: Path) -> int:
    """Validate a signed payload inventory, then remove destination drift."""
    manifest = load_payload_manifest(code_path)
    if manifest is None:
        return 0  # Backward compatibility for release format v1 overlays.
    policy = validate_payload_manifest(code_path, manifest)
    return remove_stale_managed_files(destination, incoming_files=policy["files"], **{
        key: policy[key]
        for key in ("managed_roots", "managed_files", "preserve_paths")
    })
