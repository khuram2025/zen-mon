import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Copy,
  Layers,
  Pencil,
  Play,
  Plus,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Switch } from '@/components/ui/Switch'
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
import type { ServiceCheckTemplate } from '@/types'

type FormState = {
  id?: string
  name: string
  description: string
  check_type: 'http' | 'tcp' | 'tls' | 'icmp' | 'dns'
  level: 1 | 2 | 3
  default_interval: number
  default_timeout: number
  default_retry_count: number
  default_retry_delay_s: number
  target_url_template: string
  target_port_default: number | ''
  http_method: string
  http_expected_status: number
  http_content_match: string
  http_follow_redirects: boolean
  tls_warn_days: number
  tls_critical_days: number
  tags: string[]
  tagsInput: string
  // dns / icmp options (stored in config)
  dns_record_type: 'A' | 'AAAA' | 'CNAME' | 'MX' | 'TXT' | 'NS'
  icmp_count: number
}

const LEVEL_BY_TYPE: Record<FormState['check_type'], 1 | 2 | 3> = {
  icmp: 1, tcp: 1, http: 2, tls: 2, dns: 2,
}

const emptyForm: FormState = {
  name: '',
  description: '',
  check_type: 'http',
  level: 2,
  default_interval: 60,
  default_timeout: 10,
  default_retry_count: 1,
  default_retry_delay_s: 30,
  target_url_template: 'http://{{ip}}/',
  target_port_default: '',
  http_method: 'GET',
  http_expected_status: 200,
  http_content_match: '',
  http_follow_redirects: true,
  tls_warn_days: 30,
  tls_critical_days: 7,
  tags: [],
  tagsInput: '',
  dns_record_type: 'A',
  icmp_count: 3,
}

