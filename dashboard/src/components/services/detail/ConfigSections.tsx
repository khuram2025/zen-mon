/**
 * Type-specific configuration (UI 10), ownership (feature) and
 * configuration history (audit feature).
 *
 * Only fields that mean something for the monitor type are shown. "Not
 * applicable" and "Not collected" are distinguished: e.g. certificate
 * details are not applicable to plain HTTP, and are "not reported yet" for
 * an HTTPS monitor whose probes have not returned certificate data.
 */
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, Download, History, LockKeyhole, Pencil } from 'lucide-react'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { withCapability } from './capabilities'
import { NOT_APPLICABLE, NOT_SET, exactTime } from './format'
import type { ConfigHistoryEntry, ServiceCheckExt } from './types'

type InfoRow = { label: string; value: ReactNode; mono?: boolean; title?: string; muted?: boolean }

function Card({ title, subtitle, actions, children }: { title: string; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-text">{title}</h3>
          {subtitle && <div className="mt-0.5 text-[11px] text-muted">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      <div className="flex-1 p-4">{children}</div>
    </div>
  )
}

export function InfoGrid({ rows, labelWidth = 112 }: { rows: InfoRow[]; labelWidth?: number }) {
  return (
    <dl className="space-y-1.5">
      {rows.map((r) => (
        <div key={r.label} className="grid items-start gap-2 text-[11px]" style={{ gridTemplateColumns: `${labelWidth}px 1fr` }}>
          <dt className="text-muted">{r.label}</dt>
          <dd
            className={cn('min-w-0 truncate font-medium', r.mono && 'font-mono', r.muted && 'font-normal text-muted')}
            title={r.title ?? (typeof r.value === 'string' ? r.value : undefined)}
          >
            {r.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

const TYPE_LABEL: Record<string, string> = { http: 'HTTP(S)', tcp: 'TCP port', tls: 'TLS certificate', icmp: 'ICMP ping', dns: 'DNS' }

const LEVEL_TEXT: Record<number, string> = {
  1: 'L1 · reachability',
  2: 'L2 · single request / protocol check',
  3: 'L3 · multi-step journey',
}

function cfg(check: ServiceCheckExt): Record<string, any> {
  return (check.config as Record<string, any>) || {}
}

function ipVersionText(v: unknown): string {
  return v === 'ipv4' ? 'IPv4 only' : v === 'ipv6' ? 'IPv6 only' : 'Auto'
}

function isHttps(check: ServiceCheckExt) {
  return (check.target_url || '').trim().toLowerCase().startsWith('https://')
}

/** Rows describing what this monitor probes, by type. */
export function endpointRows(check: ServiceCheckExt, compact = false): InfoRow[] {
  const c = cfg(check)
  const host = check.target_host || NOT_SET
  switch (check.check_type) {
    case 'http': {
      const steps = check.workflow_steps || []
      const rows: InfoRow[] = [
        { label: 'URL', value: check.target_url || NOT_SET, mono: true },
        { label: 'Method', value: check.http_method || 'GET' },
        { label: 'Expected status', value: check.http_expected_statuses || String(check.http_expected_status || 200) },
        { label: 'Content match', value: check.http_content_match || 'None required', mono: !!check.http_content_match, muted: !check.http_content_match },
      ]
      if (!compact) {
        rows.push(
          { label: 'Follow redirects', value: check.http_follow_redirects ? 'Yes' : 'No' },
          { label: 'IP version', value: ipVersionText(c.ip_version) },
        )
      }
      rows.push({ label: 'Journey', value: steps.length > 0 ? `${steps.length} steps · ${(check.workflow_operator || 'all').toUpperCase()} must pass` : 'Single request' })
      return rows
    }
    case 'tcp':
      return [
        { label: 'Host', value: host, mono: true },
        { label: 'Port', value: check.target_port != null ? String(check.target_port) : NOT_SET },
        { label: 'Success means', value: 'TCP connection accepted' },
      ]
    case 'tls':
      return [
        { label: 'Host', value: host, mono: true },
        { label: 'Port', value: check.target_port != null ? String(check.target_port) : '443 (default)' },
        { label: 'Warn / critical', value: `${check.tls_warn_days}d / ${check.tls_critical_days}d before expiry` },
      ]
    case 'icmp':
      return [
        { label: 'Host', value: host, mono: true },
        { label: 'Packets per probe', value: String(Number(c.count) || 3) },
        { label: 'IP version', value: ipVersionText(c.ip_version) },
      ]
    case 'dns':
      return [
        { label: 'Query name', value: host, mono: true },
        { label: 'Record type', value: String(c.record_type || 'A') },
        { label: 'Expected answer', value: c.expected ? String(c.expected) : 'Any answer', mono: !!c.expected, muted: !c.expected },
      ]
    default:
      return [{ label: 'Target', value: check.target_url || host, mono: true }]
  }
}

/** Certificate rows only where a certificate exists to report. */
function certificateSection(check: ServiceCheckExt): { rows: InfoRow[]; note?: string } | null {
  if (check.check_type === 'tls' || (check.check_type === 'http' && isHttps(check))) {
    const collected = !!check.tls_expiry_date || check.tls_days_remaining != null || !!check.tls_issuer
    if (!collected) {
      return {
        rows: [],
        note: check.check_type === 'tls'
          ? 'No certificate has been reported yet. Details appear after a successful TLS probe.'
          : 'This HTTPS monitor has not reported certificate details. Expiry tracking is guaranteed only for TLS certificate monitors; add one to alert on expiry.',
      }
    }
    return {
      rows: [
        { label: 'Expires', value: check.tls_expiry_date ? exactTime(check.tls_expiry_date, false) : 'Not reported' },
        { label: 'Days remaining', value: check.tls_days_remaining != null ? String(check.tls_days_remaining) : 'Not reported' },
        { label: 'Issuer', value: check.tls_issuer || 'Not reported' },
        { label: 'Subject', value: check.tls_subject || 'Not reported' },
      ],
    }
  }
  return null
}

export function ConfigSummaryCard({ check, onEdit, onOpenConfig }: { check: ServiceCheckExt; onEdit: () => void; onOpenConfig: () => void }) {
  const rows: InfoRow[] = [
    { label: 'Type', value: TYPE_LABEL[check.check_type] || check.check_type },
    ...endpointRows(check, true),
    ...(check.check_type === 'http' ? [{ label: 'Auth', value: check.credential_name ? `${check.credential_name} (${check.credential_auth_type})` : 'None' }] : []),
    { label: 'Interval', value: `${check.check_interval}s · timeout ${check.timeout}s` },
    { label: 'Owner', value: check.owner_team || NOT_SET, muted: !check.owner_team },
    { label: 'Group', value: check.group_name || 'Unassigned', muted: !check.group_name },
  ]
  return (
    <Card
      title="Configuration"
      actions={
        <>
          <button type="button" onClick={onOpenConfig} className="text-xs text-primary hover:underline">Full</button>
          <button type="button" onClick={onEdit} className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
            <Pencil className="h-3 w-3" /> Edit
          </button>
        </>
      }
    >
      <InfoGrid rows={rows} labelWidth={80} />
      {check.tags.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1 border-t border-border/60 pt-2.5">
          {check.tags.map((t) => (
            <span key={t} className="rounded-full border border-border bg-surface2/50 px-2 py-0.5 text-[10px] text-muted">{t}</span>
          ))}
        </div>
      )}
    </Card>
  )
}

export function RunbookLink({ url, className }: { url: string; className?: string }) {
  let safe: string | null = null
  try {
    const u = new URL(url)
    if (u.protocol === 'http:' || u.protocol === 'https:') safe = u.toString()
  } catch { /* not a URL */ }
  if (!safe) return <span className={className}>{url}</span>
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" className={cn('inline-flex items-center gap-1 text-primary hover:underline', className)}>
      <BookOpen className="h-3 w-3" /> Runbook
    </a>
  )
}

export function ConfigTab({ check, onEdit, onExport }: { check: ServiceCheckExt; onEdit: () => void; onExport: () => void }) {
  const steps = check.workflow_steps || []
  const cert = certificateSection(check)
  const editAction = (
    <button type="button" onClick={onEdit} className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
      <Pencil className="h-3 w-3" /> Edit
    </button>
  )
  const target = check.sla_target_pct != null && check.sla_target_pct !== '' ? `${Number(check.sla_target_pct)}%` : 'Appliance default'
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
        <Card title="Endpoint" subtitle={TYPE_LABEL[check.check_type] || check.check_type} actions={editAction}>
          <InfoGrid rows={endpointRows(check)} />
        </Card>

        <Card title="Check schedule">
          <InfoGrid rows={[
            { label: 'Normal interval', value: `${check.check_interval}s` },
            { label: 'Request timeout', value: `${check.timeout}s` },
            { label: 'Enabled', value: check.enabled ? 'Yes' : 'Paused' },
            { label: 'Level', value: LEVEL_TEXT[check.level ?? 1] || `L${check.level}` },
          ]} />
        </Card>

        <Card title="Failure confirmation">
          <InfoGrid rows={[
            { label: 'Attempts to confirm', value: `${check.retry_count ?? 1} total attempts` },
            { label: 'Retry gap', value: `${check.retry_delay_s ?? 10}s after each failure` },
            { label: 'On successful retry', value: 'Reset confirmation; remain Up' },
            { label: 'SLA downtime', value: 'Starts when Down is confirmed' },
          ]} />
          <p className="mt-3 text-xs text-muted">Includes the initial attempt. Every failed probe remains in diagnostics. Controller and sensor checks use the same confirmation policy.</p>
        </Card>

        <Card title="Recovery">
          <InfoGrid rows={[
            { label: 'Check while Down', value: `Every ${check.down_check_interval ?? 30}s` },
            { label: 'Mark recovered', value: 'First successful check' },
            { label: 'After recovery', value: `Resume ${check.check_interval}s interval` },
            { label: 'Overlapping checks', value: 'Prevented' },
          ]} />
        </Card>

        <Card title="Ownership & objective" actions={editAction}>
          <InfoGrid rows={[
            { label: 'Owner / team', value: check.owner_team || NOT_SET, muted: !check.owner_team },
            { label: 'Runbook', value: check.runbook_url ? <RunbookLink url={check.runbook_url} /> : NOT_SET, muted: !check.runbook_url, title: check.runbook_url || undefined },
            { label: 'Business owner', value: check.business_owner || NOT_SET, muted: !check.business_owner },
            { label: 'Criticality', value: check.criticality ? check.criticality[0].toUpperCase() + check.criticality.slice(1) : 'Not classified', muted: !check.criticality },
            { label: 'SLA target', value: target },
            { label: 'Group', value: check.group_name || 'Unassigned', muted: !check.group_name },
            { label: 'Depends on', value: check.parent_check_name || 'No dependency', muted: !check.parent_check_name },
          ]} />
        </Card>

        {check.check_type === 'http' && (
          <Card title="Authentication & transport">
            <InfoGrid rows={[
              { label: 'Credential', value: check.credential_name || 'None', muted: !check.credential_name },
              { label: 'Auth type', value: check.credential_auth_type || NOT_APPLICABLE, muted: !check.credential_auth_type },
              ...(isHttps(check)
                ? [{ label: 'Ignore TLS errors', value: check.http_ignore_tls_errors ? 'Yes — certificate not verified' : 'No' }]
                : [
                  { label: 'Transport', value: 'Plain HTTP (no certificate)' },
                  { label: 'Allow insecure auth', value: check.http_allow_insecure_auth ? 'Yes' : 'No' },
                ]),
            ]} />
          </Card>
        )}

        {cert && (
          <Card title="Certificate" subtitle={check.check_type === 'tls' ? 'Collected by this monitor' : 'Reported by HTTPS probes'}>
            {cert.rows.length > 0 && <InfoGrid rows={cert.rows} />}
            {cert.note && <p className="text-xs text-muted">{cert.note}</p>}
          </Card>
        )}
      </div>

      {check.check_type === 'http' && steps.length > 0 && (
        <Card
          title="Authenticated journey"
          subtitle={`Cookie-preserving · ${(check.workflow_operator || 'all').toUpperCase()} rule · ${steps.length} step${steps.length === 1 ? '' : 's'}`}
          actions={check.credential_name ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-1 text-[10px] font-semibold text-success">
              <LockKeyhole className="h-3 w-3" /> {check.credential_name} · {check.credential_auth_type}
            </span>
          ) : undefined}
        >
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {steps.map((step, index) => (
              <div key={`${step.name}-${index}`} className="rounded-lg border border-border bg-surface2/30 p-3">
                <div className="flex items-center gap-2">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[10px] font-bold text-primary">{index + 1}</span>
                  <span className="truncate text-xs font-semibold">{step.name}</span>
                </div>
                <div className="mt-2 truncate font-mono text-[10px] text-muted" title={step.url}>{step.method} {step.url}</div>
                <div className="mt-1 text-[10px] text-muted">
                  Expect {step.expected_statuses || '200'}{step.content_match ? ' + content validation' : ''}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <ConfigHistory checkId={check.id} />

      <Card
        title="Metadata"
        actions={<Button variant="outline" size="sm" className="h-7" onClick={onExport}><Download className="h-3 w-3" /> Export JSON</Button>}
      >
        <InfoGrid rows={[
          { label: 'Description', value: check.description || NOT_SET, muted: !check.description },
          { label: 'Tags', value: check.tags.length ? check.tags.join(', ') : 'None', muted: !check.tags.length },
          { label: 'Check id', value: check.id, mono: true },
          { label: 'Created', value: exactTime(check.created_at) },
          { label: 'Updated', value: check.updated_at ? exactTime(check.updated_at) : 'Never', muted: !check.updated_at },
        ]} />
      </Card>
    </div>
  )
}

function renderValue(v: unknown): string {
  if (v == null || v === '') return '∅'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

const ACTION_TEXT: Record<string, string> = {
  create: 'Created', update: 'Changed', delete: 'Deleted', test: 'Manual test', pause: 'Paused', resume: 'Resumed',
  import: 'Imported', bulk: 'Bulk action',
}

export function ConfigHistory({ checkId }: { checkId: string }) {
  const [limit, setLimit] = useState(10)
  const q = useQuery<{ data: ConfigHistoryEntry[]; meta?: { total?: number } } | null>({
    queryKey: ['service-check-history', checkId, limit],
    queryFn: () => withCapability('history', async () => (await api.get(`/service-checks/${checkId}/history`, { params: { skip: 0, limit } })).data),
    retry: false,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  })
  const rows = q.data?.data || []
  const total = q.data?.meta?.total ?? rows.length
  return (
    <Card title="Configuration history" subtitle="Who changed this monitor, and what changed" actions={<History className="h-4 w-4 text-muted" aria-hidden />}>
      {q.isLoading ? (
        <p className="text-xs text-muted">Loading history…</p>
      ) : q.isError ? (
        <p className="text-xs text-danger">Could not load configuration history. <button type="button" className="underline" onClick={() => q.refetch()}>Retry</button></p>
      ) : q.data === null ? (
        <p className="text-xs text-muted">Configuration history is not available on this appliance version.</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted">No recorded changes. Changes made after audit history was enabled appear here.</p>
      ) : (
        <ol className="space-y-3">
          {rows.map((h) => {
            const changes = h.changes ? Object.entries(h.changes) : []
            return (
              <li key={h.id} className="rounded-md border border-border bg-surface2/30 px-3 py-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2 text-[11px]">
                  <span className="font-semibold">{ACTION_TEXT[h.action] || h.action}{h.actor ? ` by ${h.actor}` : ''}</span>
                  <span className="font-mono tabular-nums text-muted">{exactTime(h.at)} <span className="font-sans">({relativeTime(h.at)})</span></span>
                </div>
                {changes.length > 0 && (
                  <dl className="mt-1.5 space-y-1">
                    {changes.map(([field, diff]) => (
                      <div key={field} className="grid grid-cols-[140px_1fr] gap-2 text-[11px]">
                        <dt className="truncate font-mono text-muted" title={field}>{field}</dt>
                        <dd className="min-w-0 break-words">
                          <span className="font-mono text-danger line-through decoration-danger/50">{renderValue(diff?.old)}</span>
                          <span className="mx-1 text-muted">→</span>
                          <span className="font-mono text-success">{renderValue(diff?.new)}</span>
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </li>
            )
          })}
        </ol>
      )}
      {rows.length > 0 && total > rows.length && (
        <Button variant="outline" size="sm" className="mt-3 h-7" disabled={q.isFetching} onClick={() => setLimit((n) => n + 20)}>
          {q.isFetching ? 'Loading…' : `Show more (${total - rows.length} older)`}
        </Button>
      )}
    </Card>
  )
}
