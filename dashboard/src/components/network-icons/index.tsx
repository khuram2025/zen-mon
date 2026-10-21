import type { SVGProps } from 'react'

type Props = SVGProps<SVGSVGElement> & { className?: string }

/* ──────────────────────────────────────────────────────────────
 * Network device icons
 *
 * Style guide:
 *  - viewBox 0 0 64 64
 *  - stroke = currentColor for outlines, so the parent's text color
 *    flows through (used to tint by status)
 *  - solid filled "port" indicators in low-opacity currentColor so
 *    they read at a glance but don't fight with the outline
 *  - hairline accent shapes for depth (gradients avoided so dark mode
 *    just works)
 * ────────────────────────────────────────────────────────────── */

const base = (p: Props) => ({
  width: 24,
  height: 24,
  viewBox: '0 0 64 64',
  fill: 'none',
  stroke: 'currentColor',
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  strokeWidth: 2,
  ...p,
})

export function RouterIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Body */}
      <rect x="6" y="32" width="52" height="18" rx="3" />
      <path d="M6 42h52" opacity="0.4" />
      {/* Front face ports */}
      <g fill="currentColor" stroke="none" opacity="0.85">
        <rect x="10" y="36" width="3" height="2" rx="0.4" />
        <rect x="15" y="36" width="3" height="2" rx="0.4" />
        <rect x="20" y="36" width="3" height="2" rx="0.4" />
        <rect x="25" y="36" width="3" height="2" rx="0.4" />
        <rect x="30" y="36" width="3" height="2" rx="0.4" />
        <rect x="35" y="36" width="3" height="2" rx="0.4" />
        <rect x="40" y="36" width="3" height="2" rx="0.4" />
        <rect x="45" y="36" width="3" height="2" rx="0.4" />
      </g>
      {/* Indicator LEDs */}
      <circle cx="52.5" cy="37" r="1" fill="currentColor" stroke="none" />
      <circle cx="52.5" cy="42" r="1" fill="currentColor" stroke="none" opacity="0.6" />
      <circle cx="52.5" cy="47" r="1" fill="currentColor" stroke="none" opacity="0.4" />
      {/* Antennas */}
      <path d="M14 32V22M22 32V18M30 32V22M38 32V18" />
      <g fill="currentColor" stroke="none">
        <circle cx="14" cy="20" r="1.6" />
        <circle cx="22" cy="16" r="1.6" />
        <circle cx="30" cy="20" r="1.6" />
        <circle cx="38" cy="16" r="1.6" />
      </g>
      {/* Cable cradle */}
      <path d="M46 32v-4M50 32v-4M54 32v-4" opacity="0.6" />
    </svg>
  )
}

export function SwitchIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Chassis */}
      <rect x="4" y="22" width="56" height="20" rx="2" />
      <path d="M4 28h56" opacity="0.5" />
      <path d="M4 36h56" opacity="0.5" />
      {/* 24 ports in 2 rows */}
      <g fill="currentColor" stroke="none" opacity="0.85">
        {Array.from({ length: 12 }).map((_, i) => (
          <rect key={`u${i}`} x={7 + i * 4} y={24} width="2.6" height="2.4" rx="0.4" />
        ))}
        {Array.from({ length: 12 }).map((_, i) => (
          <rect key={`l${i}`} x={7 + i * 4} y={32} width="2.6" height="2.4" rx="0.4" />
        ))}
      </g>
      {/* Uplink ports */}
      <g fill="currentColor" stroke="none">
        <rect x="54" y="32" width="3" height="2.4" rx="0.4" />
      </g>
      {/* Status strip */}
      <rect x="6" y="38" width="52" height="2.5" rx="0.5" opacity="0.15" fill="currentColor" stroke="none" />
      <rect x="6" y="38" width="20" height="2.5" rx="0.5" fill="currentColor" stroke="none" opacity="0.7" />
      <text x="32" y="48" textAnchor="middle" fontSize="6" fill="currentColor" stroke="none" opacity="0.7" fontFamily="ui-monospace,Menlo,monospace">24p</text>
    </svg>
  )
}

