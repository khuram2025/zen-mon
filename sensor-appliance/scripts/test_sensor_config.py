import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

loader = importlib.machinery.SourceFileLoader('sensor_config', str(Path(__file__).with_name('sensor-config')))
spec = importlib.util.spec_from_loader(loader.name, loader)
config = importlib.util.module_from_spec(spec)
loader.exec_module(config)


class NetworkConfigTests(unittest.TestCase):
    def test_dhcp_and_static(self):
        self.assertTrue(config.network_document('ens33')['network']['ethernets']['ens33']['dhcp4'])
        value = config.network_document('ens33', '192.0.2.2/24', '192.0.2.1', '192.0.2.1')['network']['ethernets']['ens33']
        self.assertFalse(value['dhcp4'])
        self.assertEqual(value['routes'][0]['via'], '192.0.2.1')

    def test_invalid_address_family_and_interface(self):
        for args in [('bad\ninterface',), ('ens33', '192.0.2.2/24', '2001:db8::1', '1.1.1.1')]:
            with self.assertRaises(ValueError): config.network_document(*args)


if __name__ == '__main__': unittest.main()
