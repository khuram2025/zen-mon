#!/opt/zenplus/venv/bin/python
"""Zentryc operational CLI: authenticated local console, bounded commands."""
import getpass
import ipaddress
import json
import os
import readline
import shlex
import signal
import subprocess
import sys
import syslog
import time
from pathlib import Path
from auth import AuthError, ConsoleAuth, atomic_private
import network

SETUP_PASSWORD = 'Zentryc@Setup123'  # Public bootstrap only; never an operator secret.
STATE = Path('/var/lib/zentryc')
SERVICES = {'api':'zenplus-api', 'poller':'zenplus-poller', 'netflow':'zenplus-netflow-collector',
            'ncm':'zenplus-ncm-worker', 'web':'nginx', 'database':'postgresql@18-main',
            'redis':'redis-server', 'updater':'zenplus-updater', 'watchdog':'zenplus-storage-watchdog'}
READ_COMMANDS = ['show version','show system','show interfaces','show routes','show dns','show services',
                 'show license','show updates','show storage','show backups','show running-config',
                 'show candidate-config','show ssh','ping','enable','help','exit','logout']
ADMIN_COMMANDS = ['configure terminal','password console','license activate','backup create config',
                  'backup create full','backup restore','updates check','updates apply','service restart',
                  'system reboot','system shutdown','system initialization retry','setup',
                  'ssh key add','ssh keys clear','commit confirm','rollback','disable']
CONFIG_COMMANDS = ['interface','ip address dhcp','ip address','ip gateway','ip dns','commit','commit confirm',
                   'rollback','show candidate-config','show interfaces','exit','end','help']


def run(args, timeout=60, check=True, stdin=None):
    p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, LC_ALL='C'))
    if check and p.returncode:
        raise ValueError('Operation failed; inspect service health and system log')
    return p.stdout.strip()


def app(operation, **values):
    request = json.dumps(dict(operation=operation, **values))
    p = subprocess.run(['/usr/sbin/runuser','-u','zenplus','--','/opt/zenplus/venv/bin/python',
                        '/usr/local/libexec/zentryc-cli/app_ops.py'], input=request, text=True,
                       capture_output=True, timeout=1800, cwd='/opt/zenplus')
    try:
        result = json.loads(p.stdout)
    except (ValueError, TypeError):
        raise ValueError('Application unavailable; inspect show services') from None
    if not result.get('ok'):
        raise ValueError(result.get('error','Application operation failed'))
    return result['result']


def display(value):
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str))


class IdleTimeout(Exception):
    pass


def idle(_signal, _frame):
    raise IdleTimeout('Idle session expired; sign in again')


def read(prompt, secret=False):
    signal.alarm(300)
    try:
        return getpass.getpass(prompt) if secret else input(prompt)
    finally:
        signal.alarm(0)


def new_password(label):
    first = read(f'New {label} password: ', secret=True)
    second = read('Confirm new password: ', secret=True)
    if first != second:
        raise ValueError('Passwords do not match')
    return first


def web_gate(open_access):
    marker = STATE / 'console-setup-complete.json'
    previous = marker.read_bytes() if marker.exists() else None
    if open_access:
        atomic_private(marker, {'format':1, 'completed_at':time.time()})
    else:
        marker.unlink(missing_ok=True)
    settings = json.loads(Path('/etc/zenplus/tls/state.json').read_text())
    try:
        run(['/usr/local/sbin/zenplus-security-helper','apply',
             *[f'{k}={settings.get(k, default)}' for k,default in
               [('https','on'),('redirect','on'),('hsts','off'),('min_tls','1.2')]]])
        # Nginx's graceful reload cannot replace a bound loopback socket with a
        # wildcard socket on the same port. Restart once at the setup transition.
        run(['/usr/bin/systemctl','restart','nginx'])
    except Exception:
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_bytes(previous)
            marker.chmod(0o600)
        # Re-render the gate after restoring its marker, then recover nginx.
        run(['/usr/local/sbin/zenplus-security-helper','apply',
             *[f'{k}={settings.get(k, default)}' for k,default in
               [('https','on'),('redirect','on'),('hsts','off'),('min_tls','1.2')]]])
        run(['/usr/bin/systemctl','restart','nginx'])
        raise


