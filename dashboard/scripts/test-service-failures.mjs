import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'service-failures-'))
try {
  const outfile = path.join(dir, 'failure.cjs')
  await build({
    stdin: {
      contents: `export { explainServiceFailure } from './failureReason'; export { ServiceFailureReason } from './ServiceFailureReason';`,
      resolveDir: path.resolve('src/components/services'), loader: 'tsx',
    },
    outfile, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic',
  })
  const { explainServiceFailure: explain, ServiceFailureReason } = createRequire(import.meta.url)(outfile)
  const timeout = 'Service check failed: request failed: Get "https://career.smo.gov.sa": context deadline exceeded (Client.Timeout exceeded while awaiting headers)'
  assert.equal(explain(timeout), 'Request timed out — the check did not complete before the timeout limit.')
  assert.match(explain(timeout, { failureStage: 'connect', timeoutSeconds: 10 }), /^Connection timed out .*within 10s\.$/)
  assert.match(explain(timeout, { failureStage: 'response', timeoutSeconds: 10 }), /^Response timed out .*connection was established/)
  assert.match(explain(timeout, { failureStage: 'dns' }), /^DNS lookup timed out/)
  assert.match(explain(timeout, { failureStage: 'tls' }), /^TLS handshake timed out/)
  for (const [raw, label] of [
    ['dial tcp: lookup example.test: no such host', 'DNS lookup failed'],
    ['dial tcp: connect: connection refused', 'Connection refused'],
    ['dial tcp: connect: network is unreachable', 'Service unreachable'],
    ['dial tcp: connect: no route to host', 'Service unreachable'],
    ['tls: failed to verify certificate: x509: certificate signed by unknown authority', 'TLS certificate error'],
    ['x509: certificate has expired or is not yet valid', 'TLS certificate invalid'],
    ['net/http: TLS handshake timeout', 'TLS handshake timed out'],
    ['read tcp: connection reset by peer', 'Connection interrupted'],
    ['expected status 200-299, got 503', 'Unexpected HTTP status'],
    ["content match failed: 'welcome' not found in response", 'Content check failed'],
  ]) assert(explain(raw).startsWith(label), raw)
  assert.equal(explain('Service check failed: custom probe failed'), 'custom probe failed')
  assert.equal(explain('Certificate expires in 7 days'), 'Certificate expires in 7 days')
  assert.equal(explain(null), 'No failure details recorded')
  const urlOnly = 'request failed: Get "https://example.test/connection%20refused": custom error'
  assert.equal(explain(urlOnly), urlOnly)
  const html = renderToStaticMarkup(React.createElement(ServiceFailureReason, { reason: timeout }))
  assert.match(html, /Request timed out/)
  assert.match(html, /<details/)
  assert.match(html, /Technical details/)
  assert.match(html, /Client.Timeout exceeded while awaiting headers/)
  const unsafe = '<script>alert(1)</script>'
  const safeHtml = renderToStaticMarkup(React.createElement(ServiceFailureReason, { reason: unsafe }))
  assert(!safeHtml.includes('<script>'))
  assert(safeHtml.includes('&lt;script&gt;'))
  console.log('Service failure regressions passed: timeout phases, DNS, route, connection, TLS, HTTP, fallback, details, and safe text rendering.')
} finally {
  fs.rmSync(dir, { recursive: true, force: true })
}
