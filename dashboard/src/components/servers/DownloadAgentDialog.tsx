/** "Download agent" dialog: hands back the published installer with the
 *  controller URL and a freshly minted enrollment token already inside it.
 *
 *  By default the package is reusable: its token has no install limit and is
 *  bounded only by an expiry, so one download can be pushed to a whole estate
 *  through GPO/Intune/SCCM. A fixed install count is available when a rollout
 *  should be tightly scoped. */

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, CheckCircle2, Download, Loader2, Server, ShieldCheck,
} from 'lucide-react'
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
import type { AgentPolicy } from '@/types/servers'

type Platform = 'windows' | 'linux' | 'macos'

interface AgentPackage {
  id: string
  platform: string
  version: string
  file_name: string
  file_size: number
  is_latest: boolean
}

interface IssuedPackage {
  fileName: string
  version: string
  tokenPrefix: string
  maxUses: number
  expiresAt: string
}

const EXT: Record<Platform, string> = {
  windows: '.msi',
  linux: '.tar.gz',
  macos: '.tar.gz',
}

function fmtSize(bytes: number) {
  if (!bytes) return ''
  const mb = bytes / (1024 * 1024)
  return `${mb.toFixed(1)} MB`
}

/** Pull the server-supplied filename out of Content-Disposition. */
function filenameFrom(disposition: unknown, fallback: string) {
  if (typeof disposition !== 'string') return fallback
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)
  return match?.[1] ? decodeURIComponent(match[1]) : fallback
}

