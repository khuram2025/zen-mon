import os
from pathlib import Path
import pytest

@pytest.mark.skipif(os.name!='posix',reason='Package permission boundary is POSIX-specific')
def test_pip_installer_does_not_inherit_private_backup_umask(tmp_path,monkeypatch):
    from updater.steps import pip_install
    script=tmp_path/'pip-fixture'
    script.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nPath(__file__).with_name("package-file").write_text("fixture")\n')
    script.chmod(0o755)
    (tmp_path/'requirements.txt').write_text('')
    monkeypatch.setattr(pip_install,'VENV_PIP',str(script))
    old=os.umask(0o077)
    try: pip_install.pip_install({},str(tmp_path),None)
    finally: os.umask(old)
    assert (tmp_path/'package-file').stat().st_mode & 0o777==0o644


@pytest.mark.parametrize('enabled',[True,False])
def test_optional_receiver_starts_only_when_enabled(monkeypatch,enabled):
    from types import SimpleNamespace
    from updater.steps import service_control
    calls=[]
    monkeypatch.setattr(service_control.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0 if enabled else 1))
    monkeypatch.setattr(service_control,'_systemctl',lambda action,services:calls.append((action,services)))
    service_control.start_services({'services':['zenplus-api'],'enabled_services':['zenplus-syslog']},'',None)
    assert calls==[('start',['zenplus-api']+(['zenplus-syslog'] if enabled else []))]
