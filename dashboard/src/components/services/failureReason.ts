export type FailureContext = { failureStage?: string | null; timeoutSeconds?: number | null }

/** Classify legacy probe messages at display time so existing history stays useful. */
export function explainServiceFailure(raw: string | null | undefined, context: FailureContext = {}): string {
  if (!raw?.trim()) return 'No failure details recorded'
  const message = raw.trim().replace(/^Service check failed:\s*/i, '')
  // Request URLs can contain arbitrary words; they are not evidence of a failure type.
  const error = message.replace(/https?:\/\/[^\s"]+/gi, '').toLowerCase()
  const limit = context.timeoutSeconds && context.timeoutSeconds > 0 ? ` within ${context.timeoutSeconds}s` : ' before the timeout limit'
  const timedOut = /context deadline exceeded|client\.timeout|timed? ?out|timeout exceeded|timeoutexception/.test(error)
  if (timedOut) {
    if (context.failureStage === 'dns') return `DNS lookup timed out — the monitor could not resolve the hostname${limit}.`
    if (context.failureStage === 'connect') return `Connection timed out — the monitor could not connect to the service${limit}.`
    if (context.failureStage === 'tls' || /tls handshake timeout/.test(error)) return `TLS handshake timed out — the secure connection did not complete${limit}.`
    if (context.failureStage === 'response') return `Response timed out — the connection was established, but the service did not respond${limit}.`
    return `Request timed out — the check did not complete${limit}.`
  }
  if (/no such host|name or service not known|temporary failure in name resolution|enotfound|eai_again|dns lookup failed/.test(error)) {
    return 'DNS lookup failed — the monitor could not resolve the service hostname.'
  }
  if (/connection refused|econnrefused/.test(error)) return 'Connection refused — the destination rejected the connection.'
  if (/no route to host|network is unreachable|host is unreachable|enetunreach|ehostunreach/.test(error)) {
    return 'Service unreachable — the monitor has no working network route to the destination.'
  }
  if (/certificate has expired|certificate is expired|certificate has expired or is not yet valid/.test(error)) return 'TLS certificate invalid — the certificate is expired or not yet valid.'
  if (/x509:|certificate verify failed|certificate verification failed|unknown certificate authority/.test(error)) {
    return 'TLS certificate error — the monitor could not verify the service certificate.'
  }
  if (/tls handshake|handshake failure/.test(error)) return 'TLS connection failed — the secure connection could not be established.'
  if (/connection reset|econnreset|unexpected eof/.test(error)) return 'Connection interrupted — the connection closed before the check completed.'
  const status = error.match(/expected status (.+?), got (\d{3})/)
  if (status) return `Unexpected HTTP status — received ${status[2]}; expected ${status[1]}.`
  if (/content match failed|required response content was not found/.test(error)) return 'Content check failed — the response did not contain the required content.'
  return message
}
