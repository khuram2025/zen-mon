/**
 * Exporter configuration snippets for the onboarding page (CR-06).
 *
 * Each vendor entry turns the appliance address, the collector's ports and a few choices into a
 * configuration template with the recommended timers (active 60 s, inactive 15 s, template
 * refresh 60 s) and the fields ZenPlus uses: addresses, ports, protocol, ToS, TCP flags, input and
 * output interface, bytes/packets, flow start/end and, where the platform has it, direction and
 * application ID. Snippets are templates — they are checked by people against their platform
 * release, never pushed to devices.
 */

export type VendorId =
  | 'cisco-iosxe'
  | 'cisco-nxos'
  | 'cisco-asa'
  | 'fortigate'
  | 'paloalto'
  | 'juniper'
  | 'mikrotik'
  | 'aruba-cx'
  | 'huawei'
  | 'generic'

export type ExportFormat = 'ipfix' | 'netflow9' | 'sflow'

export interface SnippetOptions {
  /** Appliance address the exporter sends to. */
  collector: string
  netflowPort: number
  sflowPort: number
  /** Interfaces to enable export on (vendor names, e.g. GigabitEthernet0/0/0). */
  interfaces: string[]
  /** Export source interface or address (loopback recommended). */
  source: string
  format: ExportFormat
  /** Sampling 1-in-N for sFlow and platforms that require it (1 = unsampled where possible). */
  samplingRate: number
  includeIpv6: boolean
  activeTimeoutS: number
  inactiveTimeoutS: number
  templateRefreshS: number
}

export interface VendorInfo {
  id: VendorId
  label: string
  platform: string
  formats: ExportFormat[]
  defaultFormat: ExportFormat
  /** Placeholder for the interface list. */
  interfaceHint: string
  sourceHint: string
  /** The export source is an address (true) or an interface name (false). */
  sourceIsAddress: boolean
  notes: string[]
}

