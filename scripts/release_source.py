"""What a release was built from, and the rules for building one.

Two properties a signed release must have:

* it names the exact commit it came from, built from a clean checkout, so the
  released code can always be found again in Git;
* its signed manifest lists the SHA-256 of every file in the package, so the
  signature covers the payload and not only the instructions.

The inventory format is shared with the appliance-side verifier in
``updater/package_trust.py``.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from updater.package_trust import INVENTORY_EXCLUDED, INVENTORY_KEY  # noqa: F401  (re-exported)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120,
    )


def git_provenance(repo: Path) -> dict:
    """Commit, branch, exact tag and uncommitted-change state of ``repo``."""
    unknown = {"commit": None, "branch": None, "tag": None, "dirty": None, "dirty_paths": []}
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return unknown
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip() or None
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    tag_run = _git(repo, "describe", "--tags", "--exact-match", "HEAD")
    tag = tag_run.stdout.strip() if tag_run.returncode == 0 else None
    status = _git(repo, "status", "--porcelain")
    if commit is None or status.returncode != 0:
        return unknown
    # Untracked files count; ignored ones (node_modules, dist) do not.
    dirty_paths = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return {
        "commit": commit,
        "branch": None if branch == "HEAD" else branch,
        "tag": tag,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
    }


def source_problems(provenance: dict, *, allow_dirty: bool, require_tag: bool) -> list[str]:
    """Reasons a release must not be built from this source (empty means fine)."""
    problems: list[str] = []
    if provenance["commit"] is None:
        if not allow_dirty:
            problems.append(
                "the source directory is not a readable git checkout, so the release cannot "
                "record which commit it was built from"
            )
    elif provenance["dirty"] and not allow_dirty:
        paths = provenance["dirty_paths"]
        shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        problems.append(
            f"the working tree has {len(paths)} uncommitted change(s) ({shown}); "
            "build from a clean checkout of a committed revision"
        )
    if require_tag and not provenance["tag"]:
        problems.append("HEAD is not at a git tag, and --require-tag was given")
    return problems


def manifest_source(provenance: dict) -> dict:
    """The part of the provenance that goes into the signed manifest."""
    return {key: provenance[key] for key in ("commit", "branch", "tag", "dirty")}


def payload_inventory(build_dir: Path) -> dict[str, str]:
    """SHA-256 of every payload file under ``build_dir``, keyed by relative path."""
    inventory: dict[str, str] = {}
    for path in sorted(build_dir.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"release payload contains a symlink: {path.relative_to(build_dir)}")
        if path.is_dir():
            continue
        relative = path.relative_to(build_dir).as_posix()
        if relative in INVENTORY_EXCLUDED:
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        inventory[relative] = digest.hexdigest()
    return inventory
