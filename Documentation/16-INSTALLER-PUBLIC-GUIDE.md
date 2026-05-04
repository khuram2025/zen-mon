# ZenPlus — One-Line Installer & Knowledge Base

> **Audience.** The remote-server (zentryc.com) team that publishes customer-
> facing documentation, plus the support engineers who answer the first
> install question. This is the single source of truth for "what does the
> one-liner do, what does it require, what does it leave on the box, and how
> do future updates flow." Copy-paste sections directly into the public
> knowledge base; nothing in this document is internal-only.
>
> **Status.** Reflects `install.sh` as of v1.2.3 with the OTA-bundle fixes
> applied (sudoers, polkit, updater systemd units, `NoNewPrivileges=false`
> on the API, migration-aware `zenplus update`). After running the one-
> liner, the system is fully wired for future OTA updates without any
> follow-up scripts.

---

## 1. At a glance

| Property | Value |
|---|---|
| Distribution model | Single Bash installer fetched over HTTPS |
| Supported OS | Ubuntu 22.04 LTS or 24.04 LTS (x86_64). Debian 12 works but is unsupported. |
| Disk footprint | ~2 GB after install, grows with metric retention |
| Install time | 5–10 minutes on a clean VM (mostly pip / npm / Go fetches) |
| Networking | Outbound HTTPS to GitHub + zentryc.com + apt/npm/pip mirrors |
| First boot result | A working dashboard at `http://<server-ip>` and an OTA-armed appliance |
| Future updates | Automatic via OTA every ~4 hours after license registration. No SSH, no manual scripts. |

---

## 2. The one-liner

The customer runs **one command** on a fresh Ubuntu server:

```bash
curl -fsSL https://install.zentryc.com | sudo bash
```

That's the entire customer-facing surface. The URL is owned by the
remote-server team (see §10 for hosting). Until the cutover, the
identical script lives at:

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

When the install finishes the screen prints the dashboard URL, the
admin password, and the management CLI hint. **The customer needs no
other steps to receive future updates** — the OTA agent is armed at the
end of the same script.

### What the customer types after installing

```bash
sudo zenplus status     # Full health + OTA registration status
sudo zenplus update     # Manual local update (rare; OTA does this automatically)
sudo zenplus logs api   # Tail API logs (also: poller, updater)
```

### What the customer enters in the dashboard

After login (`admin` / `admin123`, prompt to change), they paste
exactly **one** value: the license key issued by the zentryc.com admin.
Settings → Subscription → "Enter license key". From that moment the
appliance is registered with zentryc.com and OTA updates flow.

---

## 3. Prerequisites

### 3.1 Hardware / VM sizing

| Resource | Minimum | Recommended | Why |
|---|---|---|---|
| vCPU | 2 | 4 | Go poller is concurrent; ClickHouse benefits from cores |
| RAM | 4 GB | 8 GB | ClickHouse + Postgres + FastAPI + Node build |
| Disk | 20 GB | 100 GB | 30-day raw metrics + 90-day rollups + logs |
| Network | 1 Gbps internal | — | Poller pings up to 10K devices/sec on a single box |

### 3.2 Operating system

- **Ubuntu 22.04 LTS** or **Ubuntu 24.04 LTS**, x86_64. Server install
  preferred; desktop works but ships unused packages.
- A user with `sudo` rights (cloud-init `ubuntu` user is fine).
- The server must be **fresh** — no other Postgres, ClickHouse, Redis,
  Nginx, or Docker installation. The installer is idempotent and will
  reuse what it finds, but conflicts (e.g., port 80 already bound, an
  existing `postgres` cluster with a `zenplus` role) require manual
  cleanup first.

### 3.3 Outbound network access (egress)

The installer and the appliance both need outbound HTTPS. **No inbound
ports are required from the public internet** (port 80 is local-only;
expose it through your own load balancer if needed).

| Destination | Port | Purpose | When |
|---|---|---|---|
| `github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com` | 443 | Source code (`git clone`) | Install + each `zenplus update` |
| `get.docker.com`, `download.docker.com` | 443 | Docker engine | Install only |
| `go.dev`, `dl.google.com` | 443 | Go toolchain | Install only |
| `deb.nodesource.com`, `nodejs.org` | 443 | Node.js | Install only |
| `archive.ubuntu.com`, `*.ubuntu.com` | 443 / 80 | apt | Install + apt OTA steps |
| `pypi.org`, `files.pythonhosted.org` | 443 | pip | Install + each update |
| `registry.npmjs.org` | 443 | npm | Install + each update |
| `hub.docker.com`, `registry-1.docker.io` | 443 | ClickHouse image | Install + image upgrades |
| **`zentryc.com`** | **443** | OTA channel (registration, checkin, download, report) | Continuously after registration |

