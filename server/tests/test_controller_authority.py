"""Real transactional authority and hostile local admission conditions."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from app.services import controller_authority as owner
from app.core import controller_admission as admission
from pg_fixture import pg_cluster, pg_db  # noqa: F401


async def eligible():
    return None


async def fence(node, boot):
    return {'node_id':node,'power_state':'off','request_id':str(uuid4())}


def args(node='a'):
    return dict(cell='test-cell',node=node,boot=str(uuid4()),token=str(uuid4()),eligible=eligible,fence=fence)


async def claim(pg_db, **kwargs):
    engine=create_async_engine(pg_db.url,poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as db:
            gen=await owner.acquire(db,**kwargs)
            await db.commit()
            return gen
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_generation_excludes_competitors_and_expired_owner_needs_fence(pg_db):
    a=args();assert await claim(pg_db,**a)==1
    with pytest.raises(owner.AuthorityUnavailable):await claim(pg_db,**args('b'))
    pg_db.execute("UPDATE controller_authority SET lease_expires_at=clock_timestamp()-interval '1 second'")
    calls=[]
    async def proved(node,boot):
        calls.append((node,boot));return await fence(node,boot)
    b=args('b');b['fence']=proved
    assert await claim(pg_db,**b)==2
    assert calls==[('a',a['boot'])]
    assert pg_db.scalar('SELECT count(*) FROM controller_authority_events')==2
    engine=create_async_engine(pg_db.url,poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as db:
            with pytest.raises(owner.AuthorityUnavailable):
                await owner.renew(db,cell=a['cell'],node='a',token=a['token'],generation=1)
    finally:await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('result',[None,{'node_id':'a','power_state':'unknown','request_id':'x'},
    {'node_id':'wrong','power_state':'off','request_id':'x'},{'node_id':'a','power_state':'off'}])
async def test_no_takeover_without_positive_fence_and_no_partial_audit(pg_db,result):
    await claim(pg_db,**args())
    pg_db.execute("UPDATE controller_authority SET lease_expires_at=clock_timestamp()-interval '1 second'")
    b=args('b')
    async def bad(*_):return result
    b['fence']=bad
    with pytest.raises(owner.AuthorityUnavailable):await claim(pg_db,**b)
    assert pg_db.scalar('SELECT node_id FROM controller_authority')=='a'
    assert pg_db.scalar('SELECT count(*) FROM controller_authority_events')==1


@pytest.mark.asyncio
async def test_fence_exception_and_candidate_losing_eligibility_roll_back(pg_db):
    await claim(pg_db,**args())
    pg_db.execute("UPDATE controller_authority SET lease_expires_at=clock_timestamp()-interval '1 second'")
    b=args('b');checks=[]
    async def gone():
        checks.append(1)
        if len(checks)>1:raise owner.AuthorityUnavailable('primary changed')
    b['eligible']=gone
    with pytest.raises(owner.AuthorityUnavailable):await claim(pg_db,**b)
    assert pg_db.scalar('SELECT generation FROM controller_authority')==1


@pytest.mark.asyncio
async def test_concurrent_initial_candidates_commit_one_generation(pg_db):
    async def one(node):
        try:return await claim(pg_db,**args(node))
        except owner.AuthorityUnavailable:return None
    outcomes=await asyncio.gather(one('a'),one('b'))
    assert sorted(x for x in outcomes if x is not None)==[1]
    assert pg_db.scalar('SELECT count(*) FROM controller_authority_events')==1


@pytest.mark.asyncio
async def test_application_role_cannot_change_controller_authority(pg_db):
    import sqlalchemy
    engine=create_async_engine(pg_db.url_for('zenplus'),poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as db:
            with pytest.raises(sqlalchemy.exc.DBAPIError):await owner.acquire(db,**args())
    finally:await engine.dispose()


@pytest.mark.asyncio
async def test_expired_generation_cannot_renew_even_without_successor(pg_db):
    a=args();await claim(pg_db,**a)
    pg_db.execute("UPDATE controller_authority SET lease_expires_at=clock_timestamp()-interval '1 second'")
    engine=create_async_engine(pg_db.url,poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as db:
            with pytest.raises(owner.AuthorityUnavailable):
                await owner.renew(db,cell=a['cell'],node='a',token=a['token'],generation=1)
    finally:await engine.dispose()


def test_admission_default_and_explicit_missing_grant(monkeypatch,tmp_path):
    monkeypatch.delenv('ZENPLUS_CONTROLLER_GRANT',raising=False);assert admission.permitted()
    monkeypatch.setenv('ZENPLUS_CONTROLLER_GRANT',str(tmp_path/'missing'));assert not admission.permitted()
    monkeypatch.setenv('ZENPLUS_CONTROLLER_GRANT','');assert not admission.permitted()


@pytest.mark.parametrize('grant',[
    {'generation':1,'boot_id':'old','valid_until':105},
    {'generation':1,'boot_id':'boot','valid_until':100},
    {'generation':0,'boot_id':'boot','valid_until':105},
    {'generation':True,'boot_id':'boot','valid_until':105},
    {'generation':1,'boot_id':'boot','valid_until':200},
])
def test_admission_denies_expired_future_or_wrong_boot(monkeypatch,tmp_path,grant):
    path=tmp_path/'grant';path.write_text(json.dumps(grant))
    original=Path.stat
    def stat(p,*a,**kw):
        s=original(p,*a,**kw)
        return SimpleNamespace(st_uid=0,st_mode=s.st_mode)
    monkeypatch.setattr(Path,'stat',stat)
    with pytest.raises(ValueError):admission.validate(path,now=100,boot='boot')


@pytest.mark.asyncio
async def test_admission_closes_http_and_websocket_without_running_app(monkeypatch):
    monkeypatch.setattr(admission,'permitted',lambda:False)
    async def unreachable(*_):pytest.fail('Unowned request was admitted')
    gate=admission.ControllerAdmissionMiddleware(unreachable)
    for kind in ('http','websocket'):
        messages=[]
        async def send(value):messages.append(value)
        await gate({'type':kind},None,send)
        assert messages[0].get('status',messages[0].get('code'))==(503 if kind=='http' else 1013)
