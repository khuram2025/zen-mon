# Device Tracker gateway and managed-switch correlation

Release 1.24.11 adds controller-managed switches to Device Tracker without enrolling them as monitored devices or changing their alerting. Existing directly polled switches keep their port actions. Controller observations are read-only; a child port number is never sent as a parent firewall ifIndex.

## Collection and evidence

SNMP is the primary transport: IF-MIB, BRIDGE-MIB, Q-BRIDGE-MIB, IP-MIB and available LLDP/CDP tables. FortiGate's switch tables provide serial identities, full multi-part instance indexes and local port names, native VLANs and link status. A separate bounded worker supplements missing gateway ARP and managed MAC tables through an enabled NCCM SSH connection. A UDT-only credential override can select a diagnostic-capable account while leaving configuration backups unchanged. Credentials remain encrypted at rest; only credential IDs/names appear in the UDT picker.

For Fortinet, `fgVdEntName` resolves the observed controller VDOM. The collector selects that existing context with `config global` and `execute enter`; it sends no `edit`, `set`, configuration write, console change or table clear. It reads `diagnose switch-controller switch-info mac-table`, falling back to `diagnose switch-controller mac-cache show`. Explicit serial/unique hostname aliases are resolved against SNMP inventory. Only validated port IDs/names are accepted; trunks, internal/port-zero MACs and stale cache rows are excluded. Unknown formats or denied commands are partial collection, not proof of an empty network. Managed-port status uses the Fortinet `down(0)` / `up(1)` values; unsupported values remain unknown. Endpoint search selects and orders its requested page before enriching port and gateway evidence, so enrichment cost is bounded by page size. Older FortiOS configured-port status is shown as unknown; current unknown link states are not counted as free/down capacity.

MAC/IP evidence retains reporter, local interface and source. Positive IP replacement still needs three recent observations from the same evidence key. Overlapping addresses across gateways/VRFs do not establish exclusive ownership. A gateway address observation may be shown even when its physical access port is not confirmed. Fifteen-minute attachment gaps expire current state while keeping historical periods. Firmware contexts and privilege limitations remain visible in collection status.

## Vendor coverage and read access

| Platform | Supplemental IPv4 command | Access and scope |
| --- | --- | --- |
| Fortinet FortiOS | `get system arp` | Read SNMP switch/port/VDOM tables; account must be able to enter the actual controller VDOM and run the two fixed MAC diagnostics. Diagnostic access is broader than individual read commands on some releases; do not grant blanket administration just for UDT. |
| Palo Alto PAN-OS | `show arp all` | Operational CLI ARP read access in the applicable context. No XML API write/operational privilege is requested by this collector. |
| Cisco IOS/IOS-XE/NX-OS | `show ip arp` | Command-authorized read access. Standard SNMP bridge tables also need their applicable VLAN context/view. |
| Cisco ASA | `show arp` | Read command access in the configured context. |
| Arista EOS | `show ip arp` | Read command access in the active VRF; retain the routed interface when output also lists an egress port. |
| Juniper Junos | `show arp no-resolve` | Operational read access in the configured routing/logical-system context. |
| Aruba AOS-CX/AOS-Switch/ArubaOS | `show arp` | Read command and matching interface inventory. |
| Huawei/HPE Comware | `display arp` | Read command and matching interface inventory. |

Use SNMPv3 authPriv where supported, a restricted read view and appliance-source management ACLs. No SNMP write permission is required for discovery. Nondefault VRF/VSYS/logical-system contexts must be configured/exposed by the device; this release does not guess or grant access to hidden routing contexts. Unknown or incomplete neighbor entries and multicast/zero MAC addresses are excluded. FDB IDs are mapped through `dot1qVlanFdbId`; shared or unavailable mappings remain unknown, rather than becoming invented VLAN IDs.

Live validation covered the HQ FortiGate/FortiSwitch output and installed Cisco SNMP collection. Parser fixtures cover the listed common command formats, malformed neighbors and interface variants. This is not certification of every vendor model/firmware or every multiline ARP format. Unsupported responses need a sanitized sample and an additional fixture before claiming support.

## Minimum Windows login collection permissions

Use a dedicated nonadministrative domain service account. It needs:

1. Security event-log read access on each monitored domain controller, normally through **Event Log Readers** or an equivalent explicit Security-channel read ACL.
2. WinRM remote access, normally **Remote Management Users**, plus WinRM shell/provider execution permission for the collector's PowerShell `Get-WinEvent` command. Group membership alone is not sufficient if local/GPO WinRM or shell ACLs deny execution. The current transport uses pywinrm `run_ps`; a custom JEA/PowerShell endpoint is not selected by this implementation.
3. Network access only from the appliance to the permitted DCs. Prefer HTTPS 5986 with a trusted certificate. NTLM/Kerberos over HTTP 5985 must retain message encryption; Basic authentication over HTTP is rejected. Do not disable certificate validation to bypass connection failures.
4. Successful **Audit Logon**, **Kerberos Authentication Service** and **Kerberos Service Ticket Operations** auditing so events 4624, 4768 and 4769 actually exist. Retain Security logs long enough for the chosen polling interval and expected event volume.

Domain Admin membership, domain directory writes, WinRM administrator membership and filesystem/database write privileges are not required for reading these logs. For a DC, local built-in groups are domain groups; grant scope and GPOs should be reviewed by the AD administrator.

Events are processed oldest-first in bounded batches. The cursor advances across excluded machine accounts and failed authentication. Authentication/service tickets show user activity, not proof of an interactive desktop session; 4624 logon type remains part of the record. An IP maps to an endpoint only when history contains one candidate at the event time. Shared/proxy addresses or missing evidence remain recorded without assigning an arbitrary MAC. Workstation-name matching is used only when there is no valid IP and the name is unique. Reading usernames in the appliance requires `udt.view_users` or `udt.manage`; normal inventory access is `udt.view`.

No domain controller was configured in the production appliance during this investigation. Live Windows collection acceptance requires an explicitly configured DC and this dedicated account; vendor/API fixture tests do not replace that check.

## Primary references

- [Fortinet switch MIB tables](https://docs.fortinet.com/document/fortigate/7.6.0/fortigate-mib-information-overview/508932/fortigate-switch-mibs)
- [Fortinet VDOM table/index](https://docs.fortinet.com/document/fortigate/7.4.0/fortigate-mib-information-overview/630205)
- [Fortinet read-context selection](https://docs.fortinet.com/document/fortigate/7.6.6/cli-reference/293954249/execute-enter)
- [Fortinet managed MAC diagnostics](https://docs.fortinet.com/document/fortigate/7.4.9/cli-reference/218433386/diagnose-switch-controller)
- [Fortinet port status semantics](https://docs.fortinet.com/document/fortigate/7.0.0/new-features/265675)
- [Fortinet diagnostic administrator access](https://community.fortinet.com/fortigate-3/technical-tip-granting-read-only-admins-with-diagnose-commands-114614)
- [RFC 4363 Q-BRIDGE-MIB](https://www.rfc-editor.org/info/rfc4363/)
- [Palo Alto ARP output](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000Cla2CAC&lang=en_US)
- [Cisco SNMP ARP mapping](https://www.cisco.com/c/en/us/support/docs/ip/simple-network-management-protocol-snmp/9226-mibs-9226.pdf)
- [Arista IPv4/ARP commands](https://www.arista.com/en/um-eos/eos-ipv4)
- [Aruba AOS-CX ARP output](https://arubanetworking.hpe.com/techdocs/AOS-CX/10.16/HTML/ip_services_4100i-6000-6100/Content/Chp_ARP/ARP_cmds/sho-arp.htm)
- [HPE Comware ARP reference](https://arubanetworking.hpe.com/techdocs/comware/flexfabric5710/HPEFlexFabric5710L3IPServicesCR-R27xx.pdf)
- [Huawei ARP output](https://info.support.huawei.com/hedex/api/pages/EDOC1100331440/AZM10131/04/resources/dc/display_arp.html)
- [Juniper ARP commands](https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/command/show-arp.html)
- [Microsoft Event Log Readers](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/plan/security-best-practices/appendix-b--privileged-accounts-and-groups-in-active-directory)
- [Microsoft WinRM setup and permissions](https://learn.microsoft.com/en-us/windows/win32/winrm/installation-and-configuration-for-windows-remote-management)
- [Microsoft remoting security](https://learn.microsoft.com/en-us/powershell/scripting/security/remoting/winrm-security)
- [4624 logon event](https://learn.microsoft.com/en-us/windows/security/threat-protection/auditing/event-4624), [4768](https://learn.microsoft.com/en-us/windows/security/threat-protection/auditing/event-4768), [4769](https://learn.microsoft.com/en-us/windows/security/threat-protection/auditing/event-4769)
