/** Agent Policies — collection profiles (intervals, watchlists, ignore
 *  lists, update ring) distributed to enrolled agents. Policy edits bump
 *  the config version server-side; agents apply changes within a minute. */

import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pencil, Plus, SlidersHorizontal, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { DEFAULT_EVENT_LOG_TEXT, eventLogSummary, formatEventLogFilters, parseEventLogFilters } from '@/lib/eventLogFilters'
import type { AgentPolicy, UpdateRing } from '@/types/servers'

type Platform = AgentPolicy['platform']

const PLATFORM_OPTIONS: { value: Platform; label: string }[] = [
  { value: 'any', label: 'Any platform' },
  { value: 'windows', label: 'Windows' },
  { value: 'linux', label: 'Linux' },
]

const RING_OPTIONS: { value: UpdateRing; label: string }[] = [
  { value: 'canary', label: 'Canary' },
  { value: 'beta', label: 'Beta' },
  { value: 'stable', label: 'Stable' },
  { value: 'pinned', label: 'Pinned' },
]

const PLATFORM_VARIANT: Record<Platform, 'info' | 'success' | 'default'> = {
  windows: 'info',
  linux: 'success',
  any: 'default',
}

const RING_VARIANT: Record<UpdateRing, 'warning' | 'info' | 'success' | 'outline'> = {
  canary: 'warning',
  beta: 'info',
  stable: 'success',
  pinned: 'outline',
}

function watchlistSummary(p: AgentPolicy): string | null {
  const parts: string[] = []
  if (p.service_watchlist.length) {
    parts.push(`${p.service_watchlist.length} service${p.service_watchlist.length === 1 ? '' : 's'}`)
  }
  if (p.process_watchlist.length) {
    parts.push(`${p.process_watchlist.length} process${p.process_watchlist.length === 1 ? '' : 'es'}`)
  }
  return parts.length ? parts.join(' · ') : null
}