export function FirewallIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Shield */}
      <path d="M32 4l22 8v16c0 14-10 24-22 30C20 52 10 42 10 28V12z" />
      {/* Brick pattern (firewall) */}
      <g opacity="0.85">
        <path d="M14 18h36M14 26h36M14 34h36M14 42h28" />
        <path d="M22 14v8M34 14v8M46 14v8M18 22v8M30 22v8M42 22v8M22 30v8M34 30v8M46 30v8M18 38v8M30 38v8" />
      </g>
      {/* Flame core */}
      <path d="M32 22c-3 4-3 8 0 12 3-2 4-6 0-12z" fill="currentColor" stroke="none" opacity="0.35" />
    </svg>
  )
}

export function ServerIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Rack frame */}
      <rect x="14" y="4" width="36" height="56" rx="2" />
      {/* 1U server slots — three of them */}
      <rect x="17" y="8" width="30" height="14" rx="1" />
      <rect x="17" y="25" width="30" height="14" rx="1" />
      <rect x="17" y="42" width="30" height="14" rx="1" />
      {/* Front-panel LEDs */}
      <g fill="currentColor" stroke="none">
        <circle cx="20" cy="15" r="1" />
        <circle cx="20" cy="32" r="1" />
        <circle cx="20" cy="49" r="1" opacity="0.5" />
      </g>
      {/* Drive bays */}
      <g opacity="0.85">
        <path d="M24 12h20M24 14h20M24 18h20" />
        <path d="M24 29h20M24 31h20M24 35h20" />
        <path d="M24 46h20M24 48h20M24 52h20" />
      </g>
      {/* Rack mount holes */}
      <g fill="currentColor" stroke="none" opacity="0.5">
        <circle cx="15.5" cy="14" r="0.7" />
        <circle cx="15.5" cy="32" r="0.7" />
        <circle cx="15.5" cy="49" r="0.7" />
        <circle cx="48.5" cy="14" r="0.7" />
        <circle cx="48.5" cy="32" r="0.7" />
        <circle cx="48.5" cy="49" r="0.7" />
      </g>
    </svg>
  )
}

export function DatabaseIcon(p: Props) {
  return (
    <svg {...base(p)}>
      <ellipse cx="32" cy="12" rx="22" ry="6" />
      <path d="M10 12v40c0 3.3 9.9 6 22 6s22-2.7 22-6V12" />
      <path d="M10 24c0 3.3 9.9 6 22 6s22-2.7 22-6" />
      <path d="M10 36c0 3.3 9.9 6 22 6s22-2.7 22-6" />
      <path d="M10 48c0 3.3 9.9 6 22 6s22-2.7 22-6" />
      {/* Activity LED */}
      <circle cx="48" cy="16" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function LoadBalancerIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Diamond */}
      <path d="M32 4L60 32 32 60 4 32z" />
      <path d="M32 12L52 32 32 52 12 32z" opacity="0.4" />
      {/* Distribution arrows */}
      <path d="M32 22v20" strokeWidth="2.4" />
      <path d="M32 22l-4 4M32 22l4 4" />
      <path d="M32 42l-4-4M32 42l4-4" />
      {/* Side arrows */}
      <path d="M20 32h-6M44 32h6" strokeWidth="2.4" />
      <path d="M14 32l3-2.5M14 32l3 2.5" />
      <path d="M50 32l-3-2.5M50 32l-3 2.5" />
    </svg>
  )
}

