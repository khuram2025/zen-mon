import { useEffect, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { providers, type Connector, type Provider, type Summary } from './model'
import { selectStyle } from './ui'

type Connection = {
  provider: Provider
  endpoint: string
  username: string
  secret: string
  auth_type: 'password' | 'api_key'
  ca_pem: string
  api_version: string
}
const freshConnection = (): Connection => ({
  provider: 'nutanix_pc',
  endpoint: '',
  username: '',
  secret: '',
  auth_type: 'password',
  ca_pem: '',
  api_version: 'v4.0',
})

/** Add / edit a connector. Saving requires a successful test of the exact connection (server-enforced). */
export function ConnectorDialog({ open, editing, onClose, onSaved }: {
  open: boolean
  editing: Connector | null
  onClose: () => void
  onSaved: (id: string) => void
}) {
  const [connection, setConnection] = useState<Connection>(freshConnection)
  const [name, setName] = useState('')
  const [tags, setTags] = useState('')
  const [interval, setInterval] = useState(300)
  const [test, setTest] = useState<(Summary & { test_token: string }) | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setTest(null)
    setError('')
    if (!editing) {
      setConnection(freshConnection())
      setName('')
      setTags('')
      setInterval(300)
      return
    }
    setName(editing.name)
    setTags(editing.tags.join(', '))
    setInterval(editing.interval_seconds)
    setBusy('load')
    api
      .get<Connection>(`/virtualization/connectors/${editing.id}/configuration`)
      .then((r) => setConnection({ ...r.data, secret: '' }))
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setBusy(''))
  }, [open, editing])

  function change(values: Partial<Connection>) {
    setConnection((c) => ({ ...c, ...values }))
    setTest(null)
    setError('')
  }
  async function act(label: string, action: () => Promise<unknown>) {
    setBusy(label)
    setError('')
    try {
      await action()
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy('')
    }
  }
  async function save() {
    if (!test) return
    await act('save', async () => {
      const payload = {
        name,
        connection,
        interval_seconds: interval,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        test_token: test.test_token,
      }
      const r = editing
        ? await api.put<Connector>(`/virtualization/connectors/${editing.id}`, payload)
        : await api.post<Connector>('/virtualization/connectors', payload)
      setConnection(freshConnection())
      setTest(null)
      onSaved(r.data.id)
    })
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!busy && !o) onClose() }}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-auto">
        <DialogHeader>
          <DialogTitle>{editing ? 'Edit virtualization connector' : 'Add virtualization connector'}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <p className="flex items-center gap-2 text-sm text-muted">
            <ShieldCheck className="h-4 w-4" />
            TLS certificate and hostname validation are always enabled.
          </p>
          <fieldset disabled={!!busy} className="space-y-4">
            <label className="block space-y-1 text-sm">
              Name
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={150} />
            </label>
            <label className="block space-y-1 text-sm">
              Platform
              <select
                className={`${selectStyle} w-full`}
                value={connection.provider}
                disabled={!!editing}
                onChange={(e) => change({ provider: e.target.value as Provider, auth_type: 'password' })}
              >
                {Object.entries(providers).map(([k, label]) => (
                  <option key={k} value={k}>{label}</option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm">
              HTTPS endpoint
              <Input
                placeholder={
                  connection.provider === 'hyperv'
                    ? 'https://hyperv.example.org:5986'
                    : connection.provider === 'vmware'
                      ? 'https://vcenter.example.org'
                      : 'https://prism.example.org:9440'
                }
                value={connection.endpoint}
                disabled={!!editing}
                onChange={(e) => change({ endpoint: e.target.value })}
              />
            </label>
            {connection.provider === 'nutanix_pc' && (
              <div className="grid grid-cols-2 gap-3">
                <label className="block space-y-1 text-sm">
                  GA API version
                  <select className={`${selectStyle} w-full`} value={connection.api_version} onChange={(e) => change({ api_version: e.target.value })}>
                    {['v4.0', 'v4.1', 'v4.2', 'v4.3'].map((v) => <option key={v}>{v}</option>)}
                  </select>
                </label>
                <label className="block space-y-1 text-sm">
                  Credential type
                  <select
                    className={`${selectStyle} w-full`}
                    value={connection.auth_type}
                    onChange={(e) => change({ auth_type: e.target.value as 'password' | 'api_key' })}
                  >
                    <option value="password">Read-only account</option>
                    <option value="api_key">IAM service account API key</option>
                  </select>
                </label>
              </div>
            )}
            {connection.auth_type === 'password' && (
              <label className="block space-y-1 text-sm">
                Monitoring username
                <Input autoComplete="off" value={connection.username} onChange={(e) => change({ username: e.target.value })} />
              </label>
            )}
            <label className="block space-y-1 text-sm">
              {connection.auth_type === 'api_key' ? 'API key' : 'Password'}
              {editing && ' (re-enter to test changes)'}
              <Input type="password" autoComplete="new-password" value={connection.secret} onChange={(e) => change({ secret: e.target.value })} />
            </label>
            <label className="block space-y-1 text-sm">
              Private CA certificate chain (PEM, optional)
              <textarea
                className="min-h-24 w-full rounded-md border border-border bg-surface p-2 font-mono text-xs"
                placeholder="-----BEGIN CERTIFICATE-----"
                value={connection.ca_pem}
                onChange={(e) => change({ ca_pem: e.target.value })}
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block space-y-1 text-sm">
                Polling interval (seconds)
                <Input type="number" min={60} max={3600} value={interval} onChange={(e) => setInterval(Number(e.target.value))} />
              </label>
              <label className="block space-y-1 text-sm">
                Visibility tags (comma separated)
                <Input value={tags} onChange={(e) => setTags(e.target.value)} />
              </label>
            </div>
          </fieldset>
          {connection.provider === 'hyperv' && (
            <p className="text-xs text-muted">
              Requires the ZenPlusMonitoring JEA module on each host and a trusted WinRM HTTPS listener. Use a dedicated
              account authorized only for this endpoint.
            </p>
          )}
          {connection.provider === 'nutanix_pe' && (
            <p className="text-xs text-muted">Element v2 coverage depends on AOS. VM performance counters may require Prism Central v4.</p>
          )}
          {error && <p role="alert" className="text-sm text-danger">{error}</p>}
          {test && (
            <div className="space-y-2 rounded-md border border-primary/30 bg-primary/5 p-3 text-sm">
              <p className="font-medium">Authenticated collection succeeded · {test.version}</p>
              <p>{Object.entries(test.counts || {}).map(([k, v]) => `${v} ${k}`).join(' · ')}</p>
              {test.warnings?.map((w, i) => <p key={i} className="text-warning">{w}</p>)}
              <p className="text-xs text-muted">Review coverage before saving. Test validity: 10 minutes.</p>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={!!busy || !connection.endpoint || !connection.secret}
              onClick={() => {
                setTest(null)
                act('test', async () => {
                  const r = await api.post('/virtualization/test', connection, { timeout: 310000 })
                  setTest(r.data)
                })
              }}
            >
              {busy === 'test' ? 'Testing discovery and metrics…' : 'Test connection'}
            </Button>
            <Button disabled={!!busy || !test || !name.trim() || interval < 60 || interval > 3600} onClick={save}>
              {busy === 'save' ? 'Saving…' : 'Save connector'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
