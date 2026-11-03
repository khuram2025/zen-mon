"""Regional timestamps across real notification dispatch and report renderers."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.branding import Branding, branding_from_values
from app.services.regional_time import localize_text, timestamp_label
from app.services.alert_phrasing import sms_with_timestamp
from app.services import email_render as er
from app.api.v1.alert_rules import _build_alert_message, _preview_email_ctx

NOW = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)
RIYADH = Branding(timezone='Asia/Riyadh')


@pytest.mark.parametrize('zone,at,expected', [
    ('Asia/Riyadh', NOW, '2026-11-03 16:00 GMT+3'),
    ('UTC', NOW, '2026-11-03 13:00 UTC'),
    ('Asia/Kolkata', NOW, '2026-11-03 18:30 GMT+5:30'),
    ('Asia/Riyadh', '2026-11-03T22:30:00Z', '2026-11-04 01:30 GMT+3'),
    ('America/New_York', '2026-11-01T05:30:00Z', '2026-11-01 01:30 GMT-4'),
    ('America/New_York', '2026-11-01T06:30:00Z', '2026-11-01 01:30 GMT-5'),
])
def test_same_instant_and_offset_in_email_sms_and_reports(zone, at, expected):
    brand=Branding(timezone=zone)
    assert timestamp_label(at,brand)==expected
    ctx={'timestamp':at,'first_seen':at,'incident_ref':'INC-test','message':'Service failed.', 'branding':brand}
    for render in (er.build_alert_email_html,er.build_alert_email_text,
                   er.build_notification_email_html,er.build_notification_email_text):
        assert expected in render(ctx)
    assert sms_with_timestamp('Service failed.',at,brand).endswith(expected)
    from app.services.report_html import _generated_text
    assert _generated_text({'generated_at':at,'branding':brand})==expected


def test_configured_date_format_and_twelve_hour_clock():
    brand=Branding(timezone='Asia/Riyadh',date_format='DD/MM/YYYY',time_format='12h')
    assert timestamp_label(NOW,brand)=='03/11/2026 4:00 PM GMT+3'
    assert timestamp_label(NOW.replace(tzinfo=None),brand)=='03/11/2026 4:00 PM GMT+3'
    assert timestamp_label(NOW,branding_from_values({'timezone':'bad/zone'})).endswith('13:00 UTC')


@pytest.mark.parametrize('old', ['13:00 UTC on 03 Nov 2026','03 Nov 2026 13:00 UTC',
                               '2026-11-03 13:00 UTC'])
def test_old_queued_templates_keep_original_event_time_without_duplicate_stamps(old):
    message=sms_with_timestamp('Device down. '+old,NOW,RIYADH)
    assert message=='Device down. 2026-11-03 16:00 GMT+3'
    assert sms_with_timestamp(message,NOW,RIYADH)==message
    assert sms_with_timestamp('At {timestamp}: device down.',NOW,RIYADH)=='At 2026-11-03 16:00 GMT+3: device down.'


@pytest.mark.parametrize('metric,recovery', [(metric,recovery)
    for metric in ('ping_status','service_status','host_cpu','apm_error_rate','syslog')
    for recovery in (False,True) if metric!='syslog' or not recovery])
def test_every_rule_test_and_preview_uses_configured_zone(metric,recovery):
    brand=Branding(timezone='Asia/Riyadh')
    rule={'name':'Test','metric':metric,'operator':'==','threshold':0,'severity':'critical'}
    msg=_build_alert_message(rule,is_recovery=recovery,branding=brand)
    ctx=_preview_email_ctx(rule,msg,recovery,'https://example.invalid/alerts')
    ctx['branding']=brand
    expected=timestamp_label(msg['iso_timestamp'],brand)
    assert expected in msg['sms_body']
    assert expected in er.build_alert_email_html(ctx)
    assert expected in er.build_alert_email_text(ctx)
    assert ' UTC' not in msg['sms_body']


# Reuse the disposable PostgreSQL world; only outbound transports are faked.
from test_alert_funnel import world, session  # noqa: E402,F401
from pg_fixture import pg_cluster, pg_db  # noqa: E402,F401


@pytest.mark.asyncio
@pytest.mark.parametrize('event',['trigger','recovery','reminder:1','escalation:2'])
async def test_dispatch_localizes_all_lifecycle_channels_with_original_first_seen(world,monkeypatch,event):
    from app.api.v1 import alert_engine
    from app.services.host_alert_service import dispatch_to_channels
    w=world
    w.db.execute("INSERT INTO system_settings(key,value) VALUES ('company',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value",{'timezone':'Asia/Riyadh'})
    mail=w.db.scalar("SELECT id::text FROM notification_channels WHERE name='noc'")
    w.db.execute("INSERT INTO notification_gateways(name,type,config,is_default,enabled) VALUES ('sms','sms',$1::jsonb,true,true)",{'provider':'custom_http','api_url':'https://sms.invalid'})
    sms=w.db.scalar("INSERT INTO notification_channels(name,type,config) VALUES ('sms','sms',$1::jsonb) RETURNING id::text",{'phone_numbers':'+15550000001'})
    sent=[]
    async def send(gw,rcpt,message,**kw):sent.append(message);return True
    monkeypatch.setattr(alert_engine,'_send_sms',send)
    ctx={'subject':'Service event','message':'Service event at 13:00 UTC on 03 Nov 2026',
         'body':'Observed at 2026-11-03 13:00 UTC.','severity':'critical','rule_name':'Service',
         'hostname':'test-service','status':'RESOLVED' if event=='recovery' else 'DOWN',
         'triggered_at':NOW.isoformat(),'first_seen':'2026-11-03T12:00:00Z','incident_ref':'INC-test',
         'delivery_event':event,'groupable':False}
    async with session(w.db) as db:
        assert await dispatch_to_channels(db,[mail,sms],ctx)==2
    assert sent==['Service event at 2026-11-03 16:00 GMT+3']
    assert w.mails
    for output in (w.mails[0]['body'],w.mails[0]['html']):
        assert '2026-11-03 16:00 GMT+3' in output
        assert '2026-11-03 15:00 GMT+3' in output
        assert '13:00 UTC' not in output
    assert ctx['triggered_at']==NOW.isoformat()  # Storage/webhook event identity unchanged.


@pytest.mark.asyncio
async def test_digest_first_seen_in_plain_and_html(world):
    from app.services.notification_grouping import digest_context
    w=world
    w.db.execute("INSERT INTO system_settings(key,value) VALUES ('company',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value",{'timezone':'Asia/Riyadh'})
    import uuid
    d=NS(id=uuid.uuid4(),group_label='Branch',group_key='site:test',reason='correlation')
    item=NS(alert_id=uuid.uuid4(),alert_status='active',severity='warning',summary={'first_seen':NOW.isoformat(),'severity':'warning','priority':'P3','asset':'router','condition':'Down'},recovered_at=None)
    async with session(w.db) as db:ctx=await digest_context(db,d,[item])
    assert '2026-11-03 16:00 GMT+3' in ctx['body']
    assert '2026-11-03 16:00 GMT+3' in ctx['html_body']


@pytest.mark.asyncio
async def test_gateway_tests_use_regional_time_with_captured_transports(monkeypatch):
    from app.api.v1 import settings
    import smtplib
    from email import message_from_string
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(settings, 'datetime', Clock)
    mails=[]
    class SMTP:
        def __init__(self,*args,**kwargs): pass
        def sendmail(self, sender, recipients, body): mails.append(message_from_string(body))
        def quit(self): pass
    monkeypatch.setattr(smtplib,'SMTP',SMTP)
    monkeypatch.setattr('app.core.smtp_tls.starttls_if_needed',lambda *args:None)
    settings._do_smtp_test({'host':'smtp.invalid','from_email':'test@example.invalid','encryption':'none'},'recipient@example.invalid',RIYADH)
    for part in mails[0].get_payload():
        assert '2026-11-03 16:00 GMT+3' in part.get_payload(decode=True).decode()
    texts=[]
    async def send(config,values,**kwargs):
        texts.append(values['message'])
        return NS(status_code=200,extensions={})
    monkeypatch.setattr(settings,'send_sms_request',send)
    await settings._do_sms_test({'provider':'custom_http','api_url':'https://sms.invalid'},'+15550000001',RIYADH)
    assert texts[0].endswith('2026-11-03 16:00 GMT+3')


@pytest.mark.asyncio
async def test_ncm_original_timestamp_and_structured_payload(world,monkeypatch):
    from app.services import ncm_delivery
    w=world
    w.db.execute("INSERT INTO system_settings(key,value) VALUES ('company',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value",{'timezone':'Asia/Riyadh'})
    mail=w.db.scalar("SELECT id::text FROM notification_channels WHERE name='noc'")
    sent=[]
    monkeypatch.setattr(ncm_delivery,'smtp_send',lambda gw,rcpt,payload:sent.append(payload))
    original={'event_id':'test','subject':'NCM recovery','body':'Configuration backup succeeded.','timestamp':NOW.isoformat()}
    async with session(w.db) as db: assert await ncm_delivery.send(db,[mail],original)==1
    assert sent[0]['body'].endswith('Time: 2026-11-03 16:00 GMT+3')
    assert sent[0]['sms_body'].endswith('2026-11-03 16:00 GMT+3')
    assert original['body']=='Configuration backup succeeded.' and original['timestamp']==NOW.isoformat()
