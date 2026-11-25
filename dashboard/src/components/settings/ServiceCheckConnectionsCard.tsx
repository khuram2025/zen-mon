import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Info, Network, Pencil, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { useAllowedPages, useCan } from '@/stores/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import {
  CONNECTIONS_ANCHOR, EMPTY_HOST_OVERRIDE, NOTE_MAX, PROBE_TRUST_LINK, PROBE_TRUST_PAGE, formFromEntry, payloadFromForm,
  rowWarning, scopeLabel, trustSummary, validateHostOverride,
  type HostOverrideEntry, type HostOverrideErrors, type HostOverrideField, type HostOverrideForm, type HostOverridesDoc,
  type ProbeTrustSummaryInput,
} from './hostOverrides'

const KEY = ['settings', 'host-overrides']
const BASE = '/system/monitoring/host-overrides'

/** Scroll a settings card into view when the address names it (`#service-check-connections`, `#probe-trust`). */
export function useScrollToAnchor(anchor: string, ready = true) {
  const { hash } = useLocation()
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ready || hash !== `#${anchor}`) return
    const go = () => ref.current?.scrollIntoView?.({ block: 'start' })
    go()
    // Cards above this one may still be loading and push it down: follow it briefly, unless the reader scrolls.
    const timers = [250, 800, 1600].map((ms) => window.setTimeout(go, ms))
    const stop = () => timers.forEach((timer) => window.clearTimeout(timer))
    const events = ['wheel', 'touchstart', 'keydown'] as const
    events.forEach((name) => window.addEventListener(name, stop, { once: true, passive: true }))
    return () => { stop(); events.forEach((name) => window.removeEventListener(name, stop)) }
  }, [anchor, hash, ready])
  return ref
}

/**
 * Settings > General > Monitoring: how service checks reach what they monitor
 * (docs/service-check-connections.md). Manual host addresses are edited here; trusted certificates are
 * summarised with a link to the card that manages them on Security & TLS.
 *
 * The API is administrator-only (a check with a credential sends it to the address given here), so the
 * section follows the Security & TLS tab: other accounts are told who manages it and see nothing else.
 */
export function ServiceCheckConnectionsCard() {
  const admin = useCan()('system.admin')
  const ref = useScrollToAnchor(CONNECTIONS_ANCHOR)
  return (
    <Card id={CONNECTIONS_ANCHOR} ref={ref} className="scroll-mt-16">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Network className="h-5 w-5" /> Service check connections</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <p className="text-sm text-muted">
          How service checks reach what they monitor: the address they connect to when a name is not in DNS (or must be reached at a
          specific address), and the certificates they accept from HTTPS services.
        </p>
        {admin
          ? <><ManualAddresses /><TrustedCertificates /></>
          : <p role="note" className="flex gap-2 rounded-lg border border-border bg-surface2/30 p-3 text-sm text-text2">
              <Info className="mt-0.5 h-4 w-4 shrink-0 text-info" aria-hidden />
              Manual host addresses and trusted certificates are managed by an administrator.
            </p>}
      </CardContent>
    </Card>
  )
}

