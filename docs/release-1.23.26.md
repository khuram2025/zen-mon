# ZenPlus 1.23.26 — NCCM SSH and Device Tracker search fixes

## Release scope and source

Appliance-only cumulative maintenance release based on the exact clean source
checkout used for published 1.23.25 (`b037398c61f4f298d5d0ca9b1b2139f43bf8965d`).
GitHub main currently records 1.23.19. The release branch preserves the already
published 1.23.25 features; its pull request targets that release baseline.
Existing separately installed agent packages are preserved.

Minimum installed prerequisite: **1.23.9**, unchanged from the release policy.
No new operating-system or signing-key bridge is introduced. The signed
prerequisite path and rollout eligibility must be checked before promotion.

## Changes

- NCCM classifies underlying SSH negotiation, authentication, missing trust,
  changed host keys and unreachable sockets through wrapped exceptions.
  Permanent failures stop retries; real timeouts remain retryable.
- Host-key negotiation selects independently verified key types. RSA trust
  permits RSA SHA2 signatures without treating an unverified Ed25519 key as a
  changed RSA key. Multiple verified algorithms can coexist for one host/port.
- Legacy RSA SHA1 signatures and SHA1 key exchange require explicit SSH
  compatibility on the connection profile. The default remains disabled.
- Fortinet read-only collection retains its non-mutating command behavior and
  modern negotiated algorithms. Host-key verification instructions are fixed.
- Device Tracker recognizes IP addresses before MAC normalization, fixing
  `192.168.100.101`. Current and historical IPv4/IPv6 observations are searchable.
- MAC search accepts colon, hyphen, Cisco dotted and compact formats and prefixes.
  Hostname/vendor/user matching is case-insensitive and wildcard characters are
  literal. Current and historical usernames/domains require existing user-view
  permissions; results retain identity redaction for other callers.
- Search applies after a short typing delay, supports Search/Clear buttons,
  synchronizes with URL changes, and displays API failures with Retry.

## Schema and recovery

Append-only migration `migrate-151-ncm-ssh-compatibility.sql` adds
`ncm_credentials.ssh_legacy_compat` with a false default, and changes the active
host-key uniqueness key to `(host, port, key_type)`. Existing credentials and
verified keys are retained. The complete historical migration ledger is shipped.
The migration is idempotent and was applied twice in a disposable fixture.

Use the normal updater's code/database backup and restore procedure on failure.
The migration is additive; earlier code can run with the extra column/index.
No device restore is requested or used for release testing.

## Verification

- Strict dashboard TypeScript/Vite build passed. Production dependency audit
  has no high/critical vulnerabilities (one existing low DOMPurify advisory).
- 105 Python regressions passed on the appliance, including UDT database
  searches, current/history identities, permission gating, literal wildcards,
  IPv6 canonicalization and all MAC formats.
- SSH loopback fixtures passed for legacy negotiation, trusted RSA on a
  multiple-key server, actual key rotation, paged Fortinet read-only capture,
  nested permanent errors and genuine timeouts.
- Live UDT API/UI on 192.168.8.221 returns the requested IP with MAC
  `8c:16:45:2f:0a:a1`; alternate MAC formats, MAC prefix and vendor queries pass.
  The live inventory has no user identities; user search is verified with
  isolated database fixtures, not fabricated live records.
- All 12 requested lab devices are up with fresh NCCM state, successful
  first-attempt jobs and 24 encrypted, validated running/startup captures.
  Scheduled collection is enabled every 24 hours. Restore recovery was not tested.

Signed package, source commit, CI, canary update and publication evidence are
recorded separately after each gate completes.
