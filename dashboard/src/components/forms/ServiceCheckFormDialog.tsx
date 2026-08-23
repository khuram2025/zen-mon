import { FormEvent, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, AlertTriangle, CheckCircle2, Loader2, Play, X } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import type { ServiceCheckGroup, ServiceCredential, ServiceWorkflowStep } from '@/types'
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
import { ServiceWorkflowFields } from '@/components/forms/ServiceWorkflowFields'

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
  http_ignore_tls_errors: boolean
  http_allow_insecure_auth: boolean
  credential_id: string
  credential_auth_type: ServiceCredential['auth_type'] | ''
  workflow_enabled: boolean
  workflow_operator: 'all' | 'any'
  workflow_steps: ServiceWorkflowStep[]
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

type ServiceTestStep = {
  index: number
  name: string
  status: 'up' | 'down'
  status_code: number | null
  response_time_ms: number
  content_matched: boolean | null
  error: string
  diagnosis: string | null
  response_url: string | null
  content_type: string | null
  response_size_bytes: number | null
  redirect_count: number
}

type ServiceTestResult = {
  status: 'up' | 'down'
  response_time_ms: number
  error: string
  diagnosis: string | null
  details: {
    steps_total?: number
    steps_passed?: number
    steps?: ServiceTestStep[]
    tls_verification_disabled?: boolean
  }
}

const DIAGNOSIS_LABELS: Record<string, string> = {
  authentication: 'Credentials rejected',
  dns: 'DNS resolution failed',
  timeout: 'Connection timed out',
  connection_refused: 'Connection refused',
  unreachable: 'Service unreachable',
  connectivity: 'Connection failed',
  tls: 'TLS / certificate problem',
  redirect: 'Redirect problem',
  http_status: 'Unexpected HTTP response',
  content: 'Required content missing',
  request: 'Request failed',
  workflow: 'Navigation failed',
}

function hostFromUrl(value: string): string {
  try {
    return new URL(value).hostname
  } catch {
    return ''
  }
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
  target_url: '',
  http_method: 'GET',
  http_expected_statuses: '200',
  http_content_match: '',
  http_follow_redirects: true,
  http_ignore_tls_errors: false,
  http_allow_insecure_auth: false,
  credential_id: '',
  credential_auth_type: '',
  workflow_enabled: false,
  workflow_operator: 'all',
  workflow_steps: [],
  tls_warn_days: 30,
  tls_critical_days: 7,
  icmp_count: 3,
  dns_record_type: 'A',
  dns_expected: '',
  check_interval: 60,
  timeout: 10,
  description: '',
}