export const VENDORS: VendorInfo[] = [
  {
    id: 'cisco-iosxe', label: 'Cisco IOS-XE', platform: 'Flexible NetFlow (ISR, ASR 1000, Catalyst 9000)',
    formats: ['ipfix', 'netflow9'], defaultFormat: 'ipfix',
    interfaceHint: 'GigabitEthernet0/0/0', sourceHint: 'Loopback0', sourceIsAddress: false,
    notes: [
      'Apply the monitor on ingress (input) of every interface whose traffic you want; the record matches flow direction so ingress and egress stay distinguishable.',
      'collect application name needs NBAR2 (ip nbar protocol-discovery or AVC); remove it on platforms without NBAR.',
      'Catalyst 9000 switches apply monitors per interface and may need a sampler on high-speed ports.',
    ],
  },
  {
    id: 'cisco-nxos', label: 'Cisco NX-OS', platform: 'Nexus NetFlow v9',
    formats: ['netflow9'], defaultFormat: 'netflow9',
    interfaceHint: 'Ethernet1/1', sourceHint: 'loopback0', sourceIsAddress: false,
    notes: [
      'Nexus 9000 requires a sampler and hardware NetFlow resources; Nexus 7000 exports from the supervisor. Check the release notes of your platform.',
      'Add "use-vrf management" to the destination when the appliance is reached through the management VRF.',
    ],
  },
  {
    id: 'cisco-asa', label: 'Cisco ASA / FTD', platform: 'NetFlow Secure Event Logging (NSEL, v9)',
    formats: ['netflow9'], defaultFormat: 'netflow9',
    interfaceHint: 'inside', sourceHint: 'inside', sourceIsAddress: false,
    notes: [
      'NSEL reports connection events (created, torn down, denied) with byte counts on teardown and periodic updates; ZenPlus records the event type and NAT addresses.',
      'The destination interface is the ASA nameif from which the appliance is reachable.',
      'FTD: deploy the same commands as a FlexConfig object from the management center.',
    ],
  },
  {
    id: 'fortigate', label: 'Fortinet FortiGate', platform: 'FortiOS 7.2+ NetFlow v9',
    formats: ['netflow9'], defaultFormat: 'netflow9',
    interfaceHint: 'port1', sourceHint: '192.0.2.1', sourceIsAddress: true,
    notes: [
      'FortiOS 7.0 and earlier: set collector-ip, collector-port and source-ip directly under "config system netflow" (no "config collectors").',
      'netflow-sampler "both" exports ingress and egress; set it to "rx" to count each flow once per interface.',
    ],
  },
  {
    id: 'paloalto', label: 'Palo Alto Networks', platform: 'PAN-OS NetFlow v9 server profile',
    formats: ['netflow9'], defaultFormat: 'netflow9',
    interfaceHint: 'ethernet1/1', sourceHint: 'management or data interface', sourceIsAddress: false,
    notes: [
      'Also configurable in the web interface: Device > Server Profiles > NetFlow, then Network > Interfaces > (interface) > Advanced > NetFlow Profile.',
      'PAN-OS field types (App-ID and User-ID) are exported when "PAN-OS Field Types" is enabled; ZenPlus shows App-ID names when the exporter sends the application table.',
      'Commit after the change. Panorama: set the profile in the template instead of "shared".',
    ],
  },
  {
    id: 'juniper', label: 'Juniper', platform: 'Junos inline J-Flow IPFIX (MX, SRX)',
    formats: ['ipfix'], defaultFormat: 'ipfix',
    interfaceHint: 'ge-0/0/0', sourceHint: '192.0.2.1', sourceIsAddress: true,
    notes: [
      'Inline J-Flow samples packets: the sampling rate is exported in the options template and ZenPlus scales volumes by it.',
      'SRX branch devices: omit the chassis line; the flow server source address is set under "output inline-jflow".',
    ],
  },
  {
    id: 'mikrotik', label: 'MikroTik', platform: 'RouterOS Traffic Flow',
    formats: ['ipfix', 'netflow9'], defaultFormat: 'ipfix',
    interfaceHint: 'ether1', sourceHint: '192.0.2.1', sourceIsAddress: true,
    notes: ['RouterOS 7 syntax; RouterOS 6 uses the same menu with fewer options.'],
  },
  {
    id: 'aruba-cx', label: 'HPE Aruba CX', platform: 'AOS-CX sFlow or IPFIX',
    formats: ['sflow', 'ipfix'], defaultFormat: 'sflow',
    interfaceHint: '1/1/1', sourceHint: '192.0.2.1', sourceIsAddress: true,
    notes: [
      'sFlow samples packets (1 in N) and sends interface counters; ZenPlus scales sampled volumes by the rate the switch reports.',
      'IPFIX flow monitoring is available on AOS-CX 10.8 and later on supported models.',
    ],
  },
  {
    id: 'huawei', label: 'Huawei', platform: 'VRP NetStream (v9 / IPFIX)',
    formats: ['netflow9', 'ipfix'], defaultFormat: 'netflow9',
    interfaceHint: 'GigabitEthernet0/0/1', sourceHint: '192.0.2.1', sourceIsAddress: true,
    notes: ['Some platforms require "slot <n>" before NetStream commands or a license for statistics on LPUs.'],
  },
  {
    id: 'generic', label: 'Other device', platform: 'Any NetFlow v5/v9, IPFIX or sFlow exporter',
    formats: ['ipfix', 'netflow9', 'sflow'], defaultFormat: 'ipfix',
    interfaceHint: 'the monitored interfaces', sourceHint: 'a stable address (loopback)', sourceIsAddress: true,
    notes: ['NetFlow v5 works but carries no IPv6, no 32-bit interface indexes and no direction; prefer IPFIX or v9.'],
  },
]

export function vendorInfo(id: VendorId): VendorInfo {
  return VENDORS.find((v) => v.id === id) ?? VENDORS[VENDORS.length - 1]
}

export const RECOMMENDED_FIELDS: { field: string; why: string; required: boolean }[] = [
  { field: 'Source / destination address (IPv4 and IPv6)', why: 'who talks to whom', required: true },
  { field: 'Protocol, source / destination port', why: 'services and applications', required: true },
  { field: 'Bytes and packets', why: 'volume and rates', required: true },
  { field: 'Input and output interface (ifIndex)', why: 'interface views and capacity', required: true },
  { field: 'Flow start and end time', why: 'durations and accurate time placement', required: true },
  { field: 'TCP flags', why: 'SYN-scan / RST detection and flag views', required: true },
  { field: 'ToS / DSCP', why: 'QoS views', required: false },
  { field: 'Flow direction', why: 'automatic edge counting', required: false },
  { field: 'Application ID (NBAR2, App-ID)', why: 'exporter-named applications', required: false },
  { field: 'Sampler options (rate)', why: 'correct volume scaling of sampled flows', required: false },
]