If the customer terminates TLS at their own proxy, that proxy must
allow streaming responses on `zentryc.com` (the agent uses chunked
HTTPS for resumable downloads — buffering proxies break this).

### 3.4 Inbound (LAN) ports

| Port | Listener | Audience |
|---|---|---|
| 80 | Nginx → dashboard + reverse-proxied API | End users on the customer LAN |
| 8000 | FastAPI (loopback only via `127.0.0.1` if firewalled) | Internal — proxied by Nginx |
| 8081 | Go poller `/health` | Internal monitoring |
| 5432, 6379, 8123, 9000 | Postgres / Redis / ClickHouse | Loopback only |

The customer firewall only needs **80/tcp inbound** opened, and only
on the LAN side. The OTA channel does not require any inbound port.

---

## 4. What the installer does, step by step

The installer is a single Bash script that runs nine numbered steps.
Each step is idempotent; re-running the one-liner over an existing
install upgrades in place without touching customer data.

| # | Step | What it does | Re-run safe? |
|---|---|---|---|
| 1 | **Prerequisites** | `apt-get install` core packages; install Docker, Go 1.22, Node.js 20; enable `net.ipv4.ping_group_range` for non-root ICMP | Yes |
| 2 | **System user** | Creates `zenplus` system user, joins `docker` group, sets `/opt/zenplus` ownership | Yes |
| 3 | **Fetch code** | `git clone` (or `git fetch && reset --hard origin/main`) into `/opt/zenplus`; preserves `.env` + `data/` + `backups/` | Yes |
| 4 | **Environment** | Generates random Postgres / ClickHouse / Redis / JWT secrets into `/opt/zenplus/.env` (mode 0640). Existing values are preserved on re-run. | Yes |
| 5 | **Build components** | Builds the Go poller (with `cap_net_raw`), creates the Python venv, installs requirements, runs `vite build` for the React dashboard | Yes |
| 6 | **Databases** | Configures Postgres user/database; runs `init-postgres.sql`, `seed-devices.sql`, all `migrate-*.sql`; brings ClickHouse up via Docker Compose; runs `init-clickhouse.sql`, `fix-clickhouse.sql`, all `migrate-*-clickhouse.sql`; sets the `admin` password to `admin123` | Yes |
| 7 | **Systemd services** | Writes `zenplus-wait-deps.service`, `zenplus-api.service`, `zenplus-poller.service`, the Nginx site, `zenplus-wait-deps.sh`; reloads systemd; enables + starts everything | Yes |
| 8 | **OTA agent** | Installs `zenplus-updater.service` + `.timer`, the polkit rule, `/etc/sudoers.d/zenplus-updater`, the default `agent.conf`, then enables and arms the timer (first check 5 min after boot, then every 4 h ±5 min) | Yes |
| 9 | **CLI** | Drops the `zenplus` management CLI at `/usr/local/bin/zenplus` | Yes |

### Why each piece exists

- **Step 7's `wait-for-deps`** — Postgres / Redis / ClickHouse boot
  asynchronously. Without an explicit gate the API and poller race the
  databases on every reboot. The wait service blocks until all three
  answer health checks, then `Requires=` fans out to API + poller.
- **Step 8's polkit + sudoers** — the dashboard ("Settings → Updates")
  triggers two privileged operations: starting the updater oneshot, and
  writing a systemd timer drop-in to change the check interval.
  These flow through `sudo -n /bin/systemctl ...` from the FastAPI
  process. The sudoers rule in `/etc/sudoers.d/zenplus-updater`
  enumerates exactly six allowed invocations — nothing else.
- **The `cap_net_raw` capability on `bin/zenplus-poller`** — lets the
  Go poller send ICMP echoes without running as root.

---

## 5. What gets installed (filesystem layout)

