/**
 * AddServer — guided flow to onboard a new monitored server.
 *
 * Step 1: choose collection mode (Windows Agent, Agentless WMI/WinRM, SNMP, Linux Agent, Linux SSH).
 * Step 2: capture identity (display name, hostname, site, policy).
 * Step 3: agent path → controller-only install and appliance authorization;
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
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Badge } from '@/components/ui/Badge'
import { toast } from '@/components/ui/Toast'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import type { Server } from '@/types/server'

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
  const [createdServerId, setCreatedServerId] = useState<string | null>(null)
  const [installOpen, setInstallOpen] = useState(false)

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
    onSuccess: (server) => {
      setCreatedServerId(server.id)
      setStep(3)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

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
            </div>
            <div className="flex items-center justify-between pt-2">
              <Button variant="ghost" onClick={() => setStep(1)}>
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </Button>
              <Button onClick={() => createServerM.mutate()} disabled={!displayName || createServerM.isPending}>
                {createServerM.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                Create server
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
                    ? 'Install the agent using only the controller address. It will appear in Agent Fleet for appliance approval before monitoring begins.'
                    : 'Next, assign agentless credentials to a remote sensor so it can probe this host.'}
                </p>
              </div>
            </div>

            {mode === 'agent_windows' || mode === 'agent_linux' ? (
              <div className="flex items-center justify-between gap-3 rounded-md border border-primary/30 bg-primary/5 p-4">
                <div className="text-xs text-text2">
                  Download the signed installer and use the controller-only deployment command.
                  No endpoint enrollment secret is required.
                </div>
                <Button onClick={() => setInstallOpen(true)}>Install agent</Button>
              </div>
            ) : (
              <div className="flex items-start gap-3 rounded-md border border-warning/30 bg-warning/5 p-4">
                <AlertCircle className="mt-0.5 h-4 w-4 text-warning" />
                <div className="text-xs text-text2">
                  Agentless monitoring uses a remote sensor at the site. Open the sensor's
                  configuration on the Sensors page to assign credentials for this server.
                </div>
              </div>
            )}

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

      <InstallTokenDialog
        open={installOpen}
        onOpenChange={setInstallOpen}
        serverId={createdServerId ?? undefined}
        serverName={displayName || undefined}
      />
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