function ifaces(o: SnippetOptions, hint: string): string[] {
  const list = o.interfaces.map((s) => s.trim()).filter(Boolean)
  return list.length ? list : [hint]
}

function minutesCeil(seconds: number): number {
  return Math.max(1, Math.ceil(seconds / 60))
}

/** The configuration text for one vendor. Deterministic for a given input. */
export function buildSnippet(vendor: VendorId, o: SnippetOptions): string {
  const v = vendorInfo(vendor)
  const format = v.formats.includes(o.format) ? o.format : v.defaultFormat
  const port = format === 'sflow' ? o.sflowPort : o.netflowPort
  const src = o.source.trim() || v.sourceHint
  const rate = Math.max(1, Math.round(o.samplingRate || 1))
  const act = o.activeTimeoutS
  const inact = o.inactiveTimeoutS
  const tpl = o.templateRefreshS
  const lines: string[] = []
  const push = (...l: string[]) => lines.push(...l)

  switch (vendor) {
    case 'cisco-iosxe': {
      push(
        'flow record ZENPLUS-V4',
        ' match ipv4 tos',
        ' match ipv4 protocol',
        ' match ipv4 source address',
        ' match ipv4 destination address',
        ' match transport source-port',
        ' match transport destination-port',
        ' match interface input',
        ' match flow direction',
        ' collect interface output',
        ' collect transport tcp flags',
        ' collect counter bytes long',
        ' collect counter packets long',
        ' collect timestamp absolute first',
        ' collect timestamp absolute last',
        ' collect application name',
        '!',
      )
      if (o.includeIpv6) {
        push(
          'flow record ZENPLUS-V6',
          ' match ipv6 traffic-class',
          ' match ipv6 protocol',
          ' match ipv6 source address',
          ' match ipv6 destination address',
          ' match transport source-port',
          ' match transport destination-port',
          ' match interface input',
          ' match flow direction',
          ' collect interface output',
          ' collect transport tcp flags',
          ' collect counter bytes long',
          ' collect counter packets long',
          ' collect timestamp absolute first',
          ' collect timestamp absolute last',
          '!',
        )
      }
      push(
        'flow exporter ZENPLUS',
        ` destination ${o.collector}`,
        ` source ${src}`,
        ` transport udp ${port}`,
        ` export-protocol ${format === 'netflow9' ? 'netflow-v9' : 'ipfix'}`,
        ` template data timeout ${tpl}`,
        ' option interface-table',
        ' option application-table',
        ' option sampler-table',
        '!',
        'flow monitor ZENPLUS-V4',
        ' exporter ZENPLUS',
        ' record ZENPLUS-V4',
        ` cache timeout active ${act}`,
        ` cache timeout inactive ${inact}`,
        '!',
      )
      if (o.includeIpv6) {
        push('flow monitor ZENPLUS-V6', ' exporter ZENPLUS', ' record ZENPLUS-V6',
          ` cache timeout active ${act}`, ` cache timeout inactive ${inact}`, '!')
      }
      for (const i of ifaces(o, v.interfaceHint)) {
        push(`interface ${i}`, ' ip flow monitor ZENPLUS-V4 input')
        if (o.includeIpv6) push(' ipv6 flow monitor ZENPLUS-V6 input')
        push('!')
      }
      break
    }
    case 'cisco-nxos': {
      push(
        'feature netflow',
        '!',
        'flow exporter ZENPLUS',
        `  destination ${o.collector}`,
        `  source ${src}`,
        `  transport udp ${port}`,
        '  version 9',
        `    template data timeout ${tpl}`,
        '!',
        'flow record ZENPLUS-V4',
        '  match ipv4 source address',
        '  match ipv4 destination address',
        '  match ip protocol',
        '  match ip tos',
        '  match transport source-port',
        '  match transport destination-port',
        '  collect counter bytes long',
        '  collect counter packets long',
        '  collect timestamp sys-uptime first',
        '  collect timestamp sys-uptime last',
        '  collect transport tcp flags',
        '  collect routing next-hop address ipv4',
        '!',
        'flow monitor ZENPLUS-V4',
        '  record ZENPLUS-V4',
        '  exporter ZENPLUS',
        '!',
        `flow timeout active ${act}`,
        `flow timeout inactive ${inact}`,
        '!',
      )
      for (const i of ifaces(o, v.interfaceHint)) push(`interface ${i}`, '  ip flow monitor ZENPLUS-V4 input', '!')
      break
    }
    case 'cisco-asa': {
      const nameif = src
      push(
        `flow-export destination ${nameif} ${o.collector} ${port}`,
        `flow-export template timeout-rate ${minutesCeil(tpl)}`,
        `flow-export active refresh-interval ${minutesCeil(act)}`,
        'logging flow-export-syslogs disable',
        '!',
        'access-list ZENPLUS-FLOWS extended permit ip any any',
        'class-map ZENPLUS-FLOWS',
        ' match access-list ZENPLUS-FLOWS',
        '!',
        'policy-map global_policy',
        ' class ZENPLUS-FLOWS',
        `  flow-export event-type all destination ${o.collector}`,
        '!',
        'service-policy global_policy global',
      )
      break
    }
    case 'fortigate': {
      push(
        'config system netflow',
        `    set active-flow-timeout ${Math.max(60, act)}`,
        `    set inactive-flow-timeout ${Math.max(10, inact)}`,
        `    set template-tx-timeout ${Math.max(60, tpl)}`,
        '    config collectors',
        '        edit 1',
        `            set collector-ip ${o.collector}`,
        `            set collector-port ${port}`,
        `            set source-ip ${src}`,
        '        next',
        '    end',
        'end',
        'config system interface',
      )
      for (const i of ifaces(o, v.interfaceHint)) push(`    edit "${i}"`, '        set netflow-sampler both', '    next')
      push('end')
      break
    }
    case 'paloalto': {
      push(
        'configure',
        `set shared server-profile netflow ZENPLUS server ZENPLUS host ${o.collector} port ${port}`,
        `set shared server-profile netflow ZENPLUS template-refresh-rate minutes ${minutesCeil(tpl)}`,
        `set shared server-profile netflow ZENPLUS active-timeout ${minutesCeil(act)}`,
        'set shared server-profile netflow ZENPLUS export-enterprise-fields yes',
      )
      for (const i of ifaces(o, v.interfaceHint)) push(`set network interface ethernet ${i} layer3 netflow-profile ZENPLUS`)
      push('commit')
      break
    }
    case 'juniper': {
      push(
        'set services flow-monitoring version-ipfix template ZENPLUS-V4 ipv4-template',
        `set services flow-monitoring version-ipfix template ZENPLUS-V4 flow-active-timeout ${act}`,
        `set services flow-monitoring version-ipfix template ZENPLUS-V4 flow-inactive-timeout ${inact}`,
        `set services flow-monitoring version-ipfix template ZENPLUS-V4 template-refresh-rate seconds ${tpl}`,
        `set services flow-monitoring version-ipfix template ZENPLUS-V4 option-refresh-rate seconds ${tpl}`,
      )
      if (o.includeIpv6) {
        push(
          'set services flow-monitoring version-ipfix template ZENPLUS-V6 ipv6-template',
          `set services flow-monitoring version-ipfix template ZENPLUS-V6 flow-active-timeout ${act}`,
          `set services flow-monitoring version-ipfix template ZENPLUS-V6 flow-inactive-timeout ${inact}`,
          `set services flow-monitoring version-ipfix template ZENPLUS-V6 template-refresh-rate seconds ${tpl}`,
        )
      }
      push(
        `set forwarding-options sampling instance ZENPLUS input rate ${rate}`,
        `set forwarding-options sampling instance ZENPLUS family inet output flow-server ${o.collector} port ${port}`,
        `set forwarding-options sampling instance ZENPLUS family inet output flow-server ${o.collector} version-ipfix template ZENPLUS-V4`,
        `set forwarding-options sampling instance ZENPLUS family inet output inline-jflow source-address ${src}`,
      )
      if (o.includeIpv6) {
        push(
          `set forwarding-options sampling instance ZENPLUS family inet6 output flow-server ${o.collector} port ${port}`,
          `set forwarding-options sampling instance ZENPLUS family inet6 output flow-server ${o.collector} version-ipfix template ZENPLUS-V6`,
          `set forwarding-options sampling instance ZENPLUS family inet6 output inline-jflow source-address ${src}`,
        )
      }
      push('set chassis fpc 0 sampling-instance ZENPLUS')
      for (const i of ifaces(o, v.interfaceHint)) {
        push(`set interfaces ${i} unit 0 family inet sampling input`)
        if (o.includeIpv6) push(`set interfaces ${i} unit 0 family inet6 sampling input`)
      }
      push('commit')
      break
    }
    case 'mikrotik': {
      const list = o.interfaces.map((s) => s.trim()).filter(Boolean)
      push(
        `/ip traffic-flow set enabled=yes interfaces=${list.length ? list.join(',') : 'all'} ` +
          `active-flow-timeout=${formatRouterOsTime(act)} inactive-flow-timeout=${formatRouterOsTime(inact)} cache-entries=128k`,
        `/ip traffic-flow target add dst-address=${o.collector} port=${port} src-address=${src} ` +
          `version=${format === 'netflow9' ? '9' : 'ipfix'} v9-template-timeout=${formatRouterOsTime(tpl)}`,
      )
      break
    }
    case 'aruba-cx': {
      if (format === 'sflow') {
        push(
          'sflow',
          `sflow collector ${o.collector} port ${port}`,
          `sflow agent-ip ${src}`,
          `sflow sampling ${rate}`,
          'sflow polling 30',
        )
        for (const i of ifaces(o, v.interfaceHint)) push(`interface ${i}`, '    sflow', '    exit')
      } else {
        push(
          'flow exporter ZENPLUS',
          `    destination ${o.collector}`,
          `    transport udp ${port}`,
          `    template data timeout ${tpl}`,
          'flow record ZENPLUS-V4',
          '    match ipv4 protocol',
          '    match ipv4 source address',
          '    match ipv4 destination address',
          '    match transport source-port',
          '    match transport destination-port',
          '    collect interface input',
          '    collect interface output',
          '    collect transport tcp flags',
          '    collect counter bytes',
          '    collect counter packets',
          '    collect timestamp absolute first',
          '    collect timestamp absolute last',
          'flow monitor ZENPLUS-V4',
          '    record ZENPLUS-V4',
          '    exporter ZENPLUS',
          `    cache timeout active ${act}`,
          `    cache timeout inactive ${inact}`,
        )
        for (const i of ifaces(o, v.interfaceHint)) push(`interface ${i}`, '    ip flow monitor ZENPLUS-V4 in', '    exit')
      }
      break
    }
    case 'huawei': {
      push(
        `ip netstream export version ${format === 'ipfix' ? 'ipfix' : '9'}`,
        `ip netstream export template timeout-rate ${minutesCeil(tpl)}`,
        `ip netstream timeout active ${minutesCeil(act)}`,
        `ip netstream timeout inactive ${Math.max(5, inact)}`,
        `ip netstream export source ${src}`,
        `ip netstream export host ${o.collector} ${port}`,
      )
      if (rate > 1) push(`ip netstream sampler fix-packets ${rate} inbound`)
      for (const i of ifaces(o, v.interfaceHint)) push(`interface ${i}`, ' ip netstream inbound', ' quit')
      break
    }
    default: {
      const fmtName = format === 'sflow' ? 'sFlow v5' : format === 'netflow9' ? 'NetFlow v9' : 'IPFIX'
      push(
        `# ${fmtName} export to ZenPlus`,
        `collector address : ${o.collector}`,
        `collector port    : UDP ${port}`,
        `export source     : ${src}`,
        ...(format === 'sflow'
          ? [`sampling rate     : 1 in ${rate}`, 'counter polling   : 30 s']
          : [`active timeout    : ${act} s`, `inactive timeout  : ${inact} s`, `template refresh  : ${tpl} s`]),
        `interfaces        : ${ifaces(o, v.interfaceHint).join(', ')}`,
      )
    }
  }
  return lines.join('\n') + '\n'
}

function formatRouterOsTime(seconds: number): string {
  return seconds % 60 === 0 ? `${seconds / 60}m` : `${seconds}s`
}

/** Default options for a vendor, from the setup info. */
export function defaultSnippetOptions(vendor: VendorId, collector: string, netflowPort: number, sflowPort: number): SnippetOptions {
  const v = vendorInfo(vendor)
  return {
    collector,
    netflowPort,
    sflowPort,
    interfaces: [],
    source: '',
    format: v.defaultFormat,
    samplingRate: v.defaultFormat === 'sflow' ? 1000 : vendor === 'juniper' ? 1000 : 1,
    includeIpv6: true,
    activeTimeoutS: 60,
    inactiveTimeoutS: 15,
    templateRefreshS: 60,
  }
}
