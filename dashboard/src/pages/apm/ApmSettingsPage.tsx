import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Plus, Trash2, Copy, Check, KeyRound, Loader2, ShieldCheck, Ticket, Terminal,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Badge } from '@/components/ui/Badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { KbLink } from '@/components/apm/KbLink'
import { ApmAgentsTab } from './ApmAgentsTab'

interface IngestKey {
  id: string
  name: string
  kind: 'sdk' | 'rum'
  key_prefix: string
  env: string | null
  origin_allowlist: string[]
  application_id: string | null
  enabled: boolean
  last_used_at: string | null
  revoked_at: string | null
  created_at: string
}

interface RumSdkOptions {
  applicationId: string
  serviceName: string
  version: string
  sampleRatePercent: number
  replaySampleRatePercent: number
  trackActions: boolean
  trackLongTasks: boolean
  consent: 'granted' | 'pending'
  privacy: 'mask-user-input' | 'strict'
}

interface CreatedKeyConfig extends IngestKey {
  key: string
  rum?: RumSdkOptions
}

interface EnrollmentToken {
  id: string
  token_prefix: string
  kind: 'sdk' | 'rum'
  env: string | null
  max_uses: number
  uses: number
  expires_at: string | null
  revoked_at: string | null
  created_at: string
}

interface Environment {
  id: string
  name: string
}

const SETTINGS_TABS = [
  { key: 'agents', label: 'APM agents' },
  { key: 'start', label: 'Getting started' },
  { key: 'keys', label: 'Ingest keys' },
  { key: 'quality', label: 'Data quality' },
] as const
type SettingsTab = typeof SETTINGS_TABS[number]['key']

