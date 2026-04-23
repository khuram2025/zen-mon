import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CalendarClock, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import {
  Dialog,
  DialogContent,
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
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import type { ServiceCheck, ServiceCheckGroup, ServiceMaintenanceWindow } from '@/types'

function isoLocalInputNow(offsetMinutes = 0): string {
  const d = new Date(Date.now() + offsetMinutes * 60_000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function ServiceMaintenancePage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [deleting, setDeleting] = useState<ServiceMaintenanceWindow | null>(null)

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

  const { active, upcoming, past } = useMemo(() => {
    const a: ServiceMaintenanceWindow[] = []
    const u: ServiceMaintenanceWindow[] = []
    const p: ServiceMaintenanceWindow[] = []
    const now = Date.now()
    for (const w of windows) {
      const s = Date.parse(w.starts_at)
      const e = Date.parse(w.ends_at)
      if (e < now) p.push(w)
      else if (s > now) u.push(w)
      else a.push(w)
    }
    return { active: a, upcoming: u, past: p }
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

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-check-maintenance/${id}`),
    onSuccess: () => {
      toast.success('Window deleted')
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
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
            While a window is active, status transitions are suppressed for the covered checks.
            Metrics keep recording so uptime/SLA stays accurate.
          </p>
        </div>
        <Button onClick={() => setFormOpen(true)}>
          <Plus className="h-4 w-4" /> New window
        </Button>
      </div>

      {active.length > 0 && <WindowTable title="Active" rows={active} onDelete={setDeleting} tone="danger" />}
      {upcoming.length > 0 && <WindowTable title="Upcoming" rows={upcoming} onDelete={setDeleting} tone="primary" />}
      <WindowTable
        title="All windows"
        rows={windows}
        onDelete={setDeleting}
        emptyMessage="No maintenance windows scheduled."
      />

      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Schedule maintenance window</DialogTitle>
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
                  <SelectItem value="all">All checks</SelectItem>
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

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete maintenance window?"
        description={
          deleting?.active
            ? "This window is currently active. Deleting it will re-enable status transitions immediately."
            : "Remove this scheduled window."
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => deleting && del.mutate(deleting.id)}
      />
    </div>
  )
}

function WindowTable({
  title,
  rows,
  onDelete,
  tone,
  emptyMessage,
}: {
  title: string
  rows: ServiceMaintenanceWindow[]
  onDelete: (w: ServiceMaintenanceWindow) => void
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
                <Th className="w-16 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {rows.map((w) => {
                const now = Date.now()
                const s = Date.parse(w.starts_at)
                const e = Date.parse(w.ends_at)
                const state =
                  e < now ? { label: 'Ended', cls: 'border-border-strong bg-surface2 text-muted' }
                  : s > now ? { label: 'Upcoming', cls: 'border-primary/30 bg-primary/10 text-primary' }
                  : { label: 'Active', cls: 'border-danger/30 bg-danger/10 text-danger' }
                return (
                  <Tr key={w.id}>
                    <Td className="text-xs">
                      <span className="font-medium">{w.scope_type}</span>
                      {w.scope_label && (
                        <span className="ml-1 text-muted">· {w.scope_label}</span>
                      )}
                    </Td>
                    <Td className="text-xs">{new Date(w.starts_at).toLocaleString()}</Td>
                    <Td className="text-xs">
                      {new Date(w.ends_at).toLocaleString()}
                      <div className="text-[10px] text-muted">{relativeTime(w.ends_at)}</div>
                    </Td>
                    <Td className="text-xs text-muted">{w.reason || '—'}</Td>
                    <Td>
                      <Badge variant="outline" className={`border ${state.cls}`}>
                        {state.label}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex justify-end">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          onClick={() => onDelete(w)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
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
