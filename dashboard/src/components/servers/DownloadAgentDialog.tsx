/** Download an immutable agent package and issue a separate rollout token.
 *
 * The controller returns the published binary unchanged. Enrollment metadata
 * is delivered in response headers and is used to build a deployment command;
 * it is never written into, or represented as part of, the MSI itself. */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Check, CheckCircle2, Copy, Download, Loader2, Server, ShieldCheck,
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

type Platform = 'windows' | 'linux'

interface AgentPackage {
  id: string
  platform: string
  version: string
  file_name: string
  file_size: number
  sha256?: string | null
  is_latest: boolean
}

interface IssuedPackage {
  fileName: string
  version: string
  packageSha256: string
  enrollmentToken: string
  tokenId: string
  tokenPrefix: string
  maxUses: number
  expiresAt: string
  controllerUrl: string
  installCommand: string
}

const EXT: Record<Platform, string> = {
  windows: '.msi',
  linux: '.tar.gz',
}

function fmtSize(bytes: number) {
  if (!bytes) return ''
  const mb = bytes / (1024 * 1024)
  return `${mb.toFixed(1)} MB`
}

function filenameFrom(disposition: unknown, fallback: string) {
  if (typeof disposition !== 'string') return fallback
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)
  return match?.[1] ? decodeURIComponent(match[1]) : fallback
}