```
/opt/zenplus/
├── .env                          # Generated secrets (mode 0640, owned zenplus:zenplus)
├── .version                      # Short SHA + ISO timestamp of last upgrade
├── bin/
│   ├── zenplus-poller            # Go binary (cap_net_raw)
│   ├── wait-for-deps.sh
│   ├── first-boot-init.sh        # OVA-only path; no-op on apt installs
│   └── expand-storage.sh         # /data LVM growth
├── server/                       # FastAPI source
├── poller/                       # Go source
├── dashboard/dist/               # Nginx-served React build
├── scripts/                      # SQL migrations, seed data, build-release.py
├── updater/
│   ├── config/agent.conf         # OTA agent config (mode 0600)
│   ├── keys/zentryc-release.pub  # Ed25519 release-signing public key
│   ├── logs/update.log           # Rotating, 10 MB max
│   ├── backups/                  # Pre-update tarballs (max 3)
│   ├── systemd/                  # Unit files copied to /etc/systemd/system/
│   └── polkit/                   # Polkit rules copied to /etc/polkit-1/
└── venv/                         # Python virtualenv

/etc/systemd/system/
├── zenplus-wait-deps.service
├── zenplus-api.service
├── zenplus-poller.service
├── zenplus-updater.service
└── zenplus-updater.timer

/etc/nginx/conf.d/zenplus.conf    # Reverse proxy + static
/etc/polkit-1/rules.d/50-zenplus-updater.rules
/etc/sudoers.d/zenplus-updater    # Mode 0440, validated by visudo
/usr/local/bin/zenplus            # Management CLI
/usr/local/go/bin/go              # Go toolchain
/var/log/zenplus/                 # First-boot log (when shipped as OVA)
/var/lib/zenplus/.initialized     # First-boot sentinel (OVA only)
```

### System user

A single `zenplus` system user owns everything. It is in the `docker`
group (so the API can talk to ClickHouse) and is granted the polkit +
sudoers entries described in §6. It has no login shell beyond
maintenance and is not exposed to SSH.

### Default credentials

| Where | Username | Password | Notes |
|---|---|---|---|
| Dashboard | `admin` | `admin123` | Forced rotation on first login. |
| Postgres | `zenplus` | (random, in `/opt/zenplus/.env`) | Loopback only; no remote auth. |
| ClickHouse | `default` | (random, in `/opt/zenplus/.env`) | Loopback only via Docker port-bind to `127.0.0.1`. |
| Redis | (no user) | (random, in `/opt/zenplus/.env`) | `requirepass` only; loopback only. |

The customer changes only the dashboard password. The database
passwords are managed by the system; they are written to `.env` once
and consumed by every service from there.

---

## 6. Permission and privilege model

This section is the answer to "why doesn't anything need a follow-up
script". Every privilege the system needs is granted by the installer
itself, narrowly scoped, and validated by `visudo`.

### 6.1 The Go poller and ICMP

The poller runs as the `zenplus` user, not root. ICMP normally
requires root or the `CAP_NET_RAW` capability. The installer:

1. Sets `net.ipv4.ping_group_range = 0 2147483647` (persisted in
   `/etc/sysctl.d/99-zenplus-ping.conf`) so unprivileged ICMP is
   allowed for any GID.
2. Applies `setcap cap_net_raw+ep` to `/opt/zenplus/bin/zenplus-poller`
   so the binary itself carries the capability.
3. Sets `AmbientCapabilities=CAP_NET_RAW` in
   `zenplus-poller.service` for systemd-driven runs.

Either of (1) or (2) alone is sufficient on modern kernels; we apply
both for resilience against a kernel that disables one path.

### 6.2 The FastAPI service

`zenplus-api.service` runs as the `zenplus` user with:

- `NoNewPrivileges=false` — required so `subprocess.run(["sudo", ...])`
  in `system_updates.py` retains the setuid bit.
- `PrivateTmp=true` — isolates `/tmp` per service.
- `Environment=MPLCONFIGDIR=/tmp/matplotlib` — matplotlib writes a
  font cache; this keeps it out of the read-only home.

### 6.3 Sudoers entries (the complete list)

The installer writes `/etc/sudoers.d/zenplus-updater`, mode `0440`,
validated by `visudo -cf` before being kept:

```
zenplus ALL=(root) NOPASSWD: /bin/systemctl --no-block start zenplus-updater.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl start zenplus-updater.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl restart zenplus-updater.timer
zenplus ALL=(root) NOPASSWD: /bin/systemctl daemon-reload
zenplus ALL=(root) NOPASSWD: /bin/mkdir -p /etc/systemd/system/zenplus-updater.timer.d
zenplus ALL=(root) NOPASSWD: /usr/bin/tee /etc/systemd/system/zenplus-updater.timer.d/override.conf
```

