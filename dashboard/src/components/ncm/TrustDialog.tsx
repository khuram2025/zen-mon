import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, KeyRound, Loader2, Radar, ShieldAlert, ShieldCheck, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { errorDetail, ncmError } from './model'
import { Ago, EmptyState, SectionError } from './ui'

type TrustEntry = {
  id: string; key_type: string; fingerprint_sha256: string; verification_method: string; verification_note: string
  registered_by_name: string | null; created_at: string; retired_at: string | null; retired_by_name: string | null; retire_reason: string | null
}
type TrustView = {
  host: string; port: number
  state: { state: 'pass' | 'blocked'; source: 'managed' | 'appliance_file' | null; message: string; fingerprint?: string }
  entries: TrustEntry[]
}
type Probe = {
  host: string; port: number; key_type: string; key_base64: string; fingerprint_sha256: string; fingerprint_md5: string; bits: number; notice: string
}

const METHODS: [string, string][] = [
  ['console', 'Device console'],
  ['out_of_band', 'Out-of-band session'],
  ['inventory_record', 'Inventory record'],
  ['vendor_portal', 'Vendor portal'],
]
const METHOD_LABEL = Object.fromEntries(METHODS)
const SOURCE_LABEL: Record<string, string> = { managed: 'Managed trust', appliance_file: 'Appliance trust file' }

/** "MD5 aa:bb…" / "md5:aa…" → "MD5:aa:bb…" so the server strips the prefix instead of reading its hex-looking letters. */
function canonicalFp(value: string): string {
  return value.trim().replace(/^md5[\s:]+/i, 'MD5:')
}

/** Mirror of the server's fingerprint normalisation (the server stays the authority). */
function normalizeFp(value: string): string {
  const v = canonicalFp(value)
  if (/^sha256:/i.test(v)) return 'SHA256:' + v.split(':').slice(1).join(':').trim().replace(/=+$/, '')
  return v.replace(/^md5:/i, '').toLowerCase().replace(/[^0-9a-f]/g, '')
}

/** Parse "<type> <base64>" (tolerating a known_hosts host prefix). */
function parsePublicKey(text: string): { type: string; b64: string } | null {
  let parts = text.trim().split(/\s+/)
  if (parts.length >= 3 && !/^(ssh-|ecdsa-)/.test(parts[0])) parts = parts.slice(1)
  if (parts.length < 2 || !/^(ssh-|ecdsa-)/.test(parts[0])) return null
  return { type: parts[0], b64: parts[1] }
}

/** SHA256 fingerprint of a pasted key, when the browser exposes WebCrypto (secure contexts only). */
async function sha256Fingerprint(b64: string): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle) return null
    const bin = atob(b64)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
    let s = ''
    digest.forEach((b) => { s += String.fromCharCode(b) })
    return 'SHA256:' + btoa(s).replace(/=+$/, '')
  } catch {
    return null
  }
}

function Step({ n, title, children, done }: { n: number; title: string; children: ReactNode; done?: boolean }) {
  return (
    <li className="relative flex min-w-0 gap-3">
      <span aria-hidden className={cn('flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold',
        done ? 'border-success bg-success/10 text-success' : 'border-border bg-surface2 text-muted')}>
        {done ? <CheckCircle2 className="h-3.5 w-3.5" /> : n}
      </span>
      <div className="min-w-0 flex-1 space-y-2 pb-1">
        <h3 className="text-sm font-semibold">
          <span className="sr-only">Step {n}{done ? ' (done)' : ''}: </span>{title}
        </h3>
        {children}
      </div>
    </li>
  )
}

function Mono({ children }: { children: ReactNode }) {
  return <code className="break-all font-mono text-xs">{children}</code>
}

