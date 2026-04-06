import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import {
  Search, Plus, Trash2, Radar, Loader2, CheckCircle, XCircle,
  Network, Wifi, Globe, ChevronDown, ChevronUp, X, Download,
  AlertTriangle, Server, ArrowRight,
} from 'lucide-react'

// ── Types ────────────────────────────────────────────────────────────────────

interface DiscoveredHost {
  ip: string
  rtt_ms: number | null
  hostname: string | null
  is_alive: boolean
}

interface ScanResult {
  subnet: string
  total_hosts: number
  alive_hosts: number
  hosts: DiscoveredHost[]
  scan_time_sec: number
}

interface ScanResponse {
  results: ScanResult[]
  total_scanned: number
  total_alive: number
}

// ── Discovery Page ───────────────────────────────────────────────────────────

export function DiscoveryPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // Scan config
  const [subnets, setSubnets] = useState<string[]>([''])
  const [timeout, setTimeout_] = useState(1.0)
  const [pingCount, setPingCount] = useState(2)

  // Results
  const [scanData, setScanData] = useState<ScanResponse | null>(null)
  const [selectedIPs, setSelectedIPs] = useState<Set<string>>(new Set())
  const [expandedSubnets, setExpandedSubnets] = useState<Set<string>>(new Set())
  const [showOnlyAlive, setShowOnlyAlive] = useState(true)

  // Bulk add config
  const [showBulkAdd, setShowBulkAdd] = useState(false)
  const [bulkDeviceType, setBulkDeviceType] = useState('other')
  const [bulkLocation, setBulkLocation] = useState('')
  const [bulkPingInterval, setBulkPingInterval] = useState(60)

  // ── Scan mutation ──
  const scanMutation = useMutation({
    mutationFn: (data: { subnets: string[]; timeout: number; count: number }) =>
      api.post<ScanResponse>('/discovery/scan', data),
    onSuccess: (data) => {
      setScanData(data)
      setSelectedIPs(new Set())
      // Auto-expand all subnets
      const expanded = new Set<string>()
      data.results.forEach(r => expanded.add(r.subnet))
      setExpandedSubnets(expanded)
    },
  })

  // ── Bulk import mutation ──
  const bulkImportMutation = useMutation({
    mutationFn: (devices: { hostname: string; ip_address: string; device_type: string; location?: string; ping_interval: number }[]) =>
      api.post<{ total: number; created: number; skipped: number; errors: string[] }>('/devices/bulk-import', { devices }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      alert(`Import complete: ${result.created} created, ${result.skipped} skipped`)
      setSelectedIPs(new Set())
      setShowBulkAdd(false)
    },
  })

  // ── Handlers ──
  const addSubnet = () => setSubnets([...subnets, ''])
  const removeSubnet = (i: number) => setSubnets(subnets.filter((_, idx) => idx !== i))
  const updateSubnet = (i: number, val: string) => {
    const updated = [...subnets]
    updated[i] = val
    setSubnets(updated)
  }

  const runScan = () => {
    const valid = subnets.filter(s => s.trim())
    if (valid.length === 0) return
    scanMutation.mutate({ subnets: valid, timeout, count: pingCount })
  }

  const toggleSubnet = (subnet: string) => {
    const next = new Set(expandedSubnets)
    next.has(subnet) ? next.delete(subnet) : next.add(subnet)
    setExpandedSubnets(next)
  }

  const toggleIP = (ip: string) => {
    const next = new Set(selectedIPs)
    next.has(ip) ? next.delete(ip) : next.add(ip)
    setSelectedIPs(next)
  }

  const selectAllAlive = useCallback(() => {
    if (!scanData) return
    const alive = new Set<string>()
    scanData.results.forEach(r => r.hosts.forEach(h => { if (h.is_alive) alive.add(h.ip) }))
    setSelectedIPs(alive)
  }, [scanData])

  const clearSelection = () => setSelectedIPs(new Set())

  const handleBulkAdd = () => {
    const devices = Array.from(selectedIPs).map(ip => {
      const host = scanData?.results.flatMap(r => r.hosts).find(h => h.ip === ip)
      return {
        hostname: host?.hostname || ip,
        ip_address: ip,
        device_type: bulkDeviceType,
        location: bulkLocation || undefined,
        ping_interval: bulkPingInterval,
      }
    })
    bulkImportMutation.mutate(devices)
  }

  const exportCSV = () => {
    if (!scanData) return
    const alive = scanData.results.flatMap(r => r.hosts.filter(h => h.is_alive))
    const csv = 'IP,Hostname,RTT (ms)\n' + alive.map(h => `${h.ip},${h.hostname || ''},${h.rtt_ms ?? ''}`).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `discovery-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  // ── Render ──
  return (
    <div className="min-h-screen w-full bg-[var(--bg-primary)] px-6 py-6">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)]">Network Discovery</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Scan subnets to discover live hosts via ICMP ping
          </p>
        </div>
        {scanData && scanData.total_alive > 0 && (
          <button onClick={exportCSV}
            className="flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:brightness-110"
            style={{ background: 'var(--bg-tertiary)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.06)' }}>
            <Download size={16} /> Export CSV
          </button>
        )}
      </div>

      {/* Scan Configuration */}
      <div className="mb-6 rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-6">
        <div className="flex items-center gap-2 mb-4">
          <Radar className="w-5 h-5 text-[var(--accent)]" />
          <h2 className="text-lg font-semibold text-[var(--text-primary)]">Scan Configuration</h2>
        </div>

        {/* Subnet inputs */}
        <div className="space-y-3 mb-5">
          <label className="text-sm font-medium text-[var(--text-secondary)]">Target Subnets</label>
          {subnets.map((subnet, i) => (
            <div key={i} className="flex items-center gap-2">
              <div className="relative flex-1">
                <Network className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
                <input
                  type="text"
                  value={subnet}
                  onChange={e => updateSubnet(i, e.target.value)}
                  placeholder="e.g. 192.168.1.0/24 or 10.0.0.0/24"
                  className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] pl-10 pr-4 py-2.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]"
                  onKeyDown={e => e.key === 'Enter' && runScan()}
                />
              </div>
              {subnets.length > 1 && (
                <button onClick={() => removeSubnet(i)}
                  className="p-2.5 rounded-lg hover:bg-red-500/10 text-[var(--text-muted)] hover:text-red-400 transition-colors">
                  <Trash2 size={16} />
                </button>
              )}
            </div>
          ))}
          <button onClick={addSubnet}
            className="flex items-center gap-1.5 text-xs text-[var(--accent)] hover:brightness-110 transition-all">
            <Plus size={14} /> Add another subnet
          </button>
        </div>

        {/* Advanced options */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-5">
          <div>
            <label className="text-xs font-medium text-[var(--text-muted)] mb-1 block">Timeout (sec)</label>
            <input type="number" min={0.2} max={5} step={0.1} value={timeout}
              onChange={e => setTimeout_(parseFloat(e.target.value) || 1)}
              className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
          </div>
          <div>
            <label className="text-xs font-medium text-[var(--text-muted)] mb-1 block">Ping Count</label>
            <input type="number" min={1} max={5} value={pingCount}
              onChange={e => setPingCount(parseInt(e.target.value) || 2)}
              className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
          </div>
          <div className="flex items-end">
            <button onClick={runScan} disabled={scanMutation.isPending || !subnets.some(s => s.trim())}
              className={cn(
                "w-full flex items-center justify-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold text-white transition-all",
                scanMutation.isPending ? "opacity-60 cursor-not-allowed" : "hover:brightness-110"
              )}
              style={{ background: 'var(--accent)' }}>
              {scanMutation.isPending
                ? <><Loader2 size={16} className="animate-spin" /> Scanning...</>
                : <><Radar size={16} /> Start Scan</>}
            </button>
          </div>
        </div>

        {scanMutation.isError && (
          <div className="flex items-center gap-2 text-sm text-red-400 bg-red-500/10 rounded-lg px-4 py-2.5">
            <AlertTriangle size={16} /> {(scanMutation.error as Error).message}
          </div>
        )}
      </div>

      {/* Scan Progress / Results */}
      {scanMutation.isPending && (
        <div className="mb-6 rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-8 text-center">
          <Loader2 className="w-10 h-10 animate-spin text-[var(--accent)] mx-auto mb-3" />
          <p className="text-sm text-[var(--text-secondary)]">Scanning network ranges...</p>
          <p className="text-xs text-[var(--text-muted)] mt-1">This may take a moment depending on subnet size</p>
        </div>
      )}

      {/* Results */}
      {scanData && !scanMutation.isPending && (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-4 mb-6">
            <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-4">
              <div className="flex items-center gap-2 text-[var(--text-muted)] text-xs mb-1"><Globe size={14} /> Total Scanned</div>
              <div className="text-2xl font-bold text-[var(--text-primary)]">{scanData.total_scanned}</div>
            </div>
            <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-4">
              <div className="flex items-center gap-2 text-green-400 text-xs mb-1"><CheckCircle size={14} /> Alive Hosts</div>
              <div className="text-2xl font-bold text-green-400">{scanData.total_alive}</div>
            </div>
            <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-4">
              <div className="flex items-center gap-2 text-red-400 text-xs mb-1"><XCircle size={14} /> Unreachable</div>
              <div className="text-2xl font-bold text-red-400">{scanData.total_scanned - scanData.total_alive}</div>
            </div>
            <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-4">
              <div className="flex items-center gap-2 text-[var(--text-muted)] text-xs mb-1"><Search size={14} /> Subnets Scanned</div>
              <div className="text-2xl font-bold text-[var(--text-primary)]">{scanData.results.length}</div>
            </div>
          </div>

          {/* Selection Toolbar */}
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <button onClick={selectAllAlive}
              className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium bg-green-500/10 text-green-400 hover:bg-green-500/20 transition-colors">
              <CheckCircle size={14} /> Select All Alive ({scanData.total_alive})
            </button>
            {selectedIPs.size > 0 && (
              <>
                <button onClick={clearSelection}
                  className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors">
                  <X size={14} /> Clear ({selectedIPs.size})
                </button>
                <button onClick={() => setShowBulkAdd(true)}
                  className="flex items-center gap-2 rounded-lg px-4 py-2 text-xs font-semibold text-white transition-all hover:brightness-110"
                  style={{ background: 'var(--accent)' }}>
                  <Plus size={14} /> Add {selectedIPs.size} to Devices
                </button>
              </>
            )}
            <div className="ml-auto flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-[var(--text-muted)] cursor-pointer">
                <input type="checkbox" checked={showOnlyAlive} onChange={e => setShowOnlyAlive(e.target.checked)}
                  className="rounded border-[var(--bg-elevated)] accent-[var(--accent)]" />
                Show alive only
              </label>
            </div>
          </div>

          {/* Subnet Results */}
          {scanData.results.map(result => {
            const isExpanded = expandedSubnets.has(result.subnet)
            const displayHosts = showOnlyAlive
              ? result.hosts.filter(h => h.is_alive)
              : result.hosts

            return (
              <div key={result.subnet} className="mb-4 rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] overflow-hidden">
                {/* Subnet header */}
                <button onClick={() => toggleSubnet(result.subnet)}
                  className="w-full flex items-center justify-between px-5 py-4 hover:bg-[var(--bg-tertiary)] transition-colors">
                  <div className="flex items-center gap-3">
                    <Network className="w-5 h-5 text-[var(--accent)]" />
                    <span className="font-semibold text-[var(--text-primary)]">{result.subnet}</span>
                    <span className="text-xs px-2 py-0.5 rounded-full bg-green-500/10 text-green-400 font-medium">
                      {result.alive_hosts} alive
                    </span>
                    <span className="text-xs text-[var(--text-muted)]">
                      / {result.total_hosts} hosts &middot; {result.scan_time_sec}s
                    </span>
                  </div>
                  {isExpanded ? <ChevronUp size={18} className="text-[var(--text-muted)]" /> : <ChevronDown size={18} className="text-[var(--text-muted)]" />}
                </button>

                {/* Host table */}
                {isExpanded && (
                  <div className="border-t border-[var(--bg-elevated)]">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-[var(--bg-tertiary)]">
                          <th className="w-10 px-4 py-2.5">
                            <input type="checkbox"
                              checked={displayHosts.filter(h => h.is_alive).every(h => selectedIPs.has(h.ip)) && displayHosts.some(h => h.is_alive)}
                              onChange={() => {
                                const aliveInSubnet = displayHosts.filter(h => h.is_alive).map(h => h.ip)
                                const allSelected = aliveInSubnet.every(ip => selectedIPs.has(ip))
                                const next = new Set(selectedIPs)
                                aliveInSubnet.forEach(ip => allSelected ? next.delete(ip) : next.add(ip))
                                setSelectedIPs(next)
                              }}
                              className="rounded accent-[var(--accent)]" />
                          </th>
                          <th className="text-left px-4 py-2.5 font-semibold text-[var(--text-muted)] text-xs uppercase tracking-wider">Status</th>
                          <th className="text-left px-4 py-2.5 font-semibold text-[var(--text-muted)] text-xs uppercase tracking-wider">IP Address</th>
                          <th className="text-left px-4 py-2.5 font-semibold text-[var(--text-muted)] text-xs uppercase tracking-wider">Hostname</th>
                          <th className="text-right px-4 py-2.5 font-semibold text-[var(--text-muted)] text-xs uppercase tracking-wider">RTT</th>
                        </tr>
                      </thead>
                      <tbody>
                        {displayHosts.map(host => (
                          <tr key={host.ip}
                            className={cn(
                              "border-t border-[var(--bg-elevated)]/40 transition-colors",
                              host.is_alive ? "hover:bg-[var(--bg-tertiary)]/50" : "opacity-40",
                              selectedIPs.has(host.ip) && "bg-[var(--accent)]/5"
                            )}>
                            <td className="px-4 py-2.5">
                              {host.is_alive && (
                                <input type="checkbox" checked={selectedIPs.has(host.ip)}
                                  onChange={() => toggleIP(host.ip)}
                                  className="rounded accent-[var(--accent)]" />
                              )}
                            </td>
                            <td className="px-4 py-2.5">
                              {host.is_alive
                                ? <span className="flex items-center gap-1.5 text-green-400 text-xs font-medium"><span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" /> Alive</span>
                                : <span className="flex items-center gap-1.5 text-[var(--text-muted)] text-xs"><span className="w-2 h-2 rounded-full bg-gray-500" /> Down</span>}
                            </td>
                            <td className="px-4 py-2.5 font-mono text-[var(--text-primary)]">{host.ip}</td>
                            <td className="px-4 py-2.5 text-[var(--text-secondary)]">{host.hostname || '—'}</td>
                            <td className="px-4 py-2.5 text-right font-mono text-[var(--text-secondary)]">
                              {host.rtt_ms !== null ? `${host.rtt_ms}ms` : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}

      {/* Empty State */}
      {!scanData && !scanMutation.isPending && (
        <div className="rounded-xl border border-dashed border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-16 text-center">
          <Radar className="w-16 h-16 text-[var(--text-muted)] mx-auto mb-4 opacity-30" />
          <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-2">No Scans Yet</h3>
          <p className="text-sm text-[var(--text-muted)] max-w-md mx-auto">
            Enter one or more subnet ranges above and click "Start Scan" to discover live hosts on your network via ICMP ping.
          </p>
        </div>
      )}

      {/* Bulk Add Modal */}
      {showBulkAdd && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-6 shadow-2xl">
            <div className="flex items-center justify-between mb-5">
              <h3 className="text-lg font-semibold text-[var(--text-primary)] flex items-center gap-2">
                <Server size={20} className="text-[var(--accent)]" />
                Add {selectedIPs.size} Devices
              </h3>
              <button onClick={() => setShowBulkAdd(false)} className="p-1 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
                <X size={18} className="text-[var(--text-muted)]" />
              </button>
            </div>

            <div className="space-y-4 mb-6">
              <div>
                <label className="text-xs font-medium text-[var(--text-muted)] mb-1 block">Device Type</label>
                <select value={bulkDeviceType} onChange={e => setBulkDeviceType(e.target.value)}
                  className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-3 py-2.5 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]">
                  <option value="other">Other</option>
                  <option value="router">Router</option>
                  <option value="switch">Switch</option>
                  <option value="firewall">Firewall</option>
                  <option value="server">Server</option>
                  <option value="access_point">Access Point</option>
                  <option value="printer">Printer</option>
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-[var(--text-muted)] mb-1 block">Location (optional)</label>
                <input type="text" value={bulkLocation} onChange={e => setBulkLocation(e.target.value)}
                  placeholder="e.g. Main Office"
                  className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-3 py-2.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]" />
              </div>
              <div>
                <label className="text-xs font-medium text-[var(--text-muted)] mb-1 block">Ping Interval (seconds)</label>
                <input type="number" min={10} max={3600} value={bulkPingInterval}
                  onChange={e => setBulkPingInterval(parseInt(e.target.value) || 60)}
                  className="w-full rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-3 py-2.5 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
              </div>

              <div className="bg-[var(--bg-tertiary)] rounded-lg p-3 max-h-32 overflow-y-auto">
                <div className="text-xs text-[var(--text-muted)] mb-1.5">{selectedIPs.size} IPs selected:</div>
                <div className="flex flex-wrap gap-1.5">
                  {Array.from(selectedIPs).sort().map(ip => (
                    <span key={ip} className="text-xs font-mono bg-[var(--bg-elevated)] text-[var(--text-secondary)] px-2 py-0.5 rounded">{ip}</span>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex gap-3">
              <button onClick={() => setShowBulkAdd(false)}
                className="flex-1 rounded-lg px-4 py-2.5 text-sm font-medium border border-[var(--bg-elevated)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors">
                Cancel
              </button>
              <button onClick={handleBulkAdd} disabled={bulkImportMutation.isPending}
                className="flex-1 flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110"
                style={{ background: 'var(--accent)' }}>
                {bulkImportMutation.isPending
                  ? <><Loader2 size={16} className="animate-spin" /> Importing...</>
                  : <><Plus size={16} /> Add to Devices</>}
              </button>
            </div>

            {bulkImportMutation.isError && (
              <div className="mt-3 text-sm text-red-400 bg-red-500/10 rounded-lg px-4 py-2">
                {(bulkImportMutation.error as Error).message}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