export function ApmSettingsPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const requestedTab = params.get('tab')
  const tab: SettingsTab = SETTINGS_TABS.some((item) => item.key === requestedTab)
    ? requestedTab as SettingsTab
    : 'agents'
  const setTab = (t: SettingsTab) => {
    const n = new URLSearchParams(params); n.set('tab', t); setParams(n, { replace: true })
  }
  const [formOpen, setFormOpen] = useState(false)
  const [formKind, setFormKind] = useState<'sdk' | 'rum'>('sdk')
  const [formOrigin, setFormOrigin] = useState('')
  const [revoking, setRevoking] = useState<IngestKey | null>(null)
  const [createdKey, setCreatedKey] = useState<CreatedKeyConfig | null>(null)

  const openCreateKey = (kind: 'sdk' | 'rum' = 'sdk', origin = '') => {
    setFormKind(kind)
    setFormOrigin(origin)
    setFormOpen(true)
  }

  useEffect(() => {
    if (params.get('create') !== 'rum') return
    openCreateKey('rum', params.get('origin') ?? '')
    const next = new URLSearchParams(params)
    next.set('tab', 'keys')
    next.delete('create')
    next.delete('origin')
    setParams(next, { replace: true })
  // Consume this navigation command once; reacting to every params identity change would reopen the dialog.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.get('create')])

  const keysQuery = useQuery<IngestKey[]>({
    queryKey: ['apm', 'ingest-keys'],
    queryFn: async () => (await api.get('/apm/ingest-keys')).data,
    refetchInterval: (query) => {
      const rows = query.state.data as IngestKey[] | undefined
      return createdKey && !rows?.find((key) => key.id === createdKey.id)?.last_used_at ? 5_000 : false
    },
  })

  const revoke = useMutation({
    mutationFn: async (id: string) => api.delete(`/apm/ingest-keys/${id}`),
    onSuccess: () => {
      toast.success('Ingest key revoked')
      qc.invalidateQueries({ queryKey: ['apm', 'ingest-keys'] })
      setRevoking(null)
    },
    onError: (e: any) => toast.error('Revoke failed', apiErrorMessage(e)),
  })

  const keys = keysQuery.data ?? []

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="APM Settings"
        description="Agent deployment, ingest keys, and the health of the telemetry pipeline itself."
        article="settings"
      />

      <div className="flex items-center gap-1 overflow-x-auto border-b border-border">
        {SETTINGS_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            aria-current={tab === t.key ? 'page' : undefined}
            onClick={() => setTab(t.key)}
            className={`-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === t.key ? 'border-primary text-text' : 'border-transparent text-muted hover:text-text'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'agents' && <ApmAgentsTab />}
      {tab === 'start' && <GettingStartedTab keys={keys} onCreateKey={() => openCreateKey('sdk')} />}
      {tab === 'quality' && <DataQualityCard />}

      {tab === 'keys' && (
        <>
      {keysQuery.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load ingest keys — {apiErrorMessage(keysQuery.error)}
        </div>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-primary" /> Ingest Keys
          </CardTitle>
          <Button onClick={() => openCreateKey('sdk')}>
            <Plus className="w-4 h-4 mr-1" /> Create ingest key
          </Button>
        </CardHeader>
        <CardContent>
          {keysQuery.isLoading ? (
            <div className="flex items-center gap-2 text-muted py-8 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : keys.length === 0 ? (
            <div className="text-center text-muted py-10">
              No ingest keys yet. Create one to send server traces or browser telemetry.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Key prefix</Th>
                  <Th>Scope</Th>
                  <Th>Status</Th>
                  <Th>Last used</Th>
                  <Th>Created</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {keys.map((k) => (
                  <Tr key={k.id}>
                    <Td className="font-medium text-text">{k.name}</Td>
                    <Td><Badge variant="outline">{k.kind.toUpperCase()}</Badge></Td>
                    <Td className="font-mono text-xs">{k.key_prefix}…</Td>
                    <Td className="max-w-[20rem]">
                      <div>{k.env ?? <span className="text-muted">all environments</span>}</div>
                      {k.kind === 'rum' && (
                        <>
                          <div className="mt-0.5 text-xs text-muted">
                            App: <span className="font-mono text-text2">{k.application_id || 'legacy / unbound'}</span>
                          </div>
                          <div className="truncate text-xs text-muted" title={(k.origin_allowlist ?? []).join(', ')}>
                            {(k.origin_allowlist ?? []).join(', ') || 'No browser origins'}
                          </div>
                        </>
                      )}
                    </Td>
                    <Td>
                      {k.revoked_at || !k.enabled ? (
                        <Badge variant="danger">Revoked</Badge>
                      ) : (
                        <Badge variant="success">Active</Badge>
                      )}
                    </Td>
                    <Td className="text-xs text-muted">
                      {k.last_used_at
                        ? relativeTime(k.last_used_at)
                        : <span title="No telemetry has ever authenticated with this key">never used</span>}
                    </Td>
                    <Td className="text-muted text-xs">
                      {new Date(k.created_at).toLocaleDateString()}
                    </Td>
                    <Td className="text-right">
                      {!k.revoked_at && k.enabled && (
                        <Button variant="ghost" size="sm" onClick={() => setRevoking(k)}>
                          <Trash2 className="w-4 h-4 text-danger" />
                        </Button>
                      )}
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
        </>
      )}

      <IngestKeyFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        defaultKind={formKind}
        defaultOrigin={formOrigin}
        onCreated={(created) => { setFormOpen(false); setCreatedKey(created) }}
      />

      <CopyOnceDialog
        value={createdKey}
        lastUsedAt={createdKey ? keys.find((key) => key.id === createdKey.id)?.last_used_at ?? null : null}
        onClose={() => setCreatedKey(null)}
      />

      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Revoke ingest key"
        description={revoking ? `Revoke "${revoking.name}"? Agents using it will be rejected within 30s and the key cannot be restored.` : ''}
        confirmText="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={() => { if (revoking) revoke.mutate(revoking.id) }}
      />
    </div>
  )
}

// ─── Getting started ────────────────────────────────────────────────────────

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-md border border-border bg-surface2/50 p-3 text-xs leading-relaxed text-text">
        <code>{code}</code>
      </pre>
      <button
        onClick={() => {
          navigator.clipboard?.writeText(code)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
        className="absolute right-2 top-2 rounded border border-border bg-surface px-1.5 py-1 text-muted hover:text-text"
        title="Copy"
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  )
}

/**
 * The onboarding path that did not exist: this screen used to show a key table
 * and nothing else, so a new operator had a key and no idea what to do with it.
 * Endpoint URLs are derived from the browser's origin, which is by definition
 * an address that reaches this appliance.
 */
function GettingStartedTab({ keys, onCreateKey }: { keys: IngestKey[]; onCreateKey: () => void }) {
  const origin = window.location.origin
  const sdkKeys = keys.filter((k) => k.kind === 'sdk')
  const activeKey = sdkKeys.find((k) => k.enabled && !k.revoked_at)
  const keyPlaceholder = activeKey ? `${activeKey.key_prefix}…` : 'zpi_your_key_here'
  const hasTraffic = sdkKeys.some((k) => k.last_used_at)

  const steps: { title: string; body: React.ReactNode }[] = [
    {
      title: 'Create an ingest key',
      body: (
        <>
          <p className="text-sm text-muted">
            Every producer authenticates with a <code className="rounded bg-surface2 px-1">zpi_</code> key scoped to an
            environment. Create one per environment (or per service, if you want to revoke them independently).
          </p>
          {sdkKeys.length === 0 ? (
            <Button size="sm" className="mt-2" onClick={onCreateKey}>
              <Plus className="h-4 w-4" /> Create ingest key
            </Button>
          ) : (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-success">
              <Check className="h-3.5 w-3.5" /> {sdkKeys.length} SDK key{sdkKeys.length === 1 ? '' : 's'} configured
            </p>
          )}
        </>
      ),
    },
    {
      title: 'Point an OpenTelemetry SDK at the appliance',
      body: (
        <>
          <p className="mb-2 text-sm text-muted">
            The receiver accepts <strong>OTLP/HTTP protobuf (recommended) and JSON</strong>. Use the local managed
            gateway when the ZenPlus APM agent is installed; direct SDK export is available for advanced deployments.
          </p>
          <CodeBlock code={`export OTEL_EXPORTER_OTLP_ENDPOINT="${origin}"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ${keyPlaceholder}"
export OTEL_SERVICE_NAME="checkout-service"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=prod,service.version=1.4.2"`} />
        </>
      ),
    },
    {
      title: 'Verify spans are arriving',
      body: (
        <>
          <p className="mb-2 text-sm text-muted">
            Send one span by hand. A <code className="rounded bg-surface2 px-1">{'{"partialSuccess":{}}'}</code> response
            means it was accepted.
          </p>
          <CodeBlock code={`curl -sS -X POST ${origin}/v1/traces \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyPlaceholder}" \\
  -d '{"resourceSpans":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"hello-service"}}]},
      "scopeSpans":[{"spans":[{
        "traceId":"5b8efff798038103d269b633813fc60c",
        "spanId":"eee19b7ec3c1b174","name":"GET /health","kind":2,
        "startTimeUnixNano":"'$(date +%s)'000000000",
        "endTimeUnixNano":"'$(date +%s)'100000000",
        "status":{"code":1}}]}]}]}'`} />
          <p className="mt-2 text-xs text-muted">
            {hasTraffic
              ? 'At least one key has authenticated telemetry — the pipeline is live.'
              : 'No key has authenticated yet. Once one does, its “last used” timestamp appears on the Ingest keys tab.'}
          </p>
        </>
      ),
    },
    {
      title: 'Set a reliability target',
      body: (
        <p className="text-sm text-muted">
          Services and traces populate on their own. What does <em>not</em> happen automatically is paging: define an SLO
          so error-budget burn raises an alert, and add a synthetic scenario for the journeys that matter most.
        </p>
      ),
    },
  ]

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Terminal className="h-4 w-4 text-primary" /> Instrument your first service
            </CardTitle>
            <p className="mt-1 text-xs text-muted">
              Four steps from an empty APM module to a service reporting golden signals.
            </p>
          </div>
          <KbLink article="getting-started" label="Full setup guide" />
        </CardHeader>
        <CardContent className="space-y-5">
          {steps.map((s, i) => (
            <div key={s.title} className="flex gap-3">
              <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-surface2 text-xs font-semibold text-text">
                {i + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="mb-1 text-sm font-medium text-text">{s.title}</div>
                {s.body}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Enrollment tokens ──────────────────────────────────────────────────────

/**
 * Short-lived, use-capped tokens a host redeems for its own ingest key, so a
 * provisioning script never has to carry a long-lived credential. The API has
 * shipped these since the module launched; this is the first UI for them.
 */
function EnrollmentTokensTab() {
  const qc = useQueryClient()
  const [created, setCreated] = useState<string | null>(null)
  const [revoking, setRevoking] = useState<EnrollmentToken | null>(null)
  const [env, setEnv] = useState('prod')
  const [maxUses, setMaxUses] = useState('1')
  const [hours, setHours] = useState('720')

  const q = useQuery<EnrollmentToken[]>({
    queryKey: ['apm', 'enrollment-tokens'],
    queryFn: async () => (await api.get('/apm/enrollment-tokens')).data,
  })
  const envs = useQuery<Environment[]>({
    queryKey: ['apm', 'environments'],
    queryFn: async () => (await api.get('/apm/environments')).data,
  })
  const create = useMutation({
    mutationFn: async () => (await api.post('/apm/enrollment-tokens', {
      kind: 'sdk', env, max_uses: Number(maxUses) || 1, expires_in_hours: Number(hours) || 720,
    })).data,
    onSuccess: (d: { token: string }) => {
      qc.invalidateQueries({ queryKey: ['apm', 'enrollment-tokens'] })
      setCreated(d.token)
    },
    onError: (e: any) => toast.error('Could not create token', apiErrorMessage(e)),
  })
  const revoke = useMutation({
    mutationFn: async (id: string) => api.delete(`/apm/enrollment-tokens/${id}`),
    onSuccess: () => {
      toast.success('Enrollment token revoked')
      qc.invalidateQueries({ queryKey: ['apm', 'enrollment-tokens'] })
      setRevoking(null)
    },
    onError: (e: any) => toast.error('Revoke failed', apiErrorMessage(e)),
  })

  const tokens = q.data ?? []
  const state = (t: EnrollmentToken) => {
    if (t.revoked_at) return <Badge variant="danger">Revoked</Badge>
    if (t.expires_at && new Date(t.expires_at) < new Date()) return <Badge variant="outline">Expired</Badge>
    if (t.uses >= t.max_uses) return <Badge variant="outline">Used up</Badge>
    return <Badge variant="success">Available</Badge>
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Ticket className="h-4 w-4 text-primary" /> Enrollment tokens
          </CardTitle>
          <p className="text-xs text-muted">
            A one-shot token a host redeems for its own ingest key at provisioning time. Prefer these over baking a
            long-lived <code className="rounded bg-surface2 px-1">zpi_</code> key into an image — a token expires, is
            use-capped, and can be revoked before it is ever redeemed.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-2">
            <FormField label="Environment">
              <Select value={env} onValueChange={setEnv}>
                <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(envs.data ?? []).map((e) => <SelectItem key={e.id} value={e.name}>{e.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Max uses">
              <Input className="w-24" type="number" min={1} max={100} value={maxUses}
                onChange={(e) => setMaxUses(e.target.value)} />
            </FormField>
            <FormField label="Expires in (hours)">
              <Input className="w-32" type="number" min={1} max={8760} value={hours}
                onChange={(e) => setHours(e.target.value)} />
            </FormField>
            <Button size="sm" disabled={create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Generate token
            </Button>
          </div>

          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : tokens.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted">
              No enrollment tokens. Generate one when provisioning a host that should self-register.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Token</Th><Th>Environment</Th><Th className="text-right">Uses</Th>
                  <Th>Expires</Th><Th>State</Th><Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {tokens.map((t) => (
                  <Tr key={t.id}>
                    <Td className="font-mono text-xs">{t.token_prefix}…</Td>
                    <Td>{t.env ?? <span className="text-muted">all</span>}</Td>
                    <Td className="text-right tabular-nums">{t.uses} / {t.max_uses}</Td>
                    <Td className="text-xs text-muted">
                      {t.expires_at ? new Date(t.expires_at).toLocaleString() : 'never'}
                    </Td>
                    <Td>{state(t)}</Td>
                    <Td className="text-right">
                      {!t.revoked_at && (
                        <Button variant="ghost" size="sm" onClick={() => setRevoking(t)}>
                          <Trash2 className="h-4 w-4 text-danger" />
                        </Button>
                      )}
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <CopyOnceDialog value={created} onClose={() => setCreated(null)} />
      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Revoke enrollment token"
        description="Revoke this token? Hosts that have not redeemed it yet will fail to enrol."
        confirmText="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={() => { if (revoking) revoke.mutate(revoking.id) }}
      />
    </>
  )
}

// ─── Data quality ───────────────────────────────────────────────────────────

interface DataQuality {
  ingest: {
    accepted: number; rejected: number; dropped: number; skewed: number
    flushes: number; reject_rate: number; queue_depth: number | null
    series: { t: string; accepted: number; rejected: number; dropped: number }[]
  }
  services: { name: string; health: string; last_seen_at: string | null; silent_for_s: number | null; reporting: boolean }[]
  agent_forwarders: {
    agent_id: string; hostname: string; agent_status: string
    enabled?: boolean; failed?: boolean; spans_forwarded_1m?: number
    export_errors_1m?: number; spool_depth_spans?: number; last_error?: string
  }[]
  health: 'ok' | 'issues'
  issues: string[]
}

function DqStat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: 'danger' | 'warning' }) {
  return (
    <div className="rounded-lg border border-border bg-surface2/30 px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold tabular-nums ${
        tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text'}`}>
        {value}
      </div>
    </div>
  )
}

function DataQualityCard() {
  const q = useQuery<DataQuality>({
    queryKey: ['apm', 'data-quality'],
    queryFn: async () => (await api.get('/apm/data-quality?hours=24')).data,
    refetchInterval: 30000,
  })
  const d = q.data

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-primary" /> Data Quality
        </CardTitle>
        <p className="text-xs text-muted">
          Monitors the telemetry pipeline itself — rejected or clock-skewed spans, flush
          failures, silent services and agent forwarder health — so bad data is caught
          before it corrupts dashboards. Counters cover the last 24 hours.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {q.isError && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            Failed to load — {apiErrorMessage(q.error)}
          </div>
        )}
        {d && (
          <>
            <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
              d.health === 'ok'
                ? 'border-success/30 bg-success/10 text-success'
                : 'border-warning/40 bg-warning/10 text-warning'}`}>
              {d.health === 'ok'
                ? <><Check className="h-4 w-4 shrink-0" /> Telemetry pipeline healthy — no data-quality issues in the last 24h.</>
                : <div>{d.issues.map((i, n) => <div key={n}>• {i}</div>)}</div>}
            </div>

            <div className="grid gap-2 sm:grid-cols-5">
              <DqStat label="Spans accepted" value={d.ingest.accepted.toLocaleString()} />
              <DqStat label="Rejected" value={d.ingest.rejected.toLocaleString()}
                tone={d.ingest.rejected ? 'warning' : undefined} />
              <DqStat label="Clock-skewed" value={d.ingest.skewed.toLocaleString()}
                tone={d.ingest.skewed ? 'warning' : undefined} />
              <DqStat label="Dropped (flush)" value={d.ingest.dropped.toLocaleString()}
                tone={d.ingest.dropped ? 'danger' : undefined} />
              <DqStat label="Queue depth" value={d.ingest.queue_depth ?? '—'} />
            </div>

            {d.services.length > 0 && (
              <div>
                <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                  Service reporting freshness
                </div>
                <div className="flex flex-wrap gap-2">
                  {d.services.map((s) => (
                    <span key={s.name} className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                      s.reporting
                        ? 'border-border bg-surface2 text-text2'
                        : 'border-danger/40 bg-danger/10 text-danger'}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${s.reporting ? 'bg-success' : 'bg-danger'}`} />
                      {s.name}
                      <span className="text-muted">
                        {s.silent_for_s == null ? 'never seen'
                          : s.silent_for_s < 120 ? 'live'
                          : `${Math.round(s.silent_for_s / 60)}m ago`}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {d.agent_forwarders.length > 0 && (
              <div>
                <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                  Agent APM forwarders
                </div>
                <Table>
                  <THead>
                    <Tr>
                      <Th>Host</Th><Th>State</Th>
                      <Th className="text-right">Spans/min</Th>
                      <Th className="text-right">Export errors</Th>
                      <Th className="text-right">Spool</Th>
                      <Th>Last error</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {d.agent_forwarders.map((f) => (
                      <Tr key={f.agent_id}>
                        <Td className="font-medium text-text">{f.hostname}</Td>
                        <Td>
                          {f.failed
                            ? <Badge variant="danger">Failed</Badge>
                            : f.enabled
                              ? <Badge variant="success">Forwarding</Badge>
                              : <Badge variant="outline">Off</Badge>}
                        </Td>
                        <Td className="text-right tabular-nums">{f.spans_forwarded_1m ?? '—'}</Td>
                        <Td className={`text-right tabular-nums ${f.export_errors_1m ? 'text-danger' : ''}`}>
                          {f.export_errors_1m ?? '—'}
                        </Td>
                        <Td className="text-right tabular-nums">{f.spool_depth_spans ?? '—'}</Td>
                        <Td className="max-w-[16rem] truncate text-xs text-muted" title={f.last_error || ''}>
                          {f.last_error || '—'}
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function parseRumOrigins(raw: string): { origins: string[]; error: string | null } {
  const values = raw.split(/[\n,]/).map((value) => value.trim()).filter(Boolean)
  if (!values.length) return { origins: [], error: 'Add at least one browser origin.' }
  const origins: string[] = []
  for (const value of values) {
    try {
      if (value.includes('*')) throw new Error('wildcard')
      const url = new URL(value)
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password
        || (url.pathname && url.pathname !== '/') || url.search || url.hash) {
        throw new Error('invalid')
      }
      origins.push(url.origin)
    } catch {
      return {
        origins: [],
        error: `“${value}” is not an exact HTTP(S) origin. Remove paths, credentials, queries, fragments, and wildcards.`,
      }
    }
  }
  return { origins: Array.from(new Set(origins)), error: null }
}

function IngestKeyFormDialog({ open, onOpenChange, onCreated, defaultKind, defaultOrigin }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onCreated: (created: CreatedKeyConfig) => void
  defaultKind: 'sdk' | 'rum'
  defaultOrigin: string
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [env, setEnv] = useState<string>('prod')
  const [kind, setKind] = useState<'sdk' | 'rum'>('sdk')
  const [origins, setOrigins] = useState('')
  const [applicationId, setApplicationId] = useState('web-app')
  const [serviceName, setServiceName] = useState('web-frontend')
  const [version, setVersion] = useState('')
  const [sampleRate, setSampleRate] = useState('100')
  const [replaySampleRate, setReplaySampleRate] = useState('0')
  const [trackActions, setTrackActions] = useState(true)
  const [trackLongTasks, setTrackLongTasks] = useState(true)
  const [consent, setConsent] = useState<'granted' | 'pending'>('granted')
  const [privacy, setPrivacy] = useState<'mask-user-input' | 'strict'>('mask-user-input')

  useEffect(() => {
    if (!open) return
    setKind(defaultKind)
    setOrigins(defaultOrigin)
    setName(defaultKind === 'rum' ? 'browser-rum-prod' : '')
    setApplicationId('web-app')
    setServiceName('web-frontend')
    setVersion('')
    setSampleRate('100')
    setReplaySampleRate('0')
    setTrackActions(true)
    setTrackLongTasks(true)
    setConsent('granted')
    setPrivacy('mask-user-input')
  }, [open, defaultKind, defaultOrigin])

  const parsedOrigins = useMemo(() => parseRumOrigins(origins), [origins])
  const originError = kind === 'rum'
    ? (!origins.trim() ? 'Add at least one exact HTTP(S) browser origin.' : parsedOrigins.error)
    : null
  const applicationError = kind === 'rum' && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(applicationId.trim())
    ? 'Use 1–128 letters, numbers, dots, underscores, or hyphens.'
    : null
  const sampleRateNumber = Number(sampleRate)
  const replayRateNumber = Number(replaySampleRate)
  const samplingError = !Number.isFinite(sampleRateNumber) || sampleRateNumber < 1 || sampleRateNumber > 100
    ? 'Session sampling must be between 1% and 100%.'
    : null
  const replaySamplingError = !Number.isFinite(replayRateNumber) || replayRateNumber !== 0
    ? 'Session replay capture and storage are not enabled in this release; keep this at 0%.'
    : null

  const envs = useQuery<Environment[]>({
    queryKey: ['apm', 'environments'],
    queryFn: async () => (await api.get('/apm/environments')).data,
    enabled: open,
  })

  const create = useMutation({
    mutationFn: async () =>
      (await api.post('/apm/ingest-keys', {
        name: name.trim(), kind, env,
        origin_allowlist: kind === 'rum' ? parsedOrigins.origins : [],
        application_id: kind === 'rum' ? applicationId.trim() : null,
      })).data,
    onSuccess: (data: IngestKey & { key: string }) => {
      qc.invalidateQueries({ queryKey: ['apm', 'ingest-keys'] })
      onCreated({
        ...data,
        rum: kind === 'rum' ? {
          applicationId: applicationId.trim(),
          serviceName: serviceName.trim() || applicationId.trim(),
          version: version.trim(),
          sampleRatePercent: sampleRateNumber,
          replaySampleRatePercent: replayRateNumber,
          trackActions,
          trackLongTasks,
          consent,
          privacy,
        } : undefined,
      })
    },
    onError: (e: any) => toast.error('Could not create key', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Create ingest key</DialogTitle>
          <DialogDescription>
            Create a secret collector key or a public Browser RUM key restricted to one application and an exact origin allowlist.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <FormField label="Name" required>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-checkout-service" />
          </FormField>
          <FormField label="Type">
            <Select value={kind} onValueChange={(value) => setKind(value as 'sdk' | 'rum')}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="sdk">SDK / Collector (secret zpi_)</SelectItem>
                <SelectItem value="rum">Browser RUM (public origin-scoped zpr_)</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Environment">
            <Select value={env} onValueChange={setEnv}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {(envs.data ?? []).map((e) => (
                  <SelectItem key={e.id} value={e.name}>{e.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          {kind === 'rum' && (
            <div className="space-y-4 rounded-lg border border-border bg-surface2/30 p-4">
              <div>
                <div className="text-sm font-medium text-text">Browser application scope</div>
                <p className="mt-0.5 text-xs text-muted">
                  A public browser key is accepted only for this application and these exact origins.
                </p>
              </div>
              <FormField label="Application ID" required error={applicationError}
                hint="A stable identifier used by filters and application-level key enforcement.">
                <Input value={applicationId} onChange={(e) => setApplicationId(e.target.value)}
                  placeholder="customer-portal" autoComplete="off" />
              </FormField>
              <FormField label="Allowed browser origins" required
                error={originError}
                hint="One per line or comma-separated. A trailing slash is normalized; paths and wildcards are rejected.">
                <textarea
                  className="min-h-20 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none placeholder:text-muted focus:border-primary focus:ring-2 focus:ring-primary/20"
                  value={origins}
                  onChange={(e) => setOrigins(e.target.value)}
                  placeholder={'https://portal.example.com\nhttp://192.168.8.19:8080'}
                />
              </FormField>

              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="Frontend service name" required hint="Used for frontend-to-backend trace pivots.">
                  <Input value={serviceName} onChange={(e) => setServiceName(e.target.value)} placeholder="customer-portal-web" />
                </FormField>
                <FormField label="Release version" hint="Optional; enables release comparison and error context.">
                  <Input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="2026.08.27" />
                </FormField>
                <FormField label="Session sample rate" error={samplingError} hint="Deterministic per-session sampling; errors are retained.">
                  <div className="relative">
                    <Input type="number" min={1} max={100} step={1} value={sampleRate}
                      onChange={(e) => setSampleRate(e.target.value)} className="pr-8" />
                    <span className="pointer-events-none absolute right-3 top-2 text-sm text-muted">%</span>
                  </div>
                </FormField>
                <FormField label="Session replay (not enabled)" error={replaySamplingError}
                  hint="Replay requires a separate privacy-reviewed capture and storage pipeline. This SDK advertises replay as unavailable.">
                  <div className="relative">
                    <Input type="number" min={0} max={0} step={1} value={replaySampleRate}
                      onChange={(e) => setReplaySampleRate(e.target.value)} className="pr-8" disabled />
                    <span className="pointer-events-none absolute right-3 top-2 text-sm text-muted">%</span>
                  </div>
                </FormField>
                <FormField label="Consent at startup" hint="Pending sends nothing until ZenPlusRUM.grantConsent() is called.">
                  <Select value={consent} onValueChange={(value) => setConsent(value as 'granted' | 'pending')}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="granted">Granted</SelectItem>
                      <SelectItem value="pending">Pending consent</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Privacy mode" hint="Input values are never collected; strict mode minimizes user context.">
                  <Select value={privacy} onValueChange={(value) => setPrivacy(value as 'mask-user-input' | 'strict')}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="mask-user-input">Mask user input</SelectItem>
                      <SelectItem value="strict">Strict minimization</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <label className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2">
                  <span><span className="block text-sm text-text">User actions</span><span className="block text-xs text-muted">Clicks and interaction timing</span></span>
                  <Switch checked={trackActions} onCheckedChange={setTrackActions} aria-label="Track user actions" />
                </label>
                <label className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2">
                  <span><span className="block text-sm text-text">Long tasks</span><span className="block text-xs text-muted">Main-thread blocking over 50 ms</span></span>
                  <Switch checked={trackLongTasks} onCheckedChange={setTrackLongTasks} aria-label="Track long tasks" />
                </label>
              </div>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!name.trim() || create.isPending || (kind === 'rum' && (
            !!originError || !!applicationError || !serviceName.trim() || !!samplingError || !!replaySamplingError
          ))} onClick={() => create.mutate()}>
            {create.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
            Create {kind === 'rum' ? 'RUM key' : 'SDK key'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function htmlAttribute(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

function buildRumSnippet(value: CreatedKeyConfig): string {
  const options = value.rum
  if (!options) return ''
  const attributes = [
    ['src', `${window.location.origin}/api/v1/apm/rum/sdk.js`],
    ['data-key', value.key],
    ['data-app', options.applicationId],
    ['data-service', options.serviceName],
    ...(options.version ? [['data-version', options.version]] : []),
    ['data-sample-rate', String(options.sampleRatePercent / 100)],
    ['data-track-actions', String(options.trackActions)],
    ['data-track-long-tasks', String(options.trackLongTasks)],
    ['data-consent', options.consent],
    ['data-privacy', options.privacy],
    ['data-replay-sample-rate', String(options.replaySampleRatePercent / 100)],
    ['crossorigin', 'anonymous'],
  ]
  return `<script\n${attributes.map(([name, attributeValue]) => `  ${name}="${htmlAttribute(attributeValue)}"`).join('\n')}\n  defer><\/script>`
}

function CopyOnceDialog({ value, lastUsedAt = null, onClose }: {
  value: CreatedKeyConfig | string | null
  lastUsedAt?: string | null
  onClose: () => void
}) {
  const [copied, setCopied] = useState<'key' | 'snippet' | null>(null)
  const created = typeof value === 'string' ? null : value
  const plaintext = typeof value === 'string' ? value : value?.key ?? ''
  const isRum = created?.kind === 'rum' && !!created.rum
  const snippet = created && isRum ? buildRumSnippet(created) : ''
  const copy = (text: string, target: 'key' | 'snippet') => {
    if (text) {
      navigator.clipboard?.writeText(text)
      setCopied(target)
      window.setTimeout(() => setCopied(null), 1500)
      toast.success(target === 'snippet' ? 'Installation snippet copied' : 'Key copied to clipboard')
    }
  }
  return (
    <Dialog open={!!value} onOpenChange={(o) => { if (!o) { setCopied(null); onClose() } }}>
      <DialogContent className={isRum ? 'max-h-[90vh] max-w-2xl overflow-y-auto' : 'max-w-lg'}>
        <DialogHeader>
          <DialogTitle>{isRum ? 'Install Browser RUM' : 'Your new ingest key'}</DialogTitle>
          <DialogDescription>
            {isRum
              ? <>Add this tag before <code className="rounded bg-surface2 px-1">&lt;/head&gt;</code> on every page. The public key is protected by its exact origin allowlist and application binding.</>
              : <>Copy this key now — for security it is shown <strong>only once</strong> and cannot be retrieved again.</>}
          </DialogDescription>
        </DialogHeader>
        {isRum && (
          <>
            <div className="relative mt-3">
              <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface2/60 p-3 pr-12 text-xs leading-relaxed text-text"><code>{snippet}</code></pre>
              <Button className="absolute right-2 top-2" variant="outline" size="sm"
                onClick={() => copy(snippet, 'snippet')} aria-label="Copy installation snippet">
                {copied === 'snippet' ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
            <div className={`mt-3 rounded-md border px-3 py-2 text-sm ${lastUsedAt
              ? 'border-success/30 bg-success/10 text-success'
              : 'border-warning/30 bg-warning/10 text-warning'}`} role="status" aria-live="polite">
              {lastUsedAt
                ? <span className="flex items-center gap-2"><Check className="h-4 w-4" /> Data flow verified · last event {relativeTime(lastUsedAt)}</span>
                : <span className="flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Waiting for the first browser event…</span>}
            </div>
            <p className="mt-2 text-xs text-muted">
              ZenPlus automatically batches events, retries transient failures, respects consent, strips URL queries and fragments, and never captures input values. Revoke and replace this key if its permitted origin or application changes.
            </p>
            <p className="mt-1 text-xs text-muted">
              If your site uses Content Security Policy, allow <code className="rounded bg-surface2 px-1">{window.location.origin}</code> in both <code className="rounded bg-surface2 px-1">script-src</code> and <code className="rounded bg-surface2 px-1">connect-src</code>.
            </p>
          </>
        )}
        <details className="mt-3 rounded-md border border-border px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-text">{isRum ? 'Show public key' : 'Show key'}</summary>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 break-all rounded-md bg-surface2 px-3 py-2 font-mono text-sm text-text">
              {plaintext}
            </code>
            <Button variant="outline" size="sm" onClick={() => copy(plaintext, 'key')} aria-label="Copy key">
              {copied === 'key' ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>
        </details>
        <DialogFooter>
          <Button onClick={() => { setCopied(null); onClose() }}>{lastUsedAt ? 'Done' : isRum ? 'I’ll verify later' : 'Done'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