Each line corresponds 1:1 with a `subprocess.run()` call in
`server/app/api/v1/system_updates.py`. Broaden these only when adding
a new privileged operation to the dashboard, and only after updating
the API code.

### 6.4 Polkit rule

For polkit ≥ 0.106 the installer drops `50-zenplus-updater.rules`
into `/etc/polkit-1/rules.d/`. For older polkit it drops a `.pkla`
into `/etc/polkit-1/localauthority/50-local.d/`. Both have identical
intent: allow the `zenplus` user to call
`org.freedesktop.systemd1.manage-units` for `zenplus-updater.service`
and `zenplus-updater.timer` only.

### 6.5 The OTA agent

`zenplus-updater.service` is a `Type=oneshot` unit running as
`User=root`. It has to be root because manifest steps can install apt
packages, drop `/etc/sysctl.d/` files, and reload systemd. The
mitigations are (a) every manifest is Ed25519-signed, (b) every file
in the package is SHA-256-verified, (c) a hardcoded protected-package
list refuses to remove `postgresql`, `nginx`, `clickhouse-server`,
`systemd`, `libc6`, etc., (d) backups are taken before any apply, and
(e) any step failure triggers automatic rollback.

---

## 7. How future updates work

There are **two** update channels. Both leave through the same
`zenplus-api` and `zenplus-poller` units and end with the same
`zenplus update` outcome. They are not redundant — they serve
different roles.

### 7.1 OTA — the customer-facing channel

This is what 99% of customers will see. After they paste their
license key the appliance registers with zentryc.com and the
`zenplus-updater.timer` ticks every 4 hours (±5 min jitter, first
tick 5 min after boot).

```
[timer fires] → checkin POST /api/v1/appliances/checkin
              ← {"next_action": "update", "release": {...}}
              → download .zup (resumable, SHA-256-verified)
              → verify Ed25519 signature on manifest.json
              → take pre-update backup (code tarball + pg_dump)
              → run manifest steps in order:
                  apt_install / install_systemd / install_config /
                  apply_code / pip_install / run_migration / run_hook /
                  service_control / health_check
              → on any step failure, run rollback_steps (restore_backup,
                start_services) and report status=failed
              → on success, write /opt/zenplus/.version and report
                status=success with the new version
```

The customer never sees this run. The dashboard's "Updates" tab shows
the new version and changelog after it lands. If three consecutive
attempts at the same release fail, the agent backs off until a newer
release is published.

### 7.2 Local — the `zenplus update` CLI

The `sudo zenplus update` command exists for two cases:

1. **Air-gapped deployments** that can't reach zentryc.com.
2. **Operator overrides** when ops needs to pull `main` immediately
   without waiting for the next OTA tick.

It does the same shape of work as an OTA release, just sourced from
GitHub directly:

```
git fetch && git reset --hard origin/main
build poller (Go) + dashboard (vite) + python deps (pip)
run scripts/migrate-*.sql (Postgres)        ← idempotent CREATE/ALTER
run scripts/migrate-*-clickhouse.sql        ← via docker exec
copy any updated systemd unit files into /etc/systemd/system/
systemctl daemon-reload
systemctl restart zenplus-api zenplus-poller nginx zenplus-updater.timer
write /opt/zenplus/.version
```

The migrations and `daemon-reload` are why a release that ships
schema changes or a new unit file no longer requires SSHing in. Both
were missing from older versions of the script and have been
explicitly fixed.

### 7.3 What "I pushed an update that needs a service restart" requires

Nothing extra on the appliance. Both channels handle it:

- An **OTA release** lists the affected service in the manifest's
  `service_control` step (`{"type": "service_control", "action":
  "restart", "name": "zenplus-api"}`). The agent runs it under root
  in the order specified.
- A **`zenplus update`** run unconditionally restarts
  `zenplus-api`, `zenplus-poller`, `nginx`, and the updater timer
  after the build phase. It also `daemon-reload`s before restart so
  any new unit file is picked up.

There is no path where a normal update gets stuck on "I need to
restart a service but the running API doesn't have permission" —
that's exactly what the sudoers + polkit configuration in §6 prevents.

---

## 8. Operational reference

### 8.1 The `zenplus` CLI

