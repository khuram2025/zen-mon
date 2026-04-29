import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, X } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { ServiceCheckGroup } from '@/types'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { ExpectedStatusInput } from '@/components/forms/ExpectedStatusInput'

type State = {
  name: string
  check_type: 'http' | 'tcp' | 'tls' | 'icmp' | 'dns'
  enabled: boolean
  group_id: string
  parent_check_id: string
  retry_count: number
  retry_delay_s: number
  tags: string[]
  target_host: string
  target_port: number | ''
  target_url: string
  http_method: string
  http_expected_statuses: string
  http_content_match: string
  http_follow_redirects: boolean
  tls_warn_days: number
  tls_critical_days: number
  // icmp
  icmp_count: number
  // dns
  dns_record_type: 'A' | 'AAAA' | 'CNAME' | 'MX' | 'TXT' | 'NS'
  dns_expected: string
  check_interval: number
  timeout: number
  description: string
}

// Map check_type -> monitoring level (display only; backend also stores this).
const LEVEL_BY_TYPE: Record<State['check_type'], 1 | 2 | 3> = {
  icmp: 1,
  tcp: 1,
  http: 2,
  tls: 2,
  dns: 2,
}

const empty: State = {
  name: '',
  check_type: 'http',
  enabled: true,
  group_id: '',
  parent_check_id: '',
  retry_count: 1,
  retry_delay_s: 30,
  tags: [],
  target_host: '',
  target_port: '',
  target_url: 'https://example.com',
  http_method: 'GET',
  http_expected_statuses: '200',
  http_content_match: '',
  http_follow_redirects: true,
  tls_warn_days: 30,
  tls_critical_days: 7,
  icmp_count: 3,
  dns_record_type: 'A',
  dns_expected: '',
  check_interval: 60,
  timeout: 10,
  description: '',
}

