"""Bounded, local analytics checks for controller admission.

Query strings and credentials never enter process arguments or diagnostics.
Checking queue/log progress is required again after predecessor fencing.
"""
import base64
import json
import re
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

CLIENT = Path('/etc/zenplus-ha/analytics/client.xml')
QUERY = """SELECT table,zookeeper_path,is_readonly,is_session_expired,active_replicas,
    queue_size,log_pointer,log_max_index,lost_part_count
    FROM system.replicas WHERE database='zenplus' FORMAT JSON"""


class AnalyticsUnavailable(RuntimeError):
    pass


def validate(rows, *, caught_up):
    if not rows:
        raise AnalyticsUnavailable('No replicated analytics tables')
    for row in rows:
        if (row['is_readonly'] or row['is_session_expired']
                or int(row['active_replicas']) < 2 or int(row['lost_part_count']) != 0):
            raise AnalyticsUnavailable('Analytics replica or quorum unavailable')
        empty = (int(row['log_pointer']) == 0 and int(row['log_max_index']) == 0
                 and row.get('verified_empty_log') is True)
        if caught_up and (int(row['queue_size']) != 0 or
                          (not empty and int(row['log_pointer']) <= int(row['log_max_index']))):
            raise AnalyticsUnavailable('Analytics replica has not caught up')


def check(*, caught_up):
    st = CLIENT.stat()
    if CLIENT.is_symlink() or not CLIENT.is_file() or st.st_uid != 0 or st.st_mode & 0o027:
        raise AnalyticsUnavailable('Analytics client configuration is not private')
    config = ET.fromstring(CLIENT.read_bytes())
    if config.findtext('host') != '127.0.0.1':
        raise AnalyticsUnavailable('Analytics health must use loopback')
    credential = config.findtext('user')+':'+config.findtext('password')
    headers = {'Authorization': 'Basic '+base64.b64encode(credential.encode()).decode(),
               'Content-Type': 'text/plain'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # This authenticated interface is loopback-only. External transports use TLS.
    def query(statement, *, result=True):
        request = urllib.request.Request('http://127.0.0.1:8123/?receive_timeout=1&max_execution_time=1',data=statement.encode(),headers=headers)
        with opener.open(request,timeout=2) as response:
            body = response.read(256*1024+1)
        if len(body)>256*1024:
            raise AnalyticsUnavailable('Unexpected analytics health response size')
        return json.loads(body)['data'] if result else None
    if caught_up:
        query('SYSTEM SYNC DATABASE REPLICA zenplus',result=False)
    rows = query(QUERY)
    if caught_up:
        empty_candidates=[row for row in rows if int(row['log_pointer'])==0 and int(row['log_max_index'])==0]
        if empty_candidates:
            # Index zero also names the first real log entry. Verify absence in
            # Keeper rather than mistaking an unconsumed first entry for empty.
            paths=[]
            for row in empty_candidates:
                path=row['zookeeper_path']
                if not re.fullmatch(r'/zenplus-ha/[a-z0-9-]+/tables/1/[0-9a-f-]{36}',path):
                    raise AnalyticsUnavailable('Unexpected analytics replica path')
                paths.append("'"+path+"/log'")
            log=query("SELECT count() AS n FROM system.zookeeper WHERE path IN ("+','.join(paths)+") FORMAT JSON")
            if int(log[0]['n']):
                raise AnalyticsUnavailable('Initial analytics log entry not consumed')
            for row in empty_candidates:row['verified_empty_log']=True
    validate(rows,caught_up=caught_up)
    if caught_up:
        tables=query("SELECT count() AS n FROM system.tables WHERE database='zenplus' AND endsWith(engine,'MergeTree') FORMAT JSON")
        if int(tables[0]['n']) != len(rows):
            raise AnalyticsUnavailable('Unreplicated analytics table found')
