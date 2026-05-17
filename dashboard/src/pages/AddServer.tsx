/**
 * AddServer — guided flow to onboard a new monitored server.
 *
 * Step 1: choose collection mode (Windows Agent, Agentless WMI/WinRM, SNMP, Linux Agent, Linux SSH).
 * Step 2: capture identity (display name, hostname, site, policy).
 * Step 3: agent path → generate enrollment token + copyable install command;
 *         agentless path → reminder to assign sensor credentials.
 */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Copy,
  Download,
  Loader2,
  Server as ServerIcon,
  Shield,
  Terminal,
  Wifi,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import { FormField } from '@/components/ui/FormField'
import { Textarea } from '@/components/ui/Textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Badge } from '@/components/ui/Badge'
import { toast } from '@/components/ui/Toast'
import type { AgentPolicy, InstallToken, Server } from '@/types/server'

type Mode = 'agent_windows' | 'agentless_wmi' | 'agentless_winrm' | 'snmp' | 'agent_linux' | 'ssh'

const MODE_OPTIONS: Array<{
  id: Mode
  title: string
  body: string
  icon: React.ComponentType<{ className?: string }>
  recommended?: boolean
  os: 'windows' | 'linux'
  collectionMode: Server['collection_mode']
  osType: Server['os_type']
}> = [
  {
    id: 'agent_windows',
    title: 'Windows Agent',
    body:
      'Best fidelity. Installs zenplus-agent.exe via MSI. CPU, memory, disk, network, Windows services, event log summaries.',
    icon: ServerIcon,
    recommended: true,
    os: 'windows',
    collectionMode: 'agent',
    osType: 'windows',
  },
  {
    id: 'agentless_wmi',
    title: 'Agentless WMI',
    body:
      'No software on the host. Remote sensor polls WMI for baseline metrics. Requires admin credentials.',
    icon: Wifi,
    os: 'windows',
    collectionMode: 'agentless_wmi',
    osType: 'windows',
  },
  {
    id: 'agentless_winrm',
    title: 'Agentless WinRM / PowerShell',
    body:
      'Richer checks than WMI. Uses HTTP(S) management endpoint. Requires WinRM enabled on the target.',
    icon: Terminal,
    os: 'windows',
    collectionMode: 'agentless_winrm',
    osType: 'windows',
  },
  {
    id: 'snmp',
    title: 'SNMP',
    body:
      'Universal lightweight fallback. Works for appliances and legacy systems. Limited Windows detail.',
    icon: Shield,
    os: 'windows',
    collectionMode: 'snmp',
    osType: 'unknown',
  },
  {
    id: 'agent_linux',
    title: 'Linux Agent',
    body:
      'Best for Linux servers. .deb / .rpm packaging. Collects systemd services and journal summaries.',
    icon: ServerIcon,
    os: 'linux',
    collectionMode: 'agent',
    osType: 'linux',
  },
  {
    id: 'ssh',
    title: 'Linux SSH (agentless)',
    body:
      'No agent install. Remote sensor probes the host over SSH for baseline metrics.',
    icon: Terminal,
    os: 'linux',
    collectionMode: 'ssh',
    osType: 'linux',
  },
]