export function DownloadAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [platform, setPlatform] = useState<Platform>('windows')
  // 'fleet' issues a reusable token (max_uses = 0); 'limited' caps installs.
  const [scope, setScope] = useState<'fleet' | 'limited'>('fleet')
  const [serverCount, setServerCount] = useState('10')
  const [ttlHours, setTtlHours] = useState('72')
  const [policyId, setPolicyId] = useState('')
  const [label, setLabel] = useState('')
  const [issued, setIssued] = useState<IssuedPackage | null>(null)

  const { data: policies } = useQuery<AgentPolicy[]>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data.items,
    enabled: open,
  })

  const { data: packages, isLoading: packagesLoading } = useQuery<{ items: AgentPackage[] }>({
    queryKey: ['agent-packages'],
    queryFn: async () => (await api.get('/agent-fleet/packages')).data,
    enabled: open,
  })

  const published = (packages?.items ?? []).find(
    (p) => p.platform === platform && p.is_latest,
  )

  // 0 tells the controller the token is reusable until it expires.
  const count = scope === 'fleet'
    ? 0
    : Math.max(1, Math.min(100000, Number(serverCount) || 1))

  const download = useMutation({
    mutationFn: async () => {
      const res = await api.post(
        '/agent-fleet/packages/download',
        {
          platform,
          server_count: count,
          ttl_hours: Math.max(1, Number(ttlHours) || 72),
          policy_id: policyId || null,
          label: label.trim() || null,
        },
        // Binary response, and large: the shared 30s client timeout is not
        // enough for a ~40 MB package over a slow link.
        { responseType: 'blob', timeout: 10 * 60_000 },
      )

      const fallback = `zenplus-agent${EXT[platform]}`
      const fileName = filenameFrom(res.headers['content-disposition'], fallback)

      // Hand the blob to the browser as a download.
      const url = URL.createObjectURL(res.data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = fileName
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Revoke on the next tick so the navigation has picked the blob up.
      setTimeout(() => URL.revokeObjectURL(url), 60_000)

      return {
        fileName,
        version: String(res.headers['x-package-version'] ?? ''),
        tokenPrefix: String(res.headers['x-token-prefix'] ?? ''),
        maxUses: Number(res.headers['x-token-max-uses'] ?? count),
        expiresAt: String(res.headers['x-token-expires-at'] ?? ''),
      }
    },
    onSuccess: (info) => {
      setIssued(info)
      toast.success(
        `${info.fileName} downloaded`,
        info.maxUses === 0
          ? 'Reusable on any number of servers until it expires'
          : `Good for ${info.maxUses} install${info.maxUses === 1 ? '' : 's'}`,
      )
    },
    onError: async (err: unknown) => {
      // An error body on a blob-typed request arrives as a Blob, so the
      // usual message extraction needs the text read out first.
      const resp = (err as { response?: { data?: unknown } })?.response
      if (resp?.data instanceof Blob) {
        try {
          const parsed = JSON.parse(await resp.data.text())
          toast.error(parsed.detail ?? 'Download failed')
          return
        } catch {
          /* fall through to the generic message */
        }
      }
      toast.error(apiErrorMessage(err))
    },
  })

  const reset = (nextOpen: boolean) => {
    if (!nextOpen) {
      setIssued(null)
      download.reset()
    }
    onOpenChange(nextOpen)
  }

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>Download pre-configured agent</DialogTitle>
          <DialogDescription>
            The installer is stamped with this controller&apos;s address and an enrollment
            token before it downloads. Run it on a server and it enrolls itself — nothing
            to type, and no per-host setup.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="dl-platform">Platform</Label>
              <Select value={platform} onValueChange={(v) => setPlatform(v as Platform)}>
                <SelectTrigger id="dl-platform">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="windows">Windows</SelectItem>
                  <SelectItem value="linux">Linux</SelectItem>
                  <SelectItem value="macos">macOS</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="dl-scope">Install limit</Label>
              <Select value={scope} onValueChange={(v) => setScope(v as 'fleet' | 'limited')}>
                <SelectTrigger id="dl-scope">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="fleet">Unlimited — reusable package</SelectItem>
                  <SelectItem value="limited">Limit to a fixed number</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {scope === 'limited' && (
            <div className="space-y-1.5">
              <Label htmlFor="dl-count">Maximum installs</Label>
              <Input
                id="dl-count"
                type="number"
                min={1}
                max={100000}
                value={serverCount}
                onChange={(e) => setServerCount(e.target.value)}
              />
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="dl-ttl">Token valid for (hours)</Label>
              <Input
                id="dl-ttl"
                type="number"
                min={1}
                max={8760}
                value={ttlHours}
                onChange={(e) => setTtlHours(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="dl-policy">Policy (optional)</Label>
              <Select
                value={policyId || '__default'}
                onValueChange={(v) => setPolicyId(v === '__default' ? '' : v)}
              >
                <SelectTrigger id="dl-policy">
                  <SelectValue placeholder="Default policy" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__default">Default policy</SelectItem>
                  {(policies ?? []).map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="dl-label">Label (optional)</Label>
            <Input
              id="dl-label"
              placeholder="e.g. Datacentre A rollout"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>

          <div className="flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-muted">
            <Server className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {scope === 'fleet' ? (
              <span>
                One file, any number of servers. Push it through GPO, Intune, SCCM or your
                imaging pipeline — every host enrols with the same package. Access is bounded
                by the expiry below, and the token can be revoked at any time from
                the fleet page.
              </span>
            ) : (
              <span>
                One enrollment is consumed per host, so this package stops working after{' '}
                <span className="font-medium text-fg">{count}</span> install
                {count === 1 ? '' : 's'}. Use this to keep a rollout tightly scoped;
                choose <span className="font-medium text-fg">Unlimited</span> for estate-wide
                deployment.
              </span>
            )}
          </div>

          {!packagesLoading && !published ? (
            <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                No {platform} package is published yet. Drop the build into{' '}
                <code>/opt/zenplus/artifacts/agents/{platform}/</code> and publish it before
                downloading.
              </span>
            </div>
          ) : null}

          {published ? (
            <div className="flex items-center gap-2 text-xs text-muted">
              <ShieldCheck className="h-3.5 w-3.5 text-success" />
              <span>
                Serving <span className="font-medium text-fg">{published.file_name}</span>{' '}
                (v{published.version}
                {published.file_size ? `, ${fmtSize(published.file_size)}` : ''})
              </span>
            </div>
          ) : null}

          {issued ? (
            <div className="space-y-1 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-xs">
              <div className="flex items-center gap-2 font-medium text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                {issued.fileName} downloaded
              </div>
              <div className="text-muted">
                Token {issued.tokenPrefix}… ·{' '}
                {issued.maxUses === 0
                  ? 'unlimited installs'
                  : `${issued.maxUses} install${issued.maxUses === 1 ? '' : 's'}`}
                {issued.expiresAt
                  ? ` · expires ${new Date(issued.expiresAt).toLocaleString()}`
                  : ''}
              </div>
              <div className="text-muted">
                Install silently with{' '}
                <code>msiexec /i {issued.fileName} /qn /norestart</code>
              </div>
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => reset(false)}>
            Close
          </Button>
          <Button
            onClick={() => download.mutate()}
            disabled={download.isPending || !published}
          >
            {download.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Preparing…
              </>
            ) : (
              <>
                <Download className="h-3.5 w-3.5" />
                Download installer
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
