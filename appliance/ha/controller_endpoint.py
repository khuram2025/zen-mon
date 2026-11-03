"""Optional same-subnet IPv4 service address for a fenced controller cell."""
import ipaddress
from pathlib import Path
import socket
import struct


def select(value, node, interfaces):
    address=ipaddress.ip_interface(value)
    node=ipaddress.ip_address(node)
    if (address.version!=4 or address.ip==node or address.ip.is_loopback
            or address.ip.is_multicast or address.ip.is_unspecified
            or address.ip in (address.network.network_address,address.network.broadcast_address)):
        raise ValueError('Service address must be a separate unicast IPv4 host')
    found=[]
    for interface in interfaces:
        for item in interface.get('addr_info',[]):
            if item.get('local')==str(node):
                local=ipaddress.ip_interface(str(node)+'/'+str(item['prefixlen']))
                if local.network==address.network and interface.get('ifname')!='lo':found.append(interface['ifname'])
    if len(found)!=1:raise ValueError('Service address requires one matching local subnet')
    return str(address),found[0]


def arp_packet(mac,address):
    hardware=bytes.fromhex(mac.replace(':',''))
    if len(hardware)!=6:raise ValueError('Invalid Ethernet address')
    ip=socket.inet_aton(str(ipaddress.IPv4Address(address)))
    return b'\xff'*6+hardware+struct.pack('!H',0x0806)+struct.pack('!HHBBH',1,0x0800,6,4,1)+hardware+ip+b'\x00'*6+ip


def announce(interface,address):
    mac=(Path('/sys/class/net')/interface/'address').read_text().strip()
    frame=arp_packet(mac,address.split('/')[0])
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0806)) as sock:
        sock.bind((interface,0))
        for _ in range(3):sock.send(frame)
