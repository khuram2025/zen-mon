import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('clock', Path(__file__).with_name('clock.py'))
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


class ClockTests(unittest.TestCase):
    def test_utc_is_independent_of_guest_timezone(self):
        host = subprocess.CompletedProcess([], 0, '11 Oct 2026 10:47:06\n')
        with patch.dict(clock.os.environ, {'TZ': 'Asia/Riyadh', 'LC_ALL': 'ar_SA.UTF-8'}):
            with patch.object(clock.subprocess, 'run', side_effect=[host, host]) as run:
                clock.initialize_clock()
        self.assertEqual(run.call_args_list[0].kwargs['env']['TZ'], 'UTC')
        self.assertEqual(run.call_args_list[0].kwargs['env']['LC_ALL'], 'C')
        self.assertEqual(run.call_args_list[1].args[0][-1], '@1791715626')

    def test_unavailable_or_invalid_host_time_never_sets_clock(self):
        for result in [subprocess.CalledProcessError(1, 'hosttime'),
                       subprocess.CompletedProcess([], 0, 'invalid'),
                       subprocess.CompletedProcess([], 0, '01 Jan 1970 00:00:00')]:
            with self.subTest(result=result):
                with patch.object(clock.subprocess, 'run', side_effect=([result] * 10)) as run:
                    with patch.object(clock.time, 'sleep'):
                        with self.assertRaises(RuntimeError):
                            clock.initialize_clock()
                self.assertEqual(run.call_count, 10)
                self.assertTrue(all(c.args[0][0].endswith('vmware-toolbox-cmd') for c in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
