import { useState } from 'react'
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
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Badge } from '@/components/ui/Badge'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
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
  enabled: boolean
  last_used_at: string | null
  revoked_at: string | null
  created_at: string
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
  const tab = (params.get('tab') as SettingsTab) || 'agents'
  const setTab = (t: SettingsTab) => {
    const n = new URLSearchParams(params); n.set('tab', t); setParams(n, { replace: true })
  }
  const [formOpen, setFormOpen] = useState(false)
  const [revoking, setRevoking] = useState<IngestKey | null>(null)
  const [createdKey, setCreatedKey] = useState<string | null>(null)

  const keysQuery = useQuery<IngestKey[]>({
    queryKey: ['apm', 'ingest-keys'],
    queryFn: async () => (await api.get('/apm/ingest-keys')).data,
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
      {tab === 'start' && <GettingStartedTab keys={keys} onCreateKey={() => setFormOpen(true)} />}
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
          <Button onClick={() => setFormOpen(true)}>
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
              No ingest keys yet. Create one to start sending OTLP traces.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Key prefix</Th>
                  <Th>Environment</Th>
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
                    <Td>{k.env ?? <span className="text-muted">all</span>}</Td>
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
        onCreated={(plaintext) => { setFormOpen(false); setCreatedKey(plaintext) }}
      />

      <CopyOnceDialog value={createdKey} onClose={() => setCreatedKey(null)} />

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
  const activeKey = keys.find((k) => k.enabled && !k.revoked_at)
  const keyPlaceholder = activeKey ? `${activeKey.key_prefix}…` : 'zpi_your_key_here'
  const hasTraffic = keys.some((k) => k.last_used_at)

  const steps: { title: string; body: React.ReactNode }[] = [
    {
      title: 'Create an ingest key',
      body: (
        <>
          <p className="text-sm text-muted">
            Every producer authenticates with a <code className="rounded bg-surface2 px-1">zpi_</code> key scoped to an
            environment. Create one per environment (or per service, if you want to revoke them independently).
          </p>
          {keys.length === 0 ? (
            <Button size="sm" className="mt-2" onClick={onCreateKey}>
              <Plus className="h-4 w-4" /> Create ingest key
            </Button>
          ) : (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-success">
              <Check className="h-3.5 w-3.5" /> {keys.length} key{keys.length === 1 ? '' : 's'} configured
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

function IngestKeyFormDialog({ open, onOpenChange, onCreated }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onCreated: (plaintext: string) => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [env, setEnv] = useState<string>('prod')
  const [kind, setKind] = useState<'sdk' | 'rum'>('sdk')
  const [origins, setOrigins] = useState('')

  const envs = useQuery<Environment[]>({
    queryKey: ['apm', 'environments'],
    queryFn: async () => (await api.get('/apm/environments')).data,
    enabled: open,
  })

  const create = useMutation({
    mutationFn: async () =>
      (await api.post('/apm/ingest-keys', {
        name, kind, env,
        origin_allowlist: kind === 'rum' ? origins.split(',').map((v) => v.trim()).filter(Boolean) : [],
      })).data,
    onSuccess: (data: { key: string }) => {
      qc.invalidateQueries({ queryKey: ['apm', 'ingest-keys'] })
      setName('')
      setOrigins('')
      onCreated(data.key)
    },
    onError: (e: any) => toast.error('Could not create key', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Create ingest key</DialogTitle></DialogHeader>
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
          {kind === 'rum' && <FormField label="Allowed browser origins" required hint="Exact origins only; comma-separate multiple sites. Wildcards are rejected.">
            <Input value={origins} onChange={(e) => setOrigins(e.target.value)} placeholder="https://portal.example.com, http://192.168.8.19" />
          </FormField>}
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
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!name.trim() || (kind === 'rum' && !origins.trim()) || create.isPending} onClick={() => create.mutate()}>
            {create.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CopyOnceDialog({ value, onClose }: { value: string | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    if (value) {
      navigator.clipboard?.writeText(value)
      setCopied(true)
      toast.success('Copied to clipboard')
    }
  }
  return (
    <Dialog open={!!value} onOpenChange={(o) => { if (!o) { setCopied(false); onClose() } }}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Your new ingest key</DialogTitle></DialogHeader>
        <p className="text-sm text-muted">
          Copy this key now — for security it is shown <strong>only once</strong> and cannot be retrieved again.
        </p>
        <div className="flex items-center gap-2 mt-3">
          <code className="flex-1 px-3 py-2 rounded-md bg-surface2 text-text font-mono text-sm break-all">
            {value}
          </code>
          <Button variant="outline" size="sm" onClick={copy}>
            {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
          </Button>
        </div>
        <DialogFooter>
          <Button onClick={() => { setCopied(false); onClose() }}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
