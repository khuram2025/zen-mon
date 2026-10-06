"""OTA delivery must update the management CLI without deleting live binaries."""
import importlib
import importlib.util
from pathlib import Path
import shutil
import pytest
from updater.code_inventory import write_payload_manifest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_cli_builder', ROOT / 'scripts/build-release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_cli_and_helpers_are_delivered_but_running_binaries_are_preserved(tmp_path, monkeypatch):
    code = tmp_path / 'code'
    code.mkdir()
    monkeypatch.setattr(builder, 'ZENPLUS_DIR', ROOT)
    builder.stage_code_files(code)
    assert (code / 'bin/zenplus-cli').read_bytes() == (ROOT / 'bin/zenplus-cli').read_bytes()
    assert (code / 'bin/zenplus-apply-ntp.sh').read_bytes() == (ROOT / 'bin/zenplus-apply-ntp.sh').read_bytes()
    write_payload_manifest(code, managed_files=builder.CODE_FILES)
    destination = tmp_path / 'installed'
    (destination / 'bin').mkdir(parents=True)
    for name in ('zenplus-poller', 'zenplus-netflow-collector', 'local-operator-helper'):
        (destination / 'bin' / name).write_bytes(b'keep running inode')
    (destination / 'bin/zenplus-cli').write_bytes(b'old cli')
    apply = importlib.import_module('updater.steps.apply_code')
    monkeypatch.setattr(apply, 'ZENPLUS_DIR', destination)
    apply._apply_replace({'source': 'code'}, str(tmp_path))
    assert (destination / 'bin/zenplus-cli').read_bytes() == (ROOT / 'bin/zenplus-cli').read_bytes()
    for name in ('zenplus-poller', 'zenplus-netflow-collector', 'local-operator-helper'):
        assert (destination / 'bin' / name).read_bytes() == b'keep running inode'


def test_release_refuses_a_missing_cli_helper(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    for name in builder.CODE_FILES:
        if name == 'bin/zenplus-apply-ntp.sh':
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    monkeypatch.setattr(builder, 'ZENPLUS_DIR', source)
    with pytest.raises(RuntimeError, match='zenplus-apply-ntp.sh'):
        builder.stage_code_files(tmp_path / 'payload')
