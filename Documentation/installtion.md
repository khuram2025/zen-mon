# ZenPlus Appliance — Installation Guide

Deploy a complete ZenPlus monitoring appliance on a fresh Ubuntu server with a
single command. The installer provisions every component — databases, poller,
dashboard, web server, OTA updater — and brings the appliance up with a 30-day
trial licence, ready to monitor immediately.

---

## 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 20.04 LTS | Ubuntu 22.04 or 24.04 LTS |
| Architecture | x86_64 / amd64 | x86_64 / amd64 |
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB+ |
| Disk | 20 GB free | 100 GB+ (metric history grows with device count) |
| Access | root / sudo | root / sudo |

**Outbound HTTPS** is required during installation to `github.com`, `go.dev`,
`deb.nodesource.com`, `download.docker.com` and Docker Hub. The installer
verifies all of these before it writes anything, and stops with a clear message
if any is unreachable.

The installer refuses to run on unsupported platforms rather than failing
halfway: non-Ubuntu distributions, 32-bit or ARM architectures, and hosts below
the RAM or disk minimums are all rejected during the preflight step.

---

## 2. Install

On a fresh Ubuntu server:

```bash
curl -fsSL https://zentryc.com/install.sh | sudo bash
```

Equivalent, straight from the source repository:

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

Installation takes **8–15 minutes** on a typical server — most of it compiling
the Go poller and building the dashboard.

### What you will see

Each phase reports a single verified line. A checkmark appears only after the
step's post-conditions actually hold:

```text
  ╔══════════════════════════════════════════════════════════╗
  ║   ZenPlus — Network Monitoring Appliance                  ║
  ║   Automated Installer                                    ║
  ╚══════════════════════════════════════════════════════════╝

  Host      mon-01 (10.20.30.40)
  Source    zen-mon.git branch main
  Target    /opt/zenplus
  Log       /var/log/zenplus-install.log

  This takes 8–15 minutes on a fresh server. Safe to re-run.

  [01/16] Validating host                            ✓ (2s)
  [02/16] Installing system packages                 ✓ (3m14s)
  [03/16] Creating service account                   ✓ (0s)
  [04/16] Fetching application source                ✓ (18s)
  [05/16] Generating configuration                   ✓ (0s)
  [06/16] Building application                       ✓ (5m02s)
  [07/16] Initialising databases                     ✓ (1m11s)
  [08/16] Provisioning trial licence                 ✓ (0s)
  [09/16] Configuring services                       ✓ (12s)
  [10/16] Installing TLS/security tools              ✓ (1s)
  [11/16] Configuring OTA updater                    ✓ (3s)
  [12/16] Installing support tooling                 ✓ (2s)
  [13/16] Installing management CLI                  ✓ (0s)
  [14/16] Finalising installation                    ✓ (1s)
  [15/16] Verifying appliance health                 ✓ (14s)
  [16/16] Collecting system report                   ✓ (1s)
```

followed by a summary, access details, the trial status, and next steps.

Everything each step prints is captured in **`/var/log/zenplus-install.log`**.
The console stays a clean checklist; the log holds the full detail.

### If a step fails

The installer stops at the first failure of a critical step, prints the last 25
lines of that step's output, and exits non-zero — it never reports success over
a broken install. Non-critical components (OTA updater, support tooling, TLS
helpers) degrade to a warning and the install continues.

Fix the reported cause and re-run the same command. The installer is
idempotent: existing credentials, data directories, the admin password and any
active subscription are all preserved.

---

## 3. First login

```text
Dashboard    http://<appliance-ip>
API docs     http://<appliance-ip>/docs
Sign in      admin / admin123
```

**Change the admin password immediately** — Settings → General → Profile.

Re-running the installer will *not* reset a password you have changed. It only
repairs the stored hash if it is missing or corrupt.

---

## 4. Trial licence

A **30-day trial is provisioned automatically** during installation:

| Entitlement | Trial |
|---|---|
| Devices | 50 |
| Service checks | 20 |
| Users | 5 |
| Duration | 30 days from install |