```
sudo zenplus status     # Service health + Docker + OTA registration + dashboard URL
sudo zenplus start      # Bring everything up (databases first, then app, then OTA timer)
sudo zenplus stop       # Stop poller + API (databases stay up)
sudo zenplus restart    # Restart api + poller + nginx
sudo zenplus update     # Pull main, migrate, rebuild, daemon-reload, restart
sudo zenplus backup     # gzip pg_dump → /opt/zenplus/backups/zenplus-<ts>.sql.gz
sudo zenplus logs api   # journalctl -u zenplus-api -f. Also: poller, updater
```

### 8.2 Files an operator might need to read

| File | What's there |
|---|---|
| `/opt/zenplus/.env` | All generated secrets |
| `/opt/zenplus/.version` | Current short SHA + last-update ISO timestamp |
| `/opt/zenplus/updater/config/agent.conf` | OTA agent config; `[appliance]` populated after registration |
| `/opt/zenplus/updater/logs/update.log` | OTA agent log (rotating, 10 MB max) |
| `/var/log/zenplus/first-boot.log` | First-boot init log (OVA path only) |
| `journalctl -u zenplus-api` | API logs |
| `journalctl -u zenplus-poller` | Poller logs |
| `journalctl -u zenplus-updater` | Last OTA run logs |

### 8.3 Health endpoints

| URL | Returns |
|---|---|
| `http://<host>/api/v1/system/health` | API + DB + Redis health JSON |
| `http://localhost:8081/health` | Poller health |
| `docker exec zenplus-clickhouse clickhouse-client --query 'SELECT 1'` | ClickHouse |

---

## 9. Default behaviors customers should know about

- The first login forces a password change. Document this in the
  welcome email — it is not a bug.
- A 30-day trial subscription is seeded automatically (50 devices /
  20 service checks / 5 users). The customer can monitor immediately
  without entering a license key. They only need a license key to
  receive OTA updates.
- The dashboard auto-refreshes via SSE (Server-Sent Events). Any
  TLS-terminating proxy in front of the appliance must allow
  streaming responses on `/api/v1/*` (no buffering, no rewriting).
- Raw ping metrics retain for 30 days, 5-minute rollups for 90 days,
  1-hour rollups for 1 year. These are TTLs on ClickHouse tables and
  cannot be tuned without an OTA release that ships a new migration.

---

## 10. Hosting the installer on zentryc.com

