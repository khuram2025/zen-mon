# ZenPlus Installation Runbook

This note documents the working appliance installation flow using the main
remote domain `zentryc.com`.

## 1. Customer Install Command

The intended customer-facing one-liner is:

```bash
curl -fsSL https://zentryc.com/install.sh | sudo bash
```

The installer prepares the full ZenPlus appliance:

- installs OS packages, Docker, Go, Node.js, Python, Nginx, PostgreSQL, Redis
- starts ClickHouse through Docker Compose
- builds the Go poller and React dashboard
- creates systemd services
- installs the OTA updater
- configures the updater to use `https://zentryc.com`
- creates the management CLI at `/usr/local/bin/zenplus`

After installation, open:

```text
http://<appliance-ip>
```

Default login for a fresh one-line install:

```text
Username: admin
Password: admin123
```

Change the password after first login.

## 2. Remote Server Setup For `zentryc.com/install.sh`

The remote server team must expose the installer as a public HTTPS file at:

```text
https://zentryc.com/install.sh
```

This route must not require login, API auth, Cloudflare Access, or dashboard
session cookies.

### Nginx Example

Add this inside the existing `server { server_name zentryc.com; ... }` block,
before any generic app proxy route:

```nginx
location = /install.sh {
    root /var/www/zentryc-public;
    default_type text/plain;
    add_header Cache-Control "public, max-age=300";
    add_header X-Content-Type-Options "nosniff" always;
    try_files /install.sh =404;
}
```

Publish the installer:

```bash
sudo mkdir -p /var/www/zentryc-public
sudo curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh \
  -o /var/www/zentryc-public/install.sh
sudo chmod 0644 /var/www/zentryc-public/install.sh
sudo nginx -t
sudo systemctl reload nginx
```

Verify from another machine:

```bash
curl -fsSIL https://zentryc.com/install.sh
curl -fsSL https://zentryc.com/install.sh | head
```

Expected:

- HTTP status `200`
- content type `text/plain` or equivalent
- script begins with `#!/usr/bin/env bash`

## 3. Fresh Appliance Install

On a fresh Ubuntu appliance VM:

```bash
curl -fsSL https://zentryc.com/install.sh | sudo bash
```

If `zentryc.com/install.sh` is not published yet, use the GitHub fallback:

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

When complete, check health:

```bash
sudo zenplus status
```

Healthy services should show as active:

```text
zenplus-wait-deps
zenplus-api
zenplus-poller
zenplus-updater.timer
nginx
postgresql
redis-server
```

ClickHouse should show healthy in Docker:

```text
zenplus-clickhouse  Up ... (healthy)
```

## 4. Re-run On Existing Appliance

After the installer fix was pushed to `main`, an appliance can be re-run with:

```bash
curl -fsSL https://zentryc.com/install.sh | sudo bash
```

Fallback:

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

The installer is intended to preserve existing `.env`, data directories, and
updater registration where present.

After re-run:

```bash
sudo zenplus status
curl -i -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
```

The login test should return `HTTP/1.1 200 OK` on a default install.

## 5. OTA Registration

The appliance install only prepares the OTA agent. It does not register the
appliance automatically.

Register from the dashboard:

```text
Settings -> Subscription -> paste license key
```

After registration:

```bash
sudo zenplus status
```

Expected OTA section:

```text
registered as <appliance-id>
```

## 6. Fixes Included In Current Installer

The working installer fix was pushed to `origin/main` in commit:

```text
5d3f812 Fix appliance installer bootstrap failures
```

The fix covers two installation issues found during appliance testing.

### Poller Build Failure

Symptom:

```text
zenplus-poller.service: Main process exited, status=203/EXEC
ls: cannot access '/opt/zenplus/bin/zenplus-poller': No such file or directory
go build ... error obtaining VCS status
```

Root cause:

Go VCS stamping failed because Git considered `/opt/zenplus` a dubious
ownership repository. The installer continued even though no poller binary was
created.

Fix:

- marks `/opt/zenplus` as a safe Git directory for root and `zenplus`
- builds the poller with `go build -buildvcs=false`
- fails the installer if the poller binary is not created

Manual repair if seen on an older appliance:

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
systemctl reset-failed zenplus-poller
systemctl restart zenplus-poller
systemctl status zenplus-poller --no-pager
'
```

### Login 500 From Corrupt Admin Hash

Symptom:

```text
POST /api/v1/auth/login HTTP/1.1 500 Internal Server Error
passlib.exc.UnknownHashError: hash could not be identified
```

Root cause:

The installer generated a bcrypt hash, then passed it through an inner shell.
The `$2b$12...` prefix was expanded by the shell and corrupted before writing
to PostgreSQL.

Fix:

- writes the admin hash through `runuser -u postgres -- psql ...` without the
  extra shell expansion
- makes malformed stored password hashes fail authentication cleanly instead
  of returning API `500`

Manual repair if seen on an older appliance:

```bash
set -euo pipefail
source /opt/zenplus/.env
PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U zenplus -d zenplus -c \
"UPDATE users SET password_hash = '\$2b\$12\$vjHI8XBgL.dCyn.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai', is_active = true WHERE username = 'admin';"
```

Then test:

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
```

## 7. Support Checks

Useful commands during install support:

```bash
sudo zenplus status
sudo systemctl status zenplus-api --no-pager
sudo systemctl status zenplus-poller --no-pager
sudo journalctl -u zenplus-api -n 120 --no-pager -l
sudo journalctl -u zenplus-poller -n 120 --no-pager -l
docker ps
```

Check installer availability:

```bash
curl -fsSIL https://zentryc.com/install.sh
```

Check dashboard/API locally on the appliance:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/system/health
```

Expected:

```json
{"status":"ok","service":"zenplus-api"}
```
