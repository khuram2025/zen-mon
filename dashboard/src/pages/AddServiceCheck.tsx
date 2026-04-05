import { useState, useCallback } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Globe, Plug, ShieldCheck, ArrowLeft, Loader2 } from 'lucide-react';
import { api } from '@/lib/api';

type CheckType = 'http' | 'tcp' | 'tls';
type HttpMethod = 'GET' | 'POST' | 'HEAD' | 'PUT';

interface ServiceCheckPayload {
  name: string;
  check_type: CheckType;
  device_id: string | null;
  target_host: string;
  target_port: number | null;
  target_url: string | null;
  http_method: HttpMethod;
  http_expected_status: number;
  http_content_match: string | null;
  http_follow_redirects: boolean;
  tls_warn_days: number;
  tls_critical_days: number;
  check_interval: number;
  timeout: number;
  description: string | null;
  enabled: boolean;
}

const INPUT_CLASS =
  'bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:border-[var(--accent)] focus:outline-none w-full text-sm';

const CARD_CLASS =
  'bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5';

const CHECK_TYPES: { value: CheckType; label: string; icon: typeof Globe; desc: string }[] = [
  { value: 'http', label: 'HTTP', icon: Globe, desc: 'Monitor HTTP/HTTPS endpoints' },
  { value: 'tcp', label: 'TCP', icon: Plug, desc: 'Monitor TCP port availability' },
  { value: 'tls', label: 'TLS', icon: ShieldCheck, desc: 'Monitor TLS certificate expiry' },
];

const INTERVAL_PRESETS = [
  { label: '15s', value: 15 },
  { label: '30s', value: 30 },
  { label: '60s', value: 60 },
  { label: '5m', value: 300 },
  { label: '10m', value: 600 },
];

const HTTP_METHODS: HttpMethod[] = ['GET', 'POST', 'HEAD', 'PUT'];

function extractHostFromUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.hostname;
  } catch {
    return '';
  }
}

