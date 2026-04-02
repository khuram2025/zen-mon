import { useState, useEffect, type FormEvent } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { Device, DeviceGroup } from '@/types'
import {
  ArrowLeft, Save, CheckCircle, AlertCircle, X,
  Monitor, Server, Shield, Wifi, Radio, Printer, HelpCircle, Info,
} from 'lucide-react'
import { cn } from '@/lib/utils'

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

export function EditDevicePage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState('')

  const { data: device, isLoading } = useQuery({
    queryKey: ['device', id],
    queryFn: () => api.get<Device>(`/devices/${id}`),
    enabled: !!id,
  })

  const { data: groups } = useQuery({
    queryKey: ['device-groups'],
    queryFn: () => api.get<DeviceGroup[]>('/devices/groups'),
  })

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

  useEffect(() => {
    if (device) {
      setForm({
        hostname: device.hostname,
        ip_address: device.ip_address,
        device_type: device.device_type,
        location: device.location || '',
        group_id: device.group_id || '',
        ping_enabled: device.ping_enabled,
        ping_interval: device.ping_interval,
        description: device.description || '',
        tags: (device.tags || []).join(', '),
      })
    }
  }, [device])

  const mutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.put(`/devices/${id}`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['device', id] })
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      setSuccess(true)
      setError('')
      setTimeout(() => setSuccess(false), 3000)
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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-32">
        <div className="w-8 h-8 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (!device) {
    return (
      <div className="flex flex-col items-center justify-center py-32 gap-4">
        <h3 className="text-lg font-semibold text-[var(--text-primary)]">Device Not Found</h3>
        <button onClick={() => navigate('/devices')} className="px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm">
          Back to Devices
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-3xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <button onClick={() => navigate(`/devices/${id}`)} className="p-2 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
          <ArrowLeft className="w-5 h-5 text-[var(--text-secondary)]" />
        </button>
        <div>
          <h2 className="text-xl font-semibold text-[var(--text-primary)]">Edit Device</h2>
          <p className="text-sm text-[var(--text-muted)] font-mono">{device.hostname} ({device.ip_address})</p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {success && (
          <div className="flex items-center gap-3 p-4 bg-green-500/10 border border-green-500/20 rounded-xl text-green-400 text-sm">
            <CheckCircle className="w-5 h-5 flex-shrink-0" />
            <span>Device updated successfully!</span>
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
              <input type="text" value={form.hostname} onChange={e => update('hostname', e.target.value)} required
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm" />
            </div>
            <div>
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">IP Address *</label>
              <input type="text" value={form.ip_address} onChange={e => update('ip_address', e.target.value)} required
                pattern="^(\d{1,3}\.){3}\d{1,3}$"
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm font-mono" />
            </div>
            <div>
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Location</label>
              <input type="text" value={form.location} onChange={e => update('location', e.target.value)}
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm" />
            </div>
            <div>
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Group</label>
              <select value={form.group_id} onChange={e => update('group_id', e.target.value)}
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm">
                <option value="">No Group</option>
                {(groups || []).map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Description</label>
              <input type="text" value={form.description} onChange={e => update('description', e.target.value)}
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm" />
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
                <button key={dt.value} type="button" onClick={() => update('device_type', dt.value)}
                  className={cn(
                    'flex flex-col items-center gap-1.5 p-3 rounded-xl border text-xs font-medium transition-all',
                    selected
                      ? 'bg-[var(--accent)]/10 border-[var(--accent)] text-[var(--accent)]'
                      : 'bg-[var(--bg-tertiary)] border-[var(--bg-elevated)] text-[var(--text-muted)] hover:border-[var(--text-muted)]'
                  )}>
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
                  <button key={pi.value} type="button" onClick={() => update('ping_interval', pi.value)}
                    className={cn('flex-1 py-2 rounded-lg text-xs font-medium transition-colors',
                      form.ping_interval === pi.value
                        ? 'bg-[var(--accent)] text-white'
                        : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]')}>
                    {pi.label}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Ping Monitoring</label>
              <button type="button" onClick={() => update('ping_enabled', !form.ping_enabled)}
                className={cn('w-full py-2.5 rounded-lg text-sm font-medium transition-colors border',
                  form.ping_enabled
                    ? 'bg-green-500/10 border-green-500/30 text-green-400'
                    : 'bg-[var(--bg-tertiary)] border-[var(--bg-elevated)] text-[var(--text-muted)]')}>
                {form.ping_enabled ? 'Ping Enabled' : 'Ping Disabled'}
              </button>
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5 uppercase tracking-wider">Tags (comma separated)</label>
              <input type="text" value={form.tags} onChange={e => update('tags', e.target.value)}
                placeholder="e.g. critical, floor-1, building-a"
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm" />
            </div>
          </div>
        </div>

        {/* Submit */}
        <div className="flex items-center gap-3">
          <button type="submit" disabled={mutation.isPending}
            className="flex items-center gap-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white px-6 py-2.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
            <Save className="w-4 h-4" />
            {mutation.isPending ? 'Saving...' : 'Save Changes'}
          </button>
          <button type="button" onClick={() => navigate(`/devices/${id}`)}
            className="px-6 py-2.5 rounded-lg text-sm font-medium text-[var(--text-muted)] hover:text-[var(--text-primary)] bg-[var(--bg-tertiary)] transition-colors">
            Cancel
          </button>
        </div>
      </form>
    </div>
  )
}