function ServiceTestResultPanel({ result }: { result: ServiceTestResult }) {
  const passed = result.status === 'up'
  const diagnosis = result.diagnosis ? DIAGNOSIS_LABELS[result.diagnosis] || result.diagnosis : null
  const steps = result.details.steps || []

  return (
    <div className={`space-y-3 rounded-md border p-3 ${passed ? 'border-success/30 bg-success/5' : 'border-danger/30 bg-danger/5'}`}>
      <div className="flex items-start gap-2">
        {passed ? (
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
        ) : (
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
        )}
        <div className="min-w-0 flex-1">
          <div className={`text-sm font-semibold ${passed ? 'text-success' : 'text-danger'}`}>
            {passed ? 'URL and authentication test passed' : diagnosis || 'Test failed'}
          </div>
          <div className="mt-0.5 text-xs text-muted">
            {passed
              ? `The service responded successfully in ${Math.round(result.response_time_ms)} ms.`
              : result.error || 'The service did not return an acceptable response.'}
          </div>
        </div>
        {result.details.steps_total != null && (
          <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[10px] text-muted">
            {result.details.steps_passed}/{result.details.steps_total} steps
          </span>
        )}
      </div>

      {result.details.tls_verification_disabled && (
        <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>TLS certificate verification was bypassed for this test. Connectivity was tested, but certificate identity and trust were not validated.</span>
        </div>
      )}

      {steps.length > 0 && (
        <div className="space-y-2">
          {steps.map((step) => (
            <div key={step.index} className="rounded-md border border-border bg-surface px-3 py-2">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <span className={step.status === 'up' ? 'font-semibold text-success' : 'font-semibold text-danger'}>
                  Step {step.index}: {step.name}
                </span>
                {step.status_code != null && <span className="font-mono text-text">HTTP {step.status_code}</span>}
                <span className="text-muted">{Math.round(step.response_time_ms)} ms</span>
                {step.redirect_count > 0 && <span className="text-muted">{step.redirect_count} redirect{step.redirect_count === 1 ? '' : 's'}</span>}
              </div>
              {step.response_url && <div className="mt-1 truncate font-mono text-[10px] text-muted">Response: {step.response_url}</div>}
              <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-muted">
                {step.content_type && <span>{step.content_type}</span>}
                {step.response_size_bytes != null && <span>{step.response_size_bytes.toLocaleString()} bytes</span>}
                {step.content_matched != null && <span>Content match: {step.content_matched ? 'yes' : 'no'}</span>}
              </div>
              {step.error && <div className="mt-1 text-[11px] text-danger">{step.error}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
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
  const normalizedTargetUrl = s.target_url.trim().toLowerCase()
  const isHttpTarget = normalizedTargetUrl.startsWith('http://')
  const isHttpsTarget = normalizedTargetUrl.startsWith('https://')
  const [testResult, setTestResult] = useState<ServiceTestResult | null>(null)
  const credentialSelectionTouched = useRef(false)

  // Always hydrate edits from the detail endpoint. List rows and long-lived
  // query cache entries can omit newly-linked credential metadata.
  const { data: freshCheck } = useQuery<any>({
    queryKey: ['service-check-edit-source', check?.id, open],
    queryFn: async () => (await api.get(`/service-checks/${check.id}`)).data,
    enabled: open && isEdit,
    initialData: open && isEdit ? check : undefined,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const sourceCheck = freshCheck || check

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

  const { data: credentials = [] } = useQuery<ServiceCredential[]>({
    queryKey: ['service-credentials'],
    queryFn: async () => (await api.get('/service-credentials')).data,
    enabled: open && s.check_type === 'http',
  })
  const selectedCredential = credentials.find((credential) => credential.id === s.credential_id)
  const effectiveCredentialAuthType = selectedCredential?.auth_type || s.credential_auth_type
  const persistedCredential = credentials.find((credential) =>
    credential.id === sourceCheck?.credential_id ||
    (!!sourceCheck?.credential_name && credential.name === sourceCheck.credential_name),
  )
  const credentialHydrating = Boolean(
    open &&
    isEdit &&
    sourceCheck?.credential_id &&
    !s.credential_id &&
    !credentialSelectionTouched.current,
  )

  useEffect(() => {
    if (open) credentialSelectionTouched.current = false
  }, [open])

  // Credential metadata can arrive after the editable service object. Rejoin
  // by ID (or safe metadata name fallback) without overriding a user's choice.
  useEffect(() => {
    if (!open || !isEdit || credentialSelectionTouched.current || !persistedCredential) return
    setS((current) => current.credential_id === persistedCredential.id
      ? current
      : {
          ...current,
          credential_id: persistedCredential.id,
          credential_auth_type: persistedCredential.auth_type,
        })
  }, [open, isEdit, persistedCredential?.id, persistedCredential?.auth_type, s.credential_id])

  useEffect(() => {
    if (!open) return
    setTestResult(null)
    if (sourceCheck) {
      const cfg = (sourceCheck.config as Record<string, any>) || {}
      setS({
        ...empty,
        name: sourceCheck.name || '',
        check_type: sourceCheck.check_type || 'http',
        enabled: sourceCheck.enabled ?? true,
        group_id: sourceCheck.group_id || '',
        parent_check_id: sourceCheck.parent_check_id || '',
        retry_count: sourceCheck.retry_count ?? 1,
        retry_delay_s: sourceCheck.retry_delay_s ?? 30,
        tags: Array.isArray(sourceCheck.tags) ? sourceCheck.tags : [],
        target_host: sourceCheck.target_host || '',
        target_port: sourceCheck.target_port ?? '',
        target_url: sourceCheck.target_url || '',
        http_method: sourceCheck.http_method || 'GET',
        http_expected_statuses:
          sourceCheck.http_expected_statuses || String(sourceCheck.http_expected_status || 200),
        http_content_match: sourceCheck.http_content_match || '',
        http_follow_redirects: sourceCheck.http_follow_redirects ?? true,
        http_ignore_tls_errors: sourceCheck.http_ignore_tls_errors ?? false,
        http_allow_insecure_auth: sourceCheck.http_allow_insecure_auth ?? false,
        credential_id: sourceCheck.credential_id || '',
        credential_auth_type: sourceCheck.credential_auth_type || '',
        workflow_enabled: Array.isArray(sourceCheck.workflow_steps) && sourceCheck.workflow_steps.length > 0,
        workflow_operator: sourceCheck.workflow_operator === 'any' ? 'any' : 'all',
        workflow_steps: Array.isArray(sourceCheck.workflow_steps) ? sourceCheck.workflow_steps : [],
        tls_warn_days: sourceCheck.tls_warn_days ?? 30,
        tls_critical_days: sourceCheck.tls_critical_days ?? 7,
        icmp_count: Number(cfg.count) || 3,
        dns_record_type: (cfg.record_type || 'A') as State['dns_record_type'],
        dns_expected: cfg.expected || '',
        check_interval: sourceCheck.check_interval || 60,
        timeout: sourceCheck.timeout || 10,
        description: sourceCheck.description || '',
      })
    } else {
      setS(empty)
    }
  }, [
    open,
    sourceCheck?.id,
    sourceCheck?.updated_at,
    sourceCheck?.credential_id,
  ])

  useEffect(() => {
    setTestResult(null)
  }, [s])

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

  function buildPayload() {
    const httpHost = hostFromUrl(s.target_url)
    const base: any = {
      name: s.name.trim(),
      check_type: s.check_type,
      level: LEVEL_BY_TYPE[s.check_type],
      enabled: s.enabled,
      group_id: s.group_id || null,
      parent_check_id: s.parent_check_id || null,
      retry_count: s.retry_count,
      retry_delay_s: s.retry_delay_s,
      tags: s.tags,
      target_host: s.check_type === 'http' ? httpHost : s.target_host.trim(),
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
      base.http_ignore_tls_errors = s.http_ignore_tls_errors
      base.http_allow_insecure_auth = s.http_allow_insecure_auth
      base.credential_id = s.credential_id || null
      base.workflow_operator = s.workflow_operator
      base.workflow_steps = s.workflow_enabled ? s.workflow_steps : []
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
    return base
  }

  const testConfig = useMutation({
    mutationFn: async () =>
      (await api.post('/service-checks/test-config', buildPayload())).data as ServiceTestResult,
    onSuccess: (result) => setTestResult(result),
    onError: (e: any) => {
      setTestResult(null)
      toast.error('Configuration test could not run', apiErrorMessage(e))
    },
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (s.check_type === 'http' && !hostFromUrl(s.target_url)) {
      toast.error('Invalid URL', 'Enter a complete URL beginning with http:// or https://')
      return
    }
    if (
      s.check_type === 'http' &&
      s.credential_id &&
      s.target_url.toLowerCase().startsWith('http://') &&
      effectiveCredentialAuthType !== 'ntlm' &&
      !s.http_allow_insecure_auth
    ) {
      toast.error('Trusted HTTP confirmation required', 'Enable trusted HTTP credential transmission below, or use HTTPS.')
      return
    }
    save.mutate(buildPayload())
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-3xl overflow-y-auto">
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

          {s.check_type !== 'http' && (
            <FormField label="Target host" required>
              <Input
                required
                value={s.target_host}
                onChange={(e) => setS({ ...s, target_host: e.target.value })}
                placeholder="api.example.com"
              />
            </FormField>
          )}

          {s.check_type === 'http' && (
            <div className="space-y-3 rounded-md border border-border p-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">HTTP options</div>
              <FormField label="URL" required>
                <Input
                  required
                  type="url"
                  value={s.target_url}
                  onChange={(e) => {
                    const target_url = e.target.value
                    setTestResult(null)
                    setS((current) => ({
                      ...current,
                      target_url,
                      target_host: hostFromUrl(target_url),
                      http_ignore_tls_errors: target_url.trim().toLowerCase().startsWith('https://')
                        ? current.http_ignore_tls_errors
                        : false,
                      http_allow_insecure_auth: target_url.trim().toLowerCase().startsWith('http://')
                        ? current.http_allow_insecure_auth
                        : false,
                      workflow_steps: current.workflow_steps.map((step, index) =>
                        index === 0 && (!step.url || step.url === current.target_url)
                          ? { ...step, url: target_url }
                          : step,
                      ),
                    }))
                  }}
                  placeholder="https://api.example.com/health"
                />
                {hostFromUrl(s.target_url) && (
                  <div className="mt-1 text-[11px] text-muted">Host detected automatically: {hostFromUrl(s.target_url)}</div>
                )}
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
              {isHttpsTarget && (
                <>
                  <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
                    <div>
                      <div className="text-xs font-medium uppercase tracking-wider text-muted">Ignore TLS/certificate errors</div>
                      <div className="mt-0.5 text-[11px] text-muted">Allow self-signed, expired, or hostname-mismatched certificates.</div>
                    </div>
                    <Switch
                      checked={s.http_ignore_tls_errors}
                      onCheckedChange={(v) => setS({ ...s, http_ignore_tls_errors: v })}
                    />
                  </div>
                  {s.http_ignore_tls_errors && (
                    <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] text-warning">
                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span>TLS verification is disabled. Use this only for trusted internal services; the certificate identity and chain will not be checked.</span>
                    </div>
                  )}
                </>
              )}
              {isHttpTarget && (
                <div className="flex items-start gap-2 rounded-md border border-primary/25 bg-primary/5 px-3 py-2 text-[11px] text-muted">
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                  <span>
                    Plain HTTP monitoring is supported and does not use a certificate. Traffic is unencrypted;
                    monitor an authentication boundary by accepting its expected 401 response. To monitor after IIS
                    sign-in, attach a Windows Integrated (NTLM) credential. Basic, bearer, and form credentials require
                    an explicit trusted-HTTP confirmation because their secrets can be intercepted.
                  </span>
                </div>
              )}
              {credentialHydrating ? (
                <div className="flex items-center gap-2 rounded-lg border border-border px-3 py-4 text-xs text-muted">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading saved credential…
                </div>
              ) : (
                <ServiceWorkflowFields
                  targetUrl={s.target_url}
                  credentialId={s.credential_id}
                  credentialAuthType={s.credential_auth_type}
                  onCredentialChange={(credential_id, credential_auth_type) => {
                    credentialSelectionTouched.current = true
                    setS((current) => ({
                      ...current,
                      credential_id,
                      credential_auth_type,
                      http_allow_insecure_auth: credential_auth_type === 'ntlm' || !credential_id
                        ? false
                        : current.http_allow_insecure_auth,
                    }))
                  }}
                  enabled={s.workflow_enabled}
                  onEnabledChange={(workflow_enabled) => setS((current) => ({ ...current, workflow_enabled }))}
                  operator={s.workflow_operator}
                  onOperatorChange={(workflow_operator) => setS((current) => ({ ...current, workflow_operator }))}
                  steps={s.workflow_steps}
                  onStepsChange={(workflow_steps) => setS((current) => ({ ...current, workflow_steps }))}
                />
              )}
              {s.credential_id && s.target_url.toLowerCase().startsWith('http://') && effectiveCredentialAuthType !== 'ntlm' && (
                <div className="space-y-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-xs font-semibold text-danger">Allow credentials over trusted HTTP</div>
                      <div className="mt-0.5 text-[11px] text-muted">Required only for this clear-text HTTP target.</div>
                    </div>
                    <Switch
                      checked={s.http_allow_insecure_auth}
                      onCheckedChange={(http_allow_insecure_auth) => setS((current) => ({ ...current, http_allow_insecure_auth }))}
                    />
                  </div>
                  <div className="text-[11px] text-danger">
                    Basic/form passwords and bearer tokens may be intercepted or modified on the network. Enable only on a trusted isolated network; HTTPS is strongly recommended.
                  </div>
                </div>
              )}
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

          {testResult && <ServiceTestResultPanel result={testResult} />}

          <DialogFooter className="justify-between">
            <div>
              {s.check_type === 'http' && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={
                    testConfig.isPending ||
                    !s.name.trim() ||
                    !hostFromUrl(s.target_url) ||
                    (
                      s.credential_id !== '' &&
                      s.target_url.toLowerCase().startsWith('http://') &&
                      effectiveCredentialAuthType !== 'ntlm' &&
                      !s.http_allow_insecure_auth
                    )
                  }
                  onClick={() => {
                    setTestResult(null)
                    testConfig.mutate()
                  }}
                >
                  {testConfig.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  {testConfig.isPending ? 'Testing…' : 'Test URL & authentication'}
                </Button>
              )}
            </div>
            <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create check'}
            </Button>
            </div>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