export function AddServiceCheckPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [checkType, setCheckType] = useState<CheckType>('http');
  const [name, setName] = useState('');
  const [deviceId, setDeviceId] = useState('');
  const [checkInterval, setCheckInterval] = useState(60);
  const [timeout, setTimeout] = useState(10);
  const [description, setDescription] = useState('');

  // HTTP fields
  const [url, setUrl] = useState('');
  const [httpMethod, setHttpMethod] = useState<HttpMethod>('GET');
  const [expectedStatus, setExpectedStatus] = useState(200);
  const [contentMatch, setContentMatch] = useState('');
  const [followRedirects, setFollowRedirects] = useState(true);

  // TCP fields
  const [tcpHost, setTcpHost] = useState('');
  const [tcpPort, setTcpPort] = useState<string>('');

  // TLS fields
  const [tlsHost, setTlsHost] = useState('');
  const [tlsPort, setTlsPort] = useState(443);
  const [warnDays, setWarnDays] = useState(30);
  const [criticalDays, setCriticalDays] = useState(7);

  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (data: ServiceCheckPayload) => api.post('/service-checks', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['service-checks'] });
      navigate('/service-checks', { state: { message: 'Service check created successfully.' } });
    },
    onError: (err: unknown) => {
      const message =
        err instanceof Error ? err.message : 'Failed to create service check. Please try again.';
      setError(message);
    },
  });

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      setError(null);

      if (!name.trim()) {
        setError('Name is required.');
        return;
      }

      let targetHost = '';
      let targetPort: number | null = null;
      let targetUrl: string | null = null;

      if (checkType === 'http') {
        if (!url.trim()) {
          setError('URL is required for HTTP checks.');
          return;
        }
        targetHost = extractHostFromUrl(url);
        if (!targetHost) {
          setError('Invalid URL. Please enter a valid URL including the protocol (e.g. https://).');
          return;
        }
        targetUrl = url;
      } else if (checkType === 'tcp') {
        if (!tcpHost.trim()) {
          setError('Host is required for TCP checks.');
          return;
        }
        if (!tcpPort || isNaN(Number(tcpPort)) || Number(tcpPort) <= 0) {
          setError('A valid port number is required for TCP checks.');
          return;
        }
        targetHost = tcpHost;
        targetPort = Number(tcpPort);
      } else if (checkType === 'tls') {
        if (!tlsHost.trim()) {
          setError('Host is required for TLS checks.');
          return;
        }
        targetHost = tlsHost;
        targetPort = tlsPort;
      }

      const payload: ServiceCheckPayload = {
        name: name.trim(),
        check_type: checkType,
        device_id: deviceId.trim() || null,
        target_host: targetHost,
        target_port: targetPort,
        target_url: targetUrl,
        http_method: httpMethod,
        http_expected_status: expectedStatus,
        http_content_match: contentMatch.trim() || null,
        http_follow_redirects: followRedirects,
        tls_warn_days: warnDays,
        tls_critical_days: criticalDays,
        check_interval: checkInterval,
        timeout,
        description: description.trim() || null,
        enabled: true,
      };

      mutation.mutate(payload);
    },
    [
      name, checkType, url, tcpHost, tcpPort, tlsHost, tlsPort, deviceId,
      httpMethod, expectedStatus, contentMatch, followRedirects,
      warnDays, criticalDays, checkInterval, timeout, description, mutation,
    ],
  );

  return (
    <div className="min-h-screen bg-[var(--bg-primary)] text-[var(--text-primary)]">
      <div className="max-w-3xl mx-auto px-6 py-10">
        {/* Header */}
        <div className="flex items-center gap-4 mb-8">
          <Link
            to="/service-checks"
            className="flex items-center justify-center w-9 h-9 rounded-lg bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] hover:border-[var(--accent)] transition-colors"
          >
            <ArrowLeft className="w-4 h-4 text-[var(--text-secondary)]" />
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">Add Service Check</h1>
        </div>

        {/* Error banner */}
        {error && (
          <div className="mb-6 px-4 py-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Check Type Selector */}
          <div>
            <label className="block text-sm font-medium text-[var(--text-secondary)] mb-3">
              Check Type
            </label>
            <div className="grid grid-cols-3 gap-4">
              {CHECK_TYPES.map(({ value, label, icon: Icon, desc }) => {
                const isSelected = checkType === value;
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setCheckType(value)}
                    className={`${CARD_CLASS} flex flex-col items-center gap-2.5 text-center cursor-pointer transition-all ${
                      isSelected
                        ? 'border-[var(--accent)] ring-1 ring-[var(--accent)]'
                        : 'hover:border-[var(--text-muted)]'
                    }`}
                  >
                    <Icon
                      className={`w-7 h-7 ${
                        isSelected ? 'text-[var(--accent)]' : 'text-[var(--text-muted)]'
                      }`}
                    />
                    <span
                      className={`text-sm font-semibold ${
                        isSelected ? 'text-[var(--text-primary)]' : 'text-[var(--text-secondary)]'
                      }`}
                    >
                      {label}
                    </span>
                    <span className="text-xs text-[var(--text-muted)]">{desc}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Common Fields */}
          <div className={CARD_CLASS}>
            <h2 className="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-4">
              General
            </h2>
            <div className="space-y-4">
              {/* Name */}
              <div>
                <label htmlFor="name" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                  Name <span className="text-red-400">*</span>
                </label>
                <input
                  id="name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Production API Health"
                  className={INPUT_CLASS}
                  required
                />
              </div>

              {/* Device ID */}
              <div>
                <label htmlFor="device_id" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                  Device ID <span className="text-[var(--text-muted)]">(optional)</span>
                </label>
                <input
                  id="device_id"
                  type="text"
                  value={deviceId}
                  onChange={(e) => setDeviceId(e.target.value)}
                  placeholder="UUID of associated device"
                  className={INPUT_CLASS}
                />
              </div>

              {/* Check Interval */}
              <div>
                <label className="block text-sm text-[var(--text-secondary)] mb-1.5">
                  Check Interval
                </label>
                <div className="flex gap-2">
                  {INTERVAL_PRESETS.map(({ label, value }) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setCheckInterval(value)}
                      className={`px-3.5 py-2 rounded-lg text-sm font-medium transition-colors ${
                        checkInterval === value
                          ? 'bg-[var(--accent)] text-white'
                          : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] border border-[var(--bg-elevated)]'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Timeout */}
              <div>
                <label htmlFor="timeout" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                  Timeout (seconds)
                </label>
                <input
                  id="timeout"
                  type="number"
                  min={1}
                  max={60}
                  value={timeout}
                  onChange={(e) => setTimeout(Number(e.target.value))}
                  className={`${INPUT_CLASS} max-w-[120px]`}
                />
              </div>

              {/* Description */}
              <div>
                <label htmlFor="description" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                  Description <span className="text-[var(--text-muted)]">(optional)</span>
                </label>
                <textarea
                  id="description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Optional description of this service check"
                  rows={3}
                  className={`${INPUT_CLASS} resize-none`}
                />
              </div>
            </div>
          </div>

          {/* HTTP Fields */}
          {checkType === 'http' && (
            <div className={CARD_CLASS}>
              <h2 className="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-4">
                HTTP Configuration
              </h2>
              <div className="space-y-4">
                {/* URL */}
                <div>
                  <label htmlFor="url" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    URL <span className="text-red-400">*</span>
                  </label>
                  <input
                    id="url"
                    type="text"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://example.com/health"
                    className={INPUT_CLASS}
                    required
                  />
                  {url && extractHostFromUrl(url) && (
                    <p className="mt-1.5 text-xs text-[var(--text-muted)]">
                      Host: <span className="text-[var(--text-secondary)]">{extractHostFromUrl(url)}</span>
                    </p>
                  )}
                </div>

                {/* Method */}
                <div>
                  <label className="block text-sm text-[var(--text-secondary)] mb-1.5">Method</label>
                  <div className="flex gap-3">
                    {HTTP_METHODS.map((method) => (
                      <label
                        key={method}
                        className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm cursor-pointer transition-colors ${
                          httpMethod === method
                            ? 'bg-[var(--accent)]/15 text-[var(--accent)] border border-[var(--accent)]/40'
                            : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] border border-[var(--bg-elevated)]'
                        }`}
                      >
                        <input
                          type="radio"
                          name="http_method"
                          value={method}
                          checked={httpMethod === method}
                          onChange={() => setHttpMethod(method)}
                          className="sr-only"
                        />
                        {method}
                      </label>
                    ))}
                  </div>
                </div>

                {/* Expected Status */}
                <div>
                  <label htmlFor="expected_status" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Expected Status Code
                  </label>
                  <input
                    id="expected_status"
                    type="number"
                    min={100}
                    max={599}
                    value={expectedStatus}
                    onChange={(e) => setExpectedStatus(Number(e.target.value))}
                    className={`${INPUT_CLASS} max-w-[120px]`}
                  />
                </div>

                {/* Content Match */}
                <div>
                  <label htmlFor="content_match" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Content Match <span className="text-[var(--text-muted)]">(optional)</span>
                  </label>
                  <input
                    id="content_match"
                    type="text"
                    value={contentMatch}
                    onChange={(e) => setContentMatch(e.target.value)}
                    placeholder="Substring to find in response body"
                    className={INPUT_CLASS}
                  />
                </div>

                {/* Follow Redirects */}
                <div className="flex items-center justify-between">
                  <label htmlFor="follow_redirects" className="text-sm text-[var(--text-secondary)]">
                    Follow Redirects
                  </label>
                  <button
                    id="follow_redirects"
                    type="button"
                    role="switch"
                    aria-checked={followRedirects}
                    onClick={() => setFollowRedirects(!followRedirects)}
                    className={`relative w-11 h-6 rounded-full transition-colors ${
                      followRedirects ? 'bg-[var(--accent)]' : 'bg-[var(--bg-elevated)]'
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
                        followRedirects ? 'translate-x-5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* TCP Fields */}
          {checkType === 'tcp' && (
            <div className={CARD_CLASS}>
              <h2 className="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-4">
                TCP Configuration
              </h2>
              <div className="space-y-4">
                <div>
                  <label htmlFor="tcp_host" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Host <span className="text-red-400">*</span>
                  </label>
                  <input
                    id="tcp_host"
                    type="text"
                    value={tcpHost}
                    onChange={(e) => setTcpHost(e.target.value)}
                    placeholder="e.g. db.example.com"
                    className={INPUT_CLASS}
                    required
                  />
                </div>
                <div>
                  <label htmlFor="tcp_port" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Port <span className="text-red-400">*</span>
                  </label>
                  <input
                    id="tcp_port"
                    type="number"
                    min={1}
                    max={65535}
                    value={tcpPort}
                    onChange={(e) => setTcpPort(e.target.value)}
                    placeholder="e.g. 5432"
                    className={`${INPUT_CLASS} max-w-[160px]`}
                    required
                  />
                </div>
              </div>
            </div>
          )}

          {/* TLS Fields */}
          {checkType === 'tls' && (
            <div className={CARD_CLASS}>
              <h2 className="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-4">
                TLS Configuration
              </h2>
              <div className="space-y-4">
                <div>
                  <label htmlFor="tls_host" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Host <span className="text-red-400">*</span>
                  </label>
                  <input
                    id="tls_host"
                    type="text"
                    value={tlsHost}
                    onChange={(e) => setTlsHost(e.target.value)}
                    placeholder="e.g. example.com"
                    className={INPUT_CLASS}
                    required
                  />
                </div>
                <div>
                  <label htmlFor="tls_port" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                    Port
                  </label>
                  <input
                    id="tls_port"
                    type="number"
                    min={1}
                    max={65535}
                    value={tlsPort}
                    onChange={(e) => setTlsPort(Number(e.target.value))}
                    className={`${INPUT_CLASS} max-w-[160px]`}
                  />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="warn_days" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                      Warning Days
                    </label>
                    <input
                      id="warn_days"
                      type="number"
                      min={1}
                      value={warnDays}
                      onChange={(e) => setWarnDays(Number(e.target.value))}
                      className={INPUT_CLASS}
                    />
                    <p className="mt-1 text-xs text-[var(--text-muted)]">
                      Warn when cert expires within this many days
                    </p>
                  </div>
                  <div>
                    <label htmlFor="critical_days" className="block text-sm text-[var(--text-secondary)] mb-1.5">
                      Critical Days
                    </label>
                    <input
                      id="critical_days"
                      type="number"
                      min={1}
                      value={criticalDays}
                      onChange={(e) => setCriticalDays(Number(e.target.value))}
                      className={INPUT_CLASS}
                    />
                    <p className="mt-1 text-xs text-[var(--text-muted)]">
                      Critical when cert expires within this many days
                    </p>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Submit */}
          <div className="flex justify-end pt-2">
            <button
              type="submit"
              disabled={mutation.isPending}
              className="flex items-center gap-2 px-6 py-2.5 rounded-lg bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {mutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
              {mutation.isPending ? 'Creating...' : 'Create Service Check'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
