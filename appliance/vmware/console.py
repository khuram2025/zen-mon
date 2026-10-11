#!/opt/zenplus/venv/bin/python
"""Physical-console setup and recovery, independent of the API."""
import subprocess,os,json,ipaddress,getpass,time,secrets
from pathlib import Path
from passlib.context import CryptContext
def run(args,**kw): return subprocess.run(args,text=True,**kw)
def ips():
    data=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','scope','global']))
    return [(i['ifname'],a['local']) for i in data for a in i.get('addr_info',[]) if a['family']=='inet']
def sql(text):
    p=run(['runuser','-u','postgres','--','psql','-X','-q','-v','ON_ERROR_STOP=1','-d','zenplus'],input=text,capture_output=True)
    if p.returncode: raise RuntimeError('Database unavailable; inspect service status')
    return p.stdout
def password():
    a=getpass.getpass('New administrator password (12+ characters): ')
    b=getpass.getpass('Confirm password: ')
    if a!=b or not 12<=len(a)<=72: raise ValueError('Passwords must match and contain 12 through 72 characters')
    h=CryptContext(schemes=['bcrypt'],deprecated='auto').hash(a)
    sql(f"UPDATE users SET password_hash='{h}',must_change_password=FALSE,password_changed_at=now(),token_version=COALESCE(token_version,0)+1 WHERE username='admin'; UPDATE auth_sessions SET revoked_at=now() WHERE user_id=(SELECT id FROM users WHERE username='admin');")
    Path('/var/lib/zentryc/initial-login.json').unlink(missing_ok=True)
    print('Administrator password changed; sign in through HTTPS.')
def network():
    links=json.loads(subprocess.check_output(['ip','-j','link','show']))
    links=[x['ifname'] for x in links if x.get('link_type')=='ether' and not x['ifname'].startswith(('docker','veth','br-'))]
    print('Interfaces: '+', '.join(links));nic=input('Management interface: ').strip()
    if nic not in links: raise ValueError('Select a listed physical interface')
    mode=input('Use DHCP? [Y/n]: ').strip().lower()
    config={'network':{'version':2,'ethernets':{nic:{'dhcp4':True,'optional':True}}}}
    if mode=='n':
        addr=str(ipaddress.IPv4Interface(input('IPv4 address/prefix: ').strip()))
        gw=str(ipaddress.IPv4Address(input('Gateway: ').strip()))
        dns=[str(ipaddress.ip_address(v)) for v in input('DNS servers, separated by commas: ').split(',')]
        config['network']['ethernets'][nic]={'addresses':[addr],'routes':[{'to':'default','via':gw}],'nameservers':{'addresses':dns},'optional':True}
    import yaml
    path=Path('/etc/netplan/90-zentryc-management.yaml')
    previous=path.read_bytes() if path.exists() else None
    path.write_text(yaml.safe_dump(config));path.chmod(0o600)
    # netplan try owns the timed rollback and console confirmation.
    p=run(['netplan','try','--timeout','120'])
    if p.returncode:
        if previous is None: path.unlink(missing_ok=True)
        else: path.write_bytes(previous)
        run(['netplan','apply'])
        print('Previous configuration restored.')
def show():
    os.system('clear')
    print('  ZENTRYC  |  ZenPlus Appliance 1.24.13\n')
    identity=Path('/etc/zentryc/identity.json')
    if identity.exists(): print('Serial: '+json.loads(identity.read_text())['serial'])
    for nic,addr in ips(): print(f'Management: https://{addr}/  ({nic})')
    claim=Path('/var/lib/zentryc/initial-login.json')
    if claim.exists():
        try:
            changed='f' in sql("SELECT must_change_password FROM users WHERE username='admin';")
            if changed: claim.unlink()
            else:
                c=json.loads(claim.read_text());print('\nFirst sign-in: '+c['username']+' / '+c['password'])
                print('You must change this unique password at first sign-in.')
        except RuntimeError: print('Initialization is running or needs recovery.')
    print('\nTrial: 30 days from first initialization.\n')
while True:
    try:
        show()
        print('1  Service status\n2  Configure management network (120-second rollback)\n3  Set administrator password\n4  Retry failed initialization\n5  Restart application\n6  Reboot\n7  Shut down\n0  Refresh')
        action=input('\nSelect: ').strip()
        if action=='1': run(['systemctl','--no-pager','status','zentryc-first-boot','zenplus-api','zenplus-poller','zenplus-netflow-collector','nginx'])
        elif action=='2': network()
        elif action=='3': password()
        elif action=='4': run(['systemctl','reset-failed']);run(['systemctl','restart','zentryc-first-boot']);run(['systemctl','restart','zenplus-wait-deps','zenplus-api','zenplus-poller','nginx'])
        elif action=='5': run(['systemctl','restart','zenplus-api','zenplus-poller','zenplus-netflow-collector','nginx'])
        elif action in ('6','7'):
            if input('Type CONFIRM to continue: ').strip()=='CONFIRM': run(['systemctl','reboot' if action=='6' else 'poweroff'])
        if action!='0': input('\nPress Enter to return...')
    except (ValueError,RuntimeError) as e: print(str(e));time.sleep(3)
    except (EOFError,KeyboardInterrupt): time.sleep(1)
