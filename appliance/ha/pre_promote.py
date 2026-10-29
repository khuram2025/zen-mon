#!/usr/bin/env python3
"""Patroni pre-promotion gate; runs as postgres without privilege escalation."""
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
from controller_units import MANAGED


def run(config):
    path=Path(config);st=path.stat()
    if not path.is_file() or path.is_symlink() or st.st_uid!=0 or st.st_mode&0o022:
        raise ValueError('Promotion configuration must be root controlled')
    cfg=json.loads(path.read_text())
    if set(cfg)!={'cell_id','node_id','system_identifier','database','fence_program'}:
        raise ValueError('Unexpected promotion configuration')
    for key in ('cell_id','node_id','database'):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',cfg[key]):raise ValueError('Invalid promotion identity')
    program=Path(cfg['fence_program']);st=program.stat()
    if not program.is_absolute() or not program.is_file() or program.is_symlink() or st.st_uid!=0 or st.st_mode&0o022 or not st.st_mode&0o111:
        raise ValueError('Fence executable must be root controlled')
    # No controller consumer may be executing on a replica at promotion time.
    listed=subprocess.run(['systemctl','list-unit-files','--no-legend','--no-pager','zenplus*'],capture_output=True,text=True,timeout=5,check=True)
    installed={line.split()[0] for line in listed.stdout.splitlines() if line.strip()}
    present=[unit for unit in MANAGED if unit in installed]
    if not {'zenplus-api.service','zenplus-poller.service'} <= installed:raise RuntimeError('Incomplete controller inventory')
    states=subprocess.run(['systemctl','show',*present,'-p','ActiveState','--value'],capture_output=True,text=True,timeout=5,check=True).stdout.split()
    if len(states)!=len(present) or any(s not in ('inactive','failed') for s in states):raise RuntimeError('Local consumers have not stopped')
    for unit in present:
        output=subprocess.run(['systemctl','show',unit,'-p','MainPID','-p','ControlGroup'],capture_output=True,text=True,timeout=5,check=True).stdout
        values=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
        if values.get('MainPID')!='0':raise RuntimeError('Local consumer process remains')
        group=values.get('ControlGroup')
        if group:
            for item in (Path('/sys/fs/cgroup')/group.lstrip('/')).rglob('cgroup.procs'):
                if item.read_text().strip():raise RuntimeError('Local consumer child remains')
    query="SELECT system_identifier::text FROM pg_control_system(); SELECT node_id,boot_id FROM controller_authority WHERE cell_id='"+cfg['cell_id']+"';"
    import os
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    r=subprocess.run(['/usr/bin/psql','-XAt','-v','ON_ERROR_STOP=1','-h','/var/run/postgresql','-p','5432','-U','postgres','-d',cfg['database']],
        input=query,capture_output=True,text=True,timeout=5,check=True,env=env)
    rows=r.stdout.strip().splitlines()
    if not rows or rows[0]!=cfg['system_identifier']:raise ValueError('Wrong database incarnation')
    if len(rows)==1:return
    node,boot=rows[1].split('|')
    if not node or node==cfg['node_id']:return
    request=str(uuid4())
    result=subprocess.run([str(program)],input=json.dumps({'cell_id':cfg['cell_id'],'node_id':node,'boot_id':boot,
        'request_id':request,'action':'power_off'}),capture_output=True,text=True,timeout=45,check=True)
    receipt=json.loads(result.stdout)
    if (receipt.get('request_id')!=request or receipt.get('node_id')!=node or receipt.get('cell_id')!=cfg['cell_id']
            or receipt.get('power_state')!='off'):
        raise ValueError('Predecessor power-off was not confirmed')


if __name__=='__main__':
    try:run('/etc/zenplus-ha/promotion.json')
    except Exception as error:
        print('Promotion refused: '+type(error).__name__,file=sys.stderr)
        sys.exit(1)
