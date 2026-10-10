import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowRight, ExternalLink, HardDrive, Lock, Network, Server, ShieldAlert, ShieldCheck, ShieldQuestion,
} from 'lucide-react'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { StatusDot } from '@/components/ui/StatusDot'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { inventoryApi } from '@/pages/inventory/api'
import { parseAssetAddress } from '@/pages/inventory/filters'
import type { AssetDetail, DeviceBody, ServerBody } from '@/pages/inventory/types'
import {
  TRUNCATED_NOTE, assetChangesHref, badgeSource, assetTags, cpuText, disksText, flyoutLinks, identityText, isMonoField, kindLabel,
  memoryText, securityAgentSummary, serverSoftwareHref, statusTone,
} from './assetModel'
import { CATEGORY_LABELS, changeText } from './changeModel'
import { FreshnessBadge } from './FreshnessBadge'
import { ageWords, fmtInt, plural, toMillis } from './model'
import { productHref } from './overviewModel'
import { Sheet } from './Sheet'
import { SourceBadge } from './SourceBadge'
import { categoryLabel } from './softwareModel'

export const assetDetailKey = (address: string) => ['inventory', 'asset', address] as const

/** An unknown or out-of-scope asset: the API says the same for both, so the page does too. */
function isNotFound(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 404
}

function Section({ title, hint, children, className }: { title: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn('border-b border-border/70 px-4 py-3 last:border-b-0', className)}>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
        {hint && <span className="text-[11px] text-muted">{hint}</span>}
      </div>
      {children}
    </section>
  )
}

function Fact({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={cn('min-w-0', wide && 'col-span-2')}>
      <dt className="text-[11px] text-muted">{label}</dt>
      <dd className="mt-0.5 break-words text-[13px] text-text">{children}</dd>
    </div>
  )
}

const dash = <span className="text-muted" aria-label="not reported">—</span>

/* ── Header ──────────────────────────────────────────────────────────────── */

