# 25 — Security Tab: TLS / HTTPS Hardening

Settings → General → **Security** lets an administrator encrypt all access to the
appliance — the web dashboard, the API, and every enrolled agent/sensor that talks
to the controller URL — and harden the TLS posture.

## What it provides

| Capability | Detail |
| --- | --- |
| Self-signed certificate | Generated on-appliance (RSA 2048/4096 or ECDSA P-256), installed and served immediately. Downloadable for distribution to trust stores (browser / AD GPO / agent hosts). |
| Enterprise CA / AD CS | CSR workflow: the private key never leaves the appliance; the CSR is submitted to the CA (AD CS *Web Server* template), and the issued certificate + chain are pasted back and paired with the kept key. |
| PEM upload | Install an existing certificate + private key (+ optional chain); encrypted keys supported via passphrase. |
| PFX / P12 upload | Install a PKCS#12 bundle (the standard AD CS / Windows export format), including any bundled chain. |
| HTTPS enable/disable | nginx serves :443 with the installed certificate. |
| HTTP → HTTPS redirect | Port 80 returns 301 (the `/health` probe stays on HTTP for load balancers). |
| HSTS | `Strict-Transport-Security: max-age=15768000` on the HTTPS site. |
| Minimum TLS version | TLS 1.2+ (Mozilla-intermediate ciphers) or TLS 1.3-only. |

## Architecture

```
dashboard SecurityTabContent.tsx
        │  /api/v1/system/security/tls/...
        ▼
server/app/api/v1/security_settings.py     (runs as zenplus; admin-only + audit log)
        │  key/cert generation + parsing (python-cryptography)
        │  stages material at /opt/zenplus/data/tls-staging  (0700 zenplus)
        ▼  sudo -n /usr/local/sbin/zenplus-security-helper <subcommand>
zenplus-security-helper                     (root; the security boundary)
        ├─ install-cert   validate staged pair → /etc/zenplus/tls/{server.key,server.crt,chain.crt,fullchain.pem}
        ├─ apply …        regenerate nginx site config from embedded template → nginx -t → reload
        ├─ remove-cert    refuse while HTTPS is on
        └─ status         JSON of installed cert + applied state (/etc/zenplus/tls/state.json)
```

Key security properties:

- The sudo grant (`/etc/sudoers.d/zenplus-security`, installed by
  `scripts/setup-security.sh` with `visudo -cf` validation) points only at the
  **root-owned installed copy** in `/usr/local/sbin`, never at the repo copy.
- The helper accepts **no paths and no config text** from the API: certificates are
  read from fixed staging paths, and the nginx config is generated inside the helper
  from four validated flags (`https/redirect/hsts/min_tls`). The API cannot use the
  grant to install arbitrary files or inject nginx directives.
- nginx changes are transactional: the previous config is restored if `nginx -t`
  fails, so a bad apply can never take the web server down.
- `server.key` is 0600 root; the staged copy is deleted after install (and on failure).
- Desired settings live in `system_settings` key `security.tls`; the actually applied
  state is written by the helper to `/etc/zenplus/tls/state.json`, and the status
  endpoint reports both.

## Endpoints

All under `/api/v1/system/security`, admin-only, audit-logged
(`security.tls.*` actions):

- `GET /tls` — status: helper installed, settings, applied state, cert details, pending CSR
- `PUT /tls/config` — apply `{https_enabled, redirect_http, hsts_enabled, min_tls_version}`
- `POST /tls/self-signed`, `POST /tls/csr`, `DELETE /tls/csr`
- `POST /tls/certificate` (PEM; also accepts the CA answer to a pending CSR),
  `POST /tls/pfx` (multipart), `DELETE /tls/certificate`
- `GET /tls/certificate/download` — public cert PEM (any authenticated user)

## Deployment

- Fresh activation on an existing appliance: `sudo bash scripts/setup-security.sh`
  (the UI shows this instruction while the helper is missing).
- OTA: `build-release.py` ships `setup-security.sh` as a `run_hook`, so updated
  appliances get the helper + sudoers automatically. No DB migration is needed
  (settings ride the existing `system_settings` KV table).
- Config backups include the public cert, chain, state and nginx site config —
  **deliberately not** the private key (backup tarballs are downloadable); after a
  bare-metal restore, re-issue or re-upload the certificate.

## Agents & sensors

Host agents (`ZENPLUS_CONTROLLER_URL` in `/etc/zenplus-agent/agent.env`) and sensors
keep whatever controller URL they enrolled with. After enabling HTTPS:

- newly generated install commands use HTTPS automatically (the server URL fallback
  follows `X-Forwarded-Proto`, which becomes `https` once the redirect is on);
- existing agents should be re-pointed to `https://…` and, for self-signed
  certificates, the downloaded cert must be added to the host trust store.

## Gotchas

- Enabling HTTPS moves the browser to a new origin → operators are logged out once
  (JWT lives in per-origin storage). The UI warns and auto-navigates.
- HSTS with a still-untrusted self-signed cert makes browser warnings non-bypassable
  on some browsers — the UI hints to enable HSTS only after trust is distributed.
- nginx 1.24 (current appliance) has no `http2 on;` directive — the helper detects
  the version and only emits it on ≥ 1.25.1.
- The helper writes whichever nginx config is active: `sites-available/zenplus`
  (hand-installed appliances) or `conf.d/zenplus.conf` (installer default). Its
  embedded location template must be kept in sync with `install.sh` when proxy
  locations change.