Every product feature is fully usable during the trial — SNMP, NetFlow, NCM,
APM, discovery, agents, sensors, reporting. Nothing is gated.

A licence key is required for one thing only: **receiving over-the-air
updates**. Until the appliance is registered, update checks return an error and
the Licenses tab shows "Appliance Not Registered".

To register, go to **Settings → General → Licenses**, paste the licence key
supplied with the subscription, and press Register. Verify with:

```bash
sudo zenplus status
```

Re-running the installer never resets or extends an existing subscription.

---

## 5. Enable HTTPS

A fresh appliance serves plain HTTP. For anything beyond a lab, enable TLS
before adding devices or agents — this secures the dashboard, the API, and all
agent/sensor traffic in one move.

Go to **Settings → General → Security** and either:

- **Generate a self-signed certificate** — immediate protection; download the
  certificate and distribute it to clients (browser trust store, or AD GPO to
  *Trusted Root Certification Authorities*) to avoid warnings; or
- **Generate a CSR** and have it signed by your enterprise CA (for Active
  Directory Certificate Services, use the *Web Server* template), then paste the
  issued certificate back; or
- **Upload** an existing PEM certificate + key, or a PKCS#12 / PFX bundle.

Then enable HTTPS, and optionally the HTTP→HTTPS redirect, HSTS, and a TLS
1.2+/1.3-only floor.

Switching to HTTPS changes the browser origin, so you will sign in again.
Agents and sensors keep the controller URL they enrolled with — re-point
existing ones to `https://` and install the certificate into their host trust
store. See `Documentation/25-SECURITY-TLS.md` for the full detail.

---

## 6. Verify the installation

```bash
sudo zenplus status
```

Expected active services:

```text
zenplus-wait-deps
zenplus-api
zenplus-poller
zenplus-updater.timer
nginx
postgresql
redis-server
```

ClickHouse runs in Docker and should report healthy:

```bash
docker ps --filter name=zenplus-clickhouse
```

```text
zenplus-clickhouse   Up 3 minutes (healthy)
```

Direct API health check:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/system/health
```

```json
{"status":"ok","service":"zenplus-api"}
```

Confirm the installed version — this must be a semantic version, since the OTA
server matches releases on it:

```bash
head -1 /opt/zenplus/.version
```

---

## 7. Managing the appliance

```bash
sudo zenplus status      # service and registration status
sudo zenplus update      # apply the latest release
sudo zenplus restart     # restart all services
sudo zenplus logs api    # tail the API log
sudo zenplus backup      # on-demand backup
```

Updates also arrive automatically: the OTA timer checks zentryc.com every 4
hours once the appliance is registered with a licence key.

---

## 8. Publishing the installer (operators only)

The public one-liner is served as a static file from the release server. It
**must not** require login, API auth, or Cloudflare Access.

Nginx, inside the `server { server_name zentryc.com; … }` block, before any
generic app proxy route:

```nginx
location = /install.sh {
    root /var/www/zentryc-public;
    default_type text/plain;
    add_header Cache-Control "public, max-age=300";
    add_header X-Content-Type-Options "nosniff" always;
    try_files /install.sh =404;
}
```

Publish or refresh the file:

```bash
sudo mkdir -p /var/www/zentryc-public
sudo curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh \
  -o /var/www/zentryc-public/install.sh