export function FlyoutHeader({ detail, now }: { detail: AssetDetail; now: number }) {
  const can = useCan()
  const { asset } = detail
  const links = flyoutLinks(detail, can)
  const tone = statusTone(asset.status)
  const Icon = asset.kind === 'server' ? Server : Network
  return (
    <div className="mt-1.5 space-y-2" data-flyout-header="">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text2">
        <span className="inline-flex items-center gap-1"><Icon className="h-3.5 w-3.5 text-muted" aria-hidden />{kindLabel(asset.kind)}{asset.asset_type && asset.asset_type !== asset.kind ? ` · ${asset.asset_type}` : ''}</span>
        {asset.status && (
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status={tone === 'ok' ? 'up' : tone === 'bad' ? 'down' : tone === 'warn' ? 'warn' : 'idle'} />
            {asset.status}
          </span>
        )}
        {asset.ip && <span className="font-mono">{asset.ip}</span>}
        <FreshnessBadge freshness={asset.freshness} kind={asset.kind} inventoryAt={asset.inventory_at} lastSeen={asset.last_seen} now={now} />
      </div>
      {asset.freshness === 'not_applicable' && (
        <p className="text-xs text-muted" data-flyout="by-design">
          This asset cannot send an inventory by design (a ping-only or controller-managed device, or a server whose collection mode has no inventory upload), so it is not counted as never reported.
        </p>
      )}
      {links.length > 0 && (
        <ul role="list" className="flex flex-wrap gap-x-4 gap-y-1 text-xs" data-flyout-links="">
          {links.map((l) => (
            <li key={l.id}>
              {l.to ? (
                <Link to={l.to} className="inline-flex items-center gap-1 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
                  {l.label} <ExternalLink className="h-3 w-3" aria-hidden />
                </Link>
              ) : (
                <span className="inline-flex items-center gap-1 text-muted" title={l.reason ?? undefined}>
                  <Lock className="h-3 w-3" aria-hidden /> {l.label}: {l.reason?.toLowerCase()}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ── Identity ────────────────────────────────────────────────────────────── */

function Identity({ detail, now }: { detail: AssetDetail; now: number }) {
  return (
    <Section title="Identity" hint="value, source, age">
      <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
        {detail.identity.map((v) => {
          const text = identityText(v)
          return (
            <div key={v.field} className="contents" data-identity={v.field}>
              <dt className="pt-px text-xs text-muted">{v.label}</dt>
              <dd className="min-w-0">
                {text === null ? (
                  <span className="italic text-muted">Unknown</span>
                ) : (
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className={cn('min-w-0 break-words text-text', isMonoField(v.field) && 'font-mono text-xs')}>{text}</span>
                    <SourceBadge source={badgeSource(v.source)} at={v.observed_at} showAge now={now} />
                  </span>
                )}
              </dd>
            </div>
          )
        })}
      </dl>
      {Object.keys(detail.raw).length > 0 && (
        <details className="mt-2.5 rounded-md border border-border bg-surface2/40 px-2.5 py-1.5 text-xs">
          <summary className="cursor-pointer select-none rounded font-medium text-text2 outline-none focus-visible:ring-2 focus-visible:ring-primary">Raw evidence</summary>
          <dl className="mt-1.5 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-1">
            {Object.entries(detail.raw).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted">{k}</dt>
                <dd className="break-words font-mono text-[11px] text-text2">{v ?? '—'}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </Section>
  )
}

/* ── Server ──────────────────────────────────────────────────────────────── */

function SecurityPill({ agent }: { agent: ServerBody['software']['security_agent'] }) {
  const { state, text, detail } = securityAgentSummary(agent)
  const Icon = state === 'present' ? ShieldCheck : state === 'missing' ? ShieldAlert : ShieldQuestion
  const tone = state === 'present' ? 'border-success/30 bg-success/10 text-success-fg' : state === 'missing' ? 'border-warning/35 bg-warning/10 text-warning-fg' : 'border-border bg-surface2 text-text2'
  return (
    <div className="space-y-1">
      <span data-security-agent={state} className={cn('inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium', tone)}>
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {text}
        {state === 'present' && detail && <span className="font-normal">({detail})</span>}
      </span>
      {state === 'unknown' && detail && <p className="text-xs text-muted" data-security-reason={agent.reason ?? ''}>{detail}</p>}
    </div>
  )
}

function ServerSections({ detail }: { detail: AssetDetail }) {
  const can = useCan()
  const s = detail.server as ServerBody
  const cpu = cpuText(s.cpu_model, s.cpu_cores, s.cpu_physical_cores)
  const memory = memoryText(s.memory_total_bytes)
  const disks = disksText(s.disks)
  const sw = s.software
  const serverId = detail.links.server_id
  return (
    <>
      <Section title="Hardware">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2" data-flyout="server-hardware">
          <Fact label="CPU" wide>{cpu ?? dash}</Fact>
          <Fact label="Memory">{memory ?? dash}</Fact>
          <Fact label="Disks">{disks ?? dash}</Fact>
          <Fact label="Virtualization">{s.virtualization ?? dash}</Fact>
          <Fact label="Architecture">{s.architecture ?? dash}</Fact>
        </dl>
      </Section>
      <Section title="Software" hint={`${fmtInt(sw.total)} titles${sw.hidden > 0 ? `, ${fmtInt(sw.hidden)} hidden` : ''}`}>
        <div className="mb-2"><SecurityPill agent={sw.security_agent} /></div>
        {sw.truncated && (
          <p role="note" className="mb-2 flex items-start gap-1.5 rounded-md border border-warning/35 bg-warning/10 px-2.5 py-1.5 text-xs text-warning-fg" data-flyout="truncated">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden /> {TRUNCATED_NOTE}
          </p>
        )}
        {!s.baselined ? (
          <p className="text-xs text-muted">No software report has been accepted yet, so nothing can be said about what is installed.</p>
        ) : sw.top.length === 0 ? (
          <p className="text-xs text-muted">No visible software titles.</p>
        ) : (
          <ul role="list" className="divide-y divide-border/60" data-flyout="software">
            {sw.top.map((p) => (
              <li key={`${p.package_name}:${p.version}:${p.architecture}`} className="flex items-baseline gap-2 py-1 text-[13px]">
                {p.product_key ? (
                  <Link to={productHref(p.product_key)} className="min-w-0 flex-1 truncate rounded-sm font-medium text-text outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary" title={p.package_name}>{p.product}</Link>
                ) : (
                  <span className="min-w-0 flex-1 truncate font-medium text-text" title={p.package_name}>{p.product}</span>
                )}
                {(p.category === 'security' || p.category === 'endpoint-protection') && <span className="shrink-0 rounded border border-border bg-surface2 px-1 py-px text-[10px] font-medium text-text2">{categoryLabel(p.category)}</span>}
                <span className="shrink-0 font-mono text-[11px] text-text2">{p.version ?? '—'}</span>
              </li>
            ))}
          </ul>
        )}
        {serverId && (
          <p className="mt-2 text-xs">
            {can('servers.view') ? (
              <Link to={serverSoftwareHref(serverId)} className="inline-flex items-center gap-1 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
                View all {fmtInt(sw.total)} on the server page <ArrowRight className="h-3 w-3" aria-hidden />
              </Link>
            ) : (
              <span className="inline-flex items-center gap-1 text-muted"><Lock className="h-3 w-3" aria-hidden /> The full list is on the server page; it needs the servers.view permission.</span>
            )}
          </p>
        )}
      </Section>
    </>
  )
}

/* ── Network device ──────────────────────────────────────────────────────── */

function DeviceSections({ detail, now }: { detail: AssetDetail; now: number }) {
  const d = detail.device as DeviceBody
  const ha = d.ha
  return (
    <>
      <Section title="Device">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2" data-flyout="device">
          <Fact label="Profile">{d.profile ?? dash}</Fact>
          <Fact label="Group">{d.group ?? dash}</Fact>
          <Fact label="Type">{d.device_type ?? dash}</Fact>
          <Fact label="sysObjectID"><span className="font-mono text-xs">{d.sys_object_id ?? '—'}</span></Fact>
        </dl>
        {d.controller_managed && <p className="mt-2 text-xs text-text2">This device is managed by a controller; its inventory comes from there.</p>}
      </Section>
      {ha && (
        <Section title="High availability" hint={ha.observed_at ? `as of ${ageWords(ha.observed_at, now)}` : undefined}>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2" data-flyout="ha">
            <Fact label="Mode">{ha.mode ?? dash}</Fact>
            <Fact label="Role">{ha.role ?? dash}</Fact>
            <Fact label="Peer">{ha.peer ?? dash}</Fact>
            <Fact label="Sync">{ha.sync ?? dash}</Fact>
          </dl>
        </Section>
      )}
      <Section title="Modules" hint={d.modules.length > 0 ? plural(d.modules.reduce((n, g) => n + g.count, 0), 'component') : undefined}>
        {d.modules.length === 0 ? (
          <p className="text-xs text-muted">No ENTITY-MIB components have been read from this device.</p>
        ) : (
          <ul role="list" className="space-y-1" data-flyout="modules">
            {d.modules.map((g) => (
              <li key={`${g.class}:${g.model ?? ''}`}>
                <details className="group rounded-md border border-border/70 bg-surface2/30 px-2.5 py-1.5">
                  <summary className="flex cursor-pointer select-none items-baseline gap-2 rounded text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-primary">
                    <span className="font-medium text-text">{g.class_label}</span>
                    <span className={cn('min-w-0 flex-1 truncate font-mono text-xs text-text2', !g.model && 'font-sans italic text-muted')}>{g.model ?? 'model unknown'}</span>
                    <span className="shrink-0 tabular-nums text-xs text-muted">× {fmtInt(g.count)}</span>
                  </summary>
                  <ul role="list" className="mt-1.5 space-y-0.5 border-t border-border/60 pt-1.5">
                    {g.items.map((it) => (
                      <li key={it.ent_index} className="flex items-baseline gap-2 text-xs">
                        <span className="min-w-0 flex-1 truncate text-text2" title={it.name ?? undefined}>{it.name ?? `Component ${it.ent_index}`}</span>
                        <span className={cn('shrink-0 font-mono text-[11px]', it.serial ? 'text-text' : 'font-sans italic text-muted')}>{it.serial ?? 'no serial'}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              </li>
            ))}
          </ul>
        )}
        {d.modules_truncated && <p role="note" className="mt-2 text-xs text-warning-fg">This device has more than 500 components; the list is cut.</p>}
      </Section>
    </>
  )
}

/* ── Recent changes ──────────────────────────────────────────────────────── */

function RecentChanges({ detail, now }: { detail: AssetDetail; now: number }) {
  const { items, total, first_change_at: firstChangeAt } = detail.changes
  return (
    <Section title="Recent changes" hint={total > 0 ? `${fmtInt(items.length)} of ${fmtInt(total)}` : undefined}>
      {items.length === 0 ? (
        <p className="text-xs text-muted">No changes recorded for this asset. History starts when change capture was enabled.</p>
      ) : (
        <ol className="divide-y divide-border/60" data-flyout="changes">
          {items.map((c) => {
            const t = changeText(c)
            return (
              <li key={c.id} className="flex items-start gap-2 py-1.5 text-[13px]">
                <time dateTime={c.observed_at} title={new Date(toMillis(c.observed_at) ?? 0).toLocaleString()} className="w-14 shrink-0 whitespace-nowrap pt-px text-[11px] tabular-nums text-muted">
                  {ageWords(c.observed_at, now)}
                </time>
                <span className="min-w-0 flex-1 text-text2">
                  {t.marker ? <span className="italic text-muted">{t.sentence}</span> : <><span className="font-medium text-text">{t.subject}</span> {t.verb}{t.detail && <> <span className="break-words font-mono text-xs">{t.detail}</span></>}</>}
                </span>
                {!t.marker && <span className="shrink-0 rounded border border-border bg-surface2 px-1 py-px text-[10px] font-medium text-text2">{CATEGORY_LABELS[c.category]}</span>}
              </li>
            )
          })}
        </ol>
      )}
      {total > 0 && (
        <p className="mt-2 text-xs">
          <Link to={assetChangesHref(detail.asset.key, firstChangeAt)} className="inline-flex items-center gap-1 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
            View all {fmtInt(total)} <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
        </p>
      )}
    </Section>
  )
}

function Placement({ detail }: { detail: AssetDetail }) {
  const { asset } = detail
  const tags = assetTags(asset.tags)
  if (!asset.site && tags.length === 0) return null
  return (
    <Section title="Placement">
      <dl className="grid grid-cols-1 gap-y-2">
        {asset.site && <Fact label={asset.kind === 'server' ? 'Site' : 'Location'}>{asset.site}</Fact>}
        {tags.length > 0 && (
          <Fact label="Tags">
            <ul role="list" className="flex flex-wrap gap-1">
              {tags.map((t) => <li key={t} className="rounded-full border border-border bg-surface2 px-2 py-px text-[11px] text-text2">{t}</li>)}
            </ul>
          </Fact>
        )}
      </dl>
    </Section>
  )
}

/* ── The sheet ───────────────────────────────────────────────────────────── */

export function FlyoutBody({ detail }: { detail: AssetDetail }) {
  const now = serverNow()
  return (
    <div data-flyout-asset={detail.asset.key}>
      <Identity detail={detail} now={now} />
      {detail.server && <ServerSections detail={detail} />}
      {detail.device && <DeviceSections detail={detail} now={now} />}
      <RecentChanges detail={detail} now={now} />
      <Placement detail={detail} />
    </div>
  )
}

/**
 * The right sheet of the Assets page (doc 03 section 3.2): what an asset is, where each value came
 * from and how old it is, what it runs, and what changed on it. It is addressed by the URL
 * (`?asset=<kind>:<uuid>`), so a flyout can be shared and survives a reload. There is no separate asset
 * page: the existing device and server pages are the detail pages, linked from here only when the
 * user may open them.
 */
export function AssetFlyout({ address, onClose }: { address: string; onClose: () => void }) {
  const parsed = parseAssetAddress(address)
  const query = useQuery({
    queryKey: assetDetailKey(address),
    queryFn: () => inventoryApi.assetDetail(address),
    enabled: parsed !== null,
    staleTime: 30_000,
    // An unknown or out-of-scope asset will not appear on a second try.
    retry: (count, error) => !isNotFound(error) && count < 1,
  })
  const detail = query.data
  const notFound = isNotFound(query.error)

  const title = detail?.asset.name ?? (notFound ? 'Asset not found' : parsed ? (parsed.kind === 'server' ? 'Server' : 'Network device') : 'Asset')
  return (
    <Sheet
      open
      onOpenChange={(open) => { if (!open) onClose() }}
      title={<span className="flex items-center gap-2" data-flyout-title="">{detail && (detail.asset.kind === 'server' ? <Server className="h-4 w-4 shrink-0 text-muted" aria-hidden /> : <Network className="h-4 w-4 shrink-0 text-muted" aria-hidden />)}<span className="min-w-0 break-words">{title}</span></span>}
      description={detail ? undefined : notFound ? 'This asset does not exist, or it is outside the assets your role covers.' : 'Loading the asset.'}
      headerExtra={detail ? <FlyoutHeader detail={detail} now={serverNow()} /> : undefined}
      onCloseAutoFocus={(e) => {
        // Back to the row that opened it (a mouse click leaves nothing focused), else the default.
        const row = Array.from(document.querySelectorAll<HTMLElement>('[data-asset-link]')).find((el) => el.dataset.assetLink === address)
        if (row) {
          e.preventDefault()
          row.focus({ preventScroll: true })
        }
      }}
    >
      {query.isLoading ? (
        <div className="space-y-3 p-4" aria-busy="true" aria-label="Loading the asset">
          {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className={cn('h-5', i % 3 === 2 ? 'w-2/3' : 'w-full')} />)}
        </div>
      ) : notFound ? (
        <div className="space-y-2 p-4 text-sm text-text2" data-flyout="not-found">
          <p className="flex items-start gap-2 text-xs"><HardDrive className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden /> It may have been deleted, or it is not among the assets your role covers. Close this sheet to go back to the list.</p>
        </div>
      ) : query.isError || !detail ? (
        <div className="p-4"><QueryFailure error={query.error} what="this asset" onRetry={() => void query.refetch()} retrying={query.isFetching} /></div>
      ) : (
        <FlyoutBody detail={detail} />
      )}
    </Sheet>
  )
}