export function ServiceCheckTemplatesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<FormState>(emptyForm)
  const [deleting, setDeleting] = useState<ServiceCheckTemplate | null>(null)
  const [applyingTemplate, setApplyingTemplate] = useState<ServiceCheckTemplate | null>(null)

  const { data: templates = [] } = useQuery<ServiceCheckTemplate[]>({
    queryKey: ['service-check-templates'],
    queryFn: async () => (await api.get('/service-check-templates')).data,
    refetchInterval: 30_000,
  })

  const save = useMutation({
    mutationFn: async () => {
      const body: any = {
        name: form.name,
        description: form.description || null,
        check_type: form.check_type,
        level: LEVEL_BY_TYPE[form.check_type],
        default_interval: form.default_interval,
        default_timeout: form.default_timeout,
        default_retry_count: form.default_retry_count,
        default_retry_delay_s: form.default_retry_delay_s,
        tags: form.tags,
        config: {},
      }
      if (form.check_type === 'http') {
        body.target_url_template = form.target_url_template
        body.http_method = form.http_method
        body.http_expected_status = form.http_expected_status
        body.http_content_match = form.http_content_match || null
        body.http_follow_redirects = form.http_follow_redirects
      } else if (form.check_type === 'tcp' || form.check_type === 'tls') {
        body.target_port_default = form.target_port_default || null
      }
      if (form.check_type === 'tls') {
        body.tls_warn_days = form.tls_warn_days
        body.tls_critical_days = form.tls_critical_days
      }
      if (form.check_type === 'dns') {
        body.config = { record_type: form.dns_record_type }
      }
      if (form.check_type === 'icmp') {
        body.config = { count: form.icmp_count }
      }
      if (form.id) {
        return (await api.put(`/service-check-templates/${form.id}`, body)).data
      }
      return (await api.post('/service-check-templates', body)).data
    },
    onSuccess: () => {
      toast.success(form.id ? 'Template updated' : 'Template created')
      qc.invalidateQueries({ queryKey: ['service-check-templates'] })
      setFormOpen(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-check-templates/${id}`),
    onSuccess: () => {
      toast.success('Template deleted')
      qc.invalidateQueries({ queryKey: ['service-check-templates'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  function openCreate() {
    setForm(emptyForm)
    setFormOpen(true)
  }

  function openEdit(t: ServiceCheckTemplate) {
    const cfg = (t.config as Record<string, any>) || {}
    setForm({
      id: t.id,
      name: t.name,
      description: t.description || '',
      check_type: t.check_type as any,
      level: (t.level as any) || LEVEL_BY_TYPE[t.check_type as any],
      default_interval: t.default_interval,
      default_timeout: t.default_timeout,
      default_retry_count: t.default_retry_count,
      default_retry_delay_s: t.default_retry_delay_s,
      target_url_template: t.target_url_template || '',
      target_port_default: t.target_port_default ?? '',
      http_method: t.http_method || 'GET',
      http_expected_status: t.http_expected_status || 200,
      http_content_match: t.http_content_match || '',
      http_follow_redirects: t.http_follow_redirects ?? true,
      tls_warn_days: t.tls_warn_days || 30,
      tls_critical_days: t.tls_critical_days || 7,
      tags: t.tags || [],
      tagsInput: '',
      dns_record_type: (cfg.record_type || 'A') as any,
      icmp_count: Number(cfg.count) || 3,
    })
    setFormOpen(true)
  }

  function openClone(t: ServiceCheckTemplate) {
    openEdit(t)
    setTimeout(() => setForm((s) => ({ ...s, id: undefined, name: `${t.name} (copy)` })), 0)
  }

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
            <Layers className="h-5 w-5 text-primary" />
            Check templates
          </h1>
          <p className="text-xs text-muted">
            Define a check once, apply to many devices. Use <code className="rounded bg-surface2 px-1 font-mono">{'{{hostname}}'}</code> or{' '}
            <code className="rounded bg-surface2 px-1 font-mono">{'{{ip}}'}</code> in the URL template — they're substituted per device.
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="h-4 w-4" /> New template
        </Button>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Level</Th>
                  <Th>Target / URL</Th>
                  <Th>Interval</Th>
                  <Th>Tags</Th>
                  <Th className="w-32 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {templates.map((t) => (
                  <Tr key={t.id}>
                    <Td className="font-medium">{t.name}</Td>
                    <Td>
                      <Badge variant="outline" className="border-border uppercase">
                        {t.check_type}
                      </Badge>
                    </Td>
                    <Td>
                      <Badge variant="outline" className="border-border">
                        L{t.level}
                      </Badge>
                    </Td>
                    <Td className="max-w-[260px] truncate font-mono text-xs text-muted">
                      {t.target_url_template ||
                        (t.target_port_default ? `:${t.target_port_default}` : '—')}
                    </Td>
                    <Td className="text-xs">{t.default_interval}s</Td>
                    <Td className="text-xs">
                      {t.tags.length === 0 ? (
                        <span className="text-muted">—</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {t.tags.slice(0, 3).map((x) => (
                            <span
                              key={x}
                              className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary"
                            >
                              {x}
                            </span>
                          ))}
                          {t.tags.length > 3 && (
                            <span className="text-[10px] text-muted">+{t.tags.length - 3}</span>
                          )}
                        </div>
                      )}
                    </Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-primary hover:text-primary"
                          title="Apply to devices"
                          onClick={() => setApplyingTemplate(t)}
                        >
                          <Play className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Edit"
                          onClick={() => openEdit(t)}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Duplicate"
                          onClick={() => openClone(t)}
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          title="Delete"
                          onClick={() => setDeleting(t)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {templates.length === 0 && (
                  <Tr>
                    <Td colSpan={7} className="py-12 text-center text-muted">
                      No templates yet. Click "New template" to create one.
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {/* ── Template form ────────────────────────────────────────── */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>{form.id ? 'Edit template' : 'New template'}</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate()
            }}
          >
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Name" required className="col-span-2">
                <Input
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Management HTTP"
                />
              </FormField>
              <FormField label="Type" required>
                <Select
                  value={form.check_type}
                  onValueChange={(v: any) =>
                    setForm({ ...form, check_type: v, level: LEVEL_BY_TYPE[v] })
                  }
                  disabled={!!form.id}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="icmp">ICMP · L1</SelectItem>
                    <SelectItem value="tcp">TCP · L1</SelectItem>
                    <SelectItem value="http">HTTP · L2</SelectItem>
                    <SelectItem value="tls">TLS · L2</SelectItem>
                    <SelectItem value="dns">DNS · L2</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Description">
                <Input
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  placeholder="Optional"
                />
              </FormField>
            </div>

            {form.check_type === 'http' && (
              <div className="space-y-3 rounded-md border border-border p-3">
                <div className="text-xs font-semibold uppercase tracking-wider text-muted">HTTP</div>
                <FormField label="URL template" hint="Use {{hostname}} or {{ip}}">
                  <Input
                    value={form.target_url_template}
                    onChange={(e) => setForm({ ...form, target_url_template: e.target.value })}
                    placeholder="http://{{ip}}/health"
                  />
                </FormField>
                <div className="grid grid-cols-3 gap-3">
                  <FormField label="Method">
                    <Select
                      value={form.http_method}
                      onValueChange={(v) => setForm({ ...form, http_method: v })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="GET">GET</SelectItem>
                        <SelectItem value="POST">POST</SelectItem>
                        <SelectItem value="HEAD">HEAD</SelectItem>
                        <SelectItem value="PUT">PUT</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField label="Expected status">
                    <Input
                      type="number"
                      min={100}
                      max={599}
                      value={form.http_expected_status}
                      onChange={(e) =>
                        setForm({ ...form, http_expected_status: Number(e.target.value) })
                      }
                    />
                  </FormField>
                  <div className="flex items-end justify-between rounded-md border border-border px-3 py-2">
                    <span className="text-xs font-medium uppercase tracking-wider text-muted">
                      Follow redirects
                    </span>
                    <Switch
                      checked={form.http_follow_redirects}
                      onCheckedChange={(v) => setForm({ ...form, http_follow_redirects: v })}
                    />
                  </div>
                </div>
                <FormField label="Response must contain">
                  <Input
                    value={form.http_content_match}
                    onChange={(e) => setForm({ ...form, http_content_match: e.target.value })}
                    placeholder='"status":"ok"'
                  />
                </FormField>
              </div>
            )}

            {(form.check_type === 'tcp' || form.check_type === 'tls') && (
              <FormField label="Port">
                <Input
                  type="number"
                  min={1}
                  max={65535}
                  value={form.target_port_default}
                  onChange={(e) =>
                    setForm({ ...form, target_port_default: Number(e.target.value) || '' })
                  }
                  placeholder={form.check_type === 'tls' ? '443' : '80'}
                />
              </FormField>
            )}

            {form.check_type === 'tls' && (
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Warn (days)">
                  <Input
                    type="number"
                    min={1}
                    max={365}
                    value={form.tls_warn_days}
                    onChange={(e) =>
                      setForm({ ...form, tls_warn_days: Number(e.target.value) })
                    }
                  />
                </FormField>
                <FormField label="Critical (days)">
                  <Input
                    type="number"
                    min={1}
                    max={365}
                    value={form.tls_critical_days}
                    onChange={(e) =>
                      setForm({ ...form, tls_critical_days: Number(e.target.value) })
                    }
                  />
                </FormField>
              </div>
            )}

            {form.check_type === 'dns' && (
              <FormField label="Record type">
                <Select
                  value={form.dns_record_type}
                  onValueChange={(v: any) => setForm({ ...form, dns_record_type: v })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="A">A</SelectItem>
                    <SelectItem value="AAAA">AAAA</SelectItem>
                    <SelectItem value="CNAME">CNAME</SelectItem>
                    <SelectItem value="MX">MX</SelectItem>
                    <SelectItem value="TXT">TXT</SelectItem>
                    <SelectItem value="NS">NS</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            )}

            {form.check_type === 'icmp' && (
              <FormField label="Packets per probe">
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={form.icmp_count}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      icmp_count: Math.max(1, Math.min(10, Number(e.target.value) || 1)),
                    })
                  }
                />
              </FormField>
            )}

            <div className="grid grid-cols-2 gap-3">
              <FormField label="Interval (s)">
                <Input
                  type="number"
                  min={10}
                  max={3600}
                  value={form.default_interval}
                  onChange={(e) =>
                    setForm({ ...form, default_interval: Number(e.target.value) || 60 })
                  }
                />
              </FormField>
              <FormField label="Timeout (s)">
                <Input
                  type="number"
                  min={1}
                  max={60}
                  value={form.default_timeout}
                  onChange={(e) =>
                    setForm({ ...form, default_timeout: Number(e.target.value) || 10 })
                  }
                />
              </FormField>
              <FormField label="Retries">
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={form.default_retry_count}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      default_retry_count: Math.max(1, Math.min(10, Number(e.target.value) || 1)),
                    })
                  }
                />
              </FormField>
              <FormField label="Retry delay (s)">
                <Input
                  type="number"
                  min={1}
                  max={600}
                  value={form.default_retry_delay_s}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      default_retry_delay_s: Math.max(1, Math.min(600, Number(e.target.value) || 30)),
                    })
                  }
                />
              </FormField>
            </div>

            <FormField label="Tags (comma-separated)">
              <Input
                value={form.tagsInput || form.tags.join(', ')}
                onChange={(e) =>
                  setForm({
                    ...form,
                    tagsInput: e.target.value,
                    tags: e.target.value
                      .split(',')
                      .map((s) => s.trim().toLowerCase())
                      .filter(Boolean),
                  })
                }
                placeholder="prod, edge"
              />
            </FormField>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setFormOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={save.isPending || !form.name.trim()}>
                {form.id ? 'Save changes' : 'Create template'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* ── Apply dialog ──────────────────────────────────────────── */}
      <ApplyDialog
        template={applyingTemplate}
        onClose={() => setApplyingTemplate(null)}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete template "${deleting?.name}"?`}
        description="Service checks already created from this template are not affected."
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => deleting && del.mutate(deleting.id)}
      />
    </div>
  )
}

