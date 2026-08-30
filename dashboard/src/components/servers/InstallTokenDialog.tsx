/** Controller-only agent deployment. New hosts register as pending and the
 * appliance issues their protected credential only after operator approval. */

import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Bot, Check, Copy, Download, Loader2, ShieldCheck, Terminal, UserCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, copyText } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'

type Platform = 'windows' | 'linux'
type WindowsShell = 'powershell' | 'cmd'

interface AgentPackage {
  id: string
  platform: string
  version: string
  file_name: string
  file_size: number
  sha256?: string | null
  is_latest: boolean
}

function shellValue(value: string) {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

function deploymentCommand(
  platform: Platform,
  controllerUrl: string,
  installProfile: 'infrastructure' | 'apm' | 'combined',
  windowsShell: WindowsShell,
) {
  const controller = controllerUrl.replace(/\/$/, '')
  if (platform === 'windows') {
    const script = `${controller}/api/v1/agents/install.ps1`
    const direct = `$s=Join-Path $env:TEMP 'zenplus-agent-install.ps1'; Invoke-WebRequest -UseBasicParsing -Uri '${script}' -OutFile $s; & $s -ControllerUrl '${controller}' -InstallProfile '${installProfile}'`
    return windowsShell === 'cmd'
      ? `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "${direct}"`
      : direct
  }
  const installerUrl = `${controller}/api/v1/agents/install.sh`
  return `curl -fsSL ${shellValue(installerUrl)} | sudo env ZENPLUS_CONTROLLER_URL=${shellValue(controller)} bash`
}

function fmtSize(bytes: number) {
  return bytes ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : ''
}

function filenameFrom(disposition: unknown, fallback: string) {
  if (typeof disposition !== 'string') return fallback
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)
  return match?.[1] ? decodeURIComponent(match[1]) : fallback
}