export function TrustDialog({ deviceId, open, onOpenChange }: { deviceId: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const can = useCan()
  const canTrust = can('ncm.trust')
  const qc = useQueryClient()

  const trust = useQuery<TrustView>({
    queryKey: ['ncm', 'trust', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-trust`)).data,
    enabled: open && !!deviceId,
  })

  const [mode, setMode] = useState<'probe' | 'paste'>('probe')
  const [probe, setProbe] = useState<Probe | null>(null)
  const [pasted, setPasted] = useState('')
  const [pastedSha, setPastedSha] = useState<string | null>(null)
  const [typedFp, setTypedFp] = useState('')
  const [method, setMethod] = useState('console')
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('')
  const [serverError, setServerError] = useState<string | null>(null)
  const [forceReplace, setForceReplace] = useState(false)
  const [retiring, setRetiring] = useState<TrustEntry | null>(null)

  const reset = () => {
    setMode('probe'); setProbe(null); setPasted(''); setPastedSha(null); setTypedFp(''); setMethod('console')
    setNote(''); setReason(''); setServerError(null); setForceReplace(false)
  }
  useEffect(() => { if (!open) { reset(); setRetiring(null) } }, [open, deviceId])

  const parsedPaste = useMemo(() => parsePublicKey(pasted), [pasted])
  useEffect(() => {
    let alive = true
    setPastedSha(null)
    if (parsedPaste) sha256Fingerprint(parsedPaste.b64).then((fp) => { if (alive) setPastedSha(fp) })
    return () => { alive = false }
  }, [parsedPaste])

  const probeMut = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ncm-trust/probe`)).data as Probe,
    onSuccess: (d) => { setProbe(d); setServerError(null) },
  })

  const entries = trust.data?.entries || []
  const activeEntries = entries.filter((e) => !e.retired_at)
  const candidateType = mode === 'probe' ? probe?.key_type : parsedPaste?.type
  const active = activeEntries.find((e) => e.key_type === candidateType) || null

  // Candidate key under consideration.
  const publicKey = mode === 'probe' ? (probe ? `${probe.key_type} ${probe.key_base64}` : '') : pasted.trim()
  const candidateSha = mode === 'probe' ? probe?.fingerprint_sha256 ?? null : pastedSha
  const candidateMd5 = mode === 'probe' ? probe?.fingerprint_md5 ?? null : null
  const replacing = !!active && (forceReplace || (!!candidateSha && candidateSha !== active.fingerprint_sha256))
  const unchanged = !!active && !!candidateSha && candidateSha === active.fingerprint_sha256

  const typedNorm = typedFp.trim() ? normalizeFp(typedFp) : ''
  let match: 'none' | 'match' | 'mismatch' | 'unknown' = 'none'
  if (typedNorm) {
    const refs = [candidateSha, candidateMd5 ? normalizeFp(candidateMd5) : null].filter(Boolean) as string[]
    if (!refs.length) match = 'unknown'
    else if (refs.includes(typedNorm)) match = 'match'
    else if (typedNorm.startsWith('SHA256:') ? !!candidateSha : !!candidateMd5) match = 'mismatch'
    else match = 'unknown'
  }

  const register = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ncm-trust`, {
      public_key: publicKey,
      expected_fingerprint: canonicalFp(typedFp),
      verification_method: method,
      verification_note: note.trim(),
      replace_reason: replacing ? reason.trim() : undefined,
    })).data,
    onSuccess: (d: any) => {
      toast.success(d?.unchanged ? 'Key already trusted' : 'SSH host key registered', d?.fingerprint_sha256)
      qc.invalidateQueries({ queryKey: ['ncm'] })
      reset()
    },
    onError: (e: any) => {
      const det = errorDetail(e)
      if (e?.response?.status === 409 && det?.code === 'trust_exists') setForceReplace(true)
      setServerError(ncmError(e))
    },
  })

  const keyReady = !!publicKey && (mode === 'probe' || !!parsedPaste)
  const canRegister = canTrust && keyReady && typedFp.trim().length >= 10 && note.trim().length >= 5
    && (!replacing || reason.trim().length >= 5) && match !== 'mismatch' && !register.isPending

  const state = trust.data?.state

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><KeyRound className="h-5 w-5" aria-hidden /> SSH host key trust</DialogTitle>
          <DialogDescription>
            The collector only connects to devices whose SSH host key has been confirmed by an operator. Trust is never added automatically, and every change is audited.
          </DialogDescription>
        </DialogHeader>

        {trust.isLoading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading trust state…</div>
        ) : trust.isError ? (
          <SectionError what="SSH trust state" onRetry={() => trust.refetch()} />
        ) : trust.data && state ? (
          <div className="min-w-0 space-y-5">
            {/* Current state */}
            <section aria-label="Current trust state" className={cn('rounded-md border p-3 text-sm',
              state.state === 'pass' ? 'border-success/40 bg-success/5' : 'border-danger/40 bg-danger/5')}>
              <div className="flex flex-wrap items-center gap-2">
                {state.state === 'pass'
                  ? <Badge variant="success"><ShieldCheck className="h-3 w-3" aria-hidden /> Trusted</Badge>
                  : <Badge variant="danger"><ShieldAlert className="h-3 w-3" aria-hidden /> Not trusted</Badge>}
                {state.source && <Badge variant="outline">{SOURCE_LABEL[state.source] || state.source}</Badge>}
                <span className="font-mono text-xs text-muted">{trust.data.host}:{trust.data.port}</span>
              </div>
              {state.message && <p className="mt-2 break-words text-xs text-muted">{state.message}</p>}
              {activeEntries.map((entry) => (
                <div key={entry.id} className="mt-2 text-xs">
                  <span className="text-muted">Active managed key: </span>
                  <span className="font-mono">{entry.key_type}</span> <Mono>{entry.fingerprint_sha256}</Mono>
                </div>
              ))}
            </section>

            {!canTrust ? (
              <p className="rounded-md border border-border bg-surface2 px-3 py-2 text-xs text-muted">
                You need the SSH trust permission to register keys.
              </p>
            ) : (
              <ol className="space-y-5" aria-label="Register a host key">
                <Step n={1} title="Read the key the device presents" done={keyReady}>
                  <div role="group" aria-label="Key source" className="inline-flex rounded-md border border-border p-0.5">
                    {([['probe', 'Read from device'], ['paste', 'Paste the public key instead']] as const).map(([v, l]) => (
                      <button key={v} type="button" aria-pressed={mode === v}
                        onClick={() => { setMode(v); setServerError(null) }}
                        className={cn('rounded px-2 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                          mode === v ? 'bg-primary text-white' : 'text-muted hover:bg-surface2 hover:text-text')}>
                        {l}
                      </button>
                    ))}
                  </div>
                  {mode === 'probe' ? (
                    <div className="space-y-2">
                      <p className="text-xs text-muted">
                        Connects to {trust.data.host}:{trust.data.port} without logging in and shows the key it offers. Nothing is saved.
                      </p>
                      <Button size="sm" variant="outline" onClick={() => probeMut.mutate()} disabled={probeMut.isPending}>
                        {probeMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Radar className="h-3.5 w-3.5" />}
                        {probe ? 'Read again' : 'Read presented key'}
                      </Button>
                      {probeMut.isError && (
                        <p role="alert" className="text-xs text-danger">Could not read the key: {ncmError(probeMut.error)}</p>
                      )}
                      {probe && (
                        <div className="space-y-2">
                          <div role="note" className="flex gap-2 rounded-md border border-warning/50 bg-warning/10 p-3 text-xs text-warning">
                            <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden />
                            <span><strong>Unverified.</strong> {probe.notice} It proves nothing by itself: an interceptor would present its own key here.</span>
                          </div>
                          <dl className="grid gap-2 rounded-md border border-border p-3 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
                            <dt className="text-muted">Key type</dt><dd className="font-mono">{probe.key_type}{probe.bits ? ` (${probe.bits} bits)` : ''}</dd>
                            <dt className="text-muted">SHA256</dt><dd><Mono>{probe.fingerprint_sha256}</Mono></dd>
                            <dt className="text-muted">MD5</dt><dd><Mono>MD5:{probe.fingerprint_md5}</Mono></dd>
                          </dl>
                        </div>
                      )}
                    </div>
                  ) : (
                    <FormField label="OpenSSH public key" hint='One line, for example "ssh-ed25519 AAAAC3Nz…" (a known_hosts line is accepted). Use this when the device is not reachable from the appliance at the moment.'
                      error={pasted.trim() && !parsedPaste ? 'Expected "<key type> <base64>", for example "ssh-rsa AAAAB3…"' : null}>
                      <Textarea value={pasted} onChange={(e) => { setPasted(e.target.value); setServerError(null) }} rows={3}
                        className="font-mono text-xs" placeholder="ssh-ed25519 AAAA…" spellCheck={false} />
                    </FormField>
                  )}
                  {mode === 'paste' && parsedPaste && (
                    <p className="text-xs text-muted">
                      {pastedSha ? <>Computed fingerprint: <Mono>{pastedSha}</Mono></> : 'The fingerprint will be computed and checked by the appliance.'}
                    </p>
                  )}
                </Step>

                <Step n={2} title="Confirm against an independent source" done={match === 'match' && note.trim().length >= 5}>
                  <p className="text-xs text-muted">
                    Read the host key fingerprint from a source an attacker on the network cannot alter: a console or out-of-band session,
                    your inventory record, or the vendor portal. For example: Cisco IOS <code className="font-mono">show ip ssh</code> or{' '}
                    <code className="font-mono">show crypto key mypubkey rsa</code>; FortiOS <code className="font-mono">get system info admin ssh</code> (command names vary by release);
                    on Linux-based systems <code className="font-mono">ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>.
                  </p>
                  <FormField label="Fingerprint you read on the independent source" required>
                    <Input value={typedFp} onChange={(e) => { setTypedFp(e.target.value); setServerError(null) }}
                      placeholder="SHA256:… or MD5 aa:bb:…" className="font-mono text-xs" spellCheck={false} autoComplete="off" />
                  </FormField>
                  {match !== 'none' && (
                    <p aria-live="polite" className={cn('flex items-center gap-1.5 text-xs',
                      match === 'match' ? 'text-success' : match === 'mismatch' ? 'text-danger' : 'text-muted')}>
                      {match === 'match' && <><CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> Matches the key above. The appliance re-checks this when you register.</>}
                      {match === 'mismatch' && <><XCircle className="h-3.5 w-3.5" aria-hidden /> Does not match the key above. Do not register it; re-check the independent source.</>}
                      {match === 'unknown' && 'Cannot compare in the browser; the appliance checks it when you register.'}
                    </p>
                  )}
                  <div className="grid gap-3 sm:grid-cols-2">
                    <FormField label="Verification method" className="min-w-0">
                      <Select value={method} onValueChange={setMethod}>
                        <SelectTrigger aria-label="Verification method"><SelectValue /></SelectTrigger>
                        <SelectContent>{METHODS.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
                      </Select>
                    </FormField>
                  </div>
                  <FormField label="Verification note" required hint="Who read it, where and when (for example: “J. Smith, console in DC1 rack 4, 30 Sep 10:15”)."
                    error={note.trim() && note.trim().length < 5 ? 'At least 5 characters.' : null}>
                    <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
                  </FormField>
                  {unchanged && (
                    <p className="text-xs text-muted">This key is already the active trusted key; registering again changes nothing.</p>
                  )}
                  {!!candidateSha && !active && activeEntries.length > 0 && (
                    <p className="text-xs text-muted">This is an additional key algorithm. Independently verify its fingerprint before adding it to the verified key set.</p>
                  )}
                  {replacing && (
                    <div className="space-y-2">
                      <div role="alert" className="flex gap-2 rounded-md border border-danger/50 bg-danger/10 p-3 text-xs text-danger">
                        <ShieldAlert className="h-4 w-4 shrink-0" aria-hidden />
                        <span>
                          <strong>The trusted {candidateType} key changed.</strong> Confirm the replacement with the device owner and independently verify its fingerprint.
                        </span>
                      </div>
                      <FormField label="Replacement reason" required error={reason.trim() && reason.trim().length < 5 ? 'At least 5 characters.' : null}
                        hint="For example the change ticket and who confirmed the replacement. The old key is retired, not deleted.">
                        <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} />
                      </FormField>
                    </div>
                  )}
                </Step>

                <Step n={3} title="Register the key">
                  {serverError && (
                    <p role="alert" className="flex gap-1.5 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                      <XCircle className="h-3.5 w-3.5 shrink-0" aria-hidden /> {serverError}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    <Button onClick={() => register.mutate()} disabled={!canRegister} variant={replacing ? 'destructive' : 'default'}>
                      {register.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                      {replacing ? 'Replace trusted key' : 'Register trusted key'}
                    </Button>
                    {!canRegister && !register.isPending && (
                      <span className="text-xs text-muted">
                        {!keyReady ? 'Read or paste the key first.' : typedFp.trim().length < 10 ? 'Enter the independently read fingerprint.'
                          : match === 'mismatch' ? 'The fingerprint does not match.' : note.trim().length < 5 ? 'Add a verification note.'
                            : replacing ? 'Give a replacement reason.' : ''}
                      </span>
                    )}
                  </div>
                </Step>
              </ol>
            )}

            {/* History */}
            <section aria-labelledby="trust-history" className="space-y-2">
              <h3 id="trust-history" className="text-xs font-semibold uppercase tracking-wider text-muted">Trust history</h3>
              {!entries.length ? (
                <EmptyState>No managed keys registered for {trust.data.host}:{trust.data.port}.</EmptyState>
              ) : (
                <Table className="min-w-[640px] text-xs">
                  <THead>
                    <Tr><Th>Status</Th><Th>Fingerprint</Th><Th>Verified by</Th><Th>Registered</Th><Th>Retired</Th>{canTrust && <Th><span className="sr-only">Actions</span></Th>}</Tr>
                  </THead>
                  <TBody>
                    {entries.map((e) => (
                      <Tr key={e.id} className="align-top">
                        <Td className="p-2">
                          {e.retired_at ? <Badge variant="outline">Retired</Badge> : <Badge variant="success"><CheckCircle2 className="h-3 w-3" aria-hidden /> Active</Badge>}
                        </Td>
                        <Td className="max-w-[220px] p-2"><div className="font-mono">{e.key_type}</div><Mono>{e.fingerprint_sha256}</Mono></Td>
                        <Td className="max-w-[200px] p-2">
                          <div>{METHOD_LABEL[e.verification_method] || e.verification_method}</div>
                          <div className="break-words text-muted">{e.verification_note}</div>
                        </Td>
                        <Td className="whitespace-nowrap p-2"><div>{e.registered_by_name || '—'}</div><Ago iso={e.created_at} className="text-muted" /></Td>
                        <Td className="max-w-[200px] p-2">
                          {e.retired_at ? (
                            <>
                              <div className="whitespace-nowrap">{e.retired_by_name || '—'} · <Ago iso={e.retired_at} /></div>
                              {e.retire_reason && <div className="break-words text-muted">{e.retire_reason}</div>}
                            </>
                          ) : <span className="text-muted">—</span>}
                        </Td>
                        {canTrust && (
                          <Td className="p-2 text-right">
                            {!e.retired_at && (
                              <Button size="sm" variant="outline" className="text-danger" onClick={() => setRetiring(e)}
                                aria-label={`Retire key ${e.fingerprint_sha256}`}>Retire</Button>
                            )}
                          </Td>
                        )}
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </section>
          </div>
        ) : null}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
        <RetireDialog deviceId={deviceId} entry={retiring} onClose={() => setRetiring(null)} />
      </DialogContent>
    </Dialog>
  )
}

function RetireDialog({ deviceId, entry, onClose }: { deviceId: string; entry: TrustEntry | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  useEffect(() => { setReason('') }, [entry?.id])
  const retire = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ncm-trust/${entry!.id}/retire`, { reason: reason.trim() })).data,
    onSuccess: () => { toast.success('Trust entry retired'); qc.invalidateQueries({ queryKey: ['ncm'] }); onClose() },
  })
  return (
    <Dialog open={!!entry} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-h-[90vh] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><AlertTriangle className="h-5 w-5 text-danger" aria-hidden /> Retire trusted key</DialogTitle>
          <DialogDescription>
            Backups from this device stop until a new key is registered: the collector refuses to connect without a trusted host key. The entry stays in the history.
          </DialogDescription>
        </DialogHeader>
        {entry && <p className="text-xs"><span className="font-mono">{entry.key_type}</span> <Mono>{entry.fingerprint_sha256}</Mono></p>}
        <FormField label="Reason" required error={reason.trim() && reason.trim().length < 3 ? 'At least 3 characters.' : null}>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="For example: device decommissioned" />
        </FormField>
        {retire.isError && <p role="alert" className="text-xs text-danger">{ncmError(retire.error)}</p>}
        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={onClose} disabled={retire.isPending}>Cancel</Button>
          <Button variant="destructive" disabled={reason.trim().length < 3 || retire.isPending} onClick={() => retire.mutate()}>
            {retire.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Retire key
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
