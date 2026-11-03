"""Real disposable PostgreSQL, producer retries, and the standard delivery funnel.

No real HTTP/SMTP/ClickHouse connections: only those transport edges are captured.
"""
import asyncio
from uuid import UUID, uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_alert_funnel import world, session  # noqa: F401
from app.services import apm_event_rules as events, apm_synthetic_service as synthetic


def setup_rule(w, metric=events.SYNTHETIC, **changes):
    w.db.execute("UPDATE alert_rules SET metric=$1, operator='==', threshold=1, trigger_on='any',"
                 " severity='critical', priority='P2' WHERE id=$2", metric, w.rule)
    for column, value in changes.items():
        assert column in {'enabled', 'target', 'cooldown', 'recovery_alert', 'priority', 'min_duration'}
        w.db.execute(f'UPDATE alert_rules SET {column}=$1 WHERE id=$2', value, w.rule)


def monitor(w, name='Checkout journey', retries=0):
    ident = str(w.db.scalar("INSERT INTO apm_synthetic_monitors (name,retry_count) VALUES ($1,$2) RETURNING id", name, retries))
    channels = w.db.scalar('SELECT notify_channels FROM alert_rules WHERE id=$1', w.rule)
    return dict(id=ident, name=name, retry_count=retries, check_interval=60, timeout=5,
                config={'notify_channels': channels}, status='unknown')


def result(ok=False):
    return dict(success=ok, status='up' if ok else 'down', total_ms=10,
                steps_total=2, steps_passed=2 if ok else 1,
                failed_step='' if ok else 'Submit payment', error='' if ok else 'HTTP 503 Service unavailable', steps=[])


async def run(w, m):
    async with session(w.db) as db:
        return await synthetic.execute_and_record(db, m)


async def event(w, ident, breached, metric=events.SYNTHETIC):
    async with session(w.db) as db:
        return await events.evaluate_event(db, metric, ident, 'Checkout journey', breached,
            dict(failed_step='Submit payment', error='HTTP 503 Service unavailable', service='checkout-api',
                 tier='fast', long_burn=16, short_burn=18, factor=14.4))


@pytest.fixture
def producer(monkeypatch):
    monkeypatch.setattr(synthetic, '_insert_result_sync', lambda *a: None)
    async def failed(m):
        return result()
    monkeypatch.setattr(synthetic, 'run_scenario', failed)


@pytest.mark.asyncio
async def test_retries_then_one_incident_per_scenario_with_recovery(world, producer, monkeypatch):
    w = world
    setup_rule(w)
    a, b = monitor(w, retries=1), monitor(w, 'Login journey')
    attempts = []
    async def transient(m):
        attempts.append(m['id'])
        # There must be no page between the initial failure and retry success.
        assert not w.hooks
        return result(len(attempts) == 2)
    monkeypatch.setattr(synthetic, 'run_scenario', transient)
    assert (await run(w, a))['attempts'] == 2
    assert w.db.scalar('SELECT count(*) FROM alerts') == 0

    async def failed(m):
        attempts.append(m['id'])
        return result()
    monkeypatch.setattr(synthetic, 'run_scenario', failed)
    await run(w, a)
    assert len(attempts) == 4
    assert len(w.hooks) == 1
    for field in ['Checkout journey', 'Submit payment', 'HTTP 503']:
        assert field in w.hooks[0]['body'] and field in w.hooks[0]['message']
        assert field in w.mails[0]['html']
    assert w.hooks[0]['rule_id'] == w.rule
    assert 'Time:' in w.mails[0]['body']
    await run(w, a)
    await run(w, b)
    assert len(w.hooks) == 2
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'") == 2
    # No legacy scenario alert or duplicate local-channel delivery.
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE rule_id IS NULL") == 0
    assert all(h['delivery_event'] == 'trigger' for h in w.hooks)

    async def healthy(m):
        return result(True)
    monkeypatch.setattr(synthetic, 'run_scenario', healthy)
    await run(w, a)
    await run(w, a)
    assert len(w.hooks) == 3 and w.hooks[-1]['is_recovery']
    assert w.hooks[-1]['alert_id'] == w.hooks[0]['alert_id']
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'") == 1
    await run(w, b)
    assert len(w.hooks) == 4
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'") == 0


@pytest.mark.asyncio
async def test_worker_lock_covers_retries_dispatch_commits_and_stale_due_list(world, producer, monkeypatch):
    w = world
    setup_rule(w)
    m = monitor(w)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def waiting(m):
        entered.set()
        await finish.wait()
        return result()
    monkeypatch.setattr(synthetic, 'run_scenario', waiting)
    first = asyncio.create_task(run(w, m))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert await run(w, m) is None
    finally:
        finish.set()
        await first
    async with session(w.db) as db:
        assert await synthetic.execute_and_record(db, m, scheduled=True) is None
    assert len(w.hooks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'enabled': False}, {'target': str(uuid4())}, {'min_duration': 60}])
