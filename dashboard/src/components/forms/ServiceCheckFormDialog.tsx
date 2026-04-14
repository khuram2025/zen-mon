import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
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

type State = {
  name: string
  check_type: 'http' | 'tcp' | 'tls'
  enabled: boolean
  target_host: string
  target_port: number | ''
  target_url: string
  http_method: string
  http_expected_status: number
  http_content_match: string
  http_follow_redirects: boolean
  tls_warn_days: number
  tls_critical_days: number
  check_interval: number
  timeout: number
  description: string
}

const empty: State = {
  name: '',
  check_type: 'http',
  enabled: true,
  target_host: '',
  target_port: '',
  target_url: 'https://example.com',
  http_method: 'GET',
  http_expected_status: 200,
  http_content_match: '',
  http_follow_redirects: true,
  tls_warn_days: 30,
  tls_critical_days: 7,
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

  useEffect(() => {
    if (!open) return
    if (check) {
      setS({
        ...empty,
        name: check.name || '',
        check_type: check.check_type || 'http',
        enabled: check.enabled ?? true,
        target_host: check.target_host || '',
        target_port: check.target_port ?? '',
        target_url: check.target_url || '',
        http_method: check.http_method || 'GET',
        http_expected_status: check.http_expected_status || 200,
        http_content_match: check.http_content_match || '',
        http_follow_redirects: check.http_follow_redirects ?? true,
        tls_warn_days: check.tls_warn_days ?? 30,
        tls_critical_days: check.tls_critical_days ?? 7,
        check_interval: check.check_interval || 60,
        timeout: check.timeout || 10,
        description: check.description || '',
      })
    } else {
      setS(empty)
    }
  }, [open, check])

  const save = useMutation({
    mutationFn: async (payload: any) => {
      if (isEdit) return (await api.put(`/service-checks/${check.id}`, payload)).data
      return (await api.post('/service-checks', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Service check updated' : 'Service check created')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    const base: any = {
      name: s.name,
      check_type: s.check_type,
      enabled: s.enabled,
      target_host: s.target_host,
      check_interval: s.check_interval,
      timeout: s.timeout,
      description: s.description || null,
    }
    if (s.check_type === 'http') {
      base.target_url = s.target_url
      base.http_method = s.http_method
      base.http_expected_status = s.http_expected_status
      base.http_content_match = s.http_content_match || null
      base.http_follow_redirects = s.http_follow_redirects
    } else {
      base.target_port = s.target_port || null
    }
    if (s.check_type === 'tls') {
      base.tls_warn_days = s.tls_warn_days
      base.tls_critical_days = s.tls_critical_days
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
                  <SelectItem value="http">HTTP(S)</SelectItem>
                  <SelectItem value="tcp">TCP</SelectItem>
                  <SelectItem value="tls">TLS certificate</SelectItem>
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
                  <Input
                    type="number"
                    min={100}
                    max={599}
                    value={s.http_expected_status}
                    onChange={(e) => setS({ ...s, http_expected_status: Number(e.target.value) })}
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
