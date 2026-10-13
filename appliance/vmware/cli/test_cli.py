#!/usr/bin/env python3
"""Offline contracts. Temporary credentials, mocked host operations, no live DB."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from auth import ConsoleAuth, AuthError, atomic_private
import cli
import network

NEW='Correct Horse Battery!27'
OTHER='Independent Dashboard!73'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=[1000]
        self.auth=ConsoleAuth(self.temp.name,owner_uid=os.getuid(),clock=lambda:self.now[0])
        self.auth.initialize(cli.SETUP_PASSWORD)
    def tearDown(self):
        self.temp.cleanup()
    def test_default_requires_change(self):
        session=self.auth.authenticate('admin',cli.SETUP_PASSWORD)
        self.assertTrue(session['must_change_password'])
        with self.assertRaises(AuthError):self.auth.validate(session)
    def test_changed_password_revokes_default_and_sessions(self):
        old=self.auth.authenticate('admin',cli.SETUP_PASSWORD)
        current=self.auth.change_password(NEW,cli.SETUP_PASSWORD)
        self.auth.validate(current)
        with self.assertRaises(AuthError):self.auth.validate(old)
        with self.assertRaises(AuthError):self.auth.authenticate('admin',cli.SETUP_PASSWORD)
        self.auth.authenticate('admin',NEW)
    def test_salted_private_storage(self):
        raw=self.auth.path.read_text()
        self.assertNotIn(cli.SETUP_PASSWORD,raw)
        self.assertEqual(self.auth.path.stat().st_mode&0o777,0o600)
        with tempfile.TemporaryDirectory() as other:
            second=ConsoleAuth(other,owner_uid=os.getuid())
            state=second.initialize(cli.SETUP_PASSWORD)
            self.assertNotEqual(state['password_hash'],json.loads(raw)['password_hash'])
    def test_reinitialize_preserves_claimed_hash(self):
        self.auth.change_password(NEW,cli.SETUP_PASSWORD)
        before=self.auth.path.read_bytes()
        self.auth.initialize(cli.SETUP_PASSWORD)
        self.assertEqual(before,self.auth.path.read_bytes())
    def test_missing_claimed_storage_fails_closed(self):
        atomic_private(Path(self.temp.name)/'console-claimed.json',{})
        self.auth.path.unlink()
        with self.assertRaises(AuthError):self.auth.initialize(cli.SETUP_PASSWORD)
    def test_login_lockout(self):
        for _ in range(5):
            with self.assertRaises(AuthError):self.auth.authenticate('admin','wrong')
        with self.assertRaises(AuthError):self.auth.authenticate('admin',cli.SETUP_PASSWORD)
        self.now[0]+=61
        self.auth.authenticate('admin',cli.SETUP_PASSWORD)
    def test_unknown_username(self):
        with self.assertRaises(AuthError):self.auth.authenticate('root',cli.SETUP_PASSWORD)
    def test_permission_failure(self):
        self.auth.path.chmod(0o644)
        with self.assertRaises(AuthError):self.auth.authenticate('admin',cli.SETUP_PASSWORD)
    def test_symlink_failure(self):
        original=self.auth.path.with_name('original')
        self.auth.path.rename(original)
        self.auth.path.symlink_to(original)
        with self.assertRaises(AuthError):self.auth.authenticate('admin',cli.SETUP_PASSWORD)
    def test_weak_reused_and_default_passwords(self):
        for password in ('short','lowercasepassword',cli.SETUP_PASSWORD,'A'*129):
            with self.assertRaises(AuthError):self.auth.change_password(password,cli.SETUP_PASSWORD)
        self.auth.change_password(NEW,cli.SETUP_PASSWORD)
        with self.assertRaises(AuthError):self.auth.change_password(NEW,cli.SETUP_PASSWORD)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.auth=unittest.mock.Mock()
        self.cli=cli.CLI(self.auth,{'username':'admin','revision':2})
    def test_mode_transitions(self):
        self.cli.execute('enable');self.assertEqual(self.cli.mode,'admin')
        self.cli.execute('configure terminal');self.assertEqual(self.cli.mode,'config')
        with patch.object(cli,'interfaces',return_value=[{'ifname':'ens33'}]):self.cli.execute('interface ens33')
        self.assertEqual(self.cli.mode,'config-if')
        self.cli.execute('ip address 192.0.2.10/24');self.cli.execute('ip gateway 192.0.2.1')
        self.cli.execute('ip dns 1.1.1.1 8.8.8.8')
        self.assertEqual(self.cli.candidate['dns'],['1.1.1.1','8.8.8.8'])
        self.cli.execute('end');self.assertEqual(self.cli.mode,'admin')
        self.cli.execute('disable');self.assertEqual(self.cli.mode,'read')
    def test_privilege_required(self):
        with self.assertRaises(ValueError):self.cli.execute('system reboot')
    def test_shell_and_injection_rejected(self):
        self.cli.execute('enable')
        with patch.object(cli,'run') as run:
            for command in ('bash','shell','sudo su','show system; reboot','ping 1.1.1.1;reboot','service restart api;id','service restart ../../bin/sh','show version > /tmp/a','ping $(id)','!id'):
                with self.assertRaises(ValueError,msg=command):self.cli.execute(command)
            run.assert_not_called()
    def test_session_rechecked_per_command(self):
        self.auth.validate.side_effect=AuthError('revoked')
        with patch.object(cli,'run') as run:
            with self.assertRaises(AuthError):self.cli.execute('show system')
            run.assert_not_called()
    def test_ping_uses_fixed_arguments(self):
        with patch.object(cli,'run',return_value='ok') as run:
            self.cli.execute('ping 192.0.2.1')
            self.assertEqual(run.call_args.args[0],['/usr/bin/ping','-c','4','-W','2','--','192.0.2.1'])
    def test_service_cancel_runs_nothing(self):
        self.cli.execute('enable')
        with patch.object(cli,'read',return_value='NO'),patch.object(cli,'run') as run:
            with self.assertRaises(ValueError):self.cli.execute('service restart api')
            run.assert_not_called()
    def test_service_restart_is_bounded(self):
        self.cli.execute('enable')
        with patch.object(cli,'read',return_value='CONFIRM'),patch.object(cli,'run') as run:
            self.cli.execute('service restart api')
            run.assert_called_once_with(['/usr/bin/systemctl','restart','zenplus-api'])
    def test_no_command_history(self):
        self.assertEqual(cli.readline.get_current_history_length(),0)
    def test_key_options_not_installed(self):
        self.cli.execute('enable')
        with patch.object(cli,'read',return_value='command="id" ssh-ed25519 AAAA'):
            with self.assertRaises(ValueError):self.cli.add_ssh_key()
    def test_app_secrets_stdin_only(self):
        proc=unittest.mock.Mock(stdout=json.dumps({'ok':True,'result':{}}))
        with patch.object(cli.subprocess,'run',return_value=proc) as run:
            cli.app('setup-web-password',new_password=OTHER)
            self.assertNotIn(OTHER,' '.join(run.call_args.args[0]))
            self.assertIn(OTHER,run.call_args.kwargs['input'])


class SetupTests(unittest.TestCase):
    def test_two_changes_gate_opens_last(self):
        with tempfile.TemporaryDirectory() as folder:
            auth=ConsoleAuth(folder,owner_uid=os.getuid())
            auth.initialize(cli.SETUP_PASSWORD)
            session=auth.authenticate('admin',cli.SETUP_PASSWORD)
            events=[]
            def app(operation,**kw):
                events.append(operation)
                return {'must_change_password':True} if operation=='setup-status' else {}
            def gate(_):
                self.assertFalse(auth._read(auth.path)['must_change_password'])
                self.assertEqual(events,['setup-status','setup-web-password'])
            with patch.object(cli,'STATE',Path(folder)),patch.object(cli,'new_password',side_effect=[NEW,NEW,OTHER]),patch.object(cli,'app',side_effect=app),patch.object(cli,'web_gate',side_effect=gate):
                final=cli.setup(auth,session)
            self.assertFalse(final['must_change_password'])
    def test_existing_web_password_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            auth=unittest.mock.Mock()
            session={'must_change_password':False}
            with patch.object(cli,'STATE',Path(folder)),patch.object(cli,'app',return_value={'must_change_password':False}) as app,patch.object(cli,'web_gate'):
                cli.setup(auth,session)
            app.assert_called_once_with('setup-status')
            auth.change_password.assert_not_called()
    def test_database_failure_gate_stays_closed(self):
        auth=unittest.mock.Mock()
        with patch.object(cli,'app',side_effect=ValueError('offline')),patch.object(cli,'web_gate') as gate:
            cli.setup(auth,{'must_change_password':False})
            gate.assert_not_called()


class NetworkTests(unittest.TestCase):
    def test_dhcp(self):
        item=network.validate({'interface':'ens33','dhcp':True})['network']['ethernets']['management']
        self.assertTrue(item['dhcp4'])
    def test_valid_static(self):
        config=network.validate({'interface':'ens33','dhcp':False,'address':'192.0.2.10/24','gateway':'192.0.2.1','dns':['1.1.1.1']})
        self.assertEqual(config['network']['ethernets']['management']['addresses'],['192.0.2.10/24'])
    def test_bad_networks(self):
        for candidate in ({'interface':'x;id','dhcp':True},{'interface':'ens33','dhcp':True,'dns':['$(id)']},{'interface':'ens33','address':'192.0.2.10/24','gateway':'198.51.100.1'},{'interface':'ens33','address':'192.0.2.10/24','gateway':'192.0.2.10'}):
            with self.assertRaises((ValueError,KeyError)):network.validate(candidate)
    def test_boot_recovers_persistent_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'network.yaml';target.write_text('temporary')
            state=Path(folder)/'transaction.json';state.write_text('{}')
            with patch.object(network,'TARGET',target),patch.object(network,'STATE',state),patch.object(network,'run') as run:
                network.restore({'previous':'previous'},boot=True)
                self.assertEqual(target.read_text(),'previous')
                self.assertFalse(state.exists())
                run.assert_called_once_with(['/usr/sbin/netplan','generate'])
    def test_timeout_confirmation_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'network.yaml';target.write_text('temporary')
            state=Path(folder)/'transaction.json';state.write_text(json.dumps({'previous':'old','deadline':0}))
            with patch.object(network,'TARGET',target),patch.object(network,'STATE',state),patch.object(network,'lock',contextlib.nullcontext),patch.object(network,'run'):
                with self.assertRaises(ValueError):network.confirm()
                self.assertEqual(target.read_text(),'old')
    def test_confirmed_network_survives_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'network.yaml';target.write_text('new')
            state=Path(folder)/'transaction.json';state.write_text(json.dumps({'previous':'old','deadline':1e12}))
            with patch.object(network,'TARGET',target),patch.object(network,'STATE',state),patch.object(network,'lock',contextlib.nullcontext):
                network.confirm();network.guard('0'*32)
                self.assertEqual(target.read_text(),'new')


class WebGateTests(unittest.TestCase):
    def test_setup_restarts_nginx_after_listener_change(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Path(folder)/'tls-state.json'
            settings.write_text(json.dumps({'https':'on','redirect':'on','hsts':'off','min_tls':'1.2'}))
            real_path=Path
            def mapped(name):
                return settings if name=='/etc/zenplus/tls/state.json' else real_path(name)
            with patch.object(cli,'STATE',Path(folder)),patch.object(cli,'Path',side_effect=mapped),patch.object(cli,'run') as run:
                cli.web_gate(True)
                self.assertEqual(run.call_args_list[-1].args[0],['/usr/bin/systemctl','restart','nginx'])
                self.assertTrue((Path(folder)/'console-setup-complete.json').exists())

    def test_nginx_templates_closed_then_open(self):
        helper=Path(__file__).resolve().parents[3]/'scripts/zenplus-security-helper'
        if not helper.exists():
            helper=Path(__file__).parent/'zenplus-security-helper'
        text=helper.read_text().split('# ── Subcommands')[0]
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'etc/nginx/snippets').mkdir(parents=True)
            (base/'etc/zentryc').mkdir(parents=True);(base/'var/lib/zentryc').mkdir(parents=True)
            (base/'etc/zentryc/cli-required').write_text('r2')
            text=text.replace('/etc/',folder+'/etc/').replace('/var/lib/',folder+'/var/lib/')
            text+='\nnginx() { echo "nginx version: nginx/1.26.0" >&2; }\ngenerate_config "$1" on on off 1.2\n'
            for complete in (False,True):
                if complete:(base/'var/lib/zentryc/console-setup-complete.json').write_text('{}')
                subprocess.run(['bash','-c',text,'fixture',str(base/'site')],check=True,capture_output=True)
                config=(base/'site').read_text()
                if complete:
                    self.assertIn('listen 443 ssl',config)
                    self.assertIn('listen [::]:443',config)
                else:
                    self.assertIn('listen 127.0.0.1:443 ssl',config)
                    self.assertIn('listen [::1]:443',config)
                    self.assertNotIn('listen 443 ssl',config)


class SDKModelTests(unittest.TestCase):
    def test_complete_model_graph_without_database_connection(self):
        project=Path(__file__).resolve().parents[3]
        sys.path[:0]=[str(project/'server'),str(project)]
        with patch.dict(os.environ,{'DATABASE_URL':'postgresql+asyncpg://fixture@127.0.0.1:1/fixture'}):
            from app_ops import register_models
            register_models()
            from sqlalchemy.orm import configure_mappers
            configure_mappers()


if __name__=='__main__':
    unittest.main(verbosity=2)