export function InstallTokenDialog({
  open,
  onOpenChange,
  serverId,
  serverName,
  installProfile = 'combined',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  serverId?: string
  serverName?: string
  installProfile?: 'infrastructure' | 'apm' | 'combined'
}) {
  const [platform, setPlatform] = useState<Platform>('windows')
  const [windowsShell, setWindowsShell] = useState<WindowsShell>('powershell')
  const [copied, setCopied] = useState(false)
  const [controllerUrl, setControllerUrl] = useState(() => window.location.origin)

  const { data: packages, isLoading } = useQuery<{ items: AgentPackage[] }>({
    queryKey: ['agent-packages'],
    queryFn: async () => (await api.get('/agent-fleet/packages')).data,
    enabled: open,
  })
  const published = (packages?.items ?? []).find(
    (item) => item.platform === platform && item.is_latest,
  )
  const command = useMemo(
    () => deploymentCommand(platform, controllerUrl, installProfile, windowsShell),
    [platform, controllerUrl, installProfile, windowsShell],
  )

  const download = useMutation({
    mutationFn: async () => api.post(
      '/agent-fleet/packages/download',
      { platform },
      { responseType: 'blob', timeout: 10 * 60_000 },
    ),
    onSuccess: (response) => {
      const nextController = String(response.headers['x-controller-url'] ?? controllerUrl)
      setControllerUrl(nextController)
      const fallback = published?.file_name ?? `zenplus-agent.${platform === 'windows' ? 'msi' : 'tar.gz'}`
      const fileName = filenameFrom(response.headers['content-disposition'], fallback)
      const url = URL.createObjectURL(response.data as Blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = fileName
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
      toast.success('Agent package downloaded', 'Install it with the controller address only')
    },
    onError: async (error: unknown) => {
      const response = (error as { response?: { data?: unknown } })?.response
      if (response?.data instanceof Blob) {
        try {
          const parsed = JSON.parse(await response.data.text())
          toast.error('Download failed', parsed.detail)
          return
        } catch {
          // Use the shared formatter below.
        }
      }
      toast.error('Download failed', apiErrorMessage(error))
    },
  })

  const copyCommand = async () => {
    try {
      if (!(await copyText(command))) throw new Error('clipboard blocked')
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Copy failed', 'Clipboard access was denied by the browser')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bot className="h-4 w-4 text-primary" />
            Install ZenPlus Agent{installProfile === 'apm' ? ' for APM' : ''}{serverName ? ` on ${serverName}` : ''}
          </DialogTitle>
          <DialogDescription>
            No enrollment token, site ID, policy ID, or APM ingest key is placed on the endpoint. The appliance
            approves the installation and issues protected credentials centrally.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1">
          <div className="grid gap-2 sm:grid-cols-3">
            {[
              [Download, '1. Install', 'Use only the ZenPlus controller address.'],
              [Bot, '2. Registration', 'The host appears as Pending authorization.'],
              [UserCheck, '3. Approve', 'An operator authorizes monitoring in Agent Fleet.'],
            ].map(([Icon, title, detail]) => (
              <div key={String(title)} className="rounded-lg border border-border bg-surface2/30 p-3">
                <Icon className="mb-2 h-4 w-4 text-primary" />
                <div className="text-xs font-semibold text-text">{String(title)}</div>
                <div className="mt-1 text-[11px] leading-relaxed text-muted">{String(detail)}</div>
              </div>
            ))}
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="agent-platform">Platform</Label>
              <Select value={platform} onValueChange={(value) => setPlatform(value as Platform)}>
                <SelectTrigger id="agent-platform"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="windows">Windows</SelectItem>
                  <SelectItem value="linux">Linux</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {platform === 'windows' && (
              <div className="space-y-1.5">
                <Label htmlFor="agent-command-shell">Run command in</Label>
                <Select value={windowsShell} onValueChange={(value) => setWindowsShell(value as WindowsShell)}>
                  <SelectTrigger id="agent-command-shell"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="powershell">PowerShell (recommended)</SelectItem>
                    <SelectItem value="cmd">Command Prompt</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className={`space-y-1.5 ${platform === 'linux' ? 'sm:col-span-2' : ''}`}>
              <Label>Published package</Label>
              <div className="flex h-10 items-center rounded-md border border-border bg-bg px-3 text-xs">
                {isLoading ? (
                  <><Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> Checking package…</>
                ) : published ? (
                  <><ShieldCheck className="mr-2 h-3.5 w-3.5 text-success" /> {published.file_name} · v{published.version}{published.file_size ? ` · ${fmtSize(published.file_size)}` : ''}</>
                ) : (
                  <span className="text-danger">No published {platform} package</span>
                )}
              </div>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-install-command">Controller-only deployment command</Label>
            <div className="flex items-start gap-2">
              <code id="agent-install-command" className="min-w-0 flex-1 select-all break-all rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11px] leading-relaxed text-text">
                {command}
              </code>
              <Button size="sm" variant="outline" onClick={copyCommand}>
                {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            {platform === 'windows' && (
              <div className="flex items-start gap-2 rounded-md border border-info/25 bg-info/5 px-3 py-2 text-[11px] leading-relaxed text-muted">
                <Terminal className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
                {windowsShell === 'powershell' ? (
                  <span>Open <strong className="text-text">PowerShell as Administrator</strong> and paste this command directly. Do not wrap it inside another <code>powershell -Command</code>.</span>
                ) : (
                  <span>Open <strong className="text-text">Command Prompt as Administrator</strong> and paste this command. This option intentionally launches PowerShell once.</span>
                )}
              </div>
            )}
          </div>

          <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs text-muted">
            After installation, open <strong className="text-text">Agent Fleet</strong>, locate the
            pending host, and select <strong className="text-text">Authorize monitoring</strong>.
            {serverId && ' ZenPlus will associate it with this server when the reported hostname matches.'}
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Close</Button>
          <Button onClick={() => download.mutate()} disabled={download.isPending || !published}>
            {download.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            {download.isPending ? 'Downloading…' : 'Download installer'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
