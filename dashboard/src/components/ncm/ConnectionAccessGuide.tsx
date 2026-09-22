import { useState } from 'react'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'

const guides = {
  cisco: {
    label: 'Cisco IOS / IOS XE',
    role: 'Dedicated SSH backup account with TACACS+ command authorization. Permit complete configuration reads and session preparation; deny configuration changes, reload and write/copy operations.',
    commands: 'show running-config; show startup-config (if selected); terminal length 0; terminal width 511. Enable is only needed when the account requires elevation.',
    limit: 'A low local privilege number alone can hide configuration. Full output may require privilege 15 plus a restrictive TACACS+ command policy. Do not substitute an unrestricted privilege-15 account. Parser views require device-specific completeness testing; the collector does not issue show running-config view full.',
    url: 'https://www.cisco.com/c/en/us/support/docs/security-vpn/terminal-access-controller-access-control-system-tacacs-/23383-showrun.html',
  },
  fortinet: {
    label: 'Fortinet FortiGate / FortiOS',
    role: 'For a scoped read-only capture, use a dedicated SSH administrator with read access to the required sections and cli-show enabled. Keep cli-config, cli-exec and cli-diagnose disabled. Restrict trusted source hosts to the collector.',
    commands: 'show full-configuration. Select the Fortinet driver explicitly to avoid additional autodetection commands. No enable secret is required.',
    limit: 'This is not a guaranteed full recovery backup: custom/read-only profiles can hide administrator accounts, secrets and global/VDOM data. FortiOS full-backup permissions vary by release and can require super_admin or write permissions. Do not elevate automatically; agree the capture scope and verify it in the pilot. ZenPlus currently captures CLI text, not the native backup export.',
    url: 'https://docs.fortinet.com/document/fortigate/7.4.10/administration-guide/294491/administrator-profiles',
  },
  paloalto: {
    label: 'Palo Alto Networks / PAN-OS',
    role: 'Use a dedicated administrator with Device scope and CLI superreader for full read-only visibility. Disable unneeded Web UI and API access. Do not select superuser or deviceadmin for backup collection.',
    commands: 'show config running; show system info; set cli pager off; set cli scripting-mode on; set cli terminal width 500. These set cli commands prepare the session, not device policy. No enable secret is required.',
    limit: 'devicereader excludes password profiles and other administrator accounts; vsysreader is scoped and omits device/network settings. Choose these only for an explicitly partial capture. Running configuration is not a device-state bundle, candidate configuration or certificate/private-key recovery package.',
    url: 'https://docs.paloaltonetworks.com/ngfw/help/12-2/configure-an-admin-role-profile-pan-os',
  },
  other: {
    label: 'Cisco NX-OS / ASA and other platforms',
    role: 'Use a dedicated SSH account and the OS-specific read-only role or command authorization policy. IOS privilege levels do not apply universally to NX-OS, ASA, Junos or other operating systems.',
    commands: 'Permit the selected driver’s configuration-read and session-only pager/width commands. Select the exact platform on the device and validate with the administrator for that OS release.',
    limit: 'Do not grant write, restore or firmware permissions for collection. Aruba and F5 have no explicit collector in this release; selecting another vendor’s driver does not certify support. A successful login does not establish configuration completeness.',
    url: '',
  },
}

export function ConnectionAccessGuide() {
  const [vendor, setVendor] = useState<keyof typeof guides>('cisco')
  const guide = guides[vendor]
  return <section aria-label="Least-privilege connection guidance" className="space-y-2 rounded-md border border-primary/20 bg-primary/5 p-3 text-xs">
    <FormField label="Device vendor / OS — access guidance">
      <Select value={vendor} onValueChange={(v) => setVendor(v as keyof typeof guides)}>
        <SelectTrigger><SelectValue /></SelectTrigger>
        <SelectContent>{Object.entries(guides).map(([key, g]) => <SelectItem key={key} value={key}>{g.label}</SelectItem>)}</SelectContent>
      </Select>
    </FormField>
    <p className="font-medium">Minimum access for configuration collection</p>
    <p>{guide.role}</p>
    <details><summary className="cursor-pointer font-medium text-primary">Commands and completeness limits</summary>
      <p className="mt-2 break-words">{guide.commands}</p><p className="mt-2 text-muted">{guide.limit}</p>
      {guide.url && <a className="mt-2 inline-block text-primary underline" href={guide.url} target="_blank" rel="noreferrer">Vendor access documentation</a>}
    </details>
    <p className="text-muted">SSH username/password is required; this collector does not use SNMP communities or API tokens. This selector shows guidance only. Select the actual driver in each device’s backup settings. Confirm the exact firmware and permitted commands during the pilot.</p>
  </section>
}
