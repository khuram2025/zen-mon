"""Bundle wheels for new Python dependencies inside an update package.

The ``pip_install`` update step used to resolve every new or changed
requirement from the public package index. Appliances whose network blocks
or resets that connection could not take any update that added a dependency
(1.25.x added pyvmomi and pypsrp), failing at ``pip_install`` with
"No matching distribution found".

The builder now ships those wheels in ``wheels/`` next to the package's
``requirements.txt`` and adds ``--find-links wheels`` to that file. pip
resolves a relative ``--find-links`` in a requirements file against the
file's own directory, so this works with every updater already deployed:
an older updater still runs plain ``pip install -r requirements.txt`` and
falls back to the bundled wheels when the index is unreachable. Updaters
that know about ``wheels/`` try a fully offline install first.

Only requirements that are new or changed since the oldest version allowed
to upgrade to this release (``min_version``) are bundled, with their full
dependency closure, for every supported appliance Python. Requirements that
an appliance at ``min_version`` already has installed are satisfied locally
and never reach the index.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

WHEELS_DIR = "wheels"
FIND_LINKS_LINE = f"--find-links {WHEELS_DIR}"

# Ubuntu 22.04 ships Python 3.10 and 24.04 ships Python 3.12; the appliance
# venv is created from the system interpreter. glibc 2.35 (22.04) is the
# lowest supported, so no wheel may need a newer manylinux tag.
PYTHON_VERSIONS = ("3.10", "3.12")
PLATFORMS = (
    "manylinux1_x86_64",
    "manylinux2010_x86_64",
    "manylinux2014_x86_64",
    *(f"manylinux_2_{minor}_x86_64" for minor in range(5, 36)),
    "linux_x86_64",
)

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_lines(text: str) -> dict[str, str]:
    """Map canonical project name -> requirement line, skipping options."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _NAME.match(line)
        if match:
            out[_canonical(match.group(1))] = re.sub(r"\s+", "", line)
    return out


def changed_requirements(base_text: str, current_text: str) -> list[str]:
    """Requirement lines that are new, or pinned differently, since base."""
    def spec(name: str, line: str) -> str:
        return name + line[_NAME.match(line).end():].lower()

    base = {name: spec(name, line) for name, line in requirement_lines(base_text).items()}
    return [line for name, line in requirement_lines(current_text).items()
            if base.get(name) != spec(name, line)]


def base_requirements(repo: Path, min_version: str) -> str:
    """server/requirements.txt as released in v<min_version>."""
    tag = f"v{min_version}"
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{tag}:server/requirements.txt"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot read server/requirements.txt at {tag} to decide which "
            f"wheels to bundle; build from a full clone that has the tag "
            f"({result.stderr.strip()})"
        )
    return result.stdout


def _pip(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check"],
                          capture_output=True, text=True)


def _target_args(python: str) -> list[str]:
    args = ["--only-binary=:all:", "--implementation", "cp", "--python-version", python]
    for platform in PLATFORMS:
        args += ["--platform", platform]
    return args


def _build_pure_wheels(requirements: list[str], built: Path) -> None:
    """Build wheels for requirements published only as source archives.

    Only pure-Python results are accepted: a compiled wheel built here would
    match the build host, not every appliance.
    """
    for req in requirements:
        if all(_pip("download", "--no-deps", "-q", "-d", str(built / "probe"),
                    *_target_args(py), req).returncode == 0
               for py in PYTHON_VERSIONS):
            continue
        before = set(built.glob("*.whl"))
        result = _pip("wheel", "--no-deps", "-q", "-w", str(built), req)
        made = set(built.glob("*.whl")) - before
        if result.returncode != 0 or not made:
            raise RuntimeError(f"No wheel available or buildable for {req}: {result.stderr.strip()}")
        for wheel in made:
            if not wheel.name.endswith("-none-any.whl"):
                raise RuntimeError(
                    f"{req} has no published wheel for the appliance platforms and "
                    f"builds only as a platform wheel ({wheel.name}); pin a version "
                    f"that publishes manylinux wheels"
                )


def bundle_wheels(build_dir: Path, repo: Path, min_version: str) -> list[str]:
    """Download wheels for changed requirements into build_dir/wheels.

    Rewrites build_dir/requirements.txt to look in that directory. Returns
    the bundled wheel file names (empty when nothing changed).
    """
    req_path = build_dir / "requirements.txt"
    current = req_path.read_text()
    changed = changed_requirements(base_requirements(repo, min_version), current)
    if not changed:
        return []

    wheels = build_dir / WHEELS_DIR
    built = build_dir / ".wheel-build"
    wheels.mkdir()
    built.mkdir()
    try:
        _build_pure_wheels(changed, built)
        for python in PYTHON_VERSIONS:
            result = _pip("download", "-q", "-d", str(wheels), "--find-links", str(built),
                          *_target_args(python), *changed)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not download wheels for Python {python}: {result.stderr.strip()}"
                )
    finally:
        shutil.rmtree(built, ignore_errors=True)

    req_path.write_text(
        "# Wheels for dependencies added since the oldest supported upgrade\n"
        "# source are bundled so the update installs without index access.\n"
        f"{FIND_LINKS_LINE}\n{current}"
    )
    return sorted(p.name for p in wheels.glob("*.whl"))
