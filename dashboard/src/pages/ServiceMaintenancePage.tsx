import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CalendarClock, Plus, Square, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { FormField } from '@/components/ui/FormField'
import { toast } from '@/components/ui/Toast'
import type { ServiceCheck, ServiceCheckGroup, ServiceMaintenanceWindow } from '@/types'

type WindowState = 'scheduled' | 'active' | 'ended' | 'cancelled' | 'expired'

/** Server state when reported; otherwise derived with the appliance clock. */
function windowState(w: ServiceMaintenanceWindow, now = serverNow()): WindowState {
  if (w.state) return w.state
  if (w.cancelled_at) return 'cancelled'
  const s = Date.parse(w.starts_at)
  const e = Date.parse(w.effective_ends_at || w.ends_at)
  if (w.ended_at && Date.parse(w.ended_at) < Date.parse(w.ends_at)) return e <= now ? 'ended' : 'active'
  if (e <= now) return 'expired'
  return s > now ? 'scheduled' : 'active'
}

function isoLocalInputNow(offsetMinutes = 0): string {
  const d = new Date(serverNow() + offsetMinutes * 60_000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function ServiceMaintenancePage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [ending, setEnding] = useState<ServiceMaintenanceWindow | null>(null)
  const [endReason, setEndReason] = useState('')

  const [form, setForm] = useState<{
    scope_type: 'check' | 'group' | 'tag' | 'all'
    scope_check_id: string
    scope_group_id: string
    scope_tag: string
    starts_at: string
    ends_at: string
    reason: string
  }>({
    scope_type: 'all',
    scope_check_id: '',
    scope_group_id: '',
    scope_tag: '',
    starts_at: isoLocalInputNow(0),
    ends_at: isoLocalInputNow(60),
    reason: '',
  })

  const { data: windows = [] } = useQuery<ServiceMaintenanceWindow[]>({
    queryKey: ['service-check-maintenance'],
    queryFn: async () => (await api.get('/service-check-maintenance')).data,
    refetchInterval: 30_000,
  })

  const { data: checksResp } = useQuery<{ data: ServiceCheck[] }>({
    queryKey: ['service-checks', 'list', 'maintenance-picker'],
    queryFn: async () => (await api.get('/service-checks?limit=200')).data,
    enabled: formOpen,
  })
  const allChecks = checksResp?.data || []

  const { data: groups = [] } = useQuery<ServiceCheckGroup[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    enabled: formOpen,
  })

  const { data: tags = [] } = useQuery<string[]>({
    queryKey: ['service-checks', 'tags'],
    queryFn: async () => (await api.get('/service-checks/tags')).data,
    enabled: formOpen,
  })

  const { active, upcoming, history } = useMemo(() => {
    const a: ServiceMaintenanceWindow[] = []
    const u: ServiceMaintenanceWindow[] = []
    const h: ServiceMaintenanceWindow[] = []
    const now = serverNow()
    for (const w of windows) {
      const st = windowState(w, now)
      if (st === 'active') a.push(w)
      else if (st === 'scheduled') u.push(w)
      else h.push(w)
    }
    h.sort((x, y) => Date.parse(y.starts_at) - Date.parse(x.starts_at))
    return { active: a, upcoming: u, history: h }
  }, [windows])

  const create = useMutation({
    mutationFn: async () => {
      const body: any = {
        scope_type: form.scope_type,
        starts_at: new Date(form.starts_at).toISOString(),
        ends_at: new Date(form.ends_at).toISOString(),
        reason: form.reason || null,
      }
      if (form.scope_type === 'check') body.scope_check_id = form.scope_check_id
      if (form.scope_type === 'group') body.scope_group_id = form.scope_group_id
      if (form.scope_type === 'tag') body.scope_tag = form.scope_tag
      return (await api.post('/service-check-maintenance', body)).data
    },
    onSuccess: () => {
      toast.success('Maintenance window created')
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setFormOpen(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  // Ending keeps the record (its excluded interval stays in SLA history);
  // a window that has not started is cancelled. Older appliances only
  // support DELETE.
  const end = useMutation({
    mutationFn: async (w: ServiceMaintenanceWindow) => {
      try {
        return await api.post(`/service-check-maintenance/${w.id}/end`, { reason: endReason.trim() || null })
      } catch (e: any) {
        if (e?.response?.status === 404 || e?.response?.status === 405) {
          if (!w.state) return api.delete(`/service-check-maintenance/${w.id}`)
        }
        throw e
      }
    },
    onSuccess: (_r, w) => {
      toast.success(windowState(w) === 'scheduled' ? 'Window cancelled' : 'Window ended — kept in history')
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setEnding(null)
      setEndReason('')
    },
    onError: (e: any) => toast.error('Could not end the window', apiErrorMessage(e)),
  })

  const canSubmit =
    !!form.starts_at &&
    !!form.ends_at &&
    new Date(form.ends_at) > new Date(form.starts_at) &&
    (form.scope_type === 'all' ||
      (form.scope_type === 'check' && !!form.scope_check_id) ||
      (form.scope_type === 'group' && !!form.scope_group_id) ||
      (form.scope_type === 'tag' && !!form.scope_tag))

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <Link
            to="/services"
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to services
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <CalendarClock className="h-5 w-5 text-primary" />
            Maintenance windows
          </h1>
          <p className="text-xs text-muted">
            While a window is active, alerts and status transitions are suppressed for the covered
            monitors. Probes keep recording, and the window is excluded from SLA. Ending or cancelling a
            window keeps its record so past reports stay reproducible.
          </p>
        </div>
        <Button onClick={() => setFormOpen(true)}>
          <Plus className="h-4 w-4" /> New window
        </Button>
      </div>

      <WindowTable title="Active now" rows={active} onEnd={setEnding} tone="danger" emptyMessage="No maintenance is active." />
      <WindowTable title="Upcoming" rows={upcoming} onEnd={setEnding} tone="primary" emptyMessage="No windows scheduled." />
      <WindowTable title="History" rows={history} emptyMessage="No past windows." />

      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Schedule maintenance window</DialogTitle>
            <DialogDescription>
              Covered monitors keep probing; alerts are suppressed and the window is excluded from SLA.
              Times are in this browser's timezone.
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              if (canSubmit) create.mutate()
            }}
          >
            <FormField label="Scope" required>
              <Select
                value={form.scope_type}
                onValueChange={(v: any) => setForm({ ...form, scope_type: v })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All monitors</SelectItem>
                  <SelectItem value="check">Single check</SelectItem>
                  <SelectItem value="group">Group</SelectItem>
                  <SelectItem value="tag">Tag</SelectItem>
                </SelectContent>
              </Select>
            </FormField>

            {form.scope_type === 'check' && (
              <FormField label="Check" required>
                <Select
                  value={form.scope_check_id || '_'}
                  onValueChange={(v) => setForm({ ...form, scope_check_id: v })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Pick a check" />
                  </SelectTrigger>
                  <SelectContent>
                    {allChecks.map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {c.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            )}

            {form.scope_type === 'group' && (
              <FormField label="Group" required>
                <Select
                  value={form.scope_group_id || '_'}
                  onValueChange={(v) => setForm({ ...form, scope_group_id: v })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Pick a group" />
                  </SelectTrigger>
                  <SelectContent>
                    {groups.map((g) => (
                      <SelectItem key={g.id} value={g.id}>
                        {g.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            )}

            {form.scope_type === 'tag' && (
              <FormField label="Tag" required>
                <Select
                  value={form.scope_tag || '_'}
                  onValueChange={(v) => setForm({ ...form, scope_tag: v })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Pick a tag" />
                  </SelectTrigger>
                  <SelectContent>
                    {tags.map((t) => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            )}

            <div className="grid grid-cols-2 gap-3">
              <FormField label="Starts" required>
                <Input
                  type="datetime-local"
                  required
                  value={form.starts_at}
                  onChange={(e) => setForm({ ...form, starts_at: e.target.value })}
                />
              </FormField>
              <FormField label="Ends" required>
                <Input
                  type="datetime-local"
                  required
                  value={form.ends_at}
                  onChange={(e) => setForm({ ...form, ends_at: e.target.value })}
                />
              </FormField>
            </div>

            <FormField label="Reason">
              <Input
                value={form.reason}
                onChange={(e) => setForm({ ...form, reason: e.target.value })}
                placeholder="Planned DB upgrade"
              />
            </FormField>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setFormOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!canSubmit || create.isPending}>
                Schedule
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={!!ending} onOpenChange={(o) => { if (!o) { setEnding(null); setEndReason('') } }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{ending && windowState(ending) === 'scheduled' ? 'Cancel scheduled window?' : 'End maintenance now?'}</DialogTitle>
            <DialogDescription>
              {ending && windowState(ending) === 'scheduled'
                ? 'The window has not started. It will be marked cancelled and never apply.'
                : `Status transitions and alerts resume immediately for ${ending?.scope_type === 'check' ? 'this monitor' : `every monitor in scope (${ending?.scope_label || ending?.scope_type})`}. The time already in maintenance stays excluded from SLA.`}
            </DialogDescription>
          </DialogHeader>
          <FormField label="Reason (optional)">
            <Input value={endReason} onChange={(e) => setEndReason(e.target.value)} placeholder="Work finished early" maxLength={500} />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => { setEnding(null); setEndReason('') }}>Keep window</Button>
            <Button type="button" variant="destructive" disabled={end.isPending} onClick={() => { if (ending) end.mutate(ending) }}>
              {ending && windowState(ending) === 'scheduled' ? 'Cancel window' : 'End now'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

const STATE_META: Record<WindowState, { label: string; cls: string }> = {
  active: { label: 'Active', cls: 'border-danger/30 bg-danger/10 text-danger' },
  scheduled: { label: 'Upcoming', cls: 'border-primary/30 bg-primary/10 text-primary' },
  ended: { label: 'Ended early', cls: 'border-border-strong bg-surface2 text-text2' },
  expired: { label: 'Completed', cls: 'border-border-strong bg-surface2 text-muted' },
  cancelled: { label: 'Cancelled', cls: 'border-border bg-surface2 text-muted line-through' },
}

function WindowTable({
  title,
  rows,
  onEnd,
  tone,
  emptyMessage,
}: {
  title: string
  rows: ServiceMaintenanceWindow[]
  onEnd?: (w: ServiceMaintenanceWindow) => void
  tone?: 'danger' | 'primary'
  emptyMessage?: string
}) {
  const cls =
    tone === 'danger'
      ? 'border-danger/30 text-danger'
      : tone === 'primary'
        ? 'border-primary/30 text-primary'
        : 'border-border text-text'
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex items-center gap-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          <Badge variant="outline" className={`border ${cls}`}>
            {rows.length}
          </Badge>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Scope</Th>
                <Th>Starts</Th>
                <Th>Ends</Th>
                <Th>Reason</Th>
                <Th>State</Th>
                <Th className="w-24 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {rows.map((w) => {
                const st = windowState(w)
                const meta = STATE_META[st]
                const effEnd = w.effective_ends_at || w.ended_at || w.ends_at
                const endedEarly = !!w.ended_at && Date.parse(w.ended_at) < Date.parse(w.ends_at)
                return (
                  <Tr key={w.id}>
                    <Td className="text-xs">
                      <span className="font-medium">{w.scope_type === 'all' ? 'All monitors' : w.scope_type}</span>
                      {w.scope_label && w.scope_type !== 'all' && (
                        <span className="ml-1 text-muted">· {w.scope_label}</span>
                      )}
                    </Td>
                    <Td className="text-xs">{new Date(w.starts_at).toLocaleString()}</Td>
                    <Td className="text-xs">
                      {new Date(effEnd).toLocaleString()}
                      {endedEarly ? (
                        <div className="text-2xs text-muted">
                          Ended early{w.ended_by_name ? ` by ${w.ended_by_name}` : ''} · scheduled {new Date(w.ends_at).toLocaleString()}
                          {w.end_reason ? ` · ${w.end_reason}` : ''}
                        </div>
                      ) : st !== 'cancelled' ? (
                        <div className="text-2xs text-muted">{relativeTime(effEnd)}</div>
                      ) : (
                        <div className="text-2xs text-muted">Cancelled {w.cancelled_at ? relativeTime(w.cancelled_at) : ''}</div>
                      )}
                    </Td>
                    <Td className="text-xs text-muted">{w.reason || '—'}</Td>
                    <Td>
                      <Badge variant="outline" className={`border ${meta.cls}`}>
                        {meta.label}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex justify-end">
                        {onEnd && (st === 'active' || st === 'scheduled') && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 gap-1 px-2 text-xs text-muted hover:text-danger"
                            onClick={() => onEnd(w)}
                            title={st === 'active' ? 'End this window now (record kept)' : 'Cancel this scheduled window (record kept)'}
                          >
                            {st === 'active' ? <Square className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                            {st === 'active' ? 'End now' : 'Cancel'}
                          </Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                )
              })}
              {rows.length === 0 && (
                <Tr>
                  <Td colSpan={6} className="py-10 text-center text-xs text-muted">
                    {emptyMessage || 'Nothing here.'}
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}
