# ZenPlus 1.23.6 — service certificate trust

HTTPS monitoring now recovers a missing public intermediate certificate when
the service sends an incomplete chain. The chain must still verify against a
system root or a certificate explicitly installed by an administrator. Names,
validity dates, signatures and server authentication usage remain checked.

Settings → General → Security → Service probe trust provides:

- Automatic intermediate recovery, enabled by default and configurable.
- Public CA certificate upload in PEM or DER format for internal PKI.
- Self-signed service certificate upload with required exact DNS/IP scopes
  matching its subject alternative names.
- Optional host scopes for CA certificates, fingerprints, expiration dates,
  removal and administrative audit events.

Prefer installing the internal root CA rather than individual service leaves
when an organization operates its own PKI. This allows normal service
certificate renewal without replacing trust entries. An unscoped CA applies
to all monitored services; use exact scopes to restrict its authority.
Never upload a private key to the probe trust section.

Scheduled central checks and interactive HTTP/TLS tests use the same Go
verifier. Interactive HTTP tests pin the verified leaf for their subsequent
connection, with a separate SSL context per origin. Trust changes apply on the
next central configuration refresh and the next interactive test. The policy
is included in authenticated sensor configuration and its ETag. The included
signed sensor 1.23.6 binary supports this policy; update existing sensors from
their management page to use it.

Intermediate downloads allow public HTTP/HTTPS URLs on ports 80/443 only,
with DNS addresses validated and pinned before connecting. Private, loopback,
link-local and reserved destinations, redirects and proxy inheritance are
blocked. Retrieval is bounded by probe deadlines, four issuer attempts,
three seconds per request, 64 KiB per certificate and a 64-entry one-hour
cache. Downloaded certificates are never promoted into trusted roots.
Private AIA endpoints and PKCS#7 issuer responses require a manual CA upload.

This does not alter the appliance's incoming HTTPS certificate, install
certificates into the host-wide OS store, or disable TLS verification. The
existing per-check verification bypass remains an explicit separate option.
No database migration is required; public certificates and policy persist in
the existing system settings table and survive normal updates and restarts.

Validation includes strict chain-recovery and scoped-trust Go tests with the
race detector, Python certificate/API/transport tests, sensor contract tests,
dashboard route smoke and a production Vite build. A live regression using
the supplied SMO leaf certificate confirms failure without recovery and
successful strict verification after fetching its DigiCert intermediate.
