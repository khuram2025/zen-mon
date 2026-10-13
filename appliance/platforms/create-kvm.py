#!/usr/bin/env python3
"""Import the verified KVM package as a new independent appliance."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

p=argparse.ArgumentParser()
p.add_argument('--name',required=True)
p.add_argument('--bridge',required=True,help='Existing trusted bridge with DHCP')
p.add_argument('--directory',type=Path,required=True,help='New VM disk directory, readable by libvirt')
p.add_argument('--start',action='store_true')
a=p.parse_args()
if os.geteuid()!=0:raise SystemExit('Run with sudo on the KVM host')
if not a.name or '/' in a.name:raise SystemExit('Use a simple VM name')
base=Path(__file__).resolve().parent
manifest=json.loads((base/'manifest.json').read_text())
for name in ('system.qcow2','data.qcow2'):
    with (base/name).open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=manifest['files'][name]['sha256']:
            raise SystemExit('Disk checksum mismatch: '+name)
if subprocess.run(['virsh','dominfo',a.name],capture_output=True).returncode==0:
    raise SystemExit('A VM with this name already exists')
directory=a.directory.resolve();directory.mkdir(parents=True,exist_ok=False)
for name in ('system.qcow2','data.qcow2'):shutil.copyfile(base/name,directory/name)
domain=ET.Element('domain',type='kvm')
ET.SubElement(domain,'name').text=a.name
ET.SubElement(domain,'memory',unit='MiB').text='8192'
ET.SubElement(domain,'currentMemory',unit='MiB').text='8192'
ET.SubElement(domain,'vcpu',placement='static').text='4'
osxml=ET.SubElement(domain,'os',firmware='efi')
ET.SubElement(osxml,'type',arch='x86_64',machine='q35').text='hvm'
firmware=ET.SubElement(osxml,'firmware')
for name in ('secure-boot','enrolled-keys'):ET.SubElement(firmware,'feature',enabled='yes',name=name)
ET.SubElement(osxml,'boot',dev='hd')
features=ET.SubElement(domain,'features')
for name in ('acpi','apic'):ET.SubElement(features,name)
ET.SubElement(domain,'cpu',mode='host-model',check='partial')
clock=ET.SubElement(domain,'clock',offset='utc')
ET.SubElement(clock,'timer',name='rtc',tickpolicy='catchup')
ET.SubElement(clock,'timer',name='pit',tickpolicy='delay')
ET.SubElement(clock,'timer',name='hpet',present='no')
ET.SubElement(domain,'on_poweroff').text='destroy'
ET.SubElement(domain,'on_reboot').text='restart'
ET.SubElement(domain,'on_crash').text='destroy'
devices=ET.SubElement(domain,'devices')
for name,target in [('system.qcow2','vda'),('data.qcow2','vdb')]:
    disk=ET.SubElement(devices,'disk',type='file',device='disk')
    ET.SubElement(disk,'driver',name='qemu',type='qcow2',cache='none',discard='unmap')
    ET.SubElement(disk,'source',file=str(directory/name))
    ET.SubElement(disk,'target',dev=target,bus='virtio')
network=ET.SubElement(devices,'interface',type='bridge')
ET.SubElement(network,'source',bridge=a.bridge)
ET.SubElement(network,'model',type='virtio')
ET.SubElement(devices,'graphics',type='spice',autoport='yes',listen='127.0.0.1')
video=ET.SubElement(devices,'video');ET.SubElement(video,'model',type='virtio')
ET.SubElement(devices,'input',type='tablet',bus='usb')
xml=directory/'domain.xml';ET.indent(domain);ET.ElementTree(domain).write(xml,encoding='unicode')
subprocess.run(['virsh','define',str(xml)],check=True)
if a.start:subprocess.run(['virsh','start',a.name],check=True)
print('Appliance defined. Open its graphical console and change both administrator setup passwords before using HTTPS.')
