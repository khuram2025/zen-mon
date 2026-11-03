"""Checking must not install; manual installs stay within the reviewed path."""
import importlib.machinery
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from updater import agent, plan
from updater.config import AgentConfig, UpdateConfig

ROOT = Path(__file__).resolve().parents[2]


def release(version, minimum=None):
    return dict(id='release-' + version.replace('.', '-'), version=version, min_version=minimum,
                package_sha256='a' * 64, changelog='Release ' + version)


def catalog(releases, calls):
    def respond(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.path == '/api/v1/updates/check'
        current = request.url.params['current_version']
        offered = next((r for r in releases if plan.version_key(r['version']) > plan.version_key(current)), None)
        return httpx.Response(200, json={'available': bool(offered), 'release': offered})
    return lambda cfg: httpx.Client(base_url='https://catalog.example', transport=httpx.MockTransport(respond))


def test_preview_required_bridges_is_get_only_and_does_not_check_in(monkeypatch):
    calls = []
    offers = [release('1.24.36', '1.23.9'), release('1.24.46', '1.24.36')]
    monkeypatch.setattr(agent, 'checkin', Mock(side_effect=AssertionError('must not check in')))
    result = plan.get_plan(NS(), current='1.24.29', client_factory=catalog(offers, calls))
    assert [s['version'] for s in result['steps']] == ['1.24.36', '1.24.46']
    assert [r.url.params['current_version'] for r in calls] == ['1.24.29', '1.24.36', '1.24.46']
    assert plan.selected_path(result, '1.24.36') == [plan.release_identity(offers[0])]
    assert len(plan.selected_path(result, '1.24.46')) == 2
    with pytest.raises(ValueError):
        plan.selected_path(result, '1.24.35')


def test_plan_is_stable_until_identity_or_package_changes():
    offers = [release('1.24.46')]
    def read(): return plan.get_plan(NS(), current='1.24.45', client_factory=catalog(offers, []))
    first = read()
    assert read()['plan_id'] == first['plan_id']
    offers[0]['package_sha256'] = 'b' * 64
    assert read()['plan_id'] != first['plan_id']


@pytest.mark.parametrize('offer', [release('1.24.44'), release('1.24.46', '1.24.45'),
                                  {**release('1.24.46'), 'package_sha256': 'bad'}])
def test_invalid_or_skipped_prerequisite_preview_fails(offer):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'available': True, 'release': offer}))
    with pytest.raises(ValueError):
        plan.get_plan(NS(), current='1.24.44', client_factory=lambda cfg: httpx.Client(base_url='https://example.test', transport=transport))