export default function AddServer() {
  const navigate = useNavigate()
  const [step, setStep] = useState<1 | 2 | 3>(1)
  const [mode, setMode] = useState<Mode | null>(null)

  // Step 2 state
  const [displayName, setDisplayName] = useState('')
  const [hostname, setHostname] = useState('')
  const [primaryIp, setPrimaryIp] = useState('')
  const [environment, setEnvironment] = useState('')
  const [owner, setOwner] = useState('')
  const [siteId, setSiteId] = useState<string>('')
  const [policyId, setPolicyId] = useState<string>('')
  const [createdServerId, setCreatedServerId] = useState<string | null>(null)
  const [installToken, setInstallToken] = useState<InstallToken | null>(null)

  const policiesQ = useQuery<{ items: AgentPolicy[] }>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data,
  })

  const sitesQ = useQuery<Array<{ id: string; name: string }>>({
    queryKey: ['sites'],
    queryFn: async () => (await api.get('/sites')).data || [],
  })

  const createServerM = useMutation({
    mutationFn: async () => {
      const opt = MODE_OPTIONS.find((m) => m.id === mode)!
      const body: Partial<Server> = {
        display_name: displayName,
        hostname: hostname || displayName,
        primary_ip: primaryIp || null,
        os_type: opt.osType,
        collection_mode: opt.collectionMode,
        environment: environment || null,
        owner: owner || null,
        site_id: siteId || null,
      }
      const r = await api.post('/servers', body)
      return r.data as Server
    },
    onSuccess: async (server) => {
      setCreatedServerId(server.id)
      // For agent modes, also fetch an install token right away
      if (mode === 'agent_windows' || mode === 'agent_linux') {
        const platform = mode === 'agent_windows' ? 'windows' : 'linux'
        const tok = (
          await api.post(`/servers/${server.id}/install-token`, {
            platform,
            policy_id: policyId || null,
            site_id: siteId || null,
            ttl_hours: 24,
            max_uses: 1,
          })
        ).data as InstallToken
        setInstallToken(tok)
      }
      setStep(3)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const policies = policiesQ.data?.items ?? []
  const filteredPolicies = policies.filter(
    (p) =>
      p.platform === 'any' ||
      (mode === 'agent_windows' && p.platform === 'windows') ||
      (mode === 'agent_linux' && p.platform === 'linux'),
  )

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`${label} copied to clipboard`)
    } catch {
      toast.error('Copy failed — select the text manually')
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <Button variant="ghost" size="sm" onClick={() => navigate('/servers')}>
            <ArrowLeft className="h-3.5 w-3.5" />
            Back to servers
          </Button>
          <h1 className="mt-2 text-xl font-semibold">Add server</h1>
          <p className="mt-0.5 text-xs text-muted">
            Choose how this host will be monitored, then provide identity details. We'll generate
            an install command in the final step.
          </p>
        </div>
        <Stepper step={step} />
      </div>

      {step === 1 ? (
        <div className="grid gap-3 md:grid-cols-2">
          {MODE_OPTIONS.map((opt) => {
            const Icon = opt.icon
            const selected = mode === opt.id
            return (
              <button
                key={opt.id}
                type="button"
                onClick={() => setMode(opt.id)}
                className={`flex items-start gap-3 rounded-lg border p-4 text-left transition-colors ${
                  selected
                    ? 'border-primary bg-primary/5 shadow-sm'
                    : 'border-border bg-surface hover:bg-surface2'
                }`}
              >
                <div
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${
                    selected ? 'bg-primary/15 text-primary' : 'bg-surface2 text-muted'
                  }`}
                >
                  <Icon className="h-4 w-4" />
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{opt.title}</span>
                    {opt.recommended ? (
                      <Badge variant="success">Recommended</Badge>
                    ) : null}
                  </div>
                  <p className="mt-1 text-xs text-muted">{opt.body}</p>
                </div>
              </button>
            )
          })}
          <div className="md:col-span-2 flex justify-end">
            <Button onClick={() => setStep(2)} disabled={!mode}>
              Continue
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <Card>
          <CardContent className="space-y-4 p-5">
            <FormField label="Display name">
              <Input
                id="display-name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="prod-web-01"
                autoFocus
              />
            </FormField>
            <div className="grid gap-4 md:grid-cols-2">
              <FormField label="Hostname">
                <Input
                  id="hostname"
                  value={hostname}
                  onChange={(e) => setHostname(e.target.value)}
                  placeholder="prod-web-01.corp.local"
                />
              </FormField>
              <FormField label="Primary IP">
                <Input
                  id="primary-ip"
                  value={primaryIp}
                  onChange={(e) => setPrimaryIp(e.target.value)}
                  placeholder="10.0.1.42"
                />
              </FormField>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <FormField label="Environment">
                <Input
                  id="environment"
                  value={environment}
                  onChange={(e) => setEnvironment(e.target.value)}
                  placeholder="production / staging / dev"
                />
              </FormField>
              <FormField label="Owner">
                <Input
                  id="owner"
                  value={owner}
                  onChange={(e) => setOwner(e.target.value)}
                  placeholder="team / email"
                />
              </FormField>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <FormField label="Site">
                <Select value={siteId || '__none'} onValueChange={(v) => setSiteId(v === '__none' ? '' : v)}>
                  <SelectTrigger>
                    <SelectValue placeholder="No site" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none">No site</SelectItem>
                    {(sitesQ.data ?? []).map((s) => (
                      <SelectItem key={s.id} value={s.id}>
                        {s.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              {(mode === 'agent_windows' || mode === 'agent_linux') ? (
                <FormField label="Agent policy">
                  <Select value={policyId || '__default'} onValueChange={(v) => setPolicyId(v === '__default' ? '' : v)}>
                    <SelectTrigger>
                      <SelectValue placeholder="Default policy" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__default">Default policy (auto)</SelectItem>
                      {filteredPolicies.map((p) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.name} {p.is_builtin ? '(built-in)' : ''}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormField>
              ) : null}
            </div>
            <div className="flex items-center justify-between pt-2">
              <Button variant="ghost" onClick={() => setStep(1)}>
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </Button>
              <Button onClick={() => createServerM.mutate()} disabled={!displayName || createServerM.isPending}>
                {createServerM.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {mode === 'agent_windows' || mode === 'agent_linux'
                  ? 'Create & generate token'
                  : 'Create server'}
                <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {step === 3 ? (
        <Card>
          <CardContent className="space-y-4 p-5">
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-success/10 text-success">
                <CheckCircle2 className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-base font-semibold">Server created</h2>
                <p className="text-xs text-muted">
                  {mode === 'agent_windows' || mode === 'agent_linux'
                    ? 'Run the install command on the host below. The agent will enroll itself and start sending metrics within a minute.'
                    : 'Next, assign agentless credentials to a remote sensor so it can probe this host.'}
                </p>
              </div>
            </div>

            {installToken ? (
              <div className="space-y-3 rounded-md border border-border bg-surface2 p-4">
                <div>
                  <Label className="text-xs uppercase tracking-wide text-muted">
                    Enrollment token (shown once)
                  </Label>
                  <div className="mt-1 flex items-center gap-2">
                    <code className="block flex-1 truncate rounded bg-bg px-2 py-1.5 font-mono text-xs">
                      {installToken.enrollment_token}
                    </code>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => copyText(installToken.enrollment_token, 'Token')}
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  <p className="mt-1 text-[11px] text-muted">
                    Expires {new Date(installToken.expires_at).toLocaleString()} · single-use.
                  </p>
                </div>

                <div>
                  <Label className="text-xs uppercase tracking-wide text-muted">
                    Install command
                  </Label>
                  <Textarea
                    readOnly
                    value={installToken.install_command}
                    className="mt-1 h-24 font-mono text-xs"
                  />
                  <div className="mt-1 flex items-center justify-between text-[11px] text-muted">
                    <span>
                      {mode === 'agent_windows'
                        ? 'Run from an elevated PowerShell on the Windows host.'
                        : 'Run as root on the Linux host.'}
                    </span>
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => copyText(installToken.install_command, 'Install command')}
                      >
                        <Copy className="h-3.5 w-3.5" />
                        Copy
                      </Button>
                      {installToken.msi_download_url ? (
                        <a
                          href={installToken.msi_download_url}
                          download
                          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1 text-xs hover:bg-surface2"
                        >
                          <Download className="h-3.5 w-3.5" />
                          Download MSI
                        </a>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>
            ) : null}

            {!installToken ? (
              <div className="flex items-start gap-3 rounded-md border border-warning/30 bg-warning/5 p-4">
                <AlertCircle className="mt-0.5 h-4 w-4 text-warning" />
                <div className="text-xs text-text2">
                  Agentless monitoring uses a remote sensor at the site. Open the sensor's
                  configuration on the Sensors page to assign credentials for this server.
                </div>
              </div>
            ) : null}

            <div className="flex items-center justify-end gap-2 pt-2">
              {createdServerId ? (
                <Button variant="outline" onClick={() => navigate(`/servers/${createdServerId}`)}>
                  Open server detail
                </Button>
              ) : null}
              <Button onClick={() => navigate('/servers')}>Done</Button>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

function Stepper({ step }: { step: 1 | 2 | 3 }) {
  const steps = [
    { id: 1, label: 'Mode' },
    { id: 2, label: 'Identity' },
    { id: 3, label: 'Install' },
  ]
  return (
    <ol className="flex items-center gap-2 text-xs text-muted">
      {steps.map((s, i) => (
        <li key={s.id} className="flex items-center gap-2">
          <span
            className={`flex h-6 w-6 items-center justify-center rounded-full border ${
              step >= s.id
                ? 'border-primary bg-primary/15 text-primary'
                : 'border-border bg-surface2'
            }`}
          >
            {step > s.id ? <CheckCircle2 className="h-3.5 w-3.5" /> : s.id}
          </span>
          <span className={step >= s.id ? 'font-medium text-text' : ''}>{s.label}</span>
          {i < steps.length - 1 ? <span className="text-muted/50">→</span> : null}
        </li>
      ))}
    </ol>
  )
}