function ApplyDialog({
  template,
  onClose,
}: {
  template: ServiceCheckTemplate | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [prefix, setPrefix] = useState('')

  const { data: devicesResp } = useQuery<any>({
    queryKey: ['devices', 'list-min-apply'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    enabled: !!template,
  })
  const devices = devicesResp?.data || []

  const apply = useMutation({
    mutationFn: async () => {
      if (!template) return null
      const body = {
        device_ids: Array.from(selected),
        name_prefix: prefix || template.name,
        enabled: true,
      }
      return (await api.post(`/service-check-templates/${template.id}/apply`, body)).data
    },
    onSuccess: (res: any) => {
      const created = res?.created_ids?.length ?? 0
      const skipped = res?.skipped?.length ?? 0
      toast.success(
        'Template applied',
        `Created ${created}${skipped ? `, skipped ${skipped}` : ''}`,
      )
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      onClose()
      setSelected(new Set())
      setPrefix('')
    },
    onError: (e: any) => toast.error('Apply failed', apiErrorMessage(e)),
  })

  const allOn = useMemo(
    () => devices.length > 0 && devices.every((d: any) => selected.has(d.id)),
    [devices, selected],
  )

  return (
    <Dialog open={!!template} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Apply template: {template?.name}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <FormField label="Name prefix" hint='Each new check is named "{prefix} · {device_hostname}"'>
            <Input
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
              placeholder={template?.name || ''}
            />
          </FormField>

          <div>
            <div className="mb-1 flex items-center justify-between text-xs text-muted">
              <span>Target devices · {selected.size} selected</span>
              <button
                type="button"
                className="text-primary hover:underline"
                onClick={() =>
                  setSelected(allOn ? new Set() : new Set(devices.map((d: any) => d.id)))
                }
              >
                {allOn ? 'Clear' : 'Select all'}
              </button>
            </div>
            <div className="max-h-[240px] overflow-auto rounded-md border border-border">
              {devices.map((d: any) => (
                <label
                  key={d.id}
                  className="flex cursor-pointer items-center gap-2 border-b border-border px-3 py-1.5 text-xs hover:bg-surface2/40"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(d.id)}
                    onChange={() => {
                      const n = new Set(selected)
                      if (n.has(d.id)) n.delete(d.id)
                      else n.add(d.id)
                      setSelected(n)
                    }}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  <span className="font-medium">{d.hostname}</span>
                  <span className="font-mono text-muted">{d.ip_address}</span>
                </label>
              ))}
              {devices.length === 0 && (
                <div className="px-3 py-4 text-center text-xs text-muted">
                  No devices found.
                </div>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={selected.size === 0 || apply.isPending}
            onClick={() => apply.mutate()}
          >
            Apply to {selected.size} device{selected.size === 1 ? '' : 's'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