sudo chmod 0644 /var/www/zentryc-public/install.sh
sudo nginx -t && sudo systemctl reload nginx
```

Verify from another machine:

```bash
curl -fsSIL https://zentryc.com/install.sh          # expect HTTP 200, text/plain
curl -fsSL  https://zentryc.com/install.sh | head -1  # expect #!/usr/bin/env bash
```

> **This copy is not updated automatically.** It is a manual snapshot, so it
> drifts from the repository whenever the installer changes. Refresh it as part
> of every release, and confirm the served copy clones the intended branch:
>
> ```bash
> curl -fsSL https://zentryc.com/install.sh | grep ZENPLUS_BRANCH=
> ```

### Which version does a fresh install get?

`install.sh` clones the **`main`** branch and installs whatever `.version` that
branch carries. For customers to receive the current release, `main` must point
at the released commit — the release process therefore fast-forwards `main` as
part of cutting a release (see `scripts/release.sh`).

To install a specific branch or a fork without editing the script:

```bash
curl -fsSL https://zentryc.com/install.sh | sudo ZENPLUS_BRANCH=release-1.11 bash
```

Overridable variables: `ZENPLUS_BRANCH`, `ZENPLUS_REPO`, `ZENPLUS_HOME`,
`INSTALL_LOG`, `TRIAL_DAYS`, `MIN_RAM_MB`, `MIN_DISK_GB`.

---

## 9. Troubleshooting

**Where to look first** — `/var/log/zenplus-install.log` contains the complete
output of every step, with `===== [n/16] Step title =====` markers.

```bash
sudo zenplus status
sudo systemctl status zenplus-api --no-pager
sudo journalctl -u zenplus-api -n 120 --no-pager -l
sudo journalctl -u zenplus-poller -n 120 --no-pager -l
docker ps
```

### Preflight rejects the host

The message names the specific failure — unsupported OS, wrong architecture,
insufficient RAM or disk, or unreachable download hosts. These are hard
requirements; the install cannot proceed until they are met. Behind a proxy,
export `http_proxy` / `https_proxy` before running the installer.

### Login returns HTTP 500

Caused by a malformed stored password hash (`passlib UnknownHashError`). The
installer now detects and repairs this automatically on re-run. To fix by hand:

```bash
sudo -u postgres psql -d zenplus -c \
"UPDATE users SET password_hash = '\$2b\$12\$vjHI8XBgL.dCyn.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai', is_active = true WHERE username = 'admin';"
```

That restores the password to `admin123`. Note the escaped `$` — an unescaped
`$2b$` is expanded by the shell, which is what corrupts the hash in the first
place.

### Poller fails with status=203/EXEC

The binary was never built, historically because Go VCS stamping failed on a
"dubious ownership" repository. The installer marks `/opt/zenplus` safe, builds
with `-buildvcs=false`, and now **fails the install** if the binary is missing
rather than continuing. Manual repair:

```bash
sudo bash -lc 'set -e
export PATH=/usr/local/go/bin:/usr/local/bin:$PATH
git config --global --add safe.directory /opt/zenplus || true
mkdir -p /opt/zenplus/bin
cd /opt/zenplus/poller
go mod download
CGO_ENABLED=0 go build -buildvcs=false -o /opt/zenplus/bin/zenplus-poller ./cmd/poller
chown zenplus:zenplus /opt/zenplus/bin/zenplus-poller
chmod 0755 /opt/zenplus/bin/zenplus-poller
setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller || true
systemctl restart zenplus-poller'
```

### Settings → Security is unavailable

The privileged TLS helper is missing — expected on appliances installed before
the Security tab existed. Install it:

```bash
sudo bash /opt/zenplus/scripts/setup-security.sh
sudo systemctl restart zenplus-api
```

Fresh installs and OTA updates do this automatically.

### Update checks report "not registered"

Expected on an unlicensed appliance. Monitoring is unaffected; only OTA updates
require registration. Paste the licence key in Settings → General → Licenses.

---

## 10. What gets installed

| Component | Location |
|---|---|
| Application | `/opt/zenplus` |
| Configuration & secrets | `/opt/zenplus/.env` (0640, `zenplus`) |
| Version marker | `/opt/zenplus/.version` |
| PostgreSQL | system service, database `zenplus` |
| ClickHouse | Docker container `zenplus-clickhouse` |
| Redis | system service, password-protected |
| Web server | nginx → `/etc/nginx/conf.d/zenplus.conf` |
| TLS certificates | `/etc/zenplus/tls` (root-owned; key 0600) |
| Services | `zenplus-api`, `zenplus-poller`, `zenplus-updater.timer` |
| Management CLI | `/usr/local/bin/zenplus` |
| Install log | `/var/log/zenplus-install.log` |