The remote-server team owns the public URL. Customer-facing material
should reference `https://install.zentryc.com` (or `get.zentryc.com`,
the team's choice — pick one and stick with it). Two valid setups:

### 10.1 Static-redirect setup (simplest)

Have Nginx on `install.zentryc.com` issue a 302 to the GitHub raw URL.
This requires no storage and means a `git push` to `main` is the only
release path:

```nginx
server {
    listen 443 ssl http2;
    server_name install.zentryc.com;

    # ... TLS config ...

    location = / {
        return 302 https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh;
    }
}
```

`curl -fsSL` follows the redirect, so the customer command works
unchanged.

### 10.2 Pinned-version setup (recommended for production)

Host a copy of `install.sh` directly on zentryc.com so you control
the byte-exact version every customer downloads. Update on a release
cadence rather than every commit:

```nginx
server {
    listen 443 ssl http2;
    server_name install.zentryc.com;

    root /var/www/install.zentryc.com;
    add_header Cache-Control "public, max-age=300";
    add_header X-Content-Type-Options "nosniff" always;
    add_header Strict-Transport-Security "max-age=63072000" always;

    location = / {
        try_files /install.sh =404;
        default_type text/plain;
    }
    location = /install.sh {
        try_files /install.sh =404;
        default_type text/plain;
    }
}
```

To publish a new version: `scp install.sh root@zentryc-host:/var/www/install.zentryc.com/install.sh`.

> **Always serve over HTTPS.** A `curl ... | sudo bash` over plain
> HTTP is hijackable by any on-path attacker. Customers should also
> be encouraged to inspect the script before piping to bash:
> `curl -fsSL https://install.zentryc.com -o /tmp/install.sh && less /tmp/install.sh && sudo bash /tmp/install.sh`.

### 10.3 Public knowledge-base entry (suggested copy)

Use this as the body of the `https://docs.zentryc.com/install` page:

> **Install ZenPlus on Ubuntu 22.04+ in one command.**
>
> Copy and paste the following into a terminal as a user with sudo:
>
> ```bash
> curl -fsSL https://install.zentryc.com | sudo bash
> ```
>
> The installer takes 5–10 minutes. When it finishes you'll see the
> dashboard URL printed to the terminal. Open it in your browser and
> log in with `admin` / `admin123` — you'll be prompted to change
> the password on first login.
>
> To enable automatic updates, go to **Settings → Subscription** and
> paste the license key your account manager provided. From that
> moment your appliance will check for updates every four hours and
> apply signed releases automatically.
>
> **Requirements**
> - Ubuntu 22.04 LTS or 24.04 LTS, x86_64
> - 4 GB RAM minimum (8 GB recommended)
> - 20 GB disk minimum (100 GB recommended)
> - Outbound HTTPS (port 443) to `zentryc.com`, `github.com`,
>   `pypi.org`, `npmjs.org`, `hub.docker.com`
>
> **What gets installed**
>
> Docker, Go 1.22, Node.js 20, Python 3, PostgreSQL, Redis, Nginx,
> ClickHouse (in Docker). The installer creates a dedicated
> `zenplus` system user, configures all services to start on boot,
> and arms the OTA update agent. No other software on the server
> is modified.
>
> **Need help?** See [Troubleshooting](#troubleshooting) or open a
> ticket at support@zentryc.com.

---

## 11. Troubleshooting

Each row maps to a real customer report. Symptom → cause → command
→ what success looks like.

### 11.1 "The installer hangs at apt-get update"

Cause: customer firewall blocks `archive.ubuntu.com`. Confirm with
`curl -I http://archive.ubuntu.com/`. Resolution: open egress per
§3.3 or switch the apt mirror to a customer-internal one before
running the installer.

### 11.2 "Port 80 already in use"

Cause: Apache, another nginx, or a load balancer is bound to 80.
`sudo ss -ltnp | grep ':80 '` to identify.
- If Apache: `sudo systemctl disable --now apache2`.
- If existing Nginx config: move it under `/etc/nginx/sites-available`
  and re-run `sudo zenplus restart`.

### 11.3 "Dashboard loads but shows 502"

API didn't start. `sudo zenplus status` first; then
`sudo journalctl -u zenplus-api -n 100`.

Most common: `.env` file missing or unreadable. Re-run the installer
— the env step is idempotent and will regenerate any missing keys.

### 11.4 "Settings → Updates says 'API server not permitted to start updater'"

Cause: `/etc/sudoers.d/zenplus-updater` is missing or corrupted.
Resolution: re-run the one-liner. Step 8 reinstalls the sudoers file
and validates it with `visudo -cf` before keeping it.

To verify by hand:

```bash
sudo cat /etc/sudoers.d/zenplus-updater   # Six NOPASSWD lines
sudo visudo -cf /etc/sudoers.d/zenplus-updater  # "parsed OK"
sudo -u zenplus sudo -n /bin/systemctl is-active zenplus-updater.service  # No password prompt
```

### 11.5 "Updater timer never fires"

```bash
sudo systemctl status zenplus-updater.timer
sudo systemctl list-timers zenplus-updater.timer
sudo journalctl -u zenplus-updater -n 50
```

If `Active: inactive (dead)` and not enabled, run
`sudo systemctl enable --now zenplus-updater.timer`. If enabled but
never fires, the appliance probably isn't registered (no license key
entered). The agent skips the work but the timer should still tick.

### 11.6 "Poller can't ping devices"

```bash
# Capability on the binary
getcap /opt/zenplus/bin/zenplus-poller
# Should print: cap_net_raw=ep

# Kernel ping_group_range
sysctl net.ipv4.ping_group_range
# Should print: 0 2147483647
```

If either is missing, `sudo zenplus update` re-applies them; or
manually:

```bash
sudo setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
```

### 11.7 "I lost the admin password"

```bash
cd /opt/zenplus && source venv/bin/activate
HASH=$(python3 -c "from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('admin123'))")
sudo -u postgres psql -d zenplus -c "UPDATE users SET password_hash = '$HASH' WHERE username = 'admin';"
```

### 11.8 "Disk filled up"

Most common cause is ClickHouse retaining longer than expected because
the materialized views fell behind. Check:

```bash
docker exec zenplus-clickhouse clickhouse-client --password "$CLICKHOUSE_PASSWORD" \
  --query "SELECT table, formatReadableSize(sum(bytes)) FROM system.parts WHERE database='zenplus' GROUP BY table"
```

The customer can grow `/data` online via Settings → Storage → Add Disk,
or by increasing the VM disk in the hypervisor and clicking Grow Volume.

---

## 12. Reinstall / wipe

A complete reinstall destroys all metric data and configuration.
**This is the customer's choice; the installer never does it.**

```bash
# Stop everything
sudo systemctl stop zenplus-api zenplus-poller zenplus-updater.timer

# Remove the Docker stack and its volume (deletes ClickHouse data)
cd /opt/zenplus && sudo docker compose down -v

# Remove the install
sudo rm -rf /opt/zenplus
sudo rm -f /usr/local/bin/zenplus
sudo rm -f /etc/systemd/system/zenplus-*.service /etc/systemd/system/zenplus-*.timer
sudo rm -f /etc/nginx/conf.d/zenplus.conf
sudo rm -f /etc/sudoers.d/zenplus-updater
sudo rm -f /etc/polkit-1/rules.d/50-zenplus-updater.rules
sudo systemctl daemon-reload
sudo systemctl reload nginx 2>/dev/null

# Re-run the one-liner
curl -fsSL https://install.zentryc.com | sudo bash
```

To **re-register** an appliance against a different subscription
without wiping data, the operator clears the credentials and the
customer pastes a fresh license key:

```bash
sudo sed -i -E 's|^(id[[:space:]]*=).*|\1|; s|^(api_key[[:space:]]*=).*|\1|' \
    /opt/zenplus/updater/config/agent.conf
sudo rm -f /opt/zenplus/updater/config/subscription.json
sudo systemctl restart zenplus-api
# Customer pastes the new key in Settings → Subscription.
```

---

## 13. Security notes (what we tell customers)

- All inter-service traffic on the appliance is loopback-only.
  PostgreSQL, ClickHouse, and Redis bind to `127.0.0.1`.
- The web tier is HTTP on port 80 by default. **Customers terminate
  TLS at their own reverse proxy / load balancer.** A future release
  may bake a Caddy / Let's Encrypt path; for v1, customers place the
  appliance behind their existing TLS edge.
- All OTA artifacts are Ed25519-signed. The matching public key is
  baked into the OVA at `/opt/zenplus/updater/keys/zentryc-release.pub`
  and is the trust anchor for every update.
- The OTA agent verifies the manifest signature, then verifies the
  per-file SHA-256 from `checksums.sha256`, then runs steps. A
  signature mismatch or a checksum mismatch aborts before any change
  is made to the system.
- The OTA agent runs as root because it has to install apt packages
  and reload systemd. The mitigation is layered: signing + checksum
  + protected-package list + automatic backup + automatic rollback +
  three-failures-then-stop. There is no privilege-separation path
  that achieves system-level updates without a root-capable agent.
- The license key is single-use and is consumed at registration. The
  per-appliance API key returned at registration is stored at
  `/opt/zenplus/updater/config/agent.conf` (mode 0600). Revoking
  the appliance from the zentryc.com admin dashboard makes the next
  checkin return 403; the appliance UI surfaces this state.

---

## 14. Release-side notes (for the team running zentryc.com)

The remote-server team owns three pieces:

1. **`https://install.zentryc.com`** — the static one-liner host
   (§10).
2. **`https://zentryc.com/api/v1/...`** — the OTA API. The full
   contract is in
   [`14-REMOTE-SERVER-INTAKE.md`](14-REMOTE-SERVER-INTAKE.md). When
   anything in §A of that document changes, the appliance breaks.
3. **The release-signing public key** kept on the zentryc.com host
   to re-verify uploaded `.zup` packages at admin upload time. It
   must match the bytes embedded in every shipped OVA at
   `/opt/zenplus/updater/keys/zentryc-release.pub` exactly. Mismatch
   means every signed release will fail to install.

If `install.sh` is hosted from `install.zentryc.com` directly (§10.2),
the team should also update the `ZENPLUS_REPO` / `ZENPLUS_BRANCH`
constants if the customer source-of-truth ever moves off GitHub.

---

## 15. Change log for this guide

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-05-04 | Initial public knowledge-base article. Reflects `install.sh` post-fix: bundled OTA setup, sudoers + polkit, `NoNewPrivileges=false`, migration-aware `zenplus update`. |

---

*Document version: 1.0 | Updated: 2026-05-04 | Owner: ZenPlus Engineering*
