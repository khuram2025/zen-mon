/** "Deploy agent" dialog: generates an enrollment token and shows the
 *  copy-paste install command (PowerShell bootstrap for Windows, curl|bash
 *  for Linux), with single-server and bulk-rollout presets plus live
 *  enrollment verification. */

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, Check, CheckCircle2, Copy, Download, KeyRound, Loader2, RefreshCw,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import type { AgentPolicy, InstallToken } from '@/types/servers'

type DeployMode = 'single' | 'bulk'

interface TokenUsage {
  id: string
  uses: number
  max_uses: number
  revoked_at: string | null
}

export function InstallTokenDialog({
  open,
  onOpenChange,
  serverId,
  serverName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** When set, the token is bound to this server; otherwise enrollment creates a new server. */
  serverId?: string
  serverName?: string
}) {
  const [mode, setMode] = useState<DeployMode>('single')
  const [platform, setPlatform] = useState<'windows' | 'linux'>('windows')
  const [policyId, setPolicyId] = useState<string>('')
  const [tags, setTags] = useState('')
  const [ttlHours, setTtlHours] = useState('24')
  const [maxUses, setMaxUses] = useState('1')
  const [token, setToken] = useState<InstallToken | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const { data: policies } = useQuery<AgentPolicy[]>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data.items,
    enabled: open,
  })

  // Bulk rollout preset: shared token, many uses, longer validity.
  const applyMode = (m: DeployMode) => {
    setMode(m)
    if (m === 'single') { setMaxUses('1'); setTtlHours('24') }
    else { setMaxUses('100'); setTtlHours('72') }
  }

  const generate = useMutation({
    mutationFn: async () => {
      const body = {
        platform,
        policy_id: policyId || null,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        ttl_hours: Math.max(1, Math.min(720, Number(ttlHours) || 24)),
        max_uses: Math.max(1, Math.min(100, Number(maxUses) || 1)),
      }
      const url = serverId ? `/servers/${serverId}/install-token` : '/servers/install-token'
      return (await api.post(url, body)).data as InstallToken
    },
    onSuccess: (t) => setToken(t),
    onError: (e) => toast.error('Token generation failed', apiErrorMessage(e)),
  })

  // Live verification: poll token usage so the operator sees enrollments
  // land without leaving the dialog.
  const { data: usage } = useQuery<TokenUsage | null>({
    queryKey: ['enrollment-token-usage', token?.token_id],
    queryFn: async () => {
      const r = await api.get('/servers/enrollment-tokens/list', { params: { include_expired: true, limit: 100 } })
      return (r.data.items as TokenUsage[]).find((t) => t.id === token?.token_id) ?? null
    },
    enabled: open && Boolean(token),
    refetchInterval: 5_000,
  })
  const enrollments = usage?.uses ?? 0

  const copy = async (text: string, which: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(which)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      toast.error('Copy failed', 'Clipboard access was denied by the browser')
    }
  }

  const reset = () => {
    setToken(null)
    generate.reset()
  }

  // Changing platform after generation would show a stale command.
  useEffect(() => {
    if (!open) reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-primary" />
            Deploy agent{serverName ? ` to ${serverName}` : ''}
          </DialogTitle>
          <DialogDescription>
            Generate an enrollment token and run the install command on the target server
            {mode === 'bulk' ? 's (one shared token for the whole rollout)' : ''}.
            The agent enrolls itself and starts reporting within a minute.
          </DialogDescription>
        </DialogHeader>

        {!token ? (
          <div className="grid grid-cols-2 gap-4">
            <div className="col-span-2 space-y-1.5">
              <Label>Deployment mode</Label>
              <div className="grid grid-cols-2 gap-2">
                {([
                  ['single', 'Single server', 'One-time token · 24 h validity'],
                  ['bulk', 'Bulk rollout', 'Shared token · up to 100 hosts · 72 h'],
                ] as const).map(([value, label, hint]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => applyMode(value)}
                    className={cn(
                      'rounded-md border px-3 py-2 text-left text-sm transition',
                      mode === value
                        ? 'border-primary/50 bg-primary/10'
                        : 'border-border bg-surface2/30 hover:border-primary/30',
                    )}
                  >
                    <div className="font-medium">{label}</div>
                    <div className="text-[11px] text-muted">{hint}</div>
                  </button>
                ))}
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>Platform</Label>
              <Select value={platform} onValueChange={(v) => setPlatform(v as 'windows' | 'linux')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="windows">Windows</SelectItem>
                  <SelectItem value="linux">Linux</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Agent policy</Label>
              <Select value={policyId || 'auto'} onValueChange={(v) => setPolicyId(v === 'auto' ? '' : v)}>
                <SelectTrigger><SelectValue placeholder="Platform default" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">Platform default</SelectItem>
                  {(policies || [])
                    .filter((p) => p.platform === platform || p.platform === 'any')
                    .map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="col-span-2 space-y-1.5">
              <Label>Tags (comma-separated, applied to the enrolled server{mode === 'bulk' ? 's' : ''})</Label>
              <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="prod, web-tier, dc1" />
            </div>
            <div className="space-y-1.5">
              <Label>Token validity (hours)</Label>
              <Input type="number" min={1} max={720} value={ttlHours} onChange={(e) => setTtlHours(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Max uses</Label>
              <Input type="number" min={1} max={100} value={maxUses} onChange={(e) => setMaxUses(e.target.value)} />
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
              <span>
                This token is shown <strong>only once</strong> — copy the command before closing.
                You can revoke or audit issued tokens from the token list API.
              </span>
            </div>
            <div className="space-y-1.5">
              <Label>
                Enrollment token{' '}
                <span className="font-normal text-muted">
                  (expires {new Date(token.expires_at).toLocaleString()} · {token.max_uses} use{token.max_uses === 1 ? '' : 's'})
                </span>
              </Label>
              <div className="flex items-center gap-2">
                <code className="flex-1 break-all rounded-md border border-border bg-bg px-3 py-2 font-mono text-xs">
                  {token.enrollment_token}
                </code>
                <Button variant="outline" size="sm" onClick={() => copy(token.enrollment_token, 'token')}>
                  {copied === 'token' ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>Install command ({token.platform})</Label>
              <div className="relative">
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-3 pr-12 font-mono text-xs leading-relaxed">
                  {token.install_command}
                </pre>
                <Button
                  variant="outline" size="sm" className="absolute right-2 top-2"
                  onClick={() => copy(token.install_command, 'cmd')}
                >
                  {copied === 'cmd' ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </div>
              <p className="text-[11px] text-muted">
                {token.platform === 'windows'
                  ? 'Run in an elevated (Administrator) PowerShell or push via GPO/Intune/RMM with the same command.'
                  : 'Run as a user with sudo. For fleets, push the same command via Ansible/SSH.'}
                {' '}Already installed? Re-running with a fresh token re-enrolls the same host without creating a duplicate.
              </p>
            </div>
            {!token.msi_download_url && (
              <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
                <span>
                  No {token.platform} agent package is published yet, so the install command will
                  fail to download. Place the package in{' '}
                  <code className="font-mono">/opt/zenplus/artifacts/agents/{token.platform}/</code>{' '}
                  (e.g. <code className="font-mono">zenplus-agent-1.0.0{token.platform === 'windows' ? '.msi' : '.tar.gz'}</code>)
                  and publish it via <code className="font-mono">POST /api/v1/agent-fleet/packages/publish</code>.
                </span>
              </div>
            )}
            {token.msi_download_url && (
              <a
                href={token.msi_download_url}
                download
                rel="noopener"
                className="inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
              >
                <Download className="h-3.5 w-3.5" /> Download installer package
              </a>
            )}
            <div className="flex items-center gap-2 rounded-md border border-border bg-surface2/30 px-3 py-2 text-xs">
              {enrollments > 0 ? (
                <>
                  <CheckCircle2 className="h-4 w-4 text-success" />
                  <span className="font-medium text-success">
                    {enrollments} host{enrollments === 1 ? '' : 's'} enrolled with this token
                  </span>
                  <span className="text-muted">— check the Agent Fleet page for details</span>
                </>
              ) : (
                <>
                  <Loader2 className="h-4 w-4 animate-spin text-muted" />
                  <span className="text-muted">Waiting for the first enrollment…</span>
                </>
              )}
            </div>
          </div>
        )}

        <DialogFooter>
          {token ? (
            <>
              <Button variant="outline" onClick={reset}>
                <RefreshCw className="h-3.5 w-3.5" /> New token
              </Button>
              <Button onClick={() => onOpenChange(false)}>Done</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
                {generate.isPending ? 'Generating…' : 'Generate token'}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
