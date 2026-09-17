type Row = { label: string; instance: string; cells: Record<string, { status: string; text: string }> }
const severity: Record<string, number> = { none: 0, info: 1, ok: 2, warn: 3, crit: 4 }
export function insightRowSeverity(row: Row): number {
  return Math.max(0, ...Object.values(row.cells).map(cell => severity[cell.status] || 0))
}
export function filterInsightRows<T extends Row>(rows: T[], search: string, attentionOnly: boolean): T[] {
  const query = search.trim().toLocaleLowerCase()
  return rows.filter(row => (!attentionOnly || insightRowSeverity(row) >= 3) &&
    (!query || [row.label, readableInstanceLabel(row.label), row.instance, ...Object.values(row.cells).map(cell => cell.text)].join(' ').toLocaleLowerCase().includes(query)))
    .sort((a, b) => insightRowSeverity(b) - insightRowSeverity(a) || a.label.localeCompare(b.label, undefined, { numeric: true }))
}

// Some SNMP table indexes contain length-prefixed ASCII names. Decode only
// complete, printable sequences; retain the original instance in the UI title.
export function readableInstanceLabel(label: string): string {
  if (!/^\d+(?:\.\d+){7,}$/.test(label)) return label
  const values = label.split('.').map(Number)
  for (const start of [0, 1]) {
    const names: string[] = []
    let index = start
    let valid = true
    while (index < values.length) {
      const length = values[index++]
      const bytes = values.slice(index, index + length)
      if (!length || bytes.length !== length || bytes.some(value => value < 32 || value > 126)) { valid = false; break }
      names.push(String.fromCharCode(...bytes))
      index += length
    }
    if (valid && index === values.length && names.length && names.some(name => /[A-Za-z]/.test(name))) {
      return [...(start ? [`${values[0]}`] : []), ...names].join(' · ')
    }
  }
  return label
}

// Relative API aggregations use the appliance clock. Custom windows keep their
// explicit boundaries; a skewed operator clock must not move a preset chart.
export function alignDeviceRange<T extends { hours: number; fromISO: string; toISO: string; isCustom: boolean }>(range: T, serverTime: number | null): T {
  if (range.isCustom || serverTime == null || !Number.isFinite(serverTime)) return range
  return { ...range, fromISO: new Date(serverTime - range.hours * 3600000).toISOString(), toISO: new Date(serverTime).toISOString() }
}
