import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, KeyRound, Loader2, Plus, Route, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { hasPermission, useAuth } from '@/stores/auth'
import type { ServiceCredential, ServiceWorkflowStep } from '@/types'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { ExpectedStatusInput } from '@/components/forms/ExpectedStatusInput'

type Props = {
  targetUrl: string
  credentialId: string
  credentialAuthType: ServiceCredential['auth_type'] | ''
  onCredentialChange: (id: string, authType: ServiceCredential['auth_type'] | '') => void
  enabled: boolean
  onEnabledChange: (value: boolean) => void
  operator: 'all' | 'any'
  onOperatorChange: (value: 'all' | 'any') => void
  steps: ServiceWorkflowStep[]
  onStepsChange: (steps: ServiceWorkflowStep[]) => void
}

const emptyStep = (url: string, index: number): ServiceWorkflowStep => ({
  name: index === 0 ? 'Sign in or open service' : `Navigation ${index + 1}`,
  url,
  method: 'GET',
  headers: {},
  body: null,
  expected_statuses: '200-399',
  content_match: null,
  follow_redirects: true,
})

export function ServiceWorkflowFields(props: Props) {
  const qc = useQueryClient()
  const user = useAuth((state) => state.user)
  const canManageCredentials = user?.role === 'owner' || hasPermission(user, 'system.admin')
  const [credentialOpen, setCredentialOpen] = useState(false)
  const [credentialDraft, setCredentialDraft] = useState({
    name: '',
    auth_type: 'form' as ServiceCredential['auth_type'],
    username: '',
    secret: '',
    description: '',
  })

  const { data: credentials = [] } = useQuery<ServiceCredential[]>({
    queryKey: ['service-credentials'],
    queryFn: async () => (await api.get('/service-credentials')).data,
  })
  const selectedCredential = credentials.find((credential) => credential.id === props.credentialId)
  const effectiveAuthType = selectedCredential?.auth_type || props.credentialAuthType

  function credentialLabel(authType: ServiceCredential['auth_type']): string {
    if (authType === 'form') return 'Form login'
    if (authType === 'basic') return 'HTTP Basic'
    if (authType === 'ntlm') return 'Windows Integrated (NTLM)'
    return 'Bearer token'
  }

  const createCredential = useMutation({
    mutationFn: async () => (await api.post('/service-credentials', credentialDraft)).data as ServiceCredential,
    onSuccess: (credential) => {
      qc.invalidateQueries({ queryKey: ['service-credentials'] })
      props.onCredentialChange(credential.id, credential.auth_type)
      setCredentialOpen(false)
      setCredentialDraft({ name: '', auth_type: 'form', username: '', secret: '', description: '' })
      toast.success('Credential saved securely')
      if (credential.auth_type === 'form' && !props.enabled) enableWorkflow('form')
    },
    onError: (error) => toast.error('Credential save failed', apiErrorMessage(error)),
  })

  function enableWorkflow(authType = effectiveAuthType) {
    try {
      const parsed = new URL(props.targetUrl)
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('invalid protocol')
    } catch {
      toast.error('Enter the service URL first', 'A complete HTTP(S) URL is required before adding navigation steps.')
      return
    }
    const first = emptyStep(props.targetUrl, 0)
    if (authType === 'form') {
      first.name = 'Sign in'
      first.method = 'POST'
      first.headers = { 'Content-Type': 'application/x-www-form-urlencoded' }
      first.body = 'username={{username}}&password={{password}}'
      const protectedPage = emptyStep(props.targetUrl, 1)
      protectedPage.name = 'Open protected page'
      const steps = props.steps.length >= 2
        ? props.steps
        : props.steps.length === 1
          ? [{
              ...props.steps[0],
              name: 'Sign in',
              method: 'POST' as const,
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: 'username={{username}}&password={{password}}',
            }, protectedPage]
          : [first, protectedPage]
      props.onStepsChange(steps)
      props.onEnabledChange(true)
      return
    }
    props.onStepsChange(props.steps.length > 0 ? props.steps : [first])
    props.onEnabledChange(true)
  }

  function selectCredential(value: string) {
    const id = value === 'none' ? '' : value
    const credential = credentials.find((item) => item.id === id)
    props.onCredentialChange(id, credential?.auth_type || '')
    if (credential?.auth_type === 'form' && (!props.enabled || props.steps.length < 2)) enableWorkflow('form')
  }

  function updateStep(index: number, patch: Partial<ServiceWorkflowStep>) {
    props.onStepsChange(props.steps.map((step, i) => (i === index ? { ...step, ...patch } : step)))
  }

  function changePayload(index: number, payloadType: 'none' | 'form' | 'json') {
    const step = props.steps[index]
    if (payloadType === 'none') {
      updateStep(index, { headers: {}, body: null })
    } else if (payloadType === 'form') {
      updateStep(index, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: step.body || 'username={{username}}&password={{password}}',
      })
    } else {
      updateStep(index, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: step.body || '{"username":"{{username}}","password":"{{password}}"}',
      })
    }
  }

  function payloadType(step: ServiceWorkflowStep): 'none' | 'form' | 'json' {
    const contentType = Object.entries(step.headers).find(([key]) => key.toLowerCase() === 'content-type')?.[1]
    if (contentType?.includes('form-urlencoded')) return 'form'
    if (contentType?.includes('json')) return 'json'
    return 'none'
  }

  function move(index: number, direction: -1 | 1) {
    const next = [...props.steps]
    const destination = index + direction
    if (destination < 0 || destination >= next.length) return
    ;[next[index], next[destination]] = [next[destination], next[index]]
    props.onStepsChange(next)
  }

  return (
    <div className="space-y-3 rounded-lg border border-primary/25 bg-primary/[0.03] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-primary" />
          <div>
            <div className="text-xs font-semibold uppercase tracking-wider">Authentication</div>
            <div className="text-[11px] text-muted">Secrets are encrypted and injected only at probe time.</div>
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!canManageCredentials}
          title={canManageCredentials ? 'Save a new encrypted credential' : 'Administrator permission is required to add credentials'}
          onClick={() => setCredentialOpen(true)}
        >
          <Plus className="h-3.5 w-3.5" /> New credential
        </Button>
      </div>

      <FormField label="Saved credential">
        <Select value={props.credentialId || 'none'} onValueChange={selectCredential}>
          <SelectTrigger><SelectValue placeholder="No authentication" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="none">No authentication</SelectItem>
            {props.credentialId && !selectedCredential && (
              <SelectItem value={props.credentialId}>
                Saved credential · {props.credentialAuthType ? credentialLabel(props.credentialAuthType) : 'Loading…'}
              </SelectItem>
            )}
            {credentials.map((credential) => (
              <SelectItem key={credential.id} value={credential.id}>
                {credential.name} · {credentialLabel(credential.auth_type)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </FormField>

      {props.credentialId && (
        <div className="rounded-md border border-success/25 bg-success/5 px-3 py-2 text-[11px] text-muted">
          <span className="font-semibold text-success">Protected credential linked.</span>{' '}
          {effectiveAuthType === 'basic' && 'The username and password will be sent with HTTP Basic authentication. '}
          {effectiveAuthType === 'bearer' && 'The token will be sent in the Authorization header. '}
          {effectiveAuthType === 'form' && 'The sign-in step will inject the username and password and retain its session cookies. '}
          {effectiveAuthType === 'ntlm' && 'The probe will complete the IIS Negotiate/NTLM challenge using the saved Windows account. The plaintext password is not sent as HTTP Basic authentication. '}
          The secret is never returned to this browser or written into the service definition.
        </div>
      )}

      {effectiveAuthType === 'ntlm' && props.targetUrl.trim().toLowerCase().startsWith('http://') && (
        <div className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] text-warning">
          Trusted internal HTTP mode: NTLM challenge-response protects the password itself, but the page traffic is unencrypted and NTLM relay remains possible. Prefer HTTPS whenever IIS can provide it.
        </div>
      )}

      <div className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2">
        <div className="flex items-center gap-2">
          <Route className="h-4 w-4 text-primary" />
          <div>
            <div className="text-xs font-medium">Multi-step service journey</div>
            <div className="text-[11px] text-muted">Sign in once, retain cookies, then test protected pages in order.</div>
          </div>
        </div>
        <Switch
          checked={props.enabled}
          onCheckedChange={(checked) => {
            if (checked) enableWorkflow()
            else {
              props.onEnabledChange(false)
              props.onStepsChange([])
            }
          }}
        />
      </div>

      {props.enabled && (
        <div className="space-y-3">
          <div className="grid grid-cols-[1fr_auto] items-end gap-3">
            <FormField label="Journey health rule">
              <Select value={props.operator} onValueChange={(value: 'all' | 'any') => props.onOperatorChange(value)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">ALL steps must pass (AND)</SelectItem>
                  <SelectItem value="any">ANY step may pass (OR)</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <Button
              type="button"
              variant="outline"
              disabled={props.steps.length >= 10}
              onClick={() => props.onStepsChange([...props.steps, emptyStep(props.targetUrl, props.steps.length)])}
            >
              <Plus className="h-3.5 w-3.5" /> Add navigation
            </Button>
          </div>
          <p className="text-[11px] text-muted">
            All URLs must stay on the same origin when a credential is linked. HTTPS is recommended; trusted HTTP requires an explicit confirmation for Basic, bearer, and form credentials. Form steps may use{' '}
            <code>{'{{username}}'}</code> and <code>{'{{password}}'}</code>; token credentials use <code>{'{{token}}'}</code>.
          </p>

          {props.steps.map((step, index) => (
            <div key={index} className="space-y-3 rounded-md border border-border bg-surface p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold">Step {index + 1}</span>
                <div className="flex gap-1">
                  <Button type="button" variant="ghost" size="icon" className="h-7 w-7" disabled={index === 0} onClick={() => move(index, -1)}>
                    <ChevronUp className="h-3.5 w-3.5" />
                  </Button>
                  <Button type="button" variant="ghost" size="icon" className="h-7 w-7" disabled={index === props.steps.length - 1} onClick={() => move(index, 1)}>
                    <ChevronDown className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-danger"
                    disabled={props.steps.length <= (effectiveAuthType === 'form' ? 2 : 1)}
                    onClick={() => props.onStepsChange(props.steps.filter((_, i) => i !== index))}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
              <div className="grid grid-cols-[1fr_120px] gap-3">
                <FormField label="Step name"><Input value={step.name} onChange={(event) => updateStep(index, { name: event.target.value })} /></FormField>
                <FormField label="Method">
                  <Select value={step.method} onValueChange={(value: ServiceWorkflowStep['method']) => updateStep(index, { method: value })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>{['GET', 'POST', 'HEAD', 'PUT'].map((method) => <SelectItem key={method} value={method}>{method}</SelectItem>)}</SelectContent>
                  </Select>
                </FormField>
              </div>
              <FormField label="URL"><Input required type="url" value={step.url} onChange={(event) => updateStep(index, { url: event.target.value })} /></FormField>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Expected status"><ExpectedStatusInput compact value={step.expected_statuses} onChange={(value) => updateStep(index, { expected_statuses: value })} /></FormField>
                <FormField label="Response must contain"><Input value={step.content_match || ''} onChange={(event) => updateStep(index, { content_match: event.target.value || null })} placeholder="Optional business-health text" /></FormField>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Request payload">
                  <Select value={payloadType(step)} onValueChange={(value: 'none' | 'form' | 'json') => changePayload(index, value)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None</SelectItem>
                      <SelectItem value="form">Form encoded</SelectItem>
                      <SelectItem value="json">JSON</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <div className="flex items-center justify-between rounded-md border border-border px-3">
                  <span className="text-xs text-muted">Follow redirects</span>
                  <Switch checked={step.follow_redirects} onCheckedChange={(value) => updateStep(index, { follow_redirects: value })} />
                </div>
              </div>
              {payloadType(step) !== 'none' && (
                <FormField label="Request body">
                  <textarea
                    value={step.body || ''}
                    onChange={(event) => updateStep(index, { body: event.target.value })}
                    rows={3}
                    className="w-full rounded-md border border-border bg-surface2 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                  />
                </FormField>
              )}
            </div>
          ))}
        </div>
      )}

      <Dialog open={credentialOpen} onOpenChange={setCredentialOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>Add encrypted service credential</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <FormField label="Credential name" required><Input value={credentialDraft.name} onChange={(event) => setCredentialDraft({ ...credentialDraft, name: event.target.value })} placeholder="Production customer portal" /></FormField>
            <FormField label="Authentication method">
              <Select value={credentialDraft.auth_type} onValueChange={(value: ServiceCredential['auth_type']) => setCredentialDraft({ ...credentialDraft, auth_type: value })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="form">Web form / session login</SelectItem>
                  <SelectItem value="basic">HTTP Basic (username + password)</SelectItem>
                  <SelectItem value="ntlm">Windows Integrated / IIS (NTLM)</SelectItem>
                  <SelectItem value="bearer">Bearer / API token</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            {credentialDraft.auth_type !== 'bearer' && (
              <FormField label="Username" required><Input autoComplete="off" value={credentialDraft.username} onChange={(event) => setCredentialDraft({ ...credentialDraft, username: event.target.value })} /></FormField>
            )}
            <FormField label={credentialDraft.auth_type === 'bearer' ? 'Token' : 'Password'} required>
              <Input type="password" autoComplete="new-password" value={credentialDraft.secret} onChange={(event) => setCredentialDraft({ ...credentialDraft, secret: event.target.value })} />
            </FormField>
            <FormField label="Description"><Input value={credentialDraft.description} onChange={(event) => setCredentialDraft({ ...credentialDraft, description: event.target.value })} placeholder="Owner and intended service" /></FormField>
            <p className="text-[11px] text-muted">Only administrators can create or rotate credentials. The secret cannot be retrieved after saving.</p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setCredentialOpen(false)}>Cancel</Button>
            <Button
              type="button"
              disabled={
                !credentialDraft.name ||
                !credentialDraft.secret ||
                (credentialDraft.auth_type !== 'bearer' && !credentialDraft.username) ||
                createCredential.isPending
              }
              onClick={() => createCredential.mutate()}
            >
              {createCredential.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save securely
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