async def test_unmatched_or_unsupported_rules_keep_local_notifications(world, producer, change):
    setup_rule(world, **change)
    await run(world, monitor(world))
    assert len(world.hooks) == 1
    assert world.db.scalar('SELECT rule_id FROM alerts') is None


@pytest.mark.asyncio
async def test_specific_scope_recovery_toggle_and_fixed_incident_priority(world):
    w = world
    ident, other = str(uuid4()), str(uuid4())
    setup_rule(w, target=ident, recovery_alert=False, priority='P1')
    assert not await event(w, other, True)
    assert await event(w, ident, True)
    w.db.execute("UPDATE alert_rules SET priority='P4' WHERE id=$1", w.rule)
    await event(w, ident, True)
    assert w.db.scalar('SELECT priority FROM alerts') == 'P1'
    await event(w, ident, False)
    assert len(w.hooks) == 1
    assert w.db.scalar('SELECT status FROM alerts') == 'resolved'


@pytest.mark.asyncio
async def test_quiet_hours_defers_without_orphan_recovery_and_cooldown_is_per_entity(world, monkeypatch):
    from app.services import alert_schedule
    w = world
    setup_rule(w, cooldown=300)
    a, b = str(uuid4()), str(uuid4())
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *args: False)
    await event(w, a, True)
    assert not w.hooks
    await event(w, a, False)
    assert not w.hooks
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *args: True)
    await event(w, a, True)
    await event(w, a, False)
    await event(w, a, True)
    assert len(w.hooks) == 2  # last trigger deferred by cooldown
    await event(w, b, True)
    assert len(w.hooks) == 3  # another scenario is never suppressed
    w.db.execute("UPDATE alerts SET triggered_at=triggered_at-interval '6 minutes' WHERE status='resolved'")
    await event(w, a, True)
    assert len(w.hooks) == 4


@pytest.mark.asyncio
async def test_no_data_never_recovers_slo_and_disabled_rule_can_close_silently(world):
    w = world
    setup_rule(w, events.SLO)
    ident = str(uuid4())
    await event(w, ident, True, events.SLO)
    assert '16× / 18×' in w.hooks[0]['body']
    await event(w, ident, None, events.SLO)
    assert w.db.scalar('SELECT status FROM alerts') == 'active'
    await event(w, ident, True, events.SLO)
    assert len(w.hooks) == 1
    w.db.execute('UPDATE alert_rules SET enabled=false WHERE id=$1', w.rule)
    await event(w, ident, False, events.SLO)
    assert w.db.scalar('SELECT status FROM alerts') == 'resolved'
    assert len(w.hooks) == 1


@pytest.mark.parametrize('changes', [dict(operator='>'), dict(threshold=2), dict(min_duration=60),
    dict(clear_duration=60), dict(device_id=str(uuid4())), dict(target='checkout'),
    dict(trigger_on='up'), dict(conditions=[{'metric': events.SYNTHETIC}, {'metric': 'cpu'}])])
def test_invalid_event_contract_is_explicit(changes):
    with pytest.raises(ValueError):
        events.validate_definition(dict(metric=events.SYNTHETIC, operator='==', threshold=1) | changes)


@pytest.mark.asyncio
async def test_real_slo_producer_coalesces_tiers_and_requires_evidence_to_recover(world, monkeypatch):
    from app.services import apm_slo_service as slo
    w = world
    setup_rule(w, events.SLO)
    service_id = w.db.scalar("INSERT INTO apm_services(name) VALUES ('checkout-api') RETURNING id")
    w.db.execute("INSERT INTO apm_slos(name,service_id,sli_type,target,notify_channels)"
                 " SELECT 'Checkout availability',$1,'availability',99.9,notify_channels FROM alert_rules WHERE id=$2", service_id, w.rule)
    state = {'tiers': [dict(tier=name, breaching=True, long_burn=16, short_burn=18, factor=14.4) for name in ['fast', 'slow']]}
    monkeypatch.setattr(slo, 'compute_slo_status', lambda s: state)
    async def sweep():
        async with session(w.db) as db:
            await slo.evaluate_slo_burn(db)
    await sweep()
    await sweep()
    assert len(w.hooks) == 1
    assert w.db.scalar('SELECT count(*) FROM alerts') == 1
    state['tiers'] = [dict(tier='fast', breaching=False, long_burn=None, short_burn=None)]
    await sweep()
    assert w.db.scalar('SELECT status FROM alerts') == 'active'
    state['tiers'] = [dict(tier='fast', breaching=False, long_burn=0, short_burn=0)]
    await sweep()
    assert w.db.scalar('SELECT status FROM alerts') == 'resolved'
    assert len(w.hooks) == 2 and w.hooks[-1]['is_recovery']