function msiValue(value: string) {
  return value.replace(/"/g, '""')
}

function shellValue(value: string) {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

function deploymentCommand(
  platform: Platform,
  fileName: string,
  controllerUrl: string,
  enrollmentToken: string,
) {
  if (platform === 'windows') {
    return `msiexec.exe /i "${msiValue(fileName)}" /qn /norestart CONTROLLER_URL="${msiValue(controllerUrl)}" ENROLLMENT_TOKEN="${msiValue(enrollmentToken)}"`
  }
  const installerUrl = `${controllerUrl.replace(/\/$/, '')}/api/v1/agents/install.sh`
  return `curl -fsSL ${shellValue(installerUrl)} | sudo env ZENPLUS_CONTROLLER_URL=${shellValue(controllerUrl)} ZENPLUS_ENROLLMENT_TOKEN=${shellValue(enrollmentToken)} bash`
}

export function DownloadAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const qc = useQueryClient()
  const [platform, setPlatform] = useState<Platform>('windows')
  const [scope, setScope] = useState<'fleet' | 'limited'>('fleet')
  const [serverCount, setServerCount] = useState('10')
  const [ttlHours, setTtlHours] = useState('72')
  const [policyId, setPolicyId] = useState('')
  const [label, setLabel] = useState('')
  const [issued, setIssued] = useState<IssuedPackage | null>(null)
  const [copied, setCopied] = useState(false)

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
    (item) => item.platform === platform && item.is_latest,
  )
  const count = scope === 'fleet'
    ? 0
    : Math.max(1, Math.min(100000, Number(serverCount) || 1))

  const download = useMutation({
    mutationFn: async () => {
      const response = await api.post(
        '/agent-fleet/packages/download',
        {
          platform,
          server_count: count,
          ttl_hours: Math.max(1, Math.min(8760, Number(ttlHours) || 72)),
          policy_id: policyId || null,
          label: label.trim() || null,
        },
        { responseType: 'blob', timeout: 10 * 60_000 },
      )

      const fallback = `zenplus-agent${EXT[platform]}`
      const fileName = filenameFrom(response.headers['content-disposition'], fallback)
      const enrollmentToken = String(response.headers['x-enrollment-token'] ?? '')
      const controllerUrl = String(response.headers['x-controller-url'] ?? window.location.origin)
      if (!enrollmentToken) {
        throw new Error('The controller did not return a rollout token for this package')
      }

      const info: IssuedPackage = {
        fileName,
        version: String(response.headers['x-package-version'] ?? published?.version ?? ''),
        packageSha256: String(response.headers['x-package-sha256'] ?? published?.sha256 ?? ''),
        enrollmentToken,
        tokenId: String(response.headers['x-token-id'] ?? ''),
        tokenPrefix: String(response.headers['x-token-prefix'] ?? enrollmentToken.slice(0, 12)),
        maxUses: Number(response.headers['x-token-max-uses'] ?? count),
        expiresAt: String(response.headers['x-token-expires-at'] ?? ''),
        controllerUrl,
        installCommand: deploymentCommand(platform, fileName, controllerUrl, enrollmentToken),
      }

      // The response body is the published package byte-for-byte. The rollout
      // credential stays in the command above, preserving package signatures.
      const url = URL.createObjectURL(response.data as Blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = fileName
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
      return info
    },
    onSuccess: (info) => {
      setIssued(info)
      setCopied(false)
      qc.invalidateQueries({ queryKey: ['enrollment-tokens'] })
      toast.success(`${info.fileName} downloaded`, 'Copy the rollout command to deploy it')
    },
    onError: async (error: unknown) => {
      const response = (error as { response?: { data?: unknown } })?.response
      if (response?.data instanceof Blob) {
        try {
          const parsed = JSON.parse(await response.data.text())
          toast.error(parsed.detail ?? 'Download failed')
          return
        } catch {
          // Fall through to the shared error formatter.
        }
      }
      toast.error('Download failed', apiErrorMessage(error))
    },
  })

  const copyCommand = async () => {
    if (!issued) return
    try {
      await navigator.clipboard.writeText(issued.installCommand)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Copy failed', 'Clipboard access was denied by the browser')
    }
  }

  const clearIssued = () => {
    setIssued(null)
    setCopied(false)
    download.reset()
  }

  const reset = (nextOpen: boolean) => {
    if (!nextOpen) clearIssued()
    onOpenChange(nextOpen)
  }

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-[640px]">
        <DialogHeader>
          <DialogTitle>Download agent and rollout command</DialogTitle>
          <DialogDescription>
            Download the published, generic installer once, then deploy it with the command issued
            here. The MSI remains unchanged and reusable; the separate token controls which
            hosts may enroll and for how long.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="dl-platform">Platform</Label>
              <Select
                value={platform}
                onValueChange={(value) => { setPlatform(value as Platform); clearIssued() }}
              >
                <SelectTrigger id="dl-platform"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="windows">Windows</SelectItem>
                  <SelectItem value="linux">Linux</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="dl-scope">Rollout token</Label>
              <Select
                value={scope}
                onValueChange={(value) => { setScope(value as 'fleet' | 'limited'); clearIssued() }}
              >
                <SelectTrigger id="dl-scope"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="fleet">Unlimited hosts until expiry</SelectItem>
                  <SelectItem value="limited">Limit to a fixed number</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {scope === 'limited' && (
            <div className="space-y-1.5">
              <Label htmlFor="dl-count">Maximum enrollments</Label>
              <Input
                id="dl-count"
                type="number"
                min={1}
                max={100000}
                value={serverCount}
                onChange={(event) => { setServerCount(event.target.value); clearIssued() }}
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
                onChange={(event) => { setTtlHours(event.target.value); clearIssued() }}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="dl-policy">Policy (optional)</Label>
              <Select
                value={policyId || '__default'}
                onValueChange={(value) => { setPolicyId(value === '__default' ? '' : value); clearIssued() }}
              >
                <SelectTrigger id="dl-policy"><SelectValue placeholder="Default policy" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__default">Default policy</SelectItem>
                  {(policies ?? []).map((policy) => (
                    <SelectItem key={policy.id} value={policy.id}>{policy.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="dl-label">Rollout label (optional)</Label>
            <Input
              id="dl-label"
              placeholder="e.g. Datacentre A rollout"
              value={label}
              onChange={(event) => { setLabel(event.target.value); clearIssued() }}
            />
          </div>

          <div className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
            <Server className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              Push the same installer through GPO, Intune, SCCM, RMM, or imaging. Enrollment
              is authorized by the command, not by a modified package.{' '}
              {scope === 'fleet'
                ? 'This token permits any number of hosts until it expires or is revoked.'
                : `This token permits ${count.toLocaleString()} enrollment${count === 1 ? '' : 's'}.`}
            </span>
          </div>

          {!packagesLoading && !published && (
            <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                No {platform} package is published yet. The release pipeline must publish an
                immutable package before a rollout can be issued.
              </span>
            </div>
          )}

          {published && (
            <div className="flex items-center gap-2 text-xs text-muted">
              <ShieldCheck className="h-3.5 w-3.5 text-success" />
              <span>
                Published <span className="font-medium text-text">{published.file_name}</span>{' '}
                (v{published.version}{published.file_size ? `, ${fmtSize(published.file_size)}` : ''})
              </span>
            </div>
          )}

          {issued && (
            <div className="space-y-3 rounded-md border border-success/40 bg-success/10 px-3 py-3 text-xs">
              <div className="flex items-center gap-2 font-medium text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Immutable package downloaded; rollout command ready
              </div>
              <div className="grid gap-1 text-muted sm:grid-cols-2">
                <span>Package: <span className="font-medium text-text">{issued.fileName}</span></span>
                <span>Token: {issued.tokenPrefix}...</span>
                <span>
                  Scope: {issued.maxUses === 0 ? 'unlimited hosts' : `${issued.maxUses.toLocaleString()} hosts`}
                </span>
                <span>
                  {issued.expiresAt ? `Expires: ${new Date(issued.expiresAt).toLocaleString()}` : 'Expiry unavailable'}
                </span>
                {issued.packageSha256 && (
                  <span className="col-span-full truncate font-mono" title={issued.packageSha256}>
                    SHA-256: {issued.packageSha256}
                  </span>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="rollout-command">
                  {platform === 'windows' ? 'Silent deployment command' : 'Deployment command'}
                </Label>
                <div className="flex items-start gap-2">
                  <code
                    id="rollout-command"
                    className="min-w-0 flex-1 select-all break-all rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11px] leading-relaxed text-text"
                  >
                    {issued.installCommand}
                  </code>
                  <Button size="sm" variant="outline" onClick={copyCommand} aria-label="Copy rollout command">
                    {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                    {copied ? 'Copied' : 'Copy'}
                  </Button>
                </div>
                <p className="text-[11px] text-warning">
                  The command contains an enrollment credential. Store it as a protected deployment
                  secret and revoke it from Rollout tokens when the rollout finishes.
                </p>
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => reset(false)}>Close</Button>
          <Button onClick={() => download.mutate()} disabled={download.isPending || !published}>
            {download.isPending ? (
              <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Preparing...</>
            ) : (
              <><Download className="h-3.5 w-3.5" /> Issue token and download</>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