function ManualAddresses() {
  const queryClient = useQueryClient()
  const doc = useQuery<HostOverridesDoc>({ queryKey: KEY, queryFn: async () => (await api.get(BASE)).data })
  const [editing, setEditing] = useState<HostOverrideEntry | 'new' | null>(null)
  const [removing, setRemoving] = useState<HostOverrideEntry | null>(null)
  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`${BASE}/${id}`)).data as HostOverridesDoc,
    onSuccess: (data) => {
      queryClient.setQueryData(KEY, data)
      setRemoving(null)
      toast.success('Manual address removed', 'Checks look the name up again after their next configuration refresh.')
    },
    onError: (error) => toast.error('Could not remove the address', apiErrorMessage(error)),
  })

  const entries = doc.data?.entries ?? []
  const full = !!doc.data && entries.length >= doc.data.max_entries

  return (
    <section aria-labelledby="manual-addresses-title" className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 id="manual-addresses-title" className="text-sm font-semibold">Manual host addresses</h3>
          <p className="max-w-3xl text-sm text-muted">
            Tell service checks which address to connect to for a host name. Only the connection changes: the URL, the Host header and
            the certificate check keep the name, so a certificate issued for another name still fails. HTTP, TLS, TCP and ping checks
            use these; DNS checks never do, because they test name resolution itself.
          </p>
        </div>
        <Button size="sm" onClick={() => setEditing('new')} disabled={!doc.data || full} title={full ? `The limit of ${doc.data?.max_entries} addresses is reached` : undefined}>
          <Plus className="h-4 w-4" /> Add address
        </Button>
      </div>

      <div role="note" className="flex gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-text2">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
        <p>
          A check that signs in sends its user name, password or token to the address given here. Enter only addresses of servers that
          really are the named service. Every change is recorded in the audit log.
        </p>
      </div>

      {doc.isLoading && <p className="text-sm text-muted">Loading manual addresses…</p>}
      {doc.isError && <QueryFailure what="manual host addresses" error={doc.error} onRetry={() => doc.refetch()} />}
      {doc.data && entries.length === 0 && (
        <p className="rounded-lg border border-dashed border-border p-4 text-sm text-muted">
          No manual addresses. Every check resolves its host name through the DNS servers the appliance (or the sensor that runs it) uses.
        </p>
      )}
      {doc.data && entries.length > 0 && (
        <Table>
          <caption className="sr-only">Manual host addresses for service checks</caption>
          <THead>
            <Tr>
              <Th>Host name</Th><Th>Connects to</Th><Th>Applies</Th><Th>Note</Th><Th>Changed</Th><Th className="text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {entries.map((entry) => {
              const warning = rowWarning(entry)
              return (
                <Tr key={entry.id}>
                  <Td className="min-w-[13rem] font-mono text-xs [overflow-wrap:anywhere]">{entry.host}</Td>
                  <Td className="whitespace-nowrap font-mono text-xs">{entry.address}</Td>
                  <Td className="min-w-[12rem]">
                    <div>{scopeLabel(entry)}</div>
                    {warning && (
                      <div className="mt-1 flex gap-1.5 text-xs text-warning">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                        <span>{warning}</span>
                      </div>
                    )}
                  </Td>
                  <Td className="max-w-[18rem] break-words text-text2">{entry.note || <span className="text-muted">None</span>}</Td>
                  <Td className="whitespace-nowrap text-xs text-muted">
                    {entry.updated_at ? relativeTime(entry.updated_at) : 'Unknown'}{entry.updated_by ? ` by ${entry.updated_by}` : ''}
                  </Td>
                  <Td className="whitespace-nowrap text-right">
                    <Button variant="ghost" size="sm" aria-label={`Edit the address for ${entry.host}`} onClick={() => setEditing(entry)}><Pencil className="h-4 w-4" /></Button>
                    <Button variant="ghost" size="sm" aria-label={`Remove the address for ${entry.host}`} onClick={() => setRemoving(entry)}><Trash2 className="h-4 w-4" /></Button>
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      )}
      {doc.data && (
        <p className="text-xs text-muted">
          {entries.length} of {doc.data.max_entries} addresses. Changes reach the appliance within about a minute and each sensor at its
          next configuration refresh. A result that used a manual address says so on the check's page.
        </p>
      )}

      {editing && doc.data && (
        <AddressDialog
          entry={editing === 'new' ? null : editing}
          doc={doc.data}
          onClose={() => setEditing(null)}
          onSaved={(data) => { queryClient.setQueryData(KEY, data); setEditing(null) }}
        />
      )}
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(open) => { if (!open) setRemoving(null) }}
        title="Remove this manual address?"
        description={removing ? <>Checks of <span className="font-mono">{removing.host}</span> will look the name up in DNS again. If the name does not resolve, those checks go Down.</> : undefined}
        confirmText="Remove address"
        destructive
        loading={remove.isPending}
        onConfirm={() => { if (removing) remove.mutate(removing.id) }}
      />
    </section>
  )
}

