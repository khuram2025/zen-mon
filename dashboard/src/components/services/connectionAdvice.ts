/**
 * Where to fix a failed service check test that comes down to how the appliance connects: a certificate it
 * does not trust, or a name it cannot resolve (docs/service-check-connections.md). Pure, so the check form,
 * the detail page and the tests share one answer.
 */
import { CONNECTIONS_LINK, CONNECTIONS_PAGE, PROBE_TRUST_LINK, PROBE_TRUST_PAGE } from '@/components/settings/hostOverrides'

export type ConnectionHintKind = 'trust' | 'name' | 'resolution'

export interface ConnectionHint {
  kind: ConnectionHintKind
  title: string
  text: string
  /** Where an administrator fixes it; absent when there is nothing to configure. */
  link?: { to: string; page: string; label: string }
}

export interface ManualAddress { host: string; address: string }

const TRUST = /unknown authority|self[- ]signed|unable to get local issuer|unable to verify the first certificate|trusted chain|not signed by a certificate authority/i
const NAME = /certificate is valid for|not valid for any names|is not valid for|hostname mismatch|issued for a different name/i
const RESOLUTION = /no such host|name or service not known|temporary failure in name resolution|hostname could not be resolved|enotfound|eai_again|dns lookup failed/i

/** `diagnosis` is the test API's category; `error` covers results that only carry the poller's message. */
export function connectionHint(diagnosis: string | null | undefined, error?: string | null): ConnectionHint | null {
  const text = error || ''
  if (diagnosis === 'tls_trust' || (!diagnosis && TRUST.test(text)) || (diagnosis === 'tls' && TRUST.test(text))) {
    return {
      kind: 'trust',
      title: 'The certificate is not trusted by this appliance',
      text: 'The service answered, but its certificate was issued by a certificate authority this appliance does not know, or is self-signed. Add the issuing authority (or the self-signed certificate) to the trusted certificates. Turning off certificate verification on the check hides the problem instead.',
      link: { to: PROBE_TRUST_LINK, page: PROBE_TRUST_PAGE, label: 'Trusted certificates (Security & TLS)' },
    }
  }
  if (diagnosis === 'tls_name' || ((!diagnosis || diagnosis === 'tls') && NAME.test(text))) {
    return {
      kind: 'name',
      title: 'The certificate was issued for a different name',
      text: 'The service presented a certificate that does not cover the host name in this check. Check the name in the URL. If the name has a manual address, check that the address is the right server.',
      link: { to: CONNECTIONS_LINK, page: CONNECTIONS_PAGE, label: 'Manual host addresses (Monitoring)' },
    }
  }
  if (diagnosis === 'dns' || (!diagnosis && RESOLUTION.test(text))) {
    return {
      kind: 'resolution',
      title: 'The host name could not be resolved',
      text: 'The appliance asked its DNS servers for this name and got no address. If the name is not in the DNS the appliance uses (an internal name, or a name that must be reached at a specific address), give it a manual address.',
      link: { to: CONNECTIONS_LINK, page: CONNECTIONS_PAGE, label: 'Manual host addresses (Monitoring)' },
    }
  }
  return null
}

/** Says that a test or a result did not go through DNS; null when it did. */
export function manualAddressNote(used: readonly ManualAddress[] | null | undefined): string | null {
  const list = (used || []).filter((item) => item?.host && item?.address)
  if (!list.length) return null
  const pairs = list.map((item) => `${item.address} for ${item.host}`).join(', ')
  return `Connected to the manual address ${pairs}. DNS was not used, so this result says nothing about name resolution.`
}

/** The same statement for a stored result's connection diagnostics. */
export function resultManualAddress(diagnostics: { override_host?: string | null; override_address?: string | null } | null | undefined): ManualAddress | null {
  if (!diagnostics?.override_address) return null
  return { host: diagnostics.override_host || 'this host', address: diagnostics.override_address }
}