def setup(auth, session):
    if session['must_change_password']:
        print('\nFirst-time setup: replace the public console setup password.')
        while True:
            try:
                session = auth.change_password(new_password('console'), SETUP_PASSWORD)
                atomic_private(STATE/'console-claimed.json', {'claimed_at':time.time()})
                print('Console password saved. Web and console passwords are managed separately.')
                break
            except (ValueError, AuthError) as exc:
                print('% '+str(exc))
    # Do not reset a web administrator that already completed setup.
    try:
        status = app('setup-status')
    except ValueError:
        print('Web initialization is unavailable. Dashboard access stays closed.')
        print('Use show services and system initialization retry, then setup to finish.')
        return session
    if status['must_change_password']:
        print('\nNow replace the public web administrator setup password.')
        print('Use a separate password of 12 through 128 characters; avoid common passwords and your username.')
        while True:
            try:
                password = new_password('web administrator')
                if auth.crypto.verify(password, auth._read(auth.path)['password_hash']):
                    raise ValueError('Use a different password for the web dashboard and console')
                app('setup-web-password', current_password=SETUP_PASSWORD, new_password=password)
                (STATE/'initial-login.json').unlink(missing_ok=True)
                print('Web administrator password saved.')
                break
            except ValueError as exc:
                print('% '+str(exc))
    if not (STATE/'console-setup-complete.json').exists():
        web_gate(True)
        print('Setup complete. HTTPS dashboard access is now enabled.')
    return session


def interfaces():
    return [x for x in json.loads(run(['/usr/sbin/ip','-j','address','show']))
            if x.get('link_type')=='ether' and not x['ifname'].startswith(('docker','veth','br-'))]