export function ServiceCheckFormDialog({
  open,
  onOpenChange,
  check,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  check?: any
}) {
  const isEdit = !!check?.id
  const qc = useQueryClient()
  const [s, setS] = useState<State>(empty)

  const { data: groups = [] } = useQuery<ServiceCheckGroup[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    enabled: open,
  })

  const { data: allChecksResp } = useQuery<{ data: { id: string; name: string }[] }>({
    queryKey: ['service-checks', 'all-for-parent-picker'],
    queryFn: async () => (await api.get('/service-checks?limit=200')).data,
    enabled: open,
  })
  const parentOptions = (allChecksResp?.data || []).filter((c) => c.id !== check?.id)

  useEffect(() => {
    if (!open) return
    if (check) {
      const cfg = (check.config as Record<string, any>) || {}
      setS({
        ...empty,
        name: check.name || '',
        check_type: check.check_type || 'http',
        enabled: check.enabled ?? true,
        group_id: check.group_id || '',
        parent_check_id: check.parent_check_id || '',
        retry_count: check.retry_count ?? 1,
        retry_delay_s: check.retry_delay_s ?? 30,
        tags: Array.isArray(check.tags) ? check.tags : [],
        target_host: check.target_host || '',
        target_port: check.target_port ?? '',
        target_url: check.target_url || '',
        http_method: check.http_method || 'GET',
        http_expected_statuses:
          check.http_expected_statuses || String(check.http_expected_status || 200),
        http_content_match: check.http_content_match || '',
        http_follow_redirects: check.http_follow_redirects ?? true,
        tls_warn_days: check.tls_warn_days ?? 30,
        tls_critical_days: check.tls_critical_days ?? 7,
        icmp_count: Number(cfg.count) || 3,
        dns_record_type: (cfg.record_type || 'A') as State['dns_record_type'],
        dns_expected: cfg.expected || '',
        check_interval: check.check_interval || 60,
        timeout: check.timeout || 10,
        description: check.description || '',
      })
    } else {
      setS(empty)
    }
  }, [open, check])

  const [tagDraft, setTagDraft] = useState('')
  function addTag(raw: string) {
    const t = raw.trim().toLowerCase()
    if (!t) return
    if (s.tags.includes(t)) {
      setTagDraft('')
      return
    }
    setS({ ...s, tags: [...s.tags, t] })
    setTagDraft('')
  }
  function removeTag(t: string) {
    setS({ ...s, tags: s.tags.filter((x) => x !== t) })
  }

  const save = useMutation({
    mutationFn: async (payload: any) => {
      if (isEdit) return (await api.put(`/service-checks/${check.id}`, payload)).data
      return (await api.post('/service-checks', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Service check updated' : 'Service check created')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      qc.invalidateQueries({ queryKey: ['service-check-groups'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    const base: any = {
      name: s.name,
      check_type: s.check_type,
      level: LEVEL_BY_TYPE[s.check_type],
      enabled: s.enabled,
      group_id: s.group_id || null,
      parent_check_id: s.parent_check_id || null,
      retry_count: s.retry_count,
      retry_delay_s: s.retry_delay_s,
      tags: s.tags,
      target_host: s.target_host,
      check_interval: s.check_interval,
      timeout: s.timeout,
      description: s.description || null,
      config: {},
    }
    if (s.check_type === 'http') {
      base.target_url = s.target_url
      base.http_method = s.http_method
      base.http_expected_statuses = s.http_expected_statuses.trim() || null
      base.http_content_match = s.http_content_match || null
      base.http_follow_redirects = s.http_follow_redirects
    } else if (s.check_type === 'tcp' || s.check_type === 'tls') {
      base.target_port = s.target_port || null
    }
    if (s.check_type === 'tls') {
      base.tls_warn_days = s.tls_warn_days
      base.tls_critical_days = s.tls_critical_days
    }
    if (s.check_type === 'icmp') {
      base.config = { count: s.icmp_count }
    }
    if (s.check_type === 'dns') {
      base.config = {
        record_type: s.dns_record_type,
        ...(s.dns_expected ? { expected: s.dns_expected } : {}),
      }
    }
    save.mutate(base)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit service check' : 'New service check'}</DialogTitle>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Name" required className="col-span-2">
              <Input
                required
                value={s.name}
                onChange={(e) => setS({ ...s, name: e.target.value })}
                placeholder="Production API health"
              />
            </FormField>
            <FormField label="Type" required>
              <Select value={s.check_type} onValueChange={(v: any) => setS({ ...s, check_type: v })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="icmp">ICMP ping · L1</SelectItem>
                  <SelectItem value="tcp">TCP port · L1</SelectItem>
                  <SelectItem value="http">HTTP(S) · L2</SelectItem>
                  <SelectItem value="tls">TLS certificate · L2</SelectItem>
                  <SelectItem value="dns">DNS · L2</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <div className="flex items-center justify-between rounded-md border border-border px-3">
              <span className="text-xs font-medium uppercase tracking-wider text-muted">Enabled</span>
              <Switch checked={s.enabled} onCheckedChange={(v) => setS({ ...s, enabled: v })} />
            </div>
          </div>

          <FormField label="Target host" required>
            <Input
              required
              value={s.target_host}
              onChange={(e) => setS({ ...s, target_host: e.target.value })}
              placeholder="api.example.com"
            />
          </FormField>

          {s.check_type === 'http' && (
            <div className="space-y-3 rounded-md border border-border p-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">HTTP options</div>
              <FormField label="URL">
                <Input
                  value={s.target_url}
                  onChange={(e) => setS({ ...s, target_url: e.target.value })}
                  placeholder="https://api.example.com/health"
                />
              </FormField>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Method">
                  <Select
                    value={s.http_method}
                    onValueChange={(v) => setS({ ...s, http_method: v })}
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
                  <ExpectedStatusInput
                    compact
                    value={s.http_expected_statuses}
                    onChange={(v) => setS({ ...s, http_expected_statuses: v })}
                  />
                </FormField>
              </div>
              <FormField label="Response must contain">
                <Input
                  value={s.http_content_match}
                  onChange={(e) => setS({ ...s, http_content_match: e.target.value })}
                  placeholder='"status": "ok"'
                />
              </FormField>
              <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
                <span className="text-xs font-medium uppercase tracking-wider text-muted">Follow redirects</span>
                <Switch
                  checked={s.http_follow_redirects}
                  onCheckedChange={(v) => setS({ ...s, http_follow_redirects: v })}
                />
              </div>
            </div>
          )}

          {s.check_type === 'tcp' && (
            <FormField label="TCP port" required>
              <Input
                type="number"
                min={1}
                max={65535}
                value={s.target_port}
                onChange={(e) => setS({ ...s, target_port: Number(e.target.value) || '' })}
                placeholder="443"
              />
            </FormField>
          )}

          {s.check_type === 'tls' && (
            <div className="grid grid-cols-3 gap-3">
              <FormField label="Port">
                <Input
                  type="number"
                  min={1}
                  max={65535}
                  value={s.target_port}
                  onChange={(e) => setS({ ...s, target_port: Number(e.target.value) || '' })}
                  placeholder="443"
                />
              </FormField>
              <FormField label="Warn (days)">
                <Input
                  type="number"
                  min={1}
                  max={365}
                  value={s.tls_warn_days}
                  onChange={(e) => setS({ ...s, tls_warn_days: Number(e.target.value) })}
                />
              </FormField>
              <FormField label="Critical (days)">
                <Input
                  type="number"
                  min={1}
                  max={365}
                  value={s.tls_critical_days}
                  onChange={(e) => setS({ ...s, tls_critical_days: Number(e.target.value) })}
                />
              </FormField>
            </div>
          )}

          {s.check_type === 'icmp' && (
            <div className="space-y-3 rounded-md border border-border p-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">ICMP options</div>
              <FormField label="Packets per probe">
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={s.icmp_count}
                  onChange={(e) => setS({ ...s, icmp_count: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })}
                />
              </FormField>
            </div>
          )}

          {s.check_type === 'dns' && (
            <div className="space-y-3 rounded-md border border-border p-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">DNS options</div>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Record type">
                  <Select
                    value={s.dns_record_type}
                    onValueChange={(v: any) => setS({ ...s, dns_record_type: v })}
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
                <FormField label="Expected (optional)">
                  <Input
                    value={s.dns_expected}
                    onChange={(e) => setS({ ...s, dns_expected: e.target.value })}
                    placeholder="substring match"
                  />
                </FormField>
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <FormField label="Group">
              <Select
                value={s.group_id || 'none'}
                onValueChange={(v) => setS({ ...s, group_id: v === 'none' ? '' : v })}
              >
                <SelectTrigger>
                  <SelectValue placeholder="No group" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">No group</SelectItem>
                  {groups.map((g) => (
                    <SelectItem key={g.id} value={g.id}>
                      {g.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Tags">
              <div className="flex min-h-[40px] flex-wrap items-center gap-1 rounded-md border border-border bg-surface2/40 px-2 py-1.5">
                {s.tags.map((t) => (
                  <span
                    key={t}
                    className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary"
                  >
                    {t}
                    <button
                      type="button"
                      className="hover:text-danger"
                      onClick={() => removeTag(t)}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                <input
                  value={tagDraft}
                  onChange={(e) => setTagDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ',') {
                      e.preventDefault()
                      addTag(tagDraft)
                    } else if (e.key === 'Backspace' && !tagDraft && s.tags.length > 0) {
                      e.preventDefault()
                      setS({ ...s, tags: s.tags.slice(0, -1) })
                    }
                  }}
                  onBlur={() => tagDraft && addTag(tagDraft)}
                  placeholder={s.tags.length === 0 ? 'prod, edge…' : ''}
                  className="flex-1 min-w-[60px] bg-transparent text-xs outline-none"
                />
              </div>
            </FormField>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <FormField label="Interval (seconds)">
              <Input
                type="number"
                min={10}
                max={3600}
                value={s.check_interval}
                onChange={(e) => setS({ ...s, check_interval: Number(e.target.value) })}
              />
            </FormField>
            <FormField label="Timeout (seconds)">
              <Input
                type="number"
                min={1}
                max={60}
                value={s.timeout}
                onChange={(e) => setS({ ...s, timeout: Number(e.target.value) })}
              />
            </FormField>
          </div>

          <div className="space-y-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">
              Reliability
            </div>
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Retries before Down">
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={s.retry_count}
                  onChange={(e) =>
                    setS({ ...s, retry_count: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })
                  }
                />
              </FormField>
              <FormField label="Retry delay (seconds)">
                <Input
                  type="number"
                  min={1}
                  max={600}
                  value={s.retry_delay_s}
                  onChange={(e) =>
                    setS({ ...s, retry_delay_s: Math.max(1, Math.min(600, Number(e.target.value) || 30)) })
                  }
                />
              </FormField>
            </div>
            <FormField label="Depends on (parent)">
              <Select
                value={s.parent_check_id || 'none'}
                onValueChange={(v) => setS({ ...s, parent_check_id: v === 'none' ? '' : v })}
              >
                <SelectTrigger>
                  <SelectValue placeholder="No dependency" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">No dependency</SelectItem>
                  {parentOptions.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <div className="text-[11px] text-muted">
              Child is skipped while its parent is Down — prevents duplicate alerts.
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create check'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