@pytest.mark.asyncio
async def test_rule_api_create_update_scope_and_preview(world, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import alert_rules as api
    from app.services import alert_rule_scope as scope
    w = world
    m = monitor(w)
    monkeypatch.setattr(api.rs, 'guard_rule_change', AsyncMock())
    monkeypatch.setattr(api, 'write_audit_log', AsyncMock())
    user = SimpleNamespace(id=None, role='admin')
    body = dict(name='Any failed journey', metric=events.SYNTHETIC, operator='==', threshold=1, recovery_alert=True)
    async with session(w.db) as db:
        created = await api.create_alert_rule(api.AlertRuleCreate(**body), db=db, user=user)
        assert scope.describe_scope(created)['label'] == 'All scenarios'
        assert (await scope.affected(db, created))['count'] == 1
        specific = await api.update_alert_rule(UUID(created['id']), api.AlertRuleUpdate(target=m['id']), db=db, user=user)
        names = await scope.scope_names(db, specific)
        assert scope.describe_scope(specific, names)['label'] == 'Scenario Checkout journey'
        for changes in [dict(min_duration=10), dict(target=str(uuid4())), dict(operator='>')]:
            with pytest.raises(HTTPException) as exc:
                await api.update_alert_rule(UUID(created['id']), api.AlertRuleUpdate(**changes), db=db, user=user)
            assert exc.value.status_code == 422
        saved = await api.update_alert_rule(UUID(created['id']), api.AlertRuleUpdate(name='Journeys', target=None), db=db, user=user)
        assert saved['target'] is None
    for metric in events.EVENT_METRICS:
        for recovery in [False, True]:
            preview = api._build_alert_message({**body, 'metric': metric}, is_recovery=recovery)
            assert preview['device_status'] == ('RESOLVED' if recovery else 'DOWN')
            assert 'Checkout' in preview['email_body']
            assert '{' not in preview['sms_body']
            if metric == events.SYNTHETIC and not recovery:
                assert 'Submit payment' in preview['sms_body'] and 'HTTP 503' in preview['email_body']


@pytest.mark.asyncio
async def test_reminders_keep_scenario_identity_failure_and_timezone(world, monkeypatch):
    from app.services import alert_escalation_service as esc
    from app.api.v1 import alert_engine
    w = world
    setup_rule(w)
    w.db.execute("INSERT INTO system_settings(key,value) VALUES ('company', $1::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", {'timezone': 'Asia/Riyadh'})
    w.db.execute("INSERT INTO notification_gateways(name,type,config,is_default,enabled) VALUES ('sms','sms',$1::jsonb,true,true)", {'provider':'custom_http','api_url':'https://sms.invalid'})
    sms = w.db.scalar("INSERT INTO notification_channels(name,type,config) VALUES ('sms','sms',$1::jsonb) RETURNING id::text", {'phone_numbers':'+15550000001'})
    w.db.execute("UPDATE alert_rules SET notify_channels=notify_channels || to_jsonb($1::text), sms_template='{scenario_name}: {failed_step}: {error} at {timestamp}' WHERE id=$2", sms, w.rule)
    sent = []
    async def send(gw, rcpt, message, **kw):
        sent.append(message)
        return True
    monkeypatch.setattr(alert_engine, '_send_sms', send)
    ident = str(uuid4())
    await event(w, ident, True)
    w.db.execute("UPDATE alert_rules SET escalation_levels=jsonb_build_array(jsonb_build_object('after_minutes',1,'notify_channels',notify_channels,'repeat_every_minutes',1)) WHERE id=$1", w.rule)
    async with session(w.db) as db:
        row = (await db.execute(text(esc._legacy_sweep_sql(False)))).first()
        assert row is not None
        await esc._dispatch_escalation(db, row, 1, row.escalation_levels[0], is_repeat=True,
                                       action_url='', repeats=1)
    assert w.hooks[-1]['hostname'] == 'Checkout journey'
    assert 'Submit payment' in w.hooks[-1]['message']
    assert 'HTTP 503' in w.hooks[-1]['message']
    assert 'GMT+3' in w.mails[-1]['body']
    assert len(sent) == 2
    assert all('GMT+3' in message and 'Submit payment' in message and 'HTTP 503' in message for message in sent)
    assert sent[0].startswith('Checkout journey: Submit payment: HTTP 503')