function AddressDialog({ entry, doc, onClose, onSaved }: {
  entry: HostOverrideEntry | null
  doc: HostOverridesDoc
  onClose: () => void
  onSaved: (doc: HostOverridesDoc) => void
}) {
  const [form, setForm] = useState<HostOverrideForm>(() => (entry ? formFromEntry(entry) : EMPTY_HOST_OVERRIDE))
  const [touched, setTouched] = useState<Partial<Record<HostOverrideField, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)
  const [serverErrors, setServerErrors] = useState<HostOverrideErrors>({})
  const [failure, setFailure] = useState('')
  const set = (patch: Partial<HostOverrideForm>) => {
    setForm((current) => ({ ...current, ...patch }))
    setServerErrors({})
    setFailure('')
  }
  const errors = validateHostOverride(form, doc.entries, entry?.id)
  const shown = (field: HostOverrideField) => serverErrors[field] || ((submitted || touched[field]) ? errors[field] : undefined)
  const blur = (field: HostOverrideField) => () => setTouched((current) => ({ ...current, [field]: true }))
  const sensor = doc.sensors.find((item) => item.id === form.sensor_id)

  const save = useMutation({
    mutationFn: async () => (entry
      ? await api.put(`${BASE}/${entry.id}`, payloadFromForm(form))
      : await api.post(BASE, payloadFromForm(form))).data as HostOverridesDoc,
    onSuccess: (data) => {
      toast.success(entry ? 'Manual address changed' : 'Manual address added', 'Checks use it after their next configuration refresh.')
      onSaved(data)
    },
    onError: (error: any) => {
      // The API names the field a refusal belongs to.
      const detail = error?.response?.data?.detail
      if (detail && typeof detail === 'object' && typeof detail.field === 'string' && detail.field in EMPTY_HOST_OVERRIDE) {
        setServerErrors({ [detail.field]: String(detail.message || 'Not accepted') } as HostOverrideErrors)
      } else {
        setFailure(apiErrorMessage(error))
      }
    },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSubmitted(true)
    if (Object.keys(errors).length === 0) save.mutate()
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{entry ? 'Change manual address' : 'Add a manual address'}</DialogTitle>
        </DialogHeader>
        <DialogDescription>
          Checks of this host name connect to the address below instead of looking the name up. Nothing else about the check changes.
        </DialogDescription>
        <form onSubmit={submit} className="space-y-4" noValidate>
          <FormField label="Host name" required error={shown('host')} hint="Exactly as it appears in the check, for example portal.example.local. No wildcards.">
            <Input value={form.host} maxLength={300} autoFocus={!entry} spellCheck={false} autoCapitalize="none" autoComplete="off"
              aria-invalid={!!shown('host') || undefined} onChange={(e) => set({ host: e.target.value })} onBlur={blur('host')} placeholder="portal.example.local" />
          </FormField>
          <FormField label="Address to connect to" required error={shown('address')} hint="One IPv4 or IPv6 address.">
            <Input value={form.address} maxLength={64} spellCheck={false} autoComplete="off" inputMode="text"
              aria-invalid={!!shown('address') || undefined} onChange={(e) => set({ address: e.target.value })} onBlur={blur('address')} placeholder="10.20.30.40" />
          </FormField>
          <FormField label="Applies" hint="An address for one place takes priority there over an Everywhere address for the same name.">
            <Select value={form.scope} onValueChange={(value) => set({ scope: value as HostOverrideForm['scope'], sensor_id: value === 'sensor' ? form.sensor_id : '' })}>
              <SelectTrigger aria-label="Where the address applies"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Everywhere (this appliance and every sensor)</SelectItem>
                <SelectItem value="central">This appliance only</SelectItem>
                <SelectItem value="sensor" disabled={doc.sensors.length === 0}>One sensor{doc.sensors.length === 0 ? ' (no sensors yet)' : ''}</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {form.scope === 'sensor' && (
            <FormField label="Sensor" required error={shown('sensor_id')}
              hint={sensor && !sensor.applies ? undefined : 'Checks that run on this sensor use the address.'}>
              <Select value={form.sensor_id} onValueChange={(value) => { set({ sensor_id: value }); setTouched((current) => ({ ...current, sensor_id: true })) }}>
                <SelectTrigger aria-label="Sensor the address applies to" aria-invalid={!!shown('sensor_id') || undefined}><SelectValue placeholder="Choose a sensor" /></SelectTrigger>
                <SelectContent>
                  {doc.sensors.map((item) => (
                    <SelectItem key={item.id} value={item.id}>{item.name}{item.applies ? '' : ' (needs an update)'}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
          )}
          {form.scope === 'sensor' && sensor && !sensor.applies && !shown('sensor_id') && (
            <p role="note" className="flex gap-2 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              {sensor.name} runs a version that cannot use manual addresses{sensor.version ? ` (${sensor.version})` : ''}. The address is stored and takes effect once the sensor is updated.
            </p>
          )}
          <FormField label="Note" error={shown('note')} hint={`Optional, for the next person: why this name needs an address. Up to ${NOTE_MAX} characters.`}>
            <Input value={form.note} maxLength={NOTE_MAX * 2} aria-invalid={!!shown('note') || undefined} onChange={(e) => set({ note: e.target.value })} onBlur={blur('note')} placeholder="Internal name, not in the appliance's DNS" />
          </FormField>
          {failure && <p role="alert" className="text-sm text-danger">{failure}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>{save.isPending ? 'Saving…' : entry ? 'Save address' : 'Add address'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function TrustedCertificates() {
  const pages = useAllowedPages()
  const trust = useQuery<ProbeTrustSummaryInput>({
    queryKey: ['security', 'probe-trust'],
    queryFn: async () => (await api.get('/system/security/probe-trust')).data,
  })
  const summary = trust.data ? trustSummary(trust.data) : null
  // A role limited to selected pages may not have the Security & TLS tab.
  const reachable = !pages || pages.has(PROBE_TRUST_PAGE)
  return (
    <section aria-labelledby="trusted-certificates-title" className="space-y-2 border-t border-border pt-5">
      <h3 id="trusted-certificates-title" className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="h-4 w-4" /> Trusted certificates</h3>
      <p className="max-w-3xl text-sm text-muted">
        HTTP and TLS checks accept a certificate only when it chains to a certificate authority the appliance trusts. For services that
        use your own certificate authority or a self-signed certificate, add that certificate to the trusted list.
      </p>
      {trust.isLoading && <p className="text-sm text-muted">Loading trusted certificates…</p>}
      {trust.isError && <QueryFailure what="trusted certificates" error={trust.error} onRetry={() => trust.refetch()} />}
      {summary && (
        <p className="text-sm">
          {summary.text}
          {summary.expired > 0 && <span className="text-danger"> {summary.expired} {summary.expired === 1 ? 'has' : 'have'} expired.</span>}
        </p>
      )}
      {reachable
        ? <Button asChild variant="outline" size="sm"><Link to={PROBE_TRUST_LINK}>Manage trusted certificates on Security &amp; TLS</Link></Button>
        : <p className="text-xs text-muted">Trusted certificates are managed on the Security &amp; TLS tab, which your role does not include.</p>}
    </section>
  )
}
