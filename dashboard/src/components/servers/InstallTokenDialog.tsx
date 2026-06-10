/** "Deploy agent" dialog: generates an enrollment token and shows the
 *  copy-paste install command (msiexec for Windows, curl|bash for Linux). */

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Check, Copy, Download, KeyRound, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
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

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) reset(); onOpenChange(o) }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-primary" />
            Deploy agent{serverName ? ` to ${serverName}` : ''}
          </DialogTitle>
          <DialogDescription>
            Generate a one-time enrollment token and run the install command on the target server.
            The agent enrolls itself and starts reporting within a minute.
          </DialogDescription>
        </DialogHeader>

        {!token ? (
          <div className="grid grid-cols-2 gap-4">
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
              <Label>Tags (comma-separated, applied to the enrolled server)</Label>
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
            <div className="space-y-1.5">
              <Label>Enrollment token <span className="font-normal text-muted">(expires {new Date(token.expires_at).toLocaleString()})</span></Label>
              <div className="flex items-center gap-2">
                <code className="flex-1 truncate rounded-md border border-border bg-bg px-3 py-2 font-mono text-xs">
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
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-3 font-mono text-xs leading-relaxed">
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
                Already installed? Re-run the agent's enroll step with this token — re-enrollment
                rebinds the same host without creating a duplicate.
              </p>
            </div>
            {token.msi_download_url && (
              <a
                href={token.msi_download_url}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
              >
                <Download className="h-3.5 w-3.5" /> Download installer package
              </a>
            )}
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
