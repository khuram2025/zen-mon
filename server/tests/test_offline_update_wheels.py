import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import offline_wheels  # noqa: E402


def test_only_new_or_repinned_requirements_are_bundled():
    base = "fastapi==0.115.0\n# comment\npywinrm==0.5.0\n-r extra.txt\nPillow==12.2.0\n"
    current = "fastapi==0.115.0\npywinrm==0.5.0  # WinRM\nPillow==12.3.0\npyvmomi==8.0.3.0.1\npypsrp==0.8.1\n"
    assert offline_wheels.changed_requirements(base, current) == [
        "Pillow==12.3.0", "pyvmomi==8.0.3.0.1", "pypsrp==0.8.1"]


def test_project_names_compare_canonically():
    assert offline_wheels.changed_requirements("PyVmomi==1.0\n", "pyvmomi==1.0\n") == []


def test_supported_platforms_stop_at_ubuntu_2204_glibc():
    assert "manylinux_2_35_x86_64" in offline_wheels.PLATFORMS
    assert "manylinux_2_36_x86_64" not in offline_wheels.PLATFORMS
    assert offline_wheels.PYTHON_VERSIONS == ("3.10", "3.12")


def test_unchanged_requirements_leave_package_untouched(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("fastapi==0.115.0\n")
    monkeypatch.setattr(offline_wheels, "base_requirements", lambda repo, v: "fastapi==0.115.0\n")
    assert offline_wheels.bundle_wheels(tmp_path, tmp_path, "1.24.36") == []
    assert (tmp_path / "requirements.txt").read_text() == "fastapi==0.115.0\n"
    assert not (tmp_path / "wheels").exists()


def test_bundled_requirements_point_pip_at_package_wheels(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("pyvmomi==8.0.3.0.1\n")
    monkeypatch.setattr(offline_wheels, "base_requirements", lambda repo, v: "")
    monkeypatch.setattr(offline_wheels, "_build_pure_wheels", lambda reqs, built: None)
    calls = []

    def fake_pip(*args):
        calls.append(args)
        dest = Path(args[args.index("-d") + 1])
        (dest / "pyvmomi-8.0.3.0.1-py2.py3-none-any.whl").write_text("")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(offline_wheels, "_pip", fake_pip)
    assert offline_wheels.bundle_wheels(tmp_path, tmp_path, "1.24.36") == [
        "pyvmomi-8.0.3.0.1-py2.py3-none-any.whl"]
    assert [c[c.index("--python-version") + 1] for c in calls] == ["3.10", "3.12"]
    lines = (tmp_path / "requirements.txt").read_text().splitlines()
    assert "--find-links wheels" in lines and lines[-1] == "pyvmomi==8.0.3.0.1"
    assert not (tmp_path / ".wheel-build").exists()


def test_missing_base_tag_fails_the_build(tmp_path):
    import pytest
    with pytest.raises(RuntimeError, match="v0.0.1"):
        offline_wheels.base_requirements(tmp_path, "0.0.1")


def _record_pip(monkeypatch, returncodes):
    from updater.steps import pip_install
    calls = []

    def fake_run(args):
        calls.append(args)
        return SimpleNamespace(returncode=returncodes[len(calls) - 1], stdout="", stderr="index unreachable")

    monkeypatch.setattr(pip_install, "_run_pip", fake_run)
    return pip_install, calls


def test_updater_installs_from_bundled_wheels_without_index(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("--find-links wheels\npyvmomi==8.0.3.0.1\n")
    (tmp_path / "wheels").mkdir()
    pip_install, calls = _record_pip(monkeypatch, [0])
    pip_install.pip_install({}, str(tmp_path), None)
    assert calls == [["--no-index", "--find-links", str(tmp_path / "wheels"),
                      "-r", str(tmp_path / "requirements.txt")]]


def test_updater_falls_back_to_index_when_wheels_incomplete(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("pyvmomi==8.0.3.0.1\n")
    (tmp_path / "wheels").mkdir()
    pip_install, calls = _record_pip(monkeypatch, [1, 0])
    pip_install.pip_install({}, str(tmp_path), None)
    assert calls[1] == ["-r", str(tmp_path / "requirements.txt")]


def test_updater_without_wheels_uses_index_only(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("fastapi==0.115.0\n")
    pip_install, calls = _record_pip(monkeypatch, [0])
    pip_install.pip_install({}, str(tmp_path), None)
    assert calls == [["-r", str(tmp_path / "requirements.txt")]]
