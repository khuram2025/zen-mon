import { useState, useEffect } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Globe, Plug, ShieldCheck } from 'lucide-react'
import { useServiceCheck } from '@/hooks/useServiceChecks'
import { api } from '@/lib/api'

const typeIcons = { http: Globe, tcp: Plug, tls: ShieldCheck }
const inputClass = 'bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:border-[var(--accent)] focus:outline-none w-full text-sm'

export function EditServiceCheckPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data: check, isLoading } = useServiceCheck(id || '')

  const [name, setName] = useState('')
  const [targetUrl, setTargetUrl] = useState('')
  const [targetHost, setTargetHost] = useState('')
  const [targetPort, setTargetPort] = useState<number | ''>('')
  const [checkInterval, setCheckInterval] = useState(60)
  const [timeout, setTimeout] = useState(10)
  const [httpMethod, setHttpMethod] = useState('GET')
  const [httpExpectedStatus, setHttpExpectedStatus] = useState(200)
  const [httpContentMatch, setHttpContentMatch] = useState('')
  const [httpFollowRedirects, setHttpFollowRedirects] = useState(true)
  const [tlsWarnDays, setTlsWarnDays] = useState(30)
  const [tlsCriticalDays, setTlsCriticalDays] = useState(7)
  const [description, setDescription] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (check) {
      setName(check.name)
      setTargetUrl(check.target_url || '')
      setTargetHost(check.target_host)
      setTargetPort(check.target_port || '')
      setCheckInterval(check.check_interval)
      setTimeout(check.timeout)
      setHttpMethod(check.http_method || 'GET')
      setHttpExpectedStatus(check.http_expected_status || 200)
      setHttpContentMatch(check.http_content_match || '')
      setHttpFollowRedirects(check.http_follow_redirects)
      setTlsWarnDays(check.tls_warn_days)
      setTlsCriticalDays(check.tls_critical_days)
      setDescription(check.description || '')
      setEnabled(check.enabled)
    }
  }, [check])

  const mutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.put(`/service-checks/${id}`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['service-checks'] })
      queryClient.invalidateQueries({ queryKey: ['service-check', id] })
      navigate(`/service-checks/${id}`)
    },
    onError: (err: Error) => setError(err.message),
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError('')

    let host = targetHost
    if (check?.check_type === 'http' && targetUrl) {
      try { host = new URL(targetUrl).hostname } catch { /* keep existing */ }
    }

    const data: Record<string, unknown> = {
      name,
      enabled,
      target_host: host,
      target_port: targetPort || null,
      check_interval: checkInterval,
      timeout,
      description: description || null,
    }

    if (check?.check_type === 'http') {
      data.target_url = targetUrl
      data.http_method = httpMethod
      data.http_expected_status = httpExpectedStatus
      data.http_content_match = httpContentMatch || null
      data.http_follow_redirects = httpFollowRedirects
    }
    if (check?.check_type === 'tls') {
      data.tls_warn_days = tlsWarnDays
      data.tls_critical_days = tlsCriticalDays
    }

    mutation.mutate(data)
  }

  if (isLoading) return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[var(--accent)]" /></div>
  if (!check) return <div className="text-[var(--text-muted)] text-center py-20">Service check not found</div>

  const TypeIcon = typeIcons[check.check_type as keyof typeof typeIcons] || ShieldCheck

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-4">
        <Link to={`/service-checks/${id}`} className="p-2 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-muted)]"><ArrowLeft className="w-5 h-5" /></Link>
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">Edit Service Check</h1>
          <p className="text-sm text-[var(--text-muted)]">{check.name}</p>
        </div>
      </div>

      {error && <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 text-red-400 text-sm">{error}</div>}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Type Badge */}
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
          <p className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-3">Check Type</p>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-[var(--accent)]/10 border border-[var(--accent)]/30">
              <TypeIcon className="w-5 h-5 text-[var(--accent)]" />
              <span className="text-sm font-medium text-[var(--accent)]">{check.check_type.toUpperCase()}</span>
            </div>
            <span className="text-xs text-[var(--text-muted)]">Type cannot be changed after creation</span>
          </div>
        </div>

        {/* Common Fields */}
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 space-y-4">
          <div>
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Name</label>
            <input className={inputClass} value={name} onChange={e => setName(e.target.value)} required />
          </div>
          <div className="flex items-center gap-3">
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider">Enabled</label>
            <button type="button" onClick={() => setEnabled(!enabled)}
              className={`w-10 h-6 rounded-full transition-colors ${enabled ? 'bg-[var(--accent)]' : 'bg-[var(--bg-elevated)]'}`}>
              <div className={`w-4 h-4 rounded-full bg-white transition-transform mx-1 ${enabled ? 'translate-x-4' : ''}`} />
            </button>
          </div>
        </div>

        {/* HTTP Fields */}
        {check.check_type === 'http' && (
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 space-y-4">
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">HTTP Configuration</h3>
            <div>
              <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">URL</label>
              <input className={inputClass} value={targetUrl} onChange={e => setTargetUrl(e.target.value)} placeholder="https://example.com/health" required />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Method</label>
                <select className={inputClass} value={httpMethod} onChange={e => setHttpMethod(e.target.value)}>
                  {['GET', 'POST', 'HEAD', 'PUT'].map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Expected Status</label>
                <input type="number" className={inputClass} value={httpExpectedStatus} onChange={e => setHttpExpectedStatus(Number(e.target.value))} />
              </div>
            </div>
            <div>
              <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Content Match (optional)</label>
              <input className={inputClass} value={httpContentMatch} onChange={e => setHttpContentMatch(e.target.value)} placeholder="Substring to find in response" />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider">Follow Redirects</label>
              <button type="button" onClick={() => setHttpFollowRedirects(!httpFollowRedirects)}
                className={`w-10 h-6 rounded-full transition-colors ${httpFollowRedirects ? 'bg-[var(--accent)]' : 'bg-[var(--bg-elevated)]'}`}>
                <div className={`w-4 h-4 rounded-full bg-white transition-transform mx-1 ${httpFollowRedirects ? 'translate-x-4' : ''}`} />
              </button>
            </div>
          </div>
        )}

        {/* TCP Fields */}
        {check.check_type === 'tcp' && (
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 space-y-4">
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">TCP Configuration</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Host</label>
                <input className={inputClass} value={targetHost} onChange={e => setTargetHost(e.target.value)} required />
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Port</label>
                <input type="number" className={inputClass} value={targetPort} onChange={e => setTargetPort(Number(e.target.value))} required />
              </div>
            </div>
          </div>
        )}

        {/* TLS Fields */}
        {check.check_type === 'tls' && (
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 space-y-4">
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">TLS Configuration</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Host</label>
                <input className={inputClass} value={targetHost} onChange={e => setTargetHost(e.target.value)} required />
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Port</label>
                <input type="number" className={inputClass} value={targetPort || 443} onChange={e => setTargetPort(Number(e.target.value))} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Warning Days</label>
                <input type="number" className={inputClass} value={tlsWarnDays} onChange={e => setTlsWarnDays(Number(e.target.value))} />
              </div>
              <div>
                <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Critical Days</label>
                <input type="number" className={inputClass} value={tlsCriticalDays} onChange={e => setTlsCriticalDays(Number(e.target.value))} />
              </div>
            </div>
          </div>
        )}

        {/* Timing */}
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 space-y-4">
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">Timing</h3>
          <div>
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-2 block">Check Interval</label>
            <div className="flex gap-2">
              {[{l:'15s',v:15},{l:'30s',v:30},{l:'60s',v:60},{l:'5m',v:300},{l:'10m',v:600}].map(opt => (
                <button key={opt.v} type="button" onClick={() => setCheckInterval(opt.v)}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${checkInterval === opt.v ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
                  {opt.l}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Timeout (seconds)</label>
            <input type="number" className={inputClass + ' max-w-32'} value={timeout} onChange={e => setTimeout(Number(e.target.value))} min={1} max={60} />
          </div>
        </div>

        {/* Description */}
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
          <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1 block">Description (optional)</label>
          <textarea className={inputClass + ' h-20 resize-none'} value={description} onChange={e => setDescription(e.target.value)} />
        </div>

        <button type="submit" disabled={mutation.isPending}
          className="w-full py-3 rounded-lg bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white font-medium text-sm transition-colors disabled:opacity-50">
          {mutation.isPending ? 'Saving...' : 'Save Changes'}
        </button>
      </form>
    </div>
  )
}
