import importlib.util
from pathlib import Path
import socket
import pytest

spec=importlib.util.spec_from_file_location('controller_endpoint',Path(__file__).resolve().parents[2]/'appliance/ha/controller_endpoint.py')
endpoint=importlib.util.module_from_spec(spec);spec.loader.exec_module(endpoint)

def interfaces():return [{'ifname':'lab0','addr_info':[{'local':'192.0.2.11','prefixlen':24}]}]

def test_service_address_matches_one_commissioned_local_subnet():
    assert endpoint.select('192.0.2.10/24','192.0.2.11',interfaces())==('192.0.2.10/24','lab0')

@pytest.mark.parametrize('address',['192.0.2.11/24','192.0.2.0/24','192.0.2.255/24','192.0.2.10/25','198.51.100.10/24','127.0.0.1/8','224.0.0.1/24','::1/128'])
def test_invalid_service_address_is_refused(address):
    with pytest.raises(ValueError):endpoint.select(address,'192.0.2.11',interfaces())

def test_gratuitous_arp_announces_only_the_service_address():
    packet=endpoint.arp_packet('00:50:56:21:29:02','192.0.2.10')
    assert len(packet)==42 and packet[:6]==b'\xff'*6
    assert packet[28:32]==packet[38:42]==socket.inet_aton('192.0.2.10')
    assert packet[12:14]==b'\x08\x06' and packet[20:22]==b'\x00\x01'
