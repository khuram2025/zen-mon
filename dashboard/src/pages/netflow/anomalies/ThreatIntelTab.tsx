import { useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, FileUp, Globe, Loader2, Lock, Plus, RefreshCw, Trash2, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { formatCount, formatDateTime } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import { cn, relativeTime } from '@/lib/utils'
import { feedState, type TiCatalogEntry, type TiFeed, type TiFeedsResponse } from './model'
import { ToneChip } from './parts'

const MAX_UPLOAD_BYTES = 16 * 1024 * 1024

interface FeedResult {
  result?: { ok: boolean; status: string } | null
}

/**
 * Threat-intelligence feeds (CR-10). Nothing ships built in: operators add URL feeds (downloaded
 * daily once enabled) or upload lists on air-gapped appliances. Matches become `threat_intel`
 * findings on the Findings tab.
 */
export function ThreatIntelTab({ tz }: { tz: string }) {
  const q = useNfQuery<TiFeedsResponse>('/netflow/ti/feeds', null, { refetchInterval: false })
  const qc = useQueryClient()
  const [toDelete, setToDelete] = useState<TiFeed | null>(null)
  const done = (title: string) => (r: FeedResult) => {
    if (r.result && !r.result.ok) toast.error(title, r.result.status)
    else toast.success(title, r.result?.status)
    void qc.invalidateQueries({ queryKey: nfKeys.all })
  }
  const onError = (title: string) => (e: unknown) => toast.error(title, apiErrorMessage(e))
  const patch = useMutation({
    mutationFn: async ({ id, body }: { id: string; body: Record<string, unknown> }) => (await api.patch(`/netflow/ti/feeds/${id}`, body)).data as FeedResult,
    onSuccess: done('Feed updated'),
    onError: onError('The feed was not updated'),
  })
  const refresh = useMutation({
    mutationFn: async (id: string) => (await api.post(`/netflow/ti/feeds/${id}/refresh`)).data as FeedResult,
    onSuccess: done('Feed refreshed'),
    onError: onError('The feed was not refreshed'),
  })
  const upload = useMutation({
    mutationFn: async ({ id, content }: { id: string; content: string }) => (await api.post(`/netflow/ti/feeds/${id}/upload`, { content })).data as FeedResult,
    onSuccess: done('List uploaded'),
    onError: onError('The list was not uploaded'),
  })
  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/netflow/ti/feeds/${id}`)).data,
    onSuccess: () => {
      toast.success('Feed removed')
      setToDelete(null)
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: onError('The feed was not removed'),
  })

  return (
    <div className="space-y-3">
      <QueryPanel query={q} label="threat-intel feeds" skeleton="rows" isEmpty={() => false}>
        {(res) => {
          const d = res.data
          return (
            <div className="space-y-3">
              <Card>
                <CardContent className="flex flex-col gap-1 p-4 text-xs text-text2">
                  <p>
                    <span className="font-semibold text-text">{formatCount(d.index.prefixes)}</span> prefix{d.index.prefixes === 1 ? '' : 'es'} from{' '}
                    <span className="font-semibold text-text">{d.index.feeds}</span> enabled feed{d.index.feeds === 1 ? '' : 's'} are checked against the public addresses of every 5-minute period.
                  </p>
                  <p>{d.builtin_note} Private and special-use ranges in a list, and prefixes wider than /8 (IPv6 /16), are ignored.</p>
                  {!d.can_manage && (
                    <p className="flex items-center gap-1.5">
                      <Lock className="h-3.5 w-3.5" aria-hidden /> Read only: managing feeds needs the netflow.manage permission.
                    </p>
                  )}
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-3">
                  {d.items.length === 0 ? (
                    <p className="px-1 py-6 text-center text-sm text-text2">
                      No feeds yet. The threat-intel detector is <span className="font-medium text-text">unavailable</span> until a feed with entries is enabled.
                    </p>
                  ) : (
                    <ul className="divide-y divide-border">
                      {d.items.map((f) => (
                        <FeedRow
                          key={f.id}
                          feed={f}
                          tz={tz}
                          canManage={d.can_manage}
                          busy={patch.isPending || refresh.isPending || upload.isPending}
                          onToggle={(enabled) => patch.mutate({ id: f.id, body: { enabled } })}
                          onRefresh={() => refresh.mutate(f.id)}
                          onUpload={(content) => upload.mutate({ id: f.id, content })}
                          onDelete={() => setToDelete(f)}
                        />
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>
              {d.can_manage && <AddFeed catalog={d.catalog} categories={d.categories} existing={d.items} />}
            </div>
          )
        }}
      </QueryPanel>
      <ConfirmDialog
        open={!!toDelete}
        onOpenChange={(o) => !o && setToDelete(null)}
        title="Remove this feed?"
        description={toDelete ? `${toDelete.name} and its ${toDelete.entry_count.toLocaleString('en-US')} prefixes are deleted. Existing findings stay.` : undefined}
        confirmText="Remove"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (toDelete) remove.mutate(toDelete.id)
        }}
      />
    </div>
  )
}

function FeedRow({
  feed: f,
  tz,
  canManage,
  busy,
  onToggle,
  onRefresh,
  onUpload,
  onDelete,
}: {
  feed: TiFeed
  tz: string
  canManage: boolean
  busy: boolean
  onToggle: (enabled: boolean) => void
  onRefresh: () => void
  onUpload: (content: string) => void
  onDelete: () => void
}) {
  const state = feedState(f)
  const fileRef = useRef<HTMLInputElement>(null)
  return (
    <li className="flex flex-col gap-2 py-3 min-[720px]:flex-row min-[720px]:items-start min-[720px]:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          {f.source === 'url' ? <Globe className="h-4 w-4 text-text2" aria-hidden /> : <FileUp className="h-4 w-4 text-text2" aria-hidden />}
          <span className="text-sm font-medium text-text">{f.name}</span>
          <ToneChip tone={state.tone}>{state.label}</ToneChip>
          <span className="rounded-md bg-surface2 px-1.5 py-0.5 text-xs text-text2">{f.category}</span>
        </div>
        <p className="mt-0.5 text-xs text-text2">
          {formatCount(f.entry_count)} prefix{f.entry_count === 1 ? '' : 'es'} · {f.source === 'url' ? `every ${f.refresh_hours} h` : 'uploaded list'}
          {f.last_refreshed_at ? ` · updated ${formatDateTime(f.last_refreshed_at, tz, { seconds: false })} (${relativeTime(f.last_refreshed_at)})` : ' · never loaded'}
        </p>
        {typeof f.active_findings === 'number' && (
          <p className="mt-0.5 text-xs">
            {f.active_findings > 0 ? (
              <Link to="/netflow/anomalies?kind=threat_intel" className="font-medium text-primary hover:underline">
                {formatCount(f.active_findings)} active finding{f.active_findings === 1 ? '' : 's'} cite this feed →
              </Link>
            ) : (
              <span className="text-text2">No active findings cite this feed</span>
            )}
          </p>
        )}
        {f.url && <p className="break-all font-mono text-xs text-text2">{f.url}</p>}
        {f.last_status && (
          <p className={cn('mt-0.5 break-words text-xs', state.tone === 'danger' ? 'text-danger-fg' : 'text-text2')}>{f.last_status}</p>
        )}
      </div>
      {canManage && (
        <div className="flex flex-wrap items-center gap-1.5">
          <label className="mr-1 flex items-center gap-1.5 text-xs text-text">
            <Switch checked={f.enabled} onCheckedChange={onToggle} disabled={busy} aria-label={`${f.name} enabled`} />
            {f.enabled ? 'Enabled' : 'Disabled'}
          </label>
          {f.source === 'url' && (
            <Button size="sm" variant="outline" onClick={onRefresh} disabled={busy} aria-label={`Download ${f.name} now`}>
              <RefreshCw aria-hidden /> Download now
            </Button>
          )}
          {f.source === 'upload' && (
            <>
              <input
                ref={fileRef}
                type="file"
                accept=".txt,.csv,.netset,.list,text/plain"
                className="sr-only"
                aria-label={`Replace the list of ${f.name}`}
                onChange={async (e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (!file) return
                  if (file.size > MAX_UPLOAD_BYTES) {
                    toast.error('The file is too large', 'Lists up to 16 MB can be uploaded')
                    return
                  }
                  onUpload(await file.text())
                }}
              />
              <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()} disabled={busy}>
                <Upload aria-hidden /> Replace list
              </Button>
            </>
          )}
          <Button size="sm" variant="outline" onClick={onDelete} aria-label={`Remove feed ${f.name}`}>
            <Trash2 aria-hidden />
          </Button>
        </div>
      )}
    </li>
  )
}

function AddFeed({ catalog, categories, existing }: { catalog: TiCatalogEntry[]; categories: string[]; existing: TiFeed[] }) {
  const qc = useQueryClient()
  const [source, setSource] = useState<'url' | 'upload'>('url')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [category, setCategory] = useState('malicious')
  const [enabled, setEnabled] = useState(false)
  const [content, setContent] = useState<string | null>(null)
  const [fileName, setFileName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const create = useMutation({
    mutationFn: async (body: Record<string, unknown>) => (await api.post('/netflow/ti/feeds', body)).data as TiFeed & FeedResult,
    onSuccess: (r) => {
      if (r.result && !r.result.ok) toast.error('Feed added, but loading it failed', r.result.status)
      else toast.success('Feed added', r.result?.status ?? (r.enabled ? undefined : 'Enable it to start downloading'))
      setName('')
      setUrl('')
      setContent(null)
      setFileName(null)
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('The feed was not added', apiErrorMessage(e)),
  })
  const names = new Set(existing.map((f) => f.name.toLowerCase()))
  const submit = () => {
    if (!name.trim()) return setError('Give the feed a name')
    if (names.has(name.trim().toLowerCase())) return setError('A feed with that name already exists')
    if (source === 'url' && !/^https?:\/\//i.test(url.trim())) return setError('The URL must start with http:// or https://')
    if (source === 'upload' && !content) return setError('Choose a list file to upload')
    setError(null)
    create.mutate({ name: name.trim(), source, url: source === 'url' ? url.trim() : null, category, enabled: source === 'upload' ? true : enabled, content: source === 'upload' ? content : null })
  }
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <h3 className="text-sm font-semibold text-text">Add a feed</h3>
        {catalog.length > 0 && (
          <div>
            <p className="text-xs text-text2">Suggested public lists (downloaded only once you enable them; check each licence):</p>
            <ul className="mt-1.5 grid grid-cols-1 gap-2 lg:grid-cols-3">
              {catalog.map((c) => {
                const added = existing.some((f) => f.url === c.url)
                return (
                  <li key={c.key} className="rounded-md border border-border p-2 text-xs">
                    <p className="font-medium text-text">{c.name}</p>
                    <p className="mt-0.5 text-text2">{c.licence}</p>
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-1.5"
                      disabled={added}
                      onClick={() => {
                        setSource('url')
                        setName(c.name)
                        setUrl(c.url)
                        setCategory(c.category)
                      }}
                    >
                      {added ? 'Added' : <><Download aria-hidden /> Use this list</>}
                    </Button>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
        <form
          className="grid grid-cols-1 gap-2 min-[560px]:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <fieldset className="min-[560px]:col-span-2">
            <legend className="text-xs font-medium text-text">Source</legend>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {(['url', 'upload'] as const).map((s) => (
                <label key={s} className={cn('cursor-pointer rounded-md border px-2.5 py-1 text-xs', source === s ? 'border-primary bg-primary/10 text-text' : 'border-border text-text2')}>
                  <input type="radio" name="nf-ti-source" className="sr-only" checked={source === s} onChange={() => setSource(s)} />
                  {s === 'url' ? 'Download from a URL' : 'Upload a list (air-gapped)'}
                </label>
              ))}
            </div>
          </fieldset>
          <Row label="Name" id="nf-ti-name">
            <Input id="nf-ti-name" className="h-9" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
          </Row>
          <Row label="Category" id="nf-ti-cat">
            <select id="nf-ti-cat" className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text" value={category} onChange={(e) => setCategory(e.target.value)}>
              {categories.map((c) => (
                <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>
              ))}
            </select>
          </Row>
          {source === 'url' ? (
            <>
              <Row label="URL" id="nf-ti-url">
                <Input id="nf-ti-url" className="h-9" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
              </Row>
              <label className="flex items-center gap-2 self-end text-xs text-text">
                <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Enable and download now" />
                Enable and download now
              </label>
            </>
          ) : (
            <Row label="List file (one address or CIDR per line; # or ; comments)" id="nf-ti-file">
              <input
                id="nf-ti-file"
                type="file"
                accept=".txt,.csv,.netset,.list,text/plain"
                className="block w-full text-xs text-text file:mr-2 file:rounded-md file:border file:border-border file:bg-surface2 file:px-2 file:py-1 file:text-xs file:text-text"
                onChange={async (e) => {
                  const file = e.target.files?.[0]
                  if (!file) return
                  if (file.size > MAX_UPLOAD_BYTES) {
                    setError('Lists up to 16 MB can be uploaded')
                    return
                  }
                  setFileName(file.name)
                  setContent(await file.text())
                  if (!name) setName(file.name.replace(/\.[^.]+$/, ''))
                }}
              />
              {fileName && <span className="mt-0.5 block text-xs text-text2">{fileName} ready</span>}
            </Row>
          )}
          {error && <p className="text-xs text-danger-fg min-[560px]:col-span-2" role="alert">{error}</p>}
          <div className="min-[560px]:col-span-2">
            <Button type="submit" size="sm" disabled={create.isPending}>
              {create.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Plus aria-hidden />} Add feed
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function Row({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-text">{label}</label>
      <div className="mt-1">{children}</div>
    </div>
  )
}
