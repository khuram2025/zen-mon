import { useState, useRef, type FormEvent, type ChangeEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { DeviceGroup } from '@/types'
import {
  ArrowLeft,
  Plus,
  Upload,
  Download,
  FileSpreadsheet,
  FileJson,
  CheckCircle,
  AlertCircle,
  X,
  Monitor,
  Server,
  Shield,
  Wifi,
  Radio,
  Printer,
  HelpCircle,
  Copy,
  Info,
} from 'lucide-react'
import { cn } from '@/lib/utils'

type Tab = 'single' | 'import' | 'export'

const deviceTypes = [
  { value: 'router', label: 'Router', icon: Radio },
  { value: 'switch', label: 'Switch', icon: Monitor },
  { value: 'firewall', label: 'Firewall', icon: Shield },
  { value: 'server', label: 'Server', icon: Server },
  { value: 'access_point', label: 'Access Point', icon: Wifi },
  { value: 'printer', label: 'Printer', icon: Printer },
  { value: 'other', label: 'Other', icon: HelpCircle },
]

const pingIntervals = [
  { value: 15, label: '15s' },
  { value: 30, label: '30s' },
  { value: 60, label: '1 min' },
  { value: 120, label: '2 min' },
  { value: 300, label: '5 min' },
  { value: 600, label: '10 min' },
]

// ─── Single Device Form ───
function SingleDeviceForm() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState('')

  const [form, setForm] = useState({
    hostname: '',
    ip_address: '',
    device_type: 'other',
    location: '',
    group_id: '',
    ping_enabled: true,
    ping_interval: 60,
    description: '',
    tags: '',
  })

  const { data: groups } = useQuery({
    queryKey: ['device-groups'],
    queryFn: () => api.get<DeviceGroup[]>('/devices/groups'),
  })

  const mutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.post('/devices', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      queryClient.invalidateQueries({ queryKey: ['device-summary'] })
      setSuccess(true)
      setError('')
      setForm({
        hostname: '', ip_address: '', device_type: 'other', location: '',
        group_id: '', ping_enabled: true, ping_interval: 60, description: '', tags: '',
      })
      setTimeout(() => setSuccess(false), 4000)
    },
    onError: (err: Error) => {
      setError(err.message)
      setSuccess(false)
    },
  })

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    setError('')
    mutation.mutate({
      hostname: form.hostname,
      ip_address: form.ip_address,
      device_type: form.device_type,
      location: form.location || null,
      group_id: form.group_id || null,
      ping_enabled: form.ping_enabled,
      ping_interval: form.ping_interval,
      description: form.description || null,
      tags: form.tags ? form.tags.split(',').map(t => t.trim()).filter(Boolean) : [],
    })
  }

  const update = (field: string, value: unknown) => setForm(f => ({ ...f, [field]: value }))

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Success / Error banners */}
      {success && (
        <div className="flex items-center gap-3 p-4 bg-green-500/10 border border-green-500/20 rounded-xl text-green-400 text-sm">
          <CheckCircle className="w-5 h-5 flex-shrink-0" />
          <span>Device added successfully! You can add another or go to the <button type="button" onClick={() => navigate('/devices')} className="underline font-medium">devices list</button>.</span>
        </div>
      )}
      {error && (
        <div className="flex items-center gap-3 p-4 bg-red-500/10 border border-red-500/20 rounded-xl text-red-400 text-sm">
          <AlertCircle className="w-5 h-5 flex-shrink-0" />
          <span>{error}</span>
          <button type="button" onClick={() => setError('')} className="ml-auto"><X className="w-4 h-4" /></button>
        </div>
      )}

      {/* Basic Info */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4 flex items-center gap-2">
          <Info className="w-4 h-4 text-[var(--accent)]" />
          Basic Information
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Hostname *</label>
            <input
              type="text"
              value={form.hostname}
              onChange={e => update('hostname', e.target.value)}
              placeholder="e.g. core-router-01"
              required
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm placeholder:text-[var(--text-muted)]/50"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">IP Address *</label>
            <input
              type="text"
              value={form.ip_address}
              onChange={e => update('ip_address', e.target.value)}
              placeholder="e.g. 192.168.1.1"
              required
              pattern="^(\d{1,3}\.){3}\d{1,3}$"
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm font-mono placeholder:text-[var(--text-muted)]/50"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Location</label>
            <input
              type="text"
              value={form.location}
              onChange={e => update('location', e.target.value)}
              placeholder="e.g. DC-1 Rack A3"
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm placeholder:text-[var(--text-muted)]/50"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Group</label>
            <select
              value={form.group_id}
              onChange={e => update('group_id', e.target.value)}
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm"
            >
              <option value="">No Group</option>
              {(groups || []).map(g => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Description</label>
            <input
              type="text"
              value={form.description}
              onChange={e => update('description', e.target.value)}
              placeholder="Optional description..."
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm placeholder:text-[var(--text-muted)]/50"
            />
          </div>
        </div>
      </div>

      {/* Device Type */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Device Type</h3>
        <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-7 gap-2">
          {deviceTypes.map(dt => {
            const Icon = dt.icon
            const selected = form.device_type === dt.value
            return (
              <button
                key={dt.value}
                type="button"
                onClick={() => update('device_type', dt.value)}
                className={cn(
                  'flex flex-col items-center gap-1.5 p-3 rounded-xl border text-xs font-medium transition-all',
                  selected
                    ? 'bg-[var(--accent)]/10 border-[var(--accent)] text-[var(--accent)]'
                    : 'bg-[var(--bg-tertiary)] border-[var(--bg-elevated)] text-[var(--text-muted)] hover:border-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                )}
              >
                <Icon className="w-5 h-5" />
                {dt.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* Monitoring Config */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Monitoring Configuration</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Ping Interval</label>
            <div className="flex gap-1.5">
              {pingIntervals.map(pi => (
                <button
                  key={pi.value}
                  type="button"
                  onClick={() => update('ping_interval', pi.value)}
                  className={cn(
                    'flex-1 py-2 rounded-lg text-xs font-medium transition-colors',
                    form.ping_interval === pi.value
                      ? 'bg-[var(--accent)] text-white'
                      : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                  )}
                >
                  {pi.label}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Ping Monitoring</label>
            <button
              type="button"
              onClick={() => update('ping_enabled', !form.ping_enabled)}
              className={cn(
                'w-full py-2.5 rounded-lg text-sm font-medium transition-colors border',
                form.ping_enabled
                  ? 'bg-green-500/10 border-green-500/30 text-green-400'
                  : 'bg-[var(--bg-tertiary)] border-[var(--bg-elevated)] text-[var(--text-muted)]'
              )}
            >
              {form.ping_enabled ? 'Ping Enabled' : 'Ping Disabled'}
            </button>
          </div>

          <div className="md:col-span-2">
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Tags (comma separated)</label>
            <input
              type="text"
              value={form.tags}
              onChange={e => update('tags', e.target.value)}
              placeholder="e.g. critical, floor-1, building-a"
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm placeholder:text-[var(--text-muted)]/50"
            />
          </div>
        </div>
      </div>

      {/* Submit */}
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={mutation.isPending}
          className="flex items-center gap-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white px-6 py-2.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
        >
          <Plus className="w-4 h-4" />
          {mutation.isPending ? 'Adding...' : 'Add Device'}
        </button>
        <button
          type="button"
          onClick={() => navigate('/devices')}
          className="px-6 py-2.5 rounded-lg text-sm font-medium text-[var(--text-muted)] hover:text-[var(--text-primary)] bg-[var(--bg-tertiary)] transition-colors"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}


// ─── Import Devices ───
function ImportDevices() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [importData, setImportData] = useState<Record<string, unknown>[] | null>(null)
  const [fileName, setFileName] = useState('')
  const [parseError, setParseError] = useState('')
  const [result, setResult] = useState<{ total: number; created: number; skipped: number; errors: string[] } | null>(null)

  const mutation = useMutation({
    mutationFn: (devices: Record<string, unknown>[]) => api.post<{ total: number; created: number; skipped: number; errors: string[] }>('/devices/bulk-import', { devices }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      queryClient.invalidateQueries({ queryKey: ['device-summary'] })
      setResult(data)
      setImportData(null)
    },
    onError: (err: Error) => setParseError(err.message),
  })

  const handleFileSelect = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setFileName(file.name)
    setParseError('')
    setResult(null)

    const reader = new FileReader()
    reader.onload = (ev) => {
      try {
        const text = ev.target?.result as string
        let data: Record<string, unknown>[]

        if (file.name.endsWith('.json')) {
          const parsed = JSON.parse(text)
          data = Array.isArray(parsed) ? parsed : parsed.devices || []
        } else {
          // CSV parsing
          const lines = text.trim().split('\n')
          const headers = lines[0]!.split(',').map(h => h.trim().toLowerCase().replace(/\s+/g, '_'))
          data = lines.slice(1).filter(l => l.trim()).map(line => {
            const values = line.split(',').map(v => v.trim())
            const obj: Record<string, unknown> = {}
            headers.forEach((h, i) => {
              if (h === 'ping_enabled') obj[h] = values[i]?.toLowerCase() !== 'false'
              else if (h === 'ping_interval') obj[h] = parseInt(values[i] || '60') || 60
              else if (h === 'tags') obj[h] = values[i] ? values[i]!.split(';').map(t => t.trim()) : []
              else obj[h] = values[i] || ''
            })
            return obj
          })
        }

        if (data.length === 0) {
          setParseError('No devices found in file')
          return
        }
        setImportData(data)
      } catch (err) {
        setParseError(`Failed to parse file: ${err instanceof Error ? err.message : 'Unknown error'}`)
      }
    }
    reader.readAsText(file)
  }

  const csvTemplate = `hostname,ip_address,device_type,location,group_name,ping_enabled,ping_interval,description,tags
core-router-01,10.0.0.1,router,DC-1 Rack A1,Core Network,true,30,Primary core router,critical;core
web-server-01,10.0.10.1,server,DC-1 Rack C1,Servers,true,60,Production web server,production;web`

  const jsonTemplate = JSON.stringify({
    devices: [
      { hostname: "core-router-01", ip_address: "10.0.0.1", device_type: "router", location: "DC-1 Rack A1", group_name: "Core Network", ping_enabled: true, ping_interval: 30, description: "Primary core router", tags: ["critical", "core"] },
      { hostname: "web-server-01", ip_address: "10.0.10.1", device_type: "server", location: "DC-1 Rack C1", group_name: "Servers", ping_enabled: true, ping_interval: 60, description: "Production web server", tags: ["production", "web"] },
    ]
  }, null, 2)

  const downloadTemplate = (format: 'csv' | 'json') => {
    const content = format === 'csv' ? csvTemplate : jsonTemplate
    const blob = new Blob([content], { type: format === 'csv' ? 'text/csv' : 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `zenplus-import-template.${format}`
    a.click()
    URL.revokeObjectURL(url)
  }

  const copyTemplate = (format: 'csv' | 'json') => {
    navigator.clipboard.writeText(format === 'csv' ? csvTemplate : jsonTemplate)
  }

  return (
    <div className="space-y-6">
      {/* Result banner */}
      {result && (
        <div className={cn(
          'p-4 rounded-xl border text-sm',
          result.created > 0
            ? 'bg-green-500/10 border-green-500/20 text-green-400'
            : 'bg-yellow-500/10 border-yellow-500/20 text-yellow-400'
        )}>
          <div className="flex items-center gap-2 mb-2">
            <CheckCircle className="w-5 h-5" />
            <span className="font-semibold">Import Complete</span>
          </div>
          <div className="grid grid-cols-3 gap-4 mb-2">
            <div>Total: <span className="font-mono font-semibold">{result.total}</span></div>
            <div>Created: <span className="font-mono font-semibold text-green-400">{result.created}</span></div>
            <div>Skipped: <span className="font-mono font-semibold text-yellow-400">{result.skipped}</span></div>
          </div>
          {result.errors.length > 0 && (
            <div className="mt-2 space-y-1 text-xs text-[var(--text-muted)]">
              {result.errors.map((err, i) => <div key={i}>{err}</div>)}
            </div>
          )}
          <button onClick={() => navigate('/devices')} className="mt-3 text-xs underline">View all devices</button>
        </div>
      )}

      {/* Templates */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-1">Download Template</h3>
        <p className="text-xs text-[var(--text-muted)] mb-4">Start with a template to ensure your data is in the correct format.</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {/* CSV template */}
          <div className="bg-[var(--bg-tertiary)] rounded-lg p-4 border border-[var(--bg-elevated)]">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-10 h-10 rounded-lg bg-green-500/10 flex items-center justify-center">
                <FileSpreadsheet className="w-5 h-5 text-green-400" />
              </div>
              <div>
                <div className="text-sm font-medium text-[var(--text-primary)]">CSV Template</div>
                <div className="text-xs text-[var(--text-muted)]">Comma-separated values</div>
              </div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => downloadTemplate('csv')} className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                <Download className="w-3.5 h-3.5" /> Download
              </button>
              <button onClick={() => copyTemplate('csv')} className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                <Copy className="w-3.5 h-3.5" /> Copy
              </button>
            </div>
          </div>

          {/* JSON template */}
          <div className="bg-[var(--bg-tertiary)] rounded-lg p-4 border border-[var(--bg-elevated)]">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-10 h-10 rounded-lg bg-blue-500/10 flex items-center justify-center">
                <FileJson className="w-5 h-5 text-blue-400" />
              </div>
              <div>
                <div className="text-sm font-medium text-[var(--text-primary)]">JSON Template</div>
                <div className="text-xs text-[var(--text-muted)]">Structured JSON format</div>
              </div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => downloadTemplate('json')} className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                <Download className="w-3.5 h-3.5" /> Download
              </button>
              <button onClick={() => copyTemplate('json')} className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                <Copy className="w-3.5 h-3.5" /> Copy
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Upload Area */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Upload File</h3>
        <input ref={fileInputRef} type="file" accept=".csv,.json" onChange={handleFileSelect} className="hidden" />

        {parseError && (
          <div className="flex items-center gap-2 p-3 mb-4 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            {parseError}
          </div>
        )}

        {!importData ? (
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="w-full py-12 border-2 border-dashed border-[var(--bg-elevated)] rounded-xl hover:border-[var(--accent)] transition-colors flex flex-col items-center gap-3 group"
          >
            <div className="w-14 h-14 rounded-2xl bg-[var(--accent)]/10 flex items-center justify-center group-hover:bg-[var(--accent)]/20 transition-colors">
              <Upload className="w-7 h-7 text-[var(--accent)]" />
            </div>
            <div>
              <div className="text-sm font-medium text-[var(--text-primary)]">Click to upload CSV or JSON</div>
              <div className="text-xs text-[var(--text-muted)] mt-0.5">Supports .csv and .json files</div>
            </div>
          </button>
        ) : (
          <div>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <FileSpreadsheet className="w-4 h-4 text-[var(--accent)]" />
                <span className="text-sm text-[var(--text-primary)] font-medium">{fileName}</span>
                <span className="text-xs text-[var(--text-muted)]">({importData.length} devices)</span>
              </div>
              <button onClick={() => { setImportData(null); setFileName('') }} className="text-xs text-[var(--text-muted)] hover:text-red-400 transition-colors">
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Preview table */}
            <div className="overflow-x-auto rounded-lg border border-[var(--bg-elevated)] mb-4 max-h-64 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="bg-[var(--bg-tertiary)] sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">#</th>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">Hostname</th>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">IP Address</th>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">Type</th>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">Location</th>
                    <th className="text-left px-3 py-2 text-[var(--text-muted)] font-semibold">Group</th>
                  </tr>
                </thead>
                <tbody>
                  {importData.slice(0, 50).map((row, i) => (
                    <tr key={i} className="border-t border-[var(--bg-elevated)]/50">
                      <td className="px-3 py-2 text-[var(--text-muted)]">{i + 1}</td>
                      <td className="px-3 py-2 text-[var(--text-primary)]">{String(row.hostname || '')}</td>
                      <td className="px-3 py-2 text-[var(--text-secondary)] font-mono">{String(row.ip_address || '')}</td>
                      <td className="px-3 py-2 text-[var(--text-secondary)] capitalize">{String(row.device_type || 'other')}</td>
                      <td className="px-3 py-2 text-[var(--text-secondary)]">{String(row.location || '-')}</td>
                      <td className="px-3 py-2 text-[var(--text-secondary)]">{String(row.group_name || '-')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {importData.length > 50 && (
                <div className="text-center py-2 text-xs text-[var(--text-muted)] bg-[var(--bg-tertiary)]">
                  ...and {importData.length - 50} more devices
                </div>
              )}
            </div>

            <div className="flex gap-3">
              <button
                onClick={() => mutation.mutate(importData)}
                disabled={mutation.isPending}
                className="flex items-center gap-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white px-5 py-2.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                <Upload className="w-4 h-4" />
                {mutation.isPending ? `Importing ${importData.length} devices...` : `Import ${importData.length} Devices`}
              </button>
              <button onClick={() => fileInputRef.current?.click()} className="px-5 py-2.5 rounded-lg text-sm text-[var(--text-muted)] bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] transition-colors">
                Choose Different File
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}


// ─── Export Devices ───
function ExportDevices() {
  const [exporting, setExporting] = useState(false)

  const handleExport = async (format: 'csv' | 'json') => {
    setExporting(true)
    try {
      const data = await api.get<{ devices: Record<string, unknown>[] }>('/devices/export')
      const devices = data.devices

      let content: string
      let mimeType: string
      let ext: string

      if (format === 'json') {
        content = JSON.stringify({ devices }, null, 2)
        mimeType = 'application/json'
        ext = 'json'
      } else {
        const headers = ['hostname', 'ip_address', 'device_type', 'location', 'group_name', 'ping_enabled', 'ping_interval', 'status', 'last_rtt_ms', 'description', 'tags']
        const rows = devices.map(d =>
          headers.map(h => {
            const val = d[h]
            if (Array.isArray(val)) return (val as string[]).join(';')
            if (val === null || val === undefined) return ''
            return String(val)
          }).join(',')
        )
        content = [headers.join(','), ...rows].join('\n')
        mimeType = 'text/csv'
        ext = 'csv'
      }

      const blob = new Blob([content], { type: mimeType })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `zenplus-devices-${new Date().toISOString().slice(0, 10)}.${ext}`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Export failed:', err)
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-1">Export All Devices</h3>
        <p className="text-xs text-[var(--text-muted)] mb-6">Download your complete device inventory. Exported files can be re-imported into ZenPlus or used for backup.</p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* CSV Export */}
          <button
            onClick={() => handleExport('csv')}
            disabled={exporting}
            className="flex items-start gap-4 p-5 bg-[var(--bg-tertiary)] rounded-xl border border-[var(--bg-elevated)] hover:border-green-500/40 transition-all group text-left disabled:opacity-50"
          >
            <div className="w-12 h-12 rounded-xl bg-green-500/10 flex items-center justify-center flex-shrink-0 group-hover:bg-green-500/20 transition-colors">
              <FileSpreadsheet className="w-6 h-6 text-green-400" />
            </div>
            <div>
              <div className="text-sm font-semibold text-[var(--text-primary)] mb-0.5">Export as CSV</div>
              <div className="text-xs text-[var(--text-muted)] leading-relaxed">
                Comma-separated values. Compatible with Excel, Google Sheets, and any spreadsheet tool.
              </div>
            </div>
          </button>

          {/* JSON Export */}
          <button
            onClick={() => handleExport('json')}
            disabled={exporting}
            className="flex items-start gap-4 p-5 bg-[var(--bg-tertiary)] rounded-xl border border-[var(--bg-elevated)] hover:border-blue-500/40 transition-all group text-left disabled:opacity-50"
          >
            <div className="w-12 h-12 rounded-xl bg-blue-500/10 flex items-center justify-center flex-shrink-0 group-hover:bg-blue-500/20 transition-colors">
              <FileJson className="w-6 h-6 text-blue-400" />
            </div>
            <div>
              <div className="text-sm font-semibold text-[var(--text-primary)] mb-0.5">Export as JSON</div>
              <div className="text-xs text-[var(--text-muted)] leading-relaxed">
                Structured JSON format. Ideal for API integrations, backups, and programmatic use.
              </div>
            </div>
          </button>
        </div>

        {exporting && (
          <div className="mt-4 text-sm text-[var(--accent)] flex items-center gap-2">
            <div className="w-4 h-4 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
            Exporting devices...
          </div>
        )}
      </div>
    </div>
  )
}


// ─── Main Page ───
export function AddDevicePage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('single')

  const tabs: { key: Tab; label: string; icon: typeof Plus; desc: string }[] = [
    { key: 'single', label: 'Add Device', icon: Plus, desc: 'Add a single device manually' },
    { key: 'import', label: 'Import', icon: Upload, desc: 'Bulk import from CSV or JSON' },
    { key: 'export', label: 'Export', icon: Download, desc: 'Download device inventory' },
  ]

  return (
    <div className="max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <button onClick={() => navigate('/devices')} className="p-2 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
          <ArrowLeft className="w-5 h-5 text-[var(--text-secondary)]" />
        </button>
        <div>
          <h2 className="text-xl font-semibold text-[var(--text-primary)]">Device Management</h2>
          <p className="text-sm text-[var(--text-muted)]">Add, import, or export network devices</p>
        </div>
      </div>

      {/* Tab Selector */}
      <div className="grid grid-cols-3 gap-3 mb-6">
        {tabs.map(t => {
          const Icon = t.icon
          const active = tab === t.key
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={cn(
                'flex items-center gap-3 p-4 rounded-xl border transition-all text-left',
                active
                  ? 'bg-[var(--accent)]/10 border-[var(--accent)] ring-1 ring-[var(--accent)]/30'
                  : 'bg-[var(--bg-secondary)] border-[var(--bg-elevated)] hover:border-[var(--text-muted)]'
              )}
            >
              <div className={cn(
                'w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0',
                active ? 'bg-[var(--accent)]/20' : 'bg-[var(--bg-tertiary)]'
              )}>
                <Icon className={cn('w-5 h-5', active ? 'text-[var(--accent)]' : 'text-[var(--text-muted)]')} />
              </div>
              <div>
                <div className={cn('text-sm font-medium', active ? 'text-[var(--accent)]' : 'text-[var(--text-primary)]')}>{t.label}</div>
                <div className="text-xs text-[var(--text-muted)] hidden sm:block">{t.desc}</div>
              </div>
            </button>
          )
        })}
      </div>

      {/* Tab Content */}
      {tab === 'single' && <SingleDeviceForm />}
      {tab === 'import' && <ImportDevices />}
      {tab === 'export' && <ExportDevices />}
    </div>
  )
}
