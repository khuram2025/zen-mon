#!/usr/bin/python3
"""Install a root-owned CLI into a running QA VM or an offline mounted master.

Never used to seal a customer disk. The --factory flag only configures a fresh,
uninitialized image; it refuses existing identity or database environment.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def install(root, source, factory=False):
    def target(name):
        return root/name.lstrip('/')
    def write(name, text, mode=0o644):
        p=target(name)
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(text)
        p.chmod(mode)
    if factory and (target('/opt/zenplus/.env').exists() or target('/etc/zentryc/identity.json').exists()):
        raise SystemExit('Factory installation requires an uninitialized sealed image')
    library=target('/usr/local/libexec/zentryc-cli')
    library.mkdir(parents=True,exist_ok=True)
    library.chmod(0o755)
    for name in ('auth.py','cli.py','network.py','app_ops.py'):
        shutil.copyfile(source/name,library/name)
        (library/name).chmod(0o644)
    # Python isolated mode omits script-directory imports; use the fixed trusted
    # module directory explicitly through the launcher, without environment input.
    write('/usr/local/libexec/zentryc-console',
          '#!/opt/zenplus/venv/bin/python -I\nimport sys\nsys.path.insert(0,"/usr/local/libexec/zentryc-cli")\nfrom cli import main\nmain()\n',0o755)
    helper=source/'zenplus-security-helper'
    if not helper.exists():
        helper=source.parents[2]/'scripts/zenplus-security-helper'
    if helper.exists():
        shutil.copyfile(helper,target('/usr/local/sbin/zenplus-security-helper'))
        target('/usr/local/sbin/zenplus-security-helper').chmod(0o755)
    write('/etc/zentryc/cli-required','r2\n')
    write('/etc/issue','\nZENTRYC | ZenPlus Appliance\nUse the Zentryc console for setup and appliance commands.\n')
    write('/etc/issue.net','ZENTRYC | ZenPlus Appliance\nAuthorized access only. https://zentryc.com/\n')
    write('/etc/motd','ZENTRYC | ZenPlus Appliance\nRemote CLI: ssh -t zencli@<management-ip> (enrolled SSH key required).\nConsole and dashboard passwords are managed independently; you may choose the same or different passwords.\n')
    for name in ('00-header','10-help-text'):
        p=target('/etc/update-motd.d/'+name)
        if p.exists():
            p.chmod(p.stat().st_mode & ~0o111)
    target('/run/motd.dynamic').unlink(missing_ok=True)
    write('/etc/systemd/system/zentryc-network-recovery.service',
          '[Unit]\nDescription=Restore unconfirmed Zentryc management network\nDefaultDependencies=no\nAfter=local-fs.target\nBefore=network-pre.target systemd-networkd.service\nWants=network-pre.target\nConditionPathExists=/var/lib/zentryc/network-transaction.json\n[Service]\nType=oneshot\nExecStart=/opt/zenplus/venv/bin/python /usr/local/libexec/zentryc-cli/network.py --boot-recovery\n[Install]\nWantedBy=multi-user.target\n')
    link=target('/etc/systemd/system/multi-user.target.wants/zentryc-network-recovery.service')
    if not link.exists():
        link.symlink_to('/etc/systemd/system/zentryc-network-recovery.service')
    # SSH transport accepts enrolled keys only, then requires the console login.
    # The account owns no writable scripts, key files, or sudo-controlled paths.
    write('/etc/ssh/sshd_config.d/15-zentryc-cli.conf',
          'Match User zencli\n    AuthenticationMethods publickey\n    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n    DisableForwarding yes\n    PermitTTY yes\n    ForceCommand /usr/bin/sudo -n /usr/local/libexec/zentryc-console\nMatch all\n')
    write('/etc/sudoers.d/zentryc-cli','zencli ALL=(root) NOPASSWD: /usr/local/libexec/zentryc-console ""\n',0o440)
    if root==Path('/'):
        if subprocess.run(['id','zencli'],capture_output=True).returncode:
            subprocess.run(['useradd','--system','--home-dir','/home/zencli','--shell','/bin/sh','zencli'],check=True)
        # A non-password-authenticating account needs a valid shadow field for
        # sshd account checks; '*' disables password authentication and is not '!'.
        subprocess.run(['usermod','--password','*','zencli'],check=True)
        target('/home/zencli').mkdir(exist_ok=True,mode=0o755)
        subprocess.run(['visudo','-cf','/etc/sudoers.d/zentryc-cli'],check=True)
        subprocess.run(['sshd','-t'],check=True)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','reload','ssh'],check=True)
    else:
        # Offline chroot performs only account creation, not running services.
        if not any(line.startswith('zencli:') for line in target('/etc/passwd').read_text().splitlines()):
            subprocess.run(['chroot',str(root),'useradd','--system','--home-dir','/home/zencli','--shell','/bin/sh','zencli'],check=True)
        subprocess.run(['chroot',str(root),'usermod','--password','*','zencli'],check=True)
        target('/home/zencli').mkdir(exist_ok=True,mode=0o755)
    if factory:
        first=target('/usr/local/libexec/zentryc-first-boot')
        first_source=source/'first-boot.py'
        if not first_source.exists():
            first_source=source.parent/'first-boot.py'
        shutil.copyfile(first_source,first)
        first.chmod(0o755)
        # Gate every existing generated listener; helper regeneration retains it.
        for name in ('/etc/nginx/conf.d/zenplus.conf','/etc/nginx/sites-available/zenplus'):
            p=target(name)
            if p.exists():
                text=p.read_text()
                text=re.sub(r'listen (80|443)(\s)',r'listen 127.0.0.1:\1\2',text)
                text=text.replace('listen [::]:','listen [::1]:')
                p.write_text(text)
    print('Zentryc CLI installed'+(' in sealed factory image' if factory else ''))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default='/')
    parser.add_argument('--factory',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise SystemExit('Root required')
    install(Path(args.root),Path(__file__).resolve().parent,args.factory)