export function AccessPointIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Body (puck) */}
      <ellipse cx="32" cy="46" rx="22" ry="6" />
      <path d="M10 46v3c0 3.3 9.9 6 22 6s22-2.7 22-6v-3" />
      {/* Status LED on body */}
      <circle cx="32" cy="46" r="1.6" fill="currentColor" stroke="none" />
      {/* Wifi waves */}
      <path d="M14 32a20 20 0 0136 0" />
      <path d="M20 28a14 14 0 0124 0" />
      <path d="M26 25a8 8 0 0112 0" />
      <circle cx="32" cy="22" r="2" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function PrinterIcon(p: Props) {
  return (
    <svg {...base(p)}>
      <path d="M16 22V8h32v14" />
      <rect x="6" y="22" width="52" height="22" rx="2" />
      <rect x="16" y="34" width="32" height="20" rx="1.5" />
      {/* Paper lines */}
      <path d="M20 40h24M20 44h24M20 48h18" opacity="0.85" />
      {/* Toner / status */}
      <circle cx="50" cy="30" r="1.4" fill="currentColor" stroke="none" />
      <rect x="38" y="27" width="8" height="3" rx="0.5" opacity="0.4" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function StorageIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Chassis */}
      <rect x="8" y="6" width="48" height="52" rx="2" />
      {/* Drive bays — 4 rows */}
      <g>
        <rect x="11" y="10" width="42" height="9" rx="1" />
        <rect x="11" y="21" width="42" height="9" rx="1" />
        <rect x="11" y="32" width="42" height="9" rx="1" />
        <rect x="11" y="43" width="42" height="9" rx="1" />
      </g>
      {/* Drive indicators */}
      <g fill="currentColor" stroke="none">
        <circle cx="14.5" cy="14.5" r="0.9" />
        <circle cx="14.5" cy="25.5" r="0.9" />
        <circle cx="14.5" cy="36.5" r="0.9" />
        <circle cx="14.5" cy="47.5" r="0.9" opacity="0.5" />
      </g>
      <g opacity="0.75">
        <path d="M18 14.5h32M18 25.5h32M18 36.5h32M18 47.5h32" />
      </g>
    </svg>
  )
}

export function CloudIcon(p: Props) {
  return (
    <svg {...base(p)}>
      <path d="M18 46h30a10 10 0 002-19.8A12 12 0 0026 22a9 9 0 00-9.5 8.5A8 8 0 0018 46z" />
      {/* Internal lines suggesting compute */}
      <g opacity="0.4">
        <path d="M22 38h6M32 38h10M22 42h14M38 42h6" />
      </g>
    </svg>
  )
}

export function InternetIcon(p: Props) {
  return (
    <svg {...base(p)}>
      <circle cx="32" cy="32" r="26" />
      <ellipse cx="32" cy="32" rx="11" ry="26" />
      <ellipse cx="32" cy="32" rx="22" ry="11" opacity="0.5" />
      <path d="M6 32h52" />
    </svg>
  )
}

export function WorkstationIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Monitor */}
      <rect x="4" y="8" width="56" height="36" rx="2" />
      <rect x="8" y="12" width="48" height="26" rx="1" opacity="0.4" />
      {/* Stand */}
      <path d="M28 52l1-8h6l1 8" />
      <path d="M20 52h24" strokeWidth="2.4" />
      {/* Status LED */}
      <circle cx="32" cy="42" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function CameraIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Body */}
      <rect x="6" y="18" width="38" height="22" rx="2" />
      {/* Battery / status panel */}
      <rect x="9" y="36" width="6" height="2" rx="0.4" fill="currentColor" stroke="none" opacity="0.6" />
      {/* Lens */}
      <circle cx="20" cy="29" r="7" />
      <circle cx="20" cy="29" r="3.5" opacity="0.55" />
      <circle cx="20" cy="29" r="1.5" fill="currentColor" stroke="none" />
      {/* Flash / IR */}
      <circle cx="38" cy="22" r="1.2" fill="currentColor" stroke="none" />
      {/* Side cap */}
      <path d="M44 24l12-4v18l-12-4z" />
      {/* Mount */}
      <path d="M14 44v6M30 44v6" />
    </svg>
  )
}

