#!/usr/bin/env python3
"""Package already sealed, powered-off disks. Requires qemu-img on the builder."""
import argparse,hashlib,subprocess,tarfile
from pathlib import Path
from xml.sax.saxutils import escape
p=argparse.ArgumentParser();p.add_argument('--system',required=True);p.add_argument('--data',required=True);p.add_argument('--output-dir',required=True);p.add_argument('--version',required=True);a=p.parse_args()
out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True)
stem='zenplus-appliance-'+a.version+'-amd64'
disks=[]
for idx,source in enumerate((a.system,a.data),1):
    dest=out/(stem+f'-disk{idx}.vmdk')
    if dest.exists():raise SystemExit('Refusing to replace existing export '+str(dest))
    subprocess.run(['qemu-img','convert','-f','vmdk','-O','vmdk','-o','subformat=streamOptimized,compat6',source,str(dest)],check=True)
    disks.append(dest)
v=escape(a.version)
ovf=out/(stem+'.ovf')
ovf.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1" xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData" xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData" xmlns:vmw="http://www.vmware.com/schema/ovf" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <References><File ovf:id="file1" ovf:href="{disks[0].name}" ovf:size="{disks[0].stat().st_size}"/><File ovf:id="file2" ovf:href="{disks[1].name}" ovf:size="{disks[1].stat().st_size}"/></References>
 <DiskSection><Info>System and expandable data storage</Info><Disk ovf:diskId="system" ovf:fileRef="file1" ovf:capacity="40" ovf:capacityAllocationUnits="byte * 2^30" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/><Disk ovf:diskId="data" ovf:fileRef="file2" ovf:capacity="100" ovf:capacityAllocationUnits="byte * 2^30" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/></DiskSection>
 <NetworkSection><Info>Connect to a trusted management network with DHCP</Info><Network ovf:name="Management"><Description>HTTPS management and telemetry collection</Description></Network></NetworkSection>
 <VirtualSystem ovf:id="ZenPlus"><Info>Zentryc ZenPlus virtual appliance</Info><Name>Zentryc ZenPlus {v}</Name>
 <OperatingSystemSection ovf:id="94" vmw:osType="ubuntu64Guest"><Info>64-bit Linux operating system</Info><Description>Ubuntu 26.04.1 LTS with Zentryc ZenPlus</Description></OperatingSystemSection>
 <ProductSection><Info>Zentryc ZenPlus appliance</Info><Product>ZenPlus</Product><Vendor>Zentryc LLC</Vendor><Version>{v}</Version><ProductUrl>https://zentryc.com/products/</ProductUrl><VendorUrl>https://zentryc.com/</VendorUrl></ProductSection>
 <VirtualHardwareSection><Info>4 vCPU, 8 GiB RAM, UEFI, 40 + 100 GiB thin disks</Info><System><vssd:ElementName>ZenPlus</vssd:ElementName><vssd:InstanceID>0</vssd:InstanceID><vssd:VirtualSystemIdentifier>ZenPlus</vssd:VirtualSystemIdentifier><vssd:VirtualSystemType>vmx-20</vssd:VirtualSystemType></System>
 <Item><rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits><rasd:Description>Virtual CPUs</rasd:Description><rasd:ElementName>4 virtual CPUs</rasd:ElementName><rasd:InstanceID>1</rasd:InstanceID><rasd:ResourceType>3</rasd:ResourceType><rasd:VirtualQuantity>4</rasd:VirtualQuantity></Item>
 <Item><rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits><rasd:Description>Memory</rasd:Description><rasd:ElementName>8192 MiB memory</rasd:ElementName><rasd:InstanceID>2</rasd:InstanceID><rasd:ResourceType>4</rasd:ResourceType><rasd:VirtualQuantity>8192</rasd:VirtualQuantity></Item>
 <Item><rasd:Address>0</rasd:Address><rasd:Description>SCSI controller</rasd:Description><rasd:ElementName>SCSI controller</rasd:ElementName><rasd:InstanceID>3</rasd:InstanceID><rasd:ResourceSubType>lsilogic</rasd:ResourceSubType><rasd:ResourceType>6</rasd:ResourceType></Item>
 <Item><rasd:AddressOnParent>0</rasd:AddressOnParent><rasd:ElementName>System disk</rasd:ElementName><rasd:HostResource>ovf:/disk/system</rasd:HostResource><rasd:InstanceID>4</rasd:InstanceID><rasd:Parent>3</rasd:Parent><rasd:ResourceType>17</rasd:ResourceType></Item>
 <Item><rasd:AddressOnParent>1</rasd:AddressOnParent><rasd:ElementName>Data disk</rasd:ElementName><rasd:HostResource>ovf:/disk/data</rasd:HostResource><rasd:InstanceID>5</rasd:InstanceID><rasd:Parent>3</rasd:Parent><rasd:ResourceType>17</rasd:ResourceType></Item>
 <Item><rasd:AddressOnParent>7</rasd:AddressOnParent><rasd:AutomaticAllocation>true</rasd:AutomaticAllocation><rasd:Connection>Management</rasd:Connection><rasd:Description>VMXNET3 management adapter</rasd:Description><rasd:ElementName>Management</rasd:ElementName><rasd:InstanceID>6</rasd:InstanceID><rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType><rasd:ResourceType>10</rasd:ResourceType></Item>
 <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/><vmw:Config ovf:required="false" vmw:key="uefi.secureBoot.enabled" vmw:value="true"/><vmw:Config ovf:required="false" vmw:key="tools.syncTime" vmw:value="false"/>
 </VirtualHardwareSection></VirtualSystem></Envelope>
''',encoding='utf-8')
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()
mf=out/(stem+'.mf');mf.write_text(''.join(f'SHA256({x.name})= {sha(x)}\n' for x in [ovf,*disks]))
ova=out/(stem+'.ova')
with tarfile.open(ova,'w',format=tarfile.USTAR_FORMAT) as tar:
    for path in [ovf,mf,*disks]:
        info=tar.gettarinfo(str(path),arcname=path.name);info.uid=info.gid=0;info.uname=info.gname='root';info.mtime=0;info.mode=0o644
        with path.open('rb') as f:tar.addfile(info,f)
(out/(ova.name+'.sha256')).write_text(f'{sha(ova)}  {ova.name}\n')
print(ova.name,ova.stat().st_size,sha(ova))