def test_catalog_error_is_not_reported_as_no_update():
    transport = httpx.MockTransport(lambda req: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        plan.get_plan(NS(), current='1.24.45', client_factory=lambda cfg: httpx.Client(base_url='https://example.test', transport=transport))


@pytest.mark.parametrize('enabled,start,end,hour,minute,allowed', [
    (False, '', '', 12, 0, False), (True, '', '', 12, 0, True),
    (True, '02:00', '04:00', 2, 0, True), (True, '02:00', '04:00', 4, 0, False),
    (True, '22:00', '02:00', 23, 0, True), (True, '22:00', '02:00', 1, 59, True),
    (True, '22:00', '02:00', 12, 0, False), (True, 'bad', '02:00', 1, 0, False),
    (True, '02:00', '', 3, 0, False),
])
def test_automatic_policy(enabled, start, end, hour, minute, allowed):
    cfg = UpdateConfig(auto_update=enabled, maintenance_window_start=start, maintenance_window_end=end)
    assert plan.automatic_install_allowed(cfg, datetime(2026, 11, 3, hour, minute)) is allowed


@pytest.fixture
def runtime(monkeypatch):
    cfg = AgentConfig()
    cfg.appliance.id = 'fixture'
    cfg.appliance.api_key = 'fixture'
    state = {'version': '1.24.45'}
    offers = [release('1.24.46'), release('1.24.47', '1.24.46')]
    preview = plan.get_plan(cfg, current=state['version'], client_factory=catalog(offers, []))
    monkeypatch.setattr(agent, 'load_config', lambda *args: cfg)
    monkeypatch.setattr(agent, 'setup_logging', lambda *args: None)
    monkeypatch.setattr(agent, 'UpdateLock', Mock())
    monkeypatch.setattr(agent, 'get_current_version', lambda: state['version'])
    monkeypatch.setattr(plan, 'get_plan', lambda cfg: preview)
    monkeypatch.setattr(plan, 'write_manual_status', Mock())
    monkeypatch.setattr(agent, 'checkin', Mock(return_value=offers[0]))
    monkeypatch.setattr(agent, 'check_for_update', Mock(return_value=offers[0]))
    def apply(cfg, rel):
        state['version'] = rel['version']
        return True
    install = Mock(side_effect=apply)
    monkeypatch.setattr(agent, 'run_update', install)
    monkeypatch.setattr(agent.os, 'execv', Mock(side_effect=SystemExit(0)))
    return cfg, state, preview, offers, install


@pytest.mark.parametrize('check_only', [False, True])
def test_check_only_and_disabled_automatic_updates_never_install(runtime, check_only):
    cfg, _, _, _, install = runtime
    cfg.update.auto_update = check_only  # --check wins even if automatic install is on
    assert agent.main(check_only=check_only) == 0
    install.assert_not_called()
    agent.check_for_update.assert_not_called()


def test_outside_maintenance_window_never_installs(runtime, monkeypatch):
    monkeypatch.setattr(plan, 'automatic_install_allowed', lambda cfg: False)
    assert agent.main() == 0
    runtime[-1].assert_not_called()


def test_manual_install_stops_at_selected_intermediate_even_with_auto_disabled(runtime):
    cfg, state, preview, offers, install = runtime
    cfg.update.auto_update = False
    assert agent.main(install_plan=preview['plan_id'] + '-1.24.46') == 0
    assert state['version'] == '1.24.46'
    install.assert_called_once_with(cfg, offers[0])
    agent.os.execv.assert_not_called()
    agent.checkin.assert_not_called()


def test_follow_path_reloads_new_code_with_only_remaining_approved_steps(runtime, tmp_path):
    cfg, _, preview, offers, install = runtime
    with pytest.raises(SystemExit):
        agent.main(str(tmp_path / 'agent.conf'), install_plan=preview['plan_id'] + '-1.24.47')
    args = agent.os.execv.call_args.args[1]
    assert json.loads(args[args.index('--continue-plan') + 1]) == [plan.release_identity(offers[1])]
    assert args[-2:] == ['--config', str((tmp_path / 'agent.conf').resolve())]
    install.assert_called_once()


def test_changed_preview_never_installs(runtime):
    assert agent.main(install_plan='0' * 64 + '-1.24.46') == 1
    runtime[-1].assert_not_called()


@pytest.mark.parametrize('offered', [None, release('1.24.48'), {**release('1.24.46'), 'package_sha256': 'b' * 64}])
def test_changed_or_withdrawn_offer_never_substitutes_an_unapproved_package(runtime, monkeypatch, offered):
    monkeypatch.setattr(agent, 'check_for_update', lambda cfg: offered)
    assert agent.main(remaining_path=[plan.release_identity(runtime[3][0])]) == 1
    runtime[-1].assert_not_called()


def test_failed_step_stops_path_without_loading_next_code(runtime):
    runtime[-1].side_effect = None
    runtime[-1].return_value = False
    assert agent.main(install_plan=runtime[2]['plan_id'] + '-1.24.47') == 1
    agent.os.execv.assert_not_called()


@pytest.fixture
def api(monkeypatch):
    from app.api.v1 import system_updates as api
    monkeypatch.setattr(api, '_registration_is_valid', lambda: True)
    monkeypatch.setattr(api, '_updater_service_is_running', lambda: False)
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    return api


@pytest.mark.asyncio
async def test_check_endpoint_runs_only_unprivileged_preview(api, monkeypatch):
    preview = {'from_version': '1.24.45', 'plan_id': 'a' * 64, 'steps': [release('1.24.46')]}
    run = Mock(return_value=NS(returncode=0, stdout=json.dumps(preview)))
    monkeypatch.setattr(api.subprocess, 'run', run)
    assert await api.trigger_check(NS(), NS(commit=AsyncMock())) == preview
    args = run.call_args.args[0]
    assert args[1:] == ['-B', '-m', 'updater', '--plan']
    assert 'sudo' not in args and 'systemctl' not in args
    run.assert_called_once()


@pytest.mark.asyncio
async def test_install_endpoint_rechecks_plan_and_queues_only_bounded_helper(api, monkeypatch):
    preview = {'plan_id': 'a' * 64, 'steps': [release('1.24.46')]}
    monkeypatch.setattr(api, '_available_update_plan', AsyncMock(return_value=preview))
    run = Mock(return_value=NS(returncode=0))
    monkeypatch.setattr(api.subprocess, 'run', run)
    body = api.InstallUpdateRequest(plan_id='a' * 64, target_version='1.24.46')
    assert (await api.install_update(body, NS(), NS(commit=AsyncMock())))['status'] == 'queued'
    assert run.call_args.args[0] == ['sudo', '-n', '/usr/local/sbin/zenplus-updater-install-helper', 'install', 'a' * 64, '1.24.46']


@pytest.mark.asyncio
@pytest.mark.parametrize('running,changed', [(True, False), (False, True)])
async def test_install_conflicts_do_not_queue(api, monkeypatch, running, changed):
    monkeypatch.setattr(api, '_updater_service_is_running', lambda: running)
    monkeypatch.setattr(api, '_available_update_plan', AsyncMock(return_value={'plan_id': 'b' * 64, 'steps': []}))
    run = Mock(side_effect=AssertionError('must not queue'))
    monkeypatch.setattr(api.subprocess, 'run', run)
    with pytest.raises(api.HTTPException) as error:
        await api.install_update(api.InstallUpdateRequest(plan_id='a' * 64, target_version='1.24.46'), NS(), NS(commit=AsyncMock()))
    assert error.value.status_code == 409
    run.assert_not_called()


def test_install_helper_rejects_extra_arguments_and_shell_or_unit_injection():
    loader = importlib.machinery.SourceFileLoader('install_helper_fixture', str(ROOT / 'scripts/zenplus-updater-install-helper'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    helper = importlib.util.module_from_spec(spec); loader.exec_module(helper)
    run = Mock(return_value=NS(returncode=0))
    for args in ([], ['install', 'a' * 64, '1.2.3', 'other.service'],
                 ['install', 'a' * 64, '1.2.3\nreboot'], ['install', '*', '1.2.3']):
        assert helper.main(args, require_root=False, run=run) == 1
    run.assert_not_called()
    assert helper.main(['install', 'a' * 64, '1.2.3'], require_root=False, run=run) == 0
    assert run.call_args.args[0] == ['/bin/systemctl', '--no-block', 'start', 'zenplus-updater-install@' + 'a' * 64 + '-1.2.3.service']


def test_two_update_instances_cannot_acquire_same_lock(tmp_path):
    from updater.lockfile import UpdateLock, LockError
    first, second = UpdateLock(str(tmp_path / 'lock')), UpdateLock(str(tmp_path / 'lock'))
    first.acquire()
    try:
        with pytest.raises(LockError): second.acquire()
        second.release()  # Must not release the first owner's lock.
        with pytest.raises(LockError): second.acquire()
    finally: first.release()
    second.acquire(); second.release()
