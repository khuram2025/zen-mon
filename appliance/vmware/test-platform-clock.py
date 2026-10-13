import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('clock',Path(__file__).with_name('clock.py'))
clock=importlib.util.module_from_spec(spec);spec.loader.exec_module(clock)

class PlatformClockTests(unittest.TestCase):
    def test_portable_profiles_use_utc_rtc_and_never_vmware_hosttime(self):
        for platform,detected in [('kvm','kvm'),('kvm','qemu')]:
            with self.subTest(platform=platform,detected=detected):
                replies=[subprocess.CompletedProcess([],0,detected),
                         subprocess.CompletedProcess([],0,''),
                         subprocess.CompletedProcess([],0,'1791715626')]
                with patch.object(clock.subprocess,'run',side_effect=replies) as run:
                    clock.initialize_clock(platform)
                self.assertEqual(run.call_args_list[1].args[0],
                    ['/usr/sbin/hwclock','--hctosys','--utc','--noadjfile'])
                self.assertEqual(run.call_args_list[1].kwargs['env']['TZ'],'UTC')
                self.assertFalse(any('vmware-toolbox-cmd' in c.args[0][0] for c in run.call_args_list))

    def test_hyperv_reads_host_utc_instead_of_local_rtc(self):
        replies=[subprocess.CompletedProcess([],0,'microsoft'),subprocess.CompletedProcess([],0,''),subprocess.CompletedProcess([],0,'')]
        with patch.object(clock.subprocess,'run',side_effect=replies) as run,patch.object(clock,'hyperv_host_epoch',return_value=1791715626):
            clock.initialize_clock('hyperv')
        self.assertEqual(run.call_args_list[-1].args[0],['/usr/bin/date','--utc','--set','@1791715626'])
        self.assertFalse(any('hwclock' in c.args[0][0] for c in run.call_args_list))

    def test_missing_hyperv_host_clock_fails_without_rtc_fallback(self):
        with patch.object(clock.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'microsoft')) as run:
            with patch.object(clock,'hyperv_host_epoch',side_effect=RuntimeError('No host UTC')),patch.object(clock.time,'sleep'):
                with self.assertRaises(RuntimeError):clock.initialize_clock('hyperv')
        self.assertEqual(run.call_count,2)

    def test_hypervisor_profile_mismatch_fails_before_setting_clock(self):
        with patch.object(clock.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'vmware')) as run:
            with self.assertRaises(RuntimeError):clock.initialize_clock('hyperv')
        self.assertEqual(run.call_count,1)

    def test_invalid_rtc_never_allows_first_boot_to_continue(self):
        replies=[subprocess.CompletedProcess([],0,'kvm')]
        for _ in range(10):replies.extend([subprocess.CompletedProcess([],0,''),subprocess.CompletedProcess([],0,'0')])
        with patch.object(clock.subprocess,'run',side_effect=replies),patch.object(clock.time,'sleep'):
            with self.assertRaises(RuntimeError):clock.initialize_clock('kvm')

    def test_unknown_profile_never_invokes_a_clock_command(self):
        with patch.object(clock.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):clock.initialize_clock('other')
        run.assert_not_called()

if __name__=='__main__':unittest.main()
