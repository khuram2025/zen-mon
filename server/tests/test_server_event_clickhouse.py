"""Opt-in integration against a dedicated disposable ClickHouse container.

ZENPLUS_TEST_EVENT_CH_PORT must be 18123. Never connects to appliance ports.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import server_event_service as events
from app.services.host_insert_batcher import rows_present


@pytest.fixture(scope="module")
def isolated_ch():
    if os.environ.get("ZENPLUS_TEST_EVENT_CH_PORT")!="18123":
        pytest.skip("dedicated disposable event ClickHouse fixture not requested")
    import clickhouse_connect
    client=clickhouse_connect.get_client(host="127.0.0.1",port=18123,username="default",password="")
    client.command("CREATE DATABASE IF NOT EXISTS zenplus")
    client.close()
    client=clickhouse_connect.get_client(host="127.0.0.1",port=18123,username="default",password="",database="zenplus")
    sql=(Path(__file__).parents[2]/"scripts/migrate-170-server-event-details-clickhouse.sql").read_text()
    client.command(sql.strip().rstrip(";"))
    yield client
    client.command("DROP DATABASE zenplus")
    client.close()


def test_replay_and_overlapping_policies_do_not_inflate_counts(isolated_ch):
    sid,aid=str(uuid4()),str(uuid4())
    stamp=datetime.now(timezone.utc)-timedelta(minutes=1)
    rows=[{"event_time":stamp,"record_id":i+1,"event_id":7031,"provider":"Service Control Manager",
           "log_name":"System","level":"error","message":"service failed","fields":{"ServiceName":f"service-{i%5}"}} for i in range(100)]
    events.insert_details(isolated_ch,aid,sid,rows)
    events.insert_details(isolated_ch,aid,sid,rows)  # Different insert blocks, same event identities.
    data=events.query_groups(isolated_ch,sid,stamp-timedelta(minutes=1),stamp+timedelta(minutes=1),level=None,channel=None,search="",limit=2,offset=0)
    assert data["total_events"]==100 and data["total_groups"]==5 and data["has_more"]
    assert all(g["count"]==20 for g in data["items"])
    group=data["items"][0]["group_key"]
    occurrences=events.query_occurrences(isolated_ch,sid,group,stamp-timedelta(minutes=1),stamp+timedelta(minutes=1),10,0)
    assert len(occurrences["items"])==10 and occurrences["has_more"]
    assert len({e["event_uid"] for e in occurrences["items"]})==10
    e=events.normalize(rows[0])
    assert rows_present(isolated_ch,"zenplus.host_event_details",["server_id","event_uid"],[[sid,e["event_uid"]]])
    assert not rows_present(isolated_ch,"zenplus.host_event_details",["server_id","event_uid"],[[sid,e["event_uid"]],[sid,"not-persisted"]])


def test_fleet_scale_group_query_uses_server_scope(isolated_ch):
    # 1,000 simulated servers, 100 events each, plus complete replay: 200k rows.
    # This validates SQL/correctness at scale, not an end-to-end capacity claim.
    started=time.monotonic()
    stamp=datetime.now(timezone.utc)-timedelta(minutes=1)
    servers=[str(uuid4()) for _ in range(1000)]
    agent=str(uuid4())
    buffer=[]
    def flush():
        if buffer:
            isolated_ch.insert("zenplus.host_event_details",buffer,column_names=events.EVENT_COLUMNS)
            buffer.clear()
    for sid in servers:
        for i in range(100):
            e=events.normalize({"event_time":stamp,"record_id":i+1,"event_id":7031,"provider":"SCM","log_name":"System","level":"error","message":"failed","fields":{"ServiceName":str(i%5)}})
            row=[sid,agent,stamp,stamp,e["event_uid"],e["group_key"],"System","SCM",7031,i+1,"error",e["component"],"failed","{}",0]
            buffer.extend([row,row])
        if len(buffer)>=10000:flush()
    flush()
    loaded=time.monotonic()
    data=events.query_groups(isolated_ch,servers[500],stamp-timedelta(minutes=1),stamp+timedelta(minutes=1),level=None,channel=None,search="",limit=50,offset=0)
    assert data["total_events"]==100 and data["total_groups"]==5
    print(f"event fleet fixture: servers=1000 raw_rows=200000 load_s={loaded-started:.3f} scoped_query_s={time.monotonic()-loaded:.3f}")