export function GenericIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Chip-style outline */}
      <rect x="14" y="14" width="36" height="36" rx="3" />
      <rect x="22" y="22" width="20" height="20" rx="1.5" />
      {/* Pins */}
      <g opacity="0.85">
        <path d="M14 22h-4M14 32h-4M14 42h-4M50 22h4M50 32h4M50 42h4" />
        <path d="M22 14v-4M32 14v-4M42 14v-4M22 50v4M32 50v4M42 50v4" />
      </g>
      {/* Center dot */}
      <circle cx="32" cy="32" r="2" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function L3SwitchIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Chassis (multilayer / core switch) */}
      <rect x="4" y="30" width="56" height="20" rx="2" />
      <path d="M4 40h56" opacity="0.5" />
      <g fill="currentColor" stroke="none" opacity="0.85">
        {Array.from({ length: 10 }).map((_, i) => (
          <rect key={`u${i}`} x={7 + i * 4.4} y={33} width="2.6" height="2.4" rx="0.4" />
        ))}
        {Array.from({ length: 10 }).map((_, i) => (
          <rect key={`l${i}`} x={7 + i * 4.4} y={43} width="2.6" height="2.4" rx="0.4" />
        ))}
      </g>
      <circle cx="54" cy="35" r="1" fill="currentColor" stroke="none" />
      <circle cx="54" cy="45" r="1" fill="currentColor" stroke="none" opacity="0.6" />
      {/* Routing engine: crossed arrows in a disc */}
      <circle cx="32" cy="15" r="11" />
      <path d="M25 11h12M33 7.5l4 3.5-4 3.5" />
      <path d="M39 19H27M31 15.5L27 19l4 3.5" />
      <path d="M32 26v4" opacity="0.6" />
    </svg>
  )
}

export function WirelessControllerIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* 1U appliance */}
      <rect x="6" y="34" width="52" height="16" rx="2" />
      <g fill="currentColor" stroke="none" opacity="0.85">
        <rect x="10" y="40" width="3" height="2.4" rx="0.4" />
        <rect x="15" y="40" width="3" height="2.4" rx="0.4" />
        <rect x="20" y="40" width="3" height="2.4" rx="0.4" />
        <rect x="25" y="40" width="3" height="2.4" rx="0.4" />
      </g>
      <path d="M34 42h14" opacity="0.6" />
      <circle cx="52.5" cy="42" r="1.2" fill="currentColor" stroke="none" />
      {/* Managed APs: waves fanning out */}
      <path d="M16 26a22 22 0 0132 0" />
      <path d="M22 22a14 14 0 0120 0" opacity="0.8" />
      <path d="M28 18a6 6 0 018 0" opacity="0.7" />
      <path d="M32 34v-6" />
      <circle cx="32" cy="27" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function HypervisorIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Host */}
      <rect x="6" y="8" width="52" height="40" rx="2.5" />
      {/* Guest VMs */}
      <rect x="11" y="13" width="12" height="16" rx="1.2" />
      <rect x="26" y="13" width="12" height="16" rx="1.2" />
      <rect x="41" y="13" width="12" height="16" rx="1.2" />
      <g opacity="0.7">
        <path d="M14 18h6M14 21h6M29 18h6M29 21h6M44 18h6M44 21h6" />
      </g>
      {/* Hypervisor layer */}
      <rect x="11" y="33" width="42" height="9" rx="1.2" fill="currentColor" stroke="none" opacity="0.15" />
      <rect x="11" y="33" width="42" height="9" rx="1.2" />
      <circle cx="16" cy="37.5" r="1" fill="currentColor" stroke="none" />
      {/* Base */}
      <path d="M18 54h28M24 48v6M40 48v6" />
    </svg>
  )
}

export function UpsIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Tower UPS */}
      <rect x="14" y="6" width="36" height="52" rx="3" />
      {/* Display */}
      <rect x="19" y="11" width="26" height="9" rx="1" />
      <path d="M22 15.5h12" opacity="0.6" />
      <circle cx="40" cy="15.5" r="1.2" fill="currentColor" stroke="none" />
      {/* Lightning bolt */}
      <path d="M34 25l-8 13h7l-3 12 9-15h-7l2-10z" fill="currentColor" fillOpacity="0.2" />
      {/* Vent / outlets */}
      <g opacity="0.6">
        <path d="M19 53h26" />
      </g>
    </svg>
  )
}