export function AgentPoliciesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<AgentPolicy | null>(null)
  const [deleting, setDeleting] = useState<AgentPolicy | null>(null)

  const { data, isLoading, isError, error } = useQuery<{ items: AgentPolicy[] }>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data,
  })
  const policies = data?.items || []

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/agent-policies/${id}`),
    onSuccess: () => {
      toast.success('Policy deleted')
      qc.invalidateQueries({ queryKey: ['agent-policies'] })
      setDeleting(null)
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <SlidersHorizontal className="h-5 w-5 text-primary" />
            Agent Policies
          </h1>
          <p className="text-xs text-muted">
            Collection profiles distributed to enrolled agents — agents refresh config within a minute
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" /> New policy
        </Button>
      </div>

      {/* Table */}
      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Name</Th>
                  <Th>Platform</Th>
                  <Th>Collection</Th>
                  <Th>Top processes</Th>
                  <Th>Watchlists</Th>
                  <Th>Event log</Th>
                  <Th>Ring</Th>
                  <Th>Agents</Th>
                  <Th>Updated</Th>
                  <Th className="w-20 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {isLoading &&
                  [...Array(3)].map((_, i) => (
                    <Tr key={i}>
                      <Td colSpan={10}><Skeleton className="h-8 w-full" /></Td>
                    </Tr>
                  ))}
                {isError && (
                  <Tr>
                    <Td colSpan={10} className="py-8 text-center text-sm text-danger">
                      {apiErrorMessage(error)}
                    </Td>
                  </Tr>
                )}
                {!isLoading && !isError && policies.length === 0 && (
                  <Tr>
                    <Td colSpan={10}>
                      <div className="flex flex-col items-center gap-2 py-10 text-center">
                        <SlidersHorizontal className="h-8 w-8 text-muted/50" />
                        <div className="text-sm font-medium">No policies yet</div>
                        <div className="max-w-sm text-xs text-muted">
                          Create a policy to control collection intervals, watchlists, and update
                          rings for your agents.
                        </div>
                      </div>
                    </Td>
                  </Tr>
                )}
                {policies.map((p) => (
                  <Tr key={p.id}>
                    <Td>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{p.name}</span>
                        {p.is_builtin && <Badge variant="outline">Built-in</Badge>}
                      </div>
                      {p.description && <div className="text-xs text-muted">{p.description}</div>}
                    </Td>
                    <Td>
                      <Badge variant={PLATFORM_VARIANT[p.platform]}>{p.platform}</Badge>
                    </Td>
                    <Td className="whitespace-nowrap font-mono text-xs tabular-nums">
                      {p.metric_interval_s}s / upload {p.upload_interval_s}s
                    </Td>
                    <Td className="tabular-nums">{p.process_top_n}</Td>
                    <Td className="text-xs">
                      {watchlistSummary(p) || <span className="text-muted">—</span>}
                    </Td>
                    <Td className="text-xs">
                      {p.platform === 'linux' ? <span className="text-muted">n/a</span> : eventLogSummary(p)}
                    </Td>
                    <Td>
                      <Badge variant={RING_VARIANT[p.update_ring]}>{p.update_ring}</Badge>
                    </Td>
                    <Td className="tabular-nums">{p.agent_count}</Td>
                    <Td className="whitespace-nowrap text-xs text-muted">{relativeTime(p.updated_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7"
                          onClick={() => { setEditing(p); setFormOpen(true) }}
                          title="Edit"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        {/* Disabled buttons swallow pointer events, so the
                            "why" tooltip lives on a wrapping span. */}
                        <span title={p.is_builtin ? 'Built-in policies cannot be deleted' : undefined}>
                          <Button
                            variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                            onClick={() => setDeleting(p)}
                            disabled={p.is_builtin}
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </span>
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <PolicyFormDialog open={formOpen} onOpenChange={setFormOpen} policy={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete policy"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? Agents
            assigned to this policy fall back to the default policy for their platform.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

// ── Create / edit dialog ─────────────────────────────────────────────

type PolicyFormState = {
  name: string
  description: string
  platform: Platform
  metric_interval_s: number
  upload_interval_s: number
  process_top_n: number
  service_watchlist: string
  process_watchlist: string
  disk_ignore: string
  network_ignore: string
  update_ring: UpdateRing
  /** The policy's `event_log` feature flag: the agent's on/off switch for the collector. */
  event_log_enabled: boolean
  /** One channel per line; blank = the default channels. */
  event_log_text: string
  /** Flags the form does not edit, kept as they are. */
  other_flags: Record<string, unknown>
  event_profile: number
  event_max_events: number
  event_max_bytes: number
  event_message_bytes: number
  event_interval_s: number
}

const EMPTY_FORM: PolicyFormState = {
  name: '',
  description: '',
  platform: 'any',
  metric_interval_s: 30,
  upload_interval_s: 60,
  process_top_n: 20,
  service_watchlist: '',
  process_watchlist: '',
  disk_ignore: '',
  network_ignore: '',
  update_ring: 'stable',
  event_log_enabled: true,
  event_log_text: '',
  other_flags: {},
  event_profile: 0, event_max_events: 200, event_max_bytes: 262144,
  event_message_bytes: 2048, event_interval_s: 60,
}

const splitLines = (v: string) => v.split('\n').map((x) => x.trim()).filter(Boolean)
const splitCommas = (v: string) => v.split(',').map((x) => x.trim()).filter(Boolean)

function PolicyFormDialog({
  open,
  onOpenChange,
  policy,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** When provided, edits in place; otherwise creates. */
  policy?: AgentPolicy | null
}) {
  const qc = useQueryClient()
  const editing = Boolean(policy)
  const [s, setS] = useState<PolicyFormState>(EMPTY_FORM)

  useEffect(() => {
    if (!open) return
    if (policy) {
      // The form writes `event_log`; the alias the agent also reads is folded into it.
      const other_flags = { ...(policy.feature_flags || {}) }
      delete other_flags.event_log
      delete other_flags.collect_event_log
      setS({
        name: policy.name,
        description: policy.description || '',
        platform: policy.platform,
        metric_interval_s: policy.metric_interval_s,
        upload_interval_s: policy.upload_interval_s,
        process_top_n: policy.process_top_n,
        service_watchlist: policy.service_watchlist.join('\n'),
        process_watchlist: policy.process_watchlist.join('\n'),
        disk_ignore: policy.disk_ignore.join(', '),
        network_ignore: policy.network_ignore.join(', '),
        update_ring: policy.update_ring,
        event_log_enabled: eventLogSummary(policy) !== 'Off',
        event_log_text: formatEventLogFilters(policy.event_log_filters),
        other_flags,
        event_profile: Number(policy.cardinality_limits?.event_profile ?? 0),
        event_max_events: Number(policy.cardinality_limits?.event_max_events ?? 200),
        event_max_bytes: Number(policy.cardinality_limits?.event_max_bytes ?? 262144),
        event_message_bytes: Number(policy.cardinality_limits?.event_message_bytes ?? 2048),
        event_interval_s: Number(policy.cardinality_limits?.event_interval_s ?? 60),
      })
    } else {
      setS(EMPTY_FORM)
    }
  }, [open, policy])

  const parsedEventLog = parseEventLogFilters(s.event_log_text)
  const eventLogError = s.event_log_enabled && 'error' in parsedEventLog ? parsedEventLog.error : null

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        // [] = the default channels; turning collection off is the flag, never an empty list.
        event_log_filters: 'filters' in parsedEventLog ? parsedEventLog.filters : [],
        feature_flags: { ...s.other_flags, event_log: s.event_log_enabled },
        cardinality_limits: { ...(policy?.cardinality_limits || {}), event_profile: s.event_profile,
          event_max_events: s.event_max_events, event_max_bytes: s.event_max_bytes,
          event_message_bytes: s.event_message_bytes, event_interval_s: s.event_interval_s },
        name: s.name.trim(),
        description: s.description.trim() || null,
        platform: s.platform,
        metric_interval_s: s.metric_interval_s,
        upload_interval_s: s.upload_interval_s,
        process_top_n: s.process_top_n,
        service_watchlist: splitLines(s.service_watchlist),
        process_watchlist: splitLines(s.process_watchlist),
        disk_ignore: splitCommas(s.disk_ignore),
        network_ignore: splitCommas(s.network_ignore),
        update_ring: s.update_ring,
      }
      if (editing && policy) {
        return (await api.patch(`/agent-policies/${policy.id}`, body)).data
      }
      return (await api.post('/agent-policies', body)).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Policy updated' : 'Policy created')
      qc.invalidateQueries({ queryKey: ['agent-policies'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (eventLogError) return
    save.mutate()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4 text-primary" />
            {editing ? 'Edit policy' : 'New policy'}
          </DialogTitle>
          <DialogDescription>
            Collection settings for agents assigned to this policy — saved changes are picked up
            by agents within a minute.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Name" required>
              <Input
                required
                value={s.name}
                onChange={(e) => setS({ ...s, name: e.target.value })}
                placeholder="Windows production"
              />
            </FormField>
            <FormField label="Platform">
              <Select value={s.platform} onValueChange={(v) => setS({ ...s, platform: v as Platform })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {PLATFORM_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Metric interval (s)">
              <Input
                type="number" required min={5} max={3600}
                value={s.metric_interval_s}
                onChange={(e) => setS({ ...s, metric_interval_s: Number(e.target.value) })}
              />
            </FormField>
            <FormField label="Upload interval (s)">
              <Input
                type="number" required min={5} max={3600}
                value={s.upload_interval_s}
                onChange={(e) => setS({ ...s, upload_interval_s: Number(e.target.value) })}
              />
            </FormField>
            <FormField label="Top N processes">
              <Input
                type="number" required min={0} max={500}
                value={s.process_top_n}
                onChange={(e) => setS({ ...s, process_top_n: Number(e.target.value) })}
              />
            </FormField>
            <FormField label="Update ring">
              <Select value={s.update_ring} onValueChange={(v) => setS({ ...s, update_ring: v as UpdateRing })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {RING_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
          </div>

          <FormField
            label="Service watchlist"
            hint="Service names to watch, e.g. W3SVC, MSSQLSERVER — agents report state and alerting can trigger on stops"
          >
            <Textarea
              rows={3}
              value={s.service_watchlist}
              onChange={(e) => setS({ ...s, service_watchlist: e.target.value })}
              placeholder="One service name per line"
            />
          </FormField>
          <FormField label="Process watchlist" hint="Process names to watch — one per line">
            <Textarea
              rows={3}
              value={s.process_watchlist}
              onChange={(e) => setS({ ...s, process_watchlist: e.target.value })}
              placeholder="One process name per line"
            />
          </FormField>
          {s.platform !== 'linux' && (
            <div className="space-y-2 rounded-md border border-border p-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div id="policy-event-log" className="text-sm font-medium">Windows Event Log</div>
                  <p className="text-xs text-muted">Collects selected event details in bounded batches; repeats are grouped on the server&apos;s Events tab.</p>
                </div>
                <Switch checked={s.event_log_enabled} onCheckedChange={(v) => setS({ ...s, event_log_enabled: v })}
                  aria-labelledby="policy-event-log" />
              </div>
              {s.event_log_enabled && <div className="space-y-3">
                <FormField label="Server role">
                  <Select value={String(s.event_profile)} onValueChange={v => setS({ ...s, event_profile: Number(v) })}>
                    <SelectTrigger><SelectValue /></SelectTrigger><SelectContent>
                      <SelectItem value="0">General server</SelectItem><SelectItem value="1">Domain controller / DNS</SelectItem>
                      <SelectItem value="2">IIS web server</SelectItem><SelectItem value="3">SQL Server</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <p className="text-xs text-muted">Leave Channels empty to use the role preset. Custom filters override the preset. OS restart/shutdown evidence has a separate collection allowance. Requires agent 1.14.4 or later.</p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField label="Event interval (seconds)"><Input type="number" required min={60} max={3600} value={s.event_interval_s} onChange={e => setS({ ...s, event_interval_s: Number(e.target.value) })} /></FormField>
                  <FormField label="Events per collection"><Input type="number" required min={50} max={1000} value={s.event_max_events} onChange={e => setS({ ...s, event_max_events: Number(e.target.value) })} /></FormField>
                  <FormField label="Event payload limit (KB)"><Input type="number" required min={16} max={1024} value={s.event_max_bytes / 1024} onChange={e => setS({ ...s, event_max_bytes: Number(e.target.value) * 1024 })} /></FormField>
                  <FormField label="Message limit (bytes)"><Input type="number" required min={256} max={8192} value={s.event_message_bytes} onChange={e => setS({ ...s, event_message_bytes: Number(e.target.value) })} /></FormField>
                </div>
                <p className="text-xs text-muted">Uploads are compressed. Batch limits defer remaining events; backlog, stale collection and log coverage gaps stay visible. Detailed history is kept for 14 days.</p>
              </div>}
              {s.event_log_enabled && (
                <FormField
                  label="Channels"
                  hint={'One per line: "Channel: levels", optionally "| event IDs". Leave empty for the selected role. Security audit events use the audit level (for example Security: audit | 4625, 4740).'}
                >
                  <Textarea
                    rows={3}
                    className="font-mono text-xs"
                    value={s.event_log_text}
                    onChange={(e) => setS({ ...s, event_log_text: e.target.value })}
                    placeholder={`${DEFAULT_EVENT_LOG_TEXT}\nSecurity: audit | 4625, 4740`}
                    aria-invalid={!!eventLogError}
                  />
                </FormField>
              )}
              {eventLogError && <p role="alert" className="text-xs text-danger">{eventLogError}</p>}
            </div>
          )}
          <FormField label="Disk ignore" hint="Comma-separated mount points to skip">
            <Input
              value={s.disk_ignore}
              onChange={(e) => setS({ ...s, disk_ignore: e.target.value })}
              placeholder="/snap, /boot/efi"
            />
          </FormField>
          <FormField label="Network ignore" hint="Comma-separated interface patterns to skip">
            <Input
              value={s.network_ignore}
              onChange={(e) => setS({ ...s, network_ignore: e.target.value })}
              placeholder="lo, veth*, docker*"
            />
          </FormField>
          <FormField label="Description">
            <Textarea
              rows={2}
              value={s.description}
              onChange={(e) => setS({ ...s, description: e.target.value })}
              placeholder="Optional description"
            />
          </FormField>

          <DialogFooter>
            {editing && policy && (
              <span
                className="mr-auto self-center text-xs text-muted"
                title="Config version — bumps automatically when the policy changes"
              >
                v{policy.config_version}
              </span>
            )}
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || !!eventLogError}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {editing ? 'Save changes' : 'Create policy'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