class CLI:
    def __init__(self, auth, session):
        self.auth, self.session = auth, session
        self.mode = 'read'
        self.candidate = {}
        readline.set_history_length(0)
        readline.clear_history()
        readline.set_completer(self.complete)
        readline.set_completer_delims('\n')
        readline.parse_and_bind('tab: complete')

    def commands(self):
        return CONFIG_COMMANDS if self.mode.startswith('config') else READ_COMMANDS + (ADMIN_COMMANDS if self.mode=='admin' else [])

    def complete(self, text, position):
        options = [c for c in self.commands() if c.startswith(text)]
        return options[position] if position < len(options) else None

    def confirm(self, message):
        if read(message+' Type CONFIRM: ').strip() != 'CONFIRM':
            raise ValueError('Operation canceled')

    def execute(self, line):
        self.auth.validate(self.session)
        if len(line)>512 or any(ord(c)<32 for c in line):
            raise ValueError('Invalid command')
        args = shlex.split(line)
        if not args:
            return True
        command = ' '.join(args)
        if command in ('help','?') or line.endswith(' ?'):
            print('\n'.join(self.commands()))
        elif command == 'logout':
            return False
        elif command == 'exit':
            if self.mode=='read':
                return False
            self.mode = 'config' if self.mode=='config-if' else 'admin' if self.mode=='config' else 'read'
        elif command == 'end' and self.mode.startswith('config'):
            self.mode = 'admin'
        elif command == 'disable' and self.mode=='admin':
            self.mode = 'read'
        elif command == 'enable':
            self.mode = 'admin'
            print('Privileged appliance commands enabled.')
        elif args[0]=='show' and len(args)==2:
            self.show(args[1])
        elif args[0]=='ping' and len(args)==2:
            address = str(ipaddress.ip_address(args[1]))
            display(run(['/usr/bin/ping','-c','4','-W','2','--',address], timeout=15, check=False))
        elif self.mode=='read':
            raise ValueError('Use enable for administrative commands; help lists supported commands')
        elif command == 'configure terminal' and self.mode=='admin':
            self.mode='config'
        elif self.mode.startswith('config') and args[0]=='interface' and len(args)==2:
            if args[1] not in [x['ifname'] for x in interfaces()]:
                raise ValueError('Select an interface listed by show interfaces')
            self.candidate={'interface':args[1], 'dhcp':True}
            self.mode='config-if'
        elif self.mode=='config-if' and command=='ip address dhcp':
            self.candidate.update(dhcp=True)
            self.candidate.pop('address',None)
            self.candidate.pop('gateway',None)
        elif self.mode=='config-if' and args[:2]==['ip','address'] and len(args)==3:
            self.candidate.update(address=str(ipaddress.IPv4Interface(args[2])), dhcp=False)
        elif self.mode=='config-if' and args[:2]==['ip','gateway'] and len(args)==3:
            self.candidate['gateway']=str(ipaddress.IPv4Address(args[2]))
        elif self.mode=='config-if' and args[:2]==['ip','dns'] and 3<=len(args)<=6:
            self.candidate['dns']=[str(ipaddress.ip_address(x)) for x in args[2:]]
        elif command=='commit' and self.mode.startswith('config'):
            display(network.commit(self.candidate))
        elif command=='commit confirm':
            display(network.confirm())
        elif command=='rollback':
            display(network.rollback())
        elif self.mode!='admin':
            raise ValueError('Unknown configuration command; use help')
        elif command=='password console':
            self.session=self.auth.change_password(new_password('console'),SETUP_PASSWORD)
            print('Console password changed; other console sessions were revoked.')
        elif command=='license activate':
            self.require_setup()
            key=read('License key: ',secret=True)
            display(app('license-activate',license_key=key))
        elif command in ('backup create config','backup create full'):
            self.require_setup()
            display(app('backup-create',full=command.endswith('full')))
        elif args[:2]==['backup','restore'] and len(args)==3:
            self.require_setup()
            from uuid import UUID
            identity=str(UUID(args[2]))
            self.confirm('Restore appliance database and configuration from this backup?')
            display(app('backup-restore',id=identity))
        elif args[:2]==['service','restart'] and len(args)==3 and args[2] in SERVICES:
            self.confirm('Restart '+args[2]+'?')
            run(['/usr/bin/systemctl','restart',SERVICES[args[2]]])
            print('Service restarted.')
        elif command=='updates check':
            self.require_setup()
            print('Checking signed update metadata; no update is applied by this command.')
            display(self.check_updates())
        elif command=='updates apply':
            self.require_setup()
            self.confirm('Apply eligible signed updates under the configured rollout and maintenance policy?')
            run(['/usr/bin/systemctl','--no-block','start','zenplus-updater.service'])
            print('Updater queued; use show updates to inspect its result.')
        elif command in ('system reboot','system shutdown'):
            self.confirm(command+'?')
            run(['/usr/bin/systemctl','--no-block','reboot' if args[1]=='reboot' else 'poweroff'])
        elif command=='ssh key add':
            self.require_setup()
            self.add_ssh_key()
        elif command=='ssh keys clear':
            self.confirm('Remove all remote CLI SSH keys?')
            Path('/home/zencli/.ssh/authorized_keys').unlink(missing_ok=True)
            print('Remote CLI keys removed.')
        elif command=='setup':
            self.session=setup(self.auth,self.session)
        elif command=='system initialization retry':
            self.confirm('Retry failed appliance initialization? Existing initialized appliances are preserved.')
            run(['/usr/bin/systemctl','reset-failed','zentryc-first-boot.service'])
            run(['/usr/bin/systemctl','restart','zentryc-first-boot.service'],timeout=1800)
            run(['/usr/bin/systemctl','restart','zenplus-wait-deps','zenplus-api','zenplus-poller','nginx'],timeout=180)
            print('Initialization retried. Run setup to complete administrator setup.')
        else:
            raise ValueError('Unknown command; use help. Linux shell commands are not accepted.')
        if args[0] not in ('show','help','?','ping'):
            syslog.syslog(syslog.LOG_AUTHPRIV|syslog.LOG_NOTICE,
                          'Zentryc console:admin command='+' '.join(args[:2]))
        return True

    def require_setup(self):
        if not (STATE/'console-setup-complete.json').exists():
            raise ValueError('Complete both password changes using setup first')

    def add_ssh_key(self):
        import base64
        import re
        key=read('Paste an OpenSSH public key: ').strip()
        fields=key.split()
        if len(key)>4096 or len(fields)<2 or fields[0] not in ('ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256'):
            raise ValueError('Use a valid Ed25519, RSA or ECDSA OpenSSH public key')
        try:
            blob=base64.b64decode(fields[1],validate=True)
            size=int.from_bytes(blob[:4],'big')
            if blob[4:4+size].decode()!=fields[0] or len(blob)<32:
                raise ValueError()
        except (ValueError,UnicodeError):
            raise ValueError('Public key is malformed') from None
        # Options and comments from the pasted line are never installed.
        directory=Path('/home/zencli/.ssh')
        directory.mkdir(mode=0o755,parents=True,exist_ok=True)
        directory.chmod(0o755)
        path=directory/'authorized_keys'
        previous=path.read_text() if path.exists() else ''
        if len(previous)>32768:
            raise ValueError('Remote key limit reached')
        entry='restrict,pty '+fields[0]+' '+fields[1]+'\n'
        if entry not in previous:
            path.write_text(previous+entry)
            path.chmod(0o644)  # Public keys; root-owned and never user-writable.
        print('Remote CLI key installed. Connect with ssh -t zencli@<management-ip>.')

    def check_updates(self):
        # Trusted shipped updater, no URL/version/path supplied by the operator.
        p=subprocess.run(['/opt/zenplus/venv/bin/python','-m','updater','--check'],
                         cwd='/opt/zenplus',text=True,capture_output=True,timeout=180,
                         env=dict(os.environ,PYTHONPATH='/opt/zenplus'))
        if p.returncode:
            raise ValueError('Update check failed; registration may be required. Use show updates.')
        return 'Signed update check finished. No update was applied.'

    def show(self, what):
        if what=='version':
            print('ZENTRYC | ZenPlus Appliance '+Path('/opt/zenplus/.version').read_text().splitlines()[0])
            print('Appliance CLI edition r2')
            display(json.loads(Path('/etc/zentryc/identity.json').read_text()))
        elif what=='system':
            display(run(['/usr/bin/uptime']))
            display(run(['/usr/bin/uname','-sr']))
        elif what=='interfaces':
            for nic in interfaces():
                print(nic['ifname']+' '+nic['operstate'])
                for addr in nic.get('addr_info',[]):
                    print('  '+addr['local']+'/'+str(addr['prefixlen']))
                    if addr['family']=='inet':
                        print('  Dashboard https://'+addr['local']+'/')
        elif what=='routes':
            display(run(['/usr/sbin/ip','route','show']))
        elif what=='dns':
            display(run(['/usr/bin/resolvectl','status']))
        elif what=='services':
            for name,unit in SERVICES.items():
                status=run(['/usr/bin/systemctl','is-active',unit],check=False)
                print(f'{name:12} {status}')
        elif what in ('license','updates','backups'):
            display(app(what))
        elif what=='storage':
            display(run(['/usr/bin/df','-h','/','/data']))
        elif what=='running-config':
            display(Path('/etc/netplan/50-zentryc.yaml').read_text())
        elif what=='candidate-config':
            display(self.candidate)
        elif what=='ssh':
            print('Remote SSH: key authentication only; no public default SSH password.')
            print('Enroll a public key using enable, then ssh key add. Connect as zencli.')
            display(run(['/usr/bin/systemctl','is-active','ssh'],check=False))
        else:
            raise ValueError('Unknown show command; use help')

    def loop(self):
        print('\nWelcome to the Zentryc appliance CLI. Type help or ? for commands.')
        self.show('interfaces')
        while True:
            suffix={'read':'>','admin':'#','config':'(config)#','config-if':'(config-if)#'}[self.mode]
            try:
                line=read('zentryc'+suffix+' ').strip()
                readline.clear_history()
                if not self.execute(line):
                    return
            except (ValueError,KeyError,subprocess.SubprocessError) as exc:
                print('% '+str(exc))


def main():
    if len(sys.argv)!=1 or os.geteuid()!=0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('The appliance console service must run as root')
    signal.signal(signal.SIGALRM,idle)
    auth=ConsoleAuth()
    auth.initialize(SETUP_PASSWORD)
    while True:
        try:
            print('\n  ZENTRYC | ZenPlus Appliance\n  Authorized console access only.\n')
            if auth._read(auth.path)['must_change_password']:
                print('First-time setup login: admin / '+SETUP_PASSWORD)
                print('Both console and web passwords must be changed before dashboard access.')
            session=auth.authenticate(read('Username: ').strip(),read('Password: ',secret=True))
            session=setup(auth,session)
            CLI(auth,session).loop()
        except (AuthError,IdleTimeout,EOFError,KeyboardInterrupt) as exc:
            print('\n% '+str(exc))
            time.sleep(1)
        except (ValueError,subprocess.SubprocessError,OSError) as exc:
            print('% '+str(exc))
            time.sleep(3)
        finally:
            signal.alarm(0)
            readline.clear_history()


if __name__=='__main__':
    main()