export function PhoneIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Desk IP phone base */}
      <path d="M10 30h44l4 24H6z" />
      {/* Handset */}
      <path d="M8 22c0-6 10-10 24-10s24 4 24 10v3c0 1.5-1.2 2.5-2.6 2.2l-7-1.6c-1.4-.3-2.4-1.5-2.4-3V21c-3.6-1-8-1.4-12-1.4S23.6 20 20 21v1.6c0 1.5-1 2.7-2.4 3l-7 1.6C9.2 27.5 8 26.5 8 25z" />
      {/* Screen */}
      <rect x="14" y="34" width="14" height="8" rx="1" opacity="0.8" />
      {/* Keypad */}
      <g fill="currentColor" stroke="none" opacity="0.85">
        {[0, 1, 2].map((r) => [0, 1, 2].map((c) => (
          <rect key={`${r}${c}`} x={34 + c * 5.5} y={34 + r * 5.2} width="3.4" height="3" rx="0.6" />
        )))}
      </g>
      <path d="M14 47h14" opacity="0.6" />
    </svg>
  )
}

export function NvrIcon(p: Props) {
  return (
    <svg {...base(p)}>
      {/* Recorder chassis */}
      <rect x="4" y="30" width="56" height="22" rx="2" />
      {/* Drive bays */}
      <rect x="8" y="34" width="20" height="5" rx="0.8" />
      <rect x="8" y="42" width="20" height="5" rx="0.8" />
      {/* Record indicator */}
      <circle cx="48" cy="41" r="4.5" />
      <circle cx="48" cy="41" r="2" fill="currentColor" stroke="none" />
      <path d="M33 37h8M33 41h8M33 45h8" opacity="0.6" />
      {/* Camera feeding the recorder */}
      <rect x="18" y="8" width="22" height="12" rx="2" />
      <circle cx="26" cy="14" r="3.5" />
      <circle cx="26" cy="14" r="1.2" fill="currentColor" stroke="none" />
      <path d="M40 11l7-3v12l-7-3z" />
      <path d="M29 20v10" strokeDasharray="2 2.5" opacity="0.7" />
    </svg>
  )
}

export type IconKey =
  | 'router' | 'switch' | 'firewall' | 'server' | 'database'
  | 'load_balancer' | 'access_point' | 'printer' | 'storage'
  | 'cloud' | 'internet' | 'workstation' | 'camera' | 'other'
  | 'l3_switch' | 'wireless_controller' | 'hypervisor' | 'ups' | 'phone' | 'nvr'

export const networkIcons: Record<IconKey, (p: Props) => JSX.Element> = {
  router: RouterIcon,
  switch: SwitchIcon,
  firewall: FirewallIcon,
  server: ServerIcon,
  database: DatabaseIcon,
  load_balancer: LoadBalancerIcon,
  access_point: AccessPointIcon,
  printer: PrinterIcon,
  storage: StorageIcon,
  cloud: CloudIcon,
  internet: InternetIcon,
  workstation: WorkstationIcon,
  camera: CameraIcon,
  other: GenericIcon,
  l3_switch: L3SwitchIcon,
  wireless_controller: WirelessControllerIcon,
  hypervisor: HypervisorIcon,
  ups: UpsIcon,
  phone: PhoneIcon,
  nvr: NvrIcon,
}

export const iconLabel: Record<IconKey, string> = {
  router: 'Router',
  switch: 'Switch',
  firewall: 'Firewall',
  server: 'Server',
  database: 'Database',
  load_balancer: 'Load Balancer',
  access_point: 'Access Point',
  printer: 'Printer',
  storage: 'Storage',
  cloud: 'Cloud',
  internet: 'Internet',
  workstation: 'Workstation',
  camera: 'Camera',
  other: 'Generic',
  l3_switch: 'L3 / Core Switch',
  wireless_controller: 'Wireless Controller',
  hypervisor: 'Hypervisor',
  ups: 'UPS',
  phone: 'IP Phone',
  nvr: 'NVR',
}

export function NetworkIcon({ name, ...rest }: { name: string } & Props) {
  const key = (networkIcons[name as IconKey] ? name : 'other') as IconKey
  const Component = networkIcons[key]
  return <Component {...rest} />
}
