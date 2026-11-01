const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })

export function nextListSort(current: string, order: string, column: string): { sort: string; order: 'asc' | 'desc' } {
  return { sort: column, order: current === column && order === 'asc' ? 'desc' : 'asc' }
}

export function isIpQuery(query: string): boolean {
  const q = query.trim()
  return /^[0-9]{1,3}(?:\.[0-9]{0,3}){1,3}\.?$/.test(q) || (q.includes(':') && /^[0-9a-f:]+$/i.test(q))
}

export function compareListValues(a: string | number | null | undefined, b: string | number | null | undefined, direction = 1, natural = true): number {
  if (a == null) return b == null ? 0 : 1
  if (b == null) return -1
  const result = typeof a === 'number' && typeof b === 'number' ? a - b : natural ? collator.compare(String(a), String(b)) : a === b ? 0 : a < b ? -1 : 1
  return result * direction
}

export function searchRank(query: string, values: (string | null | undefined)[]): number {
  const q = query.trim().toLowerCase()
  if (!q) return 0
  const terms = values.map(v => (v || '').toLowerCase())
  if (terms.some(v => v === q)) return 0
  return terms.some(v => v.startsWith(q)) ? 1 : 2
}

function ipParts(value: string): number[] | null {
  const v4 = (v: string) => /^\d+\.\d+\.\d+\.\d+$/.test(v) && v.split('.').every(n => Number(n) <= 255)
    ? v.split('.').map(Number) : null
  const direct = v4(value)
  if (direct) return [4, ...direct]
  let source = value.toLowerCase()
  if (source.includes('.')) {
    const split = source.lastIndexOf(':')
    const tail = v4(source.slice(split + 1))
    if (!tail) return null
    source = source.slice(0, split + 1) + (tail[0] * 256 + tail[1]).toString(16) + ':' + (tail[2] * 256 + tail[3]).toString(16)
  }
  const halves = source.split('::')
  if (halves.length > 2 || !source.includes(':')) return null
  const left = halves[0] ? halves[0].split(':') : []
  const right = halves.length === 2 && halves[1] ? halves[1].split(':') : []
  if (![...left, ...right].every(s => /^[0-9a-f]{1,4}$/.test(s))) return null
  const missing = 8 - left.length - right.length
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null
  return [6, ...left.map(s => parseInt(s, 16)), ...Array(missing).fill(0), ...right.map(s => parseInt(s, 16))]
}

export function compareIpAddresses(a: string | null | undefined, b: string | null | undefined, direction = 1): number {
  const left = a ? ipParts(a) : null
  const right = b ? ipParts(b) : null
  if (!left) return right ? 1 : compareListValues(a, b, direction)
  if (!right) return -1
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const delta = (left[i] || 0) - (right[i] || 0)
    if (delta) return delta * direction
  }
  return 0
}
