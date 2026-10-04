/**
 * IP address and prefix parsing for NetFlow filters.
 *
 * The rules follow Python's `ipaddress` module, which is what the API validates with, so a value
 * accepted here is accepted there (and an IPv4 octet with a leading zero, which Python rejects as
 * ambiguous octal, is rejected here too). Zone ids (`fe80::1%eth0`) are rejected: flow records carry
 * no scope.
 */

const HEX_GROUP = /^[0-9a-fA-F]{1,4}$/
const OCTET = /^(0|[1-9]\d{0,2})$/

export function isIPv4(value: string): boolean {
  const parts = value.split('.')
  if (parts.length !== 4) return false
  return parts.every((p) => OCTET.test(p) && Number(p) <= 255)
}

export function isIPv6(value: string): boolean {
  if (!value || value.length > 45 || !/^[0-9a-fA-F:.]+$/.test(value)) return false
  const lastColon = value.lastIndexOf(':')
  if (lastColon < 0) return false
  let head = value
  let groups = 8
  const tail = value.slice(lastColon + 1)
  if (tail.includes('.')) {
    // An embedded IPv4 tail (`::ffff:10.0.0.1`) stands for the last two groups.
    if (!isIPv4(tail)) return false
    groups = 6
    head = value.slice(0, lastColon + 1)
    if (!head.endsWith('::')) head = head.slice(0, -1)
  }
  const halves = head.split('::')
  if (halves.length > 2) return false
  const split = (s: string) => (s === '' ? [] : s.split(':'))
  if (halves.length === 2) {
    const all = [...split(halves[0]), ...split(halves[1])]
    // `::` must stand for at least one group.
    return all.every((g) => HEX_GROUP.test(g)) && all.length <= groups - 1
  }
  const all = split(head)
  return all.length === groups && all.every((g) => HEX_GROUP.test(g))
}

export function isIpAddress(value: string): boolean {
  return isIPv4(value) || isIPv6(value)
}

export interface ParsedPrefix {
  address: string
  /** Prefix length; `null` for a bare address. */
  prefix: number | null
  version: 4 | 6
}

/** An address or CIDR prefix (`10.0.0.5`, `10.0.0.0/8`, `2001:db8::/32`), or `null` when invalid. */
export function parseIpOrCidr(raw: string): ParsedPrefix | null {
  const value = raw.trim()
  if (!value) return null
  const slash = value.indexOf('/')
  const address = slash >= 0 ? value.slice(0, slash) : value
  const version: 4 | 6 | null = isIPv4(address) ? 4 : isIPv6(address) ? 6 : null
  if (version === null) return null
  if (slash < 0) return { address, prefix: null, version }
  const len = value.slice(slash + 1)
  if (!/^\d{1,3}$/.test(len)) return null
  const prefix = Number(len)
  if (prefix > (version === 4 ? 32 : 128)) return null
  return { address, prefix, version }
}

/**
 * Canonical text for an address or prefix typed by a user: trimmed, IPv6 hex lower-cased (so
 * `FE80::1` and `fe80::1` are one filter and one cache key). Invalid input is returned trimmed.
 */
export function normalizeIpText(raw: string): string {
  const value = raw.trim()
  const parsed = parseIpOrCidr(value)
  if (!parsed || parsed.version === 4) return value
  return value.toLowerCase()
}
