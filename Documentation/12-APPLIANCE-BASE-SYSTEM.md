# ZenPlus Appliance - Ubuntu Base System Specification

> **Purpose**: This document defines exactly what the OS team must install and configure on a minimal Ubuntu Server to produce the ZenPlus base appliance image. ZenPlus application code will be layered on top separately.
>
> **Target**: Ubuntu Server 24.04 LTS (Noble Numbat) - minimal install, x86_64
>
> **Delivery**: OVA virtual appliance template

---

## 1. Hardware / VM Specification

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| CPU | 4 vCPU | 8 vCPU | ClickHouse benefits from cores |
| RAM | 8 GB | 16 GB | ClickHouse uses ~4 GB, PG ~1 GB |
| Disk | 100 GB | 250 GB | Thin-provisioned, SSD-backed |
| NIC | 1x vmxnet3 | 1x vmxnet3 | DHCP on first boot, static later |
| Firmware | BIOS or UEFI | UEFI | VMware compatible |

### Disk Layout (Single Disk)

| Mount | Size | Filesystem | Purpose |
|-------|------|------------|---------|
| `/` | 20 GB | ext4 | OS + packages |
| `/opt/zenplus` | 30 GB | ext4 | Application + data |
| `/var/lib/postgresql` | 20 GB | ext4 | PostgreSQL data |
| `/var/lib/clickhouse` | 50 GB | ext4 | ClickHouse data (or Docker volume) |
| swap | 4 GB | swap | Prevent OOM |

> If separate partitions are not feasible, a single `/` partition of 100 GB+ is acceptable. The above is the ideal layout for production isolation.

---

## 2. OS Installation

### Base Image
- **Ubuntu Server 24.04.x LTS** (Noble Numbat, latest point release)
- **Minimal installation** - no desktop, no snap services
- Language: English (US)
- Timezone: UTC (application handles display timezone)
- Keyboard: US

### Post-Install Cleanup
```bash
# Remove unnecessary packages
apt purge -y snapd cloud-init landscape-common popularity-contest
apt autoremove -y

# Disable unnecessary services
systemctl disable --now multipathd.service iscsid.service
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

---

## 3. System User

Create a dedicated service account. All ZenPlus processes run as this user.

```bash
useradd --system --shell /bin/bash --home-dir /opt/zenplus \
        --create-home --user-group zenplus

# Add to docker group (created when Docker is installed)
usermod -aG docker zenplus
```

| Property | Value |
|----------|-------|
| Username | `zenplus` |
| Home | `/opt/zenplus` |
| Shell | `/bin/bash` |
| Groups | `zenplus`, `docker` |
| Login | No password (service account only) |

---

## 4. Required Packages

### 4.1 System Utilities (install first)
```bash
apt update && apt install -y \
    curl wget git jq openssl ca-certificates \
    apt-transport-https gnupg lsb-release \
    build-essential pkg-config libffi-dev \
    net-tools iputils-ping dnsutils \
    htop iotop sysstat \
    unzip tar gzip \
    sudo logrotate cron libcap2-bin
```

### 4.2 Python 3.12 (ships with Ubuntu 24.04)
```bash
apt install -y \
    python3 python3-pip python3-venv python3-dev
```
- **Version**: 3.12.x (system default on 24.04)
- **Usage**: API server (FastAPI), OTA updater
- All ZenPlus Python dependencies are Python 3.12 compatible
- A virtualenv will be created at `/opt/zenplus/venv/` during app deployment

### 4.3 Node.js 20 LTS
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
```
- **Version**: 20.x LTS
- **Usage**: Build the React dashboard (`npm install` + `npx vite build`)
- Only needed at build time, not runtime

### 4.4 Go 1.22+
```bash
GO_VERSION="1.22.5"
curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
    | tar -C /usr/local -xzf -
echo 'export PATH=$PATH:/usr/local/go/bin' > /etc/profile.d/golang.sh
chmod +x /etc/profile.d/golang.sh
```
- **Version**: 1.22.5+
- **Usage**: Compile the Go poller binary
- Only needed at build time, not runtime

### 4.5 PostgreSQL 16
```bash
apt install -y postgresql-16 postgresql-client-16
```
- **Version**: 16.x (Ubuntu 24.04 default)
- **Binding**: 127.0.0.1:5432 (localhost only)
- **Extensions needed**: `pgcrypto` (for `gen_random_uuid()`)

**Post-install configuration** (`/etc/postgresql/16/main/postgresql.conf`):
```ini
listen_addresses = 'localhost'
max_connections = 200
shared_buffers = 256MB
effective_cache_size = 1GB
work_mem = 4MB
maintenance_work_mem = 128MB
wal_level = minimal
max_wal_senders = 0
```

**Authentication** (`/etc/postgresql/16/main/pg_hba.conf`):
```
local   all   zenplus   scram-sha-256
host    all   zenplus   127.0.0.1/32   scram-sha-256
```

> **Note**: PostgreSQL 16 defaults to `scram-sha-256` authentication instead of `md5`. Both `asyncpg` and `pgx/v5` (our Python and Go drivers) fully support SCRAM-SHA-256.

**Create database and user** (run after PostgreSQL starts):
```bash
sudo -u postgres psql -c "CREATE USER zenplus WITH PASSWORD '<generated>';"
sudo -u postgres psql -c "CREATE DATABASE zenplus OWNER zenplus;"
sudo -u postgres psql -d zenplus -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
```

### 4.6 Redis 7
```bash
apt install -y redis-server
```
- **Version**: 7.0.x (Ubuntu 24.04 default)
- **Binding**: 127.0.0.1:6379 (localhost only)
- Fully backward compatible with our Redis 5.x/6.x usage patterns

**Post-install configuration** (`/etc/redis/redis.conf`):
```ini
bind 127.0.0.1
requirepass <generated>
maxmemory 512mb
maxmemory-policy allkeys-lru
```

### 4.7 Docker Engine + Compose
```bash
# Official Docker repository
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu noble stable" \
    > /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli \
    containerd.io docker-compose-plugin
```
- **Usage**: Runs ClickHouse container only
- **Why Docker for ClickHouse**: Easier upgrades, isolated data volume, no need for ClickHouse APT repo management

### 4.8 Nginx
```bash
apt install -y nginx
```
- **Version**: 1.24+ (Ubuntu 24.04 default)
- **Role**: Reverse proxy + static file server for the dashboard
- **Binding**: 0.0.0.0:80

---

## 5. Kernel & Sysctl Tuning

### 5.1 ICMP Ping Permissions
The Go poller uses raw sockets for ICMP. Required:

```bash
# /etc/sysctl.d/99-zenplus.conf
net.ipv4.ping_group_range = 0 2147483647
```

### 5.2 Network Performance
```bash
# /etc/sysctl.d/99-zenplus.conf (continued)
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.ip_local_port_range = 1024 65535
```

Apply: `sysctl --system`

---

## 6. Directory Structure

Create the following before application deployment:

```bash
mkdir -p /opt/zenplus/{bin,server,dashboard,poller,scripts,logs,data,backups}
mkdir -p /opt/zenplus/updater/{config,keys,backups,logs}
chown -R zenplus:zenplus /opt/zenplus
chmod 750 /opt/zenplus
```

### Permissions Summary

| Path | Owner | Mode | Purpose |
|------|-------|------|---------|
| `/opt/zenplus/` | zenplus:zenplus | 750 | Application root |
| `/opt/zenplus/.env` | zenplus:zenplus | 640 | Secrets (passwords, JWT) |
| `/opt/zenplus/bin/` | zenplus:zenplus | 755 | Executable binaries |
| `/opt/zenplus/logs/` | zenplus:zenplus | 750 | Application logs |
| `/opt/zenplus/updater/config/` | zenplus:zenplus | 700 | Updater config (API keys) |
| `/opt/zenplus/updater/keys/` | zenplus:zenplus | 700 | Signing keys |
| `/opt/zenplus/dashboard/dist/` | zenplus:zenplus | 755 | Static web assets (Nginx reads) |

---

## 7. Systemd Services

The application layer will install these, but the base system must support them. Ensure `systemd` is fully functional and these capabilities work:

| Service | Type | User | Capabilities | Description |
|---------|------|------|-------------|-------------|
| `zenplus-api` | simple | zenplus | none | FastAPI on :8000 |
| `zenplus-poller` | simple | zenplus | CAP_NET_RAW | Go ICMP poller |
| `zenplus-dashboard` | simple | zenplus | none | Static HTTP on :3000 |
| `zenplus-wait-deps` | oneshot | zenplus | none | Checks PG/Redis/CH ready |
| `zenplus-updater` | oneshot | zenplus | none | OTA update agent |
| `zenplus-updater.timer` | timer | - | none | 4-hour update check |

### Required Capability Support
```bash
# The poller binary needs CAP_NET_RAW. Ensure libcap2-bin is installed:
# (already included in Section 4.1)
# Application deploy will run: setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
```

### Polkit Rules (for OTA updater)
The zenplus user must be able to restart its own services without sudo password.

> **Important**: Ubuntu 24.04 uses polkit 124+ which supports the modern JavaScript `.rules` format. Do **NOT** use the legacy `.pkla` format.

```javascript
// /etc/polkit-1/rules.d/50-zenplus-updater.rules
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "zenplus" &&
        (action.lookup("unit") == "zenplus-updater.service" ||
         action.lookup("unit") == "zenplus-updater.timer" ||
         action.lookup("unit") == "zenplus-api.service" ||
         action.lookup("unit") == "zenplus-poller.service" ||
         action.lookup("unit") == "zenplus-dashboard.service")) {
        return polkit.Result.YES;
    }
});
```

```bash
chmod 644 /etc/polkit-1/rules.d/50-zenplus-updater.rules
systemctl restart polkit
```

---

## 8. Nginx Configuration

Pre-configure the reverse proxy site. The application layer deploys `dashboard/dist/` but the Nginx config should be ready:

```nginx
# /etc/nginx/sites-available/zenplus
server {
    listen 80 default_server;
    server_name _;

    root /opt/zenplus/dashboard/dist;
    index index.html;

    # SPA routing - all frontend routes fall back to index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API reverse proxy
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support (Server-Sent Events)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }

    # Static asset caching
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2|woff|ttf)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # Gzip compression
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;

    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options SAMEORIGIN;
    add_header X-XSS-Protection "1; mode=block";
}
```

```bash
ln -sf /etc/nginx/sites-available/zenplus /etc/nginx/sites-enabled/zenplus
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

---

## 9. Firewall Rules

Open only what's necessary. All database services are localhost-only by design.

```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    comment "SSH management"
ufw allow 80/tcp    comment "ZenPlus Web UI + API"
ufw allow 443/tcp   comment "ZenPlus HTTPS (future)"
ufw --force enable
```

### Port Map (internal services - NOT exposed to firewall)

| Port | Service | Binding | Exposed |
|------|---------|---------|---------|
| 80 | Nginx | 0.0.0.0 | Yes - Web UI + API |
| 443 | Nginx (future) | 0.0.0.0 | Yes - HTTPS |
| 22 | SSH | 0.0.0.0 | Yes - Management |
| 5432 | PostgreSQL | 127.0.0.1 | No |
| 6379 | Redis | 127.0.0.1 | No |
| 8123 | ClickHouse HTTP | 127.0.0.1 | No |
| 9000 | ClickHouse Native | 127.0.0.1 | No |
| 8000 | FastAPI | 127.0.0.1 | No (via Nginx) |
| 3000 | Dashboard HTTP | 127.0.0.1 | No (via Nginx) |

---

## 10. Log Rotation

```bash
# /etc/logrotate.d/zenplus
/opt/zenplus/logs/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 zenplus zenplus
    sharedscripts
    postrotate
        systemctl reload zenplus-api > /dev/null 2>&1 || true
    endscript
}

/opt/zenplus/updater/logs/*.log {
    weekly
    missingok
    rotate 5
    compress
    size 10M
    create 0640 zenplus zenplus
}
```

---

## 11. Docker Compose File

Pre-place the ClickHouse docker-compose configuration:

```yaml
# /opt/zenplus/docker-compose.yml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:24-alpine
    container_name: zenplus-clickhouse
    restart: unless-stopped
    ports:
      - "127.0.0.1:8123:8123"
      - "127.0.0.1:9000:9000"
    volumes:
      - clickhouse_data:/var/lib/clickhouse
      - ./scripts/init-clickhouse.sql:/docker-entrypoint-initdb.d/init.sql
    environment:
      CLICKHOUSE_DB: zenplus
      CLICKHOUSE_USER: default
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
      CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: 1
    ulimits:
      nofile:
        soft: 262144
        hard: 262144
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  clickhouse_data:
```

> **Note**: The `version:` key is omitted - Docker Compose V2 (the default on 24.04) no longer requires it.

Pre-pull the image during base build:
```bash
docker pull clickhouse/clickhouse-server:24-alpine
```

---

## 12. Environment File Template

Pre-place a template at `/opt/zenplus/.env.example`:

```bash
# ─── Database Credentials (auto-generated during first boot) ───
POSTGRES_PASSWORD=
CLICKHOUSE_PASSWORD=
REDIS_PASSWORD=

# ─── API Configuration ───
JWT_SECRET=
API_HOST=0.0.0.0
API_PORT=8000

# ─── Connection Strings ───
DATABASE_URL=postgresql+asyncpg://zenplus:${POSTGRES_PASSWORD}@localhost:5432/zenplus
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=9000
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_DB=zenplus
CLICKHOUSE_USER=default
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0

# ─── Poller ───
POLLER_ID=poller-01
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=zenplus
POSTGRES_USER=zenplus
REDIS_HOST=localhost
REDIS_PORT=6379
```

---

## 13. First-Boot Initialization Script

Place a one-time setup script that runs on first deployment to generate secrets and initialize databases:

```bash
#!/bin/bash
# /opt/zenplus/bin/first-boot-init.sh
set -euo pipefail

ENV_FILE="/opt/zenplus/.env"
if [ -f "$ENV_FILE" ] && grep -q "POSTGRES_PASSWORD=." "$ENV_FILE"; then
    echo "Already initialized. Skipping."
    exit 0
fi

echo "=== ZenPlus First Boot Initialization ==="

# Generate random passwords
PG_PASS=$(openssl rand -hex 16)
CH_PASS=$(openssl rand -hex 16)
RD_PASS=$(openssl rand -hex 16)
JWT=$(openssl rand -hex 32)

# Write .env
cat > "$ENV_FILE" <<EOF
# Do NOT delete - contains database credentials
# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

POSTGRES_PASSWORD=${PG_PASS}
CLICKHOUSE_PASSWORD=${CH_PASS}
REDIS_PASSWORD=${RD_PASS}
JWT_SECRET=${JWT}
API_HOST=0.0.0.0
API_PORT=8000
DATABASE_URL=postgresql+asyncpg://zenplus:${PG_PASS}@localhost:5432/zenplus
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=9000
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_DB=zenplus
CLICKHOUSE_USER=default
REDIS_URL=redis://:${RD_PASS}@localhost:6379/0
POLLER_ID=poller-01
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=zenplus
POSTGRES_USER=zenplus
REDIS_HOST=localhost
REDIS_PORT=6379
EOF
chmod 640 "$ENV_FILE"
chown zenplus:zenplus "$ENV_FILE"

# Configure PostgreSQL
sudo -u postgres psql -c "ALTER USER zenplus WITH PASSWORD '${PG_PASS}';" 2>/dev/null \
    || sudo -u postgres psql -c "CREATE USER zenplus WITH PASSWORD '${PG_PASS}';"
sudo -u postgres psql -c "CREATE DATABASE zenplus OWNER zenplus;" 2>/dev/null || true
sudo -u postgres psql -d zenplus -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;" 2>/dev/null || true

# Configure Redis
sed -i "s/^# requirepass .*/requirepass ${RD_PASS}/" /etc/redis/redis.conf
sed -i "s/^requirepass .*/requirepass ${RD_PASS}/" /etc/redis/redis.conf
systemctl restart redis-server

# Start ClickHouse with password
cd /opt/zenplus && docker compose up -d

echo "=== Initialization Complete ==="
```

```bash
chmod 755 /opt/zenplus/bin/first-boot-init.sh
chown zenplus:zenplus /opt/zenplus/bin/first-boot-init.sh
```

---

## 14. Service Startup Order

```
                    ┌────────────────────┐
                    │   postgresql.service│
                    └────────┬───────────┘
                             │
                    ┌────────┴───────────┐
                    │  redis-server.service│
                    └────────┬───────────┘
                             │
                    ┌────────┴───────────┐
                    │   docker.service    │
                    │  (ClickHouse)       │
                    └────────┬───────────┘
                             │
                    ┌────────┴───────────┐
                    │ zenplus-wait-deps   │
                    │ (checks all 3 ready)│
                    └────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────┴──────┐ ┌────┴─────┐ ┌──────┴──────┐
     │ zenplus-api   │ │ zenplus- │ │ zenplus-    │
     │ (FastAPI:8000)│ │ poller   │ │ dashboard   │
     └───────────────┘ └──────────┘ └─────────────┘
              │
     ┌────────┴──────────┐
     │ nginx             │
     │ (reverse proxy:80)│
     └───────────────────┘
```

---

## 15. Services to Enable on Boot

```bash
systemctl enable postgresql
systemctl enable redis-server
systemctl enable docker
systemctl enable nginx
# ZenPlus services enabled during application deployment:
# systemctl enable zenplus-wait-deps zenplus-api zenplus-poller zenplus-dashboard
# systemctl enable zenplus-updater.timer
```

---

## 16. OVA Export Checklist

Before exporting the VM as OVA, run this cleanup:

```bash
# Clear package cache
apt clean && apt autoremove -y

# Clear logs
journalctl --vacuum-time=1d
truncate -s 0 /var/log/*.log /var/log/**/*.log 2>/dev/null || true

# Clear shell history
> /root/.bash_history
> /home/*/.bash_history 2>/dev/null || true

# Clear SSH host keys (regenerated on first boot)
rm -f /etc/ssh/ssh_host_*

# Clear machine-id (regenerated on first boot)
truncate -s 0 /etc/machine-id

# Zero free space for better compression
dd if=/dev/zero of=/EMPTY bs=1M 2>/dev/null || true
rm -f /EMPTY

# Shutdown
shutdown -h now
```

Then from VMware:
```
File > Export to OVF/OVA > zenplus-appliance-v1.0.ova
```

---

## 17. Summary: What the OS Team Delivers

### Installed & Running

| Component | Version | Status on Boot |
|-----------|---------|----------------|
| Ubuntu Server | **24.04 LTS** (Noble) | Running |
| PostgreSQL | **16.x** | Running, `zenplus` DB created |
| Redis | **7.0.x** | Running, password set |
| Docker + Compose V2 | 24.x+ | Running |
| ClickHouse (Docker) | 24-alpine | Running (container auto-starts) |
| Nginx | **1.24+** | Running, ZenPlus site configured |
| Python | **3.12.x** | Installed (system default) |
| Node.js | 20.x LTS | Installed |
| Go | 1.22.x | Installed at `/usr/local/go` |

### Configured

| Item | Details |
|------|---------|
| System user | `zenplus` with docker group |
| Directory structure | `/opt/zenplus/` with subdirs |
| Sysctl | ICMP ping range, network tuning |
| Firewall (ufw) | SSH + HTTP + HTTPS only |
| Nginx | Reverse proxy site ready |
| Log rotation | `/etc/logrotate.d/zenplus` |
| Polkit | `.rules` JS format (24.04 native) |
| Docker image | `clickhouse/clickhouse-server:24-alpine` pre-pulled |
| First-boot script | `/opt/zenplus/bin/first-boot-init.sh` ready |

### NOT Included (Application Layer Responsibility)

| Item | Installed By |
|------|-------------|
| ZenPlus source code | Git clone during deployment |
| Python virtualenv + deps | `pip install -r requirements.txt` |
| Node modules + build | `npm install && npx vite build` |
| Go poller binary | `go build` during deployment |
| Database schemas | SQL migration scripts |
| Systemd service files | Application installer |
| `.env` secrets | First-boot script or installer |
| SSL certificates | Post-deployment (Let's Encrypt or manual) |

---

## 18. Ubuntu 22.04 vs 24.04 - Key Differences

For teams familiar with the 22.04 setup, here are the changes that matter:

| Area | Ubuntu 22.04 | Ubuntu 24.04 | Impact |
|------|-------------|-------------|--------|
| Python | 3.10 | **3.12** | All our deps compatible. Rebuild venv. |
| PostgreSQL | 14 | **16** | Config path changes: `/etc/postgresql/16/main/` |
| PG Auth | `md5` default | **`scram-sha-256`** default | Our drivers support both. Use scram. |
| Redis | 6.0 | **7.0** | Fully backward compatible. No changes. |
| Nginx | 1.18 | **1.24** | No config changes needed. |
| Polkit | < 0.106 (`.pkla` format) | **124+** (`.rules` JS format) | Must use `.rules` file, not `.pkla` |
| Docker Compose | V1+V2, needs `version:` | **V2 only**, `version:` deprecated | Remove `version: "3.8"` from compose file |
| Kernel | 5.15 | **6.8** | Better performance. No config changes. |
| systemd | 249 | **255** | No breaking changes for our services. |
| AppArmor | Permissive defaults | **Stricter defaults** | May need profiles for custom services |

### Migration Note
If upgrading an existing 22.04 appliance to 24.04:
1. Backup PostgreSQL: `sudo -u postgres pg_dumpall > /tmp/pgbackup.sql`
2. The `pg_hba.conf` path changes from `/etc/postgresql/14/` to `/etc/postgresql/16/`
3. Remove `.pkla` polkit file, install `.rules` version
4. Rebuild Python virtualenv (3.10 venv won't work on 3.12)
5. Rebuild Go poller binary (existing binary will still run, but rebuild is recommended)

---

## 19. Verification Tests

After building the base image, run these checks before OVA export:

```bash
# 1. OS version
lsb_release -d                     # Should show "Ubuntu 24.04.x LTS"

# 2. Services running
systemctl is-active postgresql redis-server docker nginx

# 3. PostgreSQL accessible
sudo -u postgres psql -c "SELECT version();"   # Should show PostgreSQL 16.x

# 4. Redis accessible
redis-cli -a <password> ping       # Should return PONG

# 5. ClickHouse running
docker ps | grep zenplus-clickhouse
curl -s http://localhost:8123/ping  # Should return "Ok."

# 6. Nginx responding
curl -s -o /dev/null -w "%{http_code}" http://localhost/
# 502 is expected (no backend yet), NOT 404 or connection refused

# 7. User and directories
id zenplus                          # Should show uid, gid, groups=docker
ls -la /opt/zenplus/                # Should show zenplus:zenplus ownership

# 8. Sysctl
sysctl net.ipv4.ping_group_range   # Should show 0 2147483647

# 9. Language runtimes
python3 --version                   # Python 3.12.x
/usr/local/go/bin/go version        # go1.22.x
node --version                      # v20.x.x

# 10. Firewall
ufw status                          # 22, 80, 443 allowed

# 11. Capability support
which setcap                        # /usr/sbin/setcap

# 12. Polkit rules
ls /etc/polkit-1/rules.d/50-zenplus-updater.rules   # Should exist

# 13. Docker Compose
docker compose version              # Should show v2.x.x
```

---

---
---

# PART 2 - Application Layer Deployment

> Everything below turns the bare Ubuntu base into the working ZenPlus monitoring appliance (i.e., what is running on `http://10.11.50.30/` today).

---

## 20. Application Source Code

```bash
# Clone as the zenplus user
sudo -u zenplus git clone https://github.com/khuram2025/zen-mon.git /opt/zenplus/src-tmp
sudo -u zenplus cp -a /opt/zenplus/src-tmp/. /opt/zenplus/
rm -rf /opt/zenplus/src-tmp
```

Or use the full installer (recommended):
```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh \
    | sudo bash
```

The installer handles everything in Sections 20-28 automatically.

---

## 21. Python Virtual Environment & Dependencies

```bash
sudo -u zenplus python3 -m venv /opt/zenplus/venv
sudo -u zenplus /opt/zenplus/venv/bin/pip install --upgrade pip
sudo -u zenplus /opt/zenplus/venv/bin/pip install -r /opt/zenplus/server/requirements.txt
```

### Key Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| fastapi | 0.115.6 | REST API framework |
| uvicorn[standard] | 0.34.0 | ASGI server (2 workers) |
| asyncpg | 0.30.0 | PostgreSQL async driver |
| sqlalchemy[asyncio] | 2.0.36 | ORM |
| alembic | 1.14.0 | Database migrations |
| pydantic | 2.10.3 | Request/response validation |
| python-jose[cryptography] | 3.3.0 | JWT authentication |
| passlib[bcrypt] | 1.7.4 | Password hashing |
| clickhouse-connect | 0.8.9 | ClickHouse HTTP client |
| redis[hiredis] | 5.2.1 | Redis client |
| sse-starlette | 2.2.1 | Server-Sent Events (real-time) |
| httpx | 0.28.1 | Async HTTP client (service checks) |
| fpdf2 | 2.8.2 | PDF report generation |
| matplotlib | 3.9.4 | Chart rendering for reports |

---

## 22. Go Poller Binary

```bash
cd /opt/zenplus/poller
PATH=/usr/local/go/bin:$PATH CGO_ENABLED=0 go build -o /opt/zenplus/bin/zenplus-poller ./cmd/poller/

# Grant ICMP capability (requires root)
sudo setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
```

### Poller Configuration (`/opt/zenplus/poller/config.yaml`)

```yaml
poller:
  id: poller-01
  ping:
    timeout: 3s
    count: 3
    interval: 500ms
    batch_size: 500
    privileged: true
  status:
    down_threshold: 3        # consecutive failures before marking DOWN
    degraded_rtt_ms: 100     # RTT above this = degraded
    degraded_loss_pct: 10    # Packet loss above this = degraded
  device_sync_interval: 60s  # reload device list from PG

health:
  port: 8081
```

### What the Poller Does
- Loads all enabled devices from PostgreSQL every 60s
- Loads all enabled service checks (HTTP/TCP/TLS) every 60s
- Pings every device via ICMP (batch of 500, 3 packets each)
- Runs HTTP/TCP/TLS service checks concurrently (50 workers max)
- Writes raw metrics to ClickHouse (batch insert, ~10s flush)
- Updates device/service status in PostgreSQL on state change
- Publishes real-time events to Redis (pub/sub)

---

## 23. React Dashboard Build

```bash
cd /opt/zenplus/dashboard
sudo -u zenplus npm install
sudo -u zenplus npx vite build
```

Output: `/opt/zenplus/dashboard/dist/` (served by Nginx at port 80)

### Dashboard Features
- Real-time device monitoring with SSE live updates
- Response time & packet loss charts (ECharts, 100K+ data points)
- Service check monitoring (HTTP/TCP/TLS)
- Alert management with rule-based policies
- Device groups, tags, and filtering
- PDF report export
- User management (admin/editor/viewer roles)
- Regional settings (timezone, date/time format)
- Dark theme UI (Tailwind CSS)

---

## 24. Database Schema Initialization

### PostgreSQL (run in order)
```bash
PGPASSWORD=<from .env> psql -h localhost -U zenplus -d zenplus \
    -f /opt/zenplus/scripts/init-postgres.sql

PGPASSWORD=<from .env> psql -h localhost -U zenplus -d zenplus \
    -f /opt/zenplus/scripts/migrate-001-alerts.sql

PGPASSWORD=<from .env> psql -h localhost -U zenplus -d zenplus \
    -f /opt/zenplus/scripts/migrate-002-service-checks.sql
```

### PostgreSQL Tables Created

| Table | Purpose |
|-------|---------|
| `users` | Authentication & RBAC (admin, editor, viewer) |
| `device_groups` | Hierarchical device organization (Core, Distribution, Access, Servers, DMZ) |
| `devices` | Monitored endpoints (hostname, IP, type, status, ping config, SNMP config) |
| `alert_rules` | Alerting policies (metric thresholds, severity, notification channels) |
| `alerts` | Active/acknowledged/resolved incidents |
| `notification_channels` | Email, SMS, Webhook, Slack, Telegram integrations |
| `dashboard_configs` | Per-user dashboard widget layouts |
| `service_checks` | HTTP/TCP/TLS monitoring targets |
| `system_settings` | Global config (SMTP, SMS, company info, timezone) |

### Default Seed Data
- **Admin user**: `admin` / `admin@zenplus.local` (password set during install)
- **Device groups**: Core Network, Distribution, Access Layer, Servers, DMZ

### ClickHouse (auto-initialized by Docker entrypoint)
The `init-clickhouse.sql` is mounted into the container at `/docker-entrypoint-initdb.d/` and runs automatically on first start. Creates:

| Table | Retention | Purpose |
|-------|-----------|---------|
| `ping_metrics` | 30 days | Raw ICMP results (every ping) |
| `ping_metrics_5m` | 90 days | 5-minute aggregates (materialized view) |
| `ping_metrics_1h` | 1 year | Hourly aggregates with P95 (materialized view) |
| `service_metrics` | 30 days | Raw service check results |
| `service_metrics_5m` | 90 days | 5-minute service aggregates (materialized view) |
| `device_status_log` | 1 year | Device state transitions (up/down/degraded) |
| `service_status_log` | 1 year | Service state transitions |

---

## 25. Systemd Service Files

### zenplus-wait-deps.service
```ini
[Unit]
Description=ZenPlus Dependency Check
After=postgresql.service redis-server.service docker.service
Requires=postgresql.service redis-server.service docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=zenplus
ExecStart=/opt/zenplus/bin/wait-for-deps.sh
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
```

### zenplus-api.service
```ini
[Unit]
Description=ZenPlus API Server
After=network.target zenplus-wait-deps.service
Requires=zenplus-wait-deps.service

[Service]
Type=simple
User=zenplus
Group=zenplus
WorkingDirectory=/opt/zenplus/server
EnvironmentFile=/opt/zenplus/.env
ExecStart=/opt/zenplus/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60
NoNewPrivileges=false
ProtectSystem=strict
ReadWritePaths=/opt/zenplus/logs /opt/zenplus/server
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### zenplus-poller.service
```ini
[Unit]
Description=ZenPlus Ping Poller
After=zenplus-api.service zenplus-wait-deps.service
Wants=zenplus-api.service
Requires=zenplus-wait-deps.service

[Service]
Type=simple
User=zenplus
Group=zenplus
WorkingDirectory=/opt/zenplus
EnvironmentFile=/opt/zenplus/.env
ExecStart=/opt/zenplus/bin/zenplus-poller
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60
AmbientCapabilities=CAP_NET_RAW
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### zenplus-dashboard.service
```ini
[Unit]
Description=ZenPlus Dashboard
After=zenplus-api.service

[Service]
Type=simple
User=zenplus
Group=zenplus
WorkingDirectory=/opt/zenplus/dashboard/dist
ExecStart=/opt/zenplus/venv/bin/python3 -m http.server 3000 --bind 0.0.0.0
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### zenplus-updater.service & timer
```ini
# zenplus-updater.service
[Unit]
Description=ZenPlus OTA Update Agent

[Service]
Type=oneshot
User=zenplus
WorkingDirectory=/opt/zenplus
EnvironmentFile=/opt/zenplus/.env
ExecStart=/opt/zenplus/venv/bin/python -m updater
TimeoutStartSec=900

# zenplus-updater.timer
[Unit]
Description=ZenPlus Update Check Timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=4h
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
```

### Enable All Services
```bash
sudo systemctl daemon-reload
sudo systemctl enable zenplus-wait-deps zenplus-api zenplus-poller zenplus-dashboard
sudo systemctl enable zenplus-updater.timer
sudo systemctl start zenplus-wait-deps
sudo systemctl start zenplus-api zenplus-poller zenplus-dashboard
sudo systemctl start zenplus-updater.timer
```

---

## 26. Dependency Wait Script

```bash
#!/usr/bin/env bash
# /opt/zenplus/bin/wait-for-deps.sh
set -euo pipefail
MAX_WAIT=90; SLEEP_SEC=2
log() { echo "[zenplus-deps] $*"; }

log "Waiting for PostgreSQL..."
elapsed=0
until pg_isready -h localhost -p 5432 -U zenplus -q 2>/dev/null; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: PostgreSQL not ready"; exit 1; }
done
log "PostgreSQL ready (${elapsed}s)"

log "Waiting for Redis..."
elapsed=0
until redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: Redis not ready"; exit 1; }
done
log "Redis ready (${elapsed}s)"

log "Waiting for ClickHouse..."
elapsed=0
until docker exec zenplus-clickhouse clickhouse-client --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1" >/dev/null 2>&1; do
    sleep $SLEEP_SEC; elapsed=$((elapsed + SLEEP_SEC))
    [ $elapsed -ge $MAX_WAIT ] && { log "ERROR: ClickHouse not ready"; exit 1; }
done
log "ClickHouse ready (${elapsed}s)"

log "All dependencies ready"
```

```bash
chmod 755 /opt/zenplus/bin/wait-for-deps.sh
chown zenplus:zenplus /opt/zenplus/bin/wait-for-deps.sh
```

---

## 27. Management CLI

Install a convenience command at `/usr/local/bin/zenplus`:

```bash
#!/bin/bash
# /usr/local/bin/zenplus - ZenPlus management CLI
SERVICES="zenplus-api zenplus-poller zenplus-dashboard"

case "${1:-}" in
    status)
        echo "=== ZenPlus Services ==="
        for svc in $SERVICES; do
            printf "  %-25s %s\n" "$svc" "$(systemctl is-active $svc 2>/dev/null || echo 'not found')"
        done
        echo "=== Infrastructure ==="
        printf "  %-25s %s\n" "postgresql" "$(systemctl is-active postgresql)"
        printf "  %-25s %s\n" "redis-server" "$(systemctl is-active redis-server)"
        printf "  %-25s %s\n" "clickhouse" "$(docker inspect -f '{{.State.Status}}' zenplus-clickhouse 2>/dev/null || echo 'not running')"
        printf "  %-25s %s\n" "nginx" "$(systemctl is-active nginx)"
        ;;
    start)   sudo systemctl start $SERVICES ;;
    stop)    sudo systemctl stop $SERVICES ;;
    restart) sudo systemctl restart $SERVICES ;;
    logs)
        svc="${2:-api}"
        sudo journalctl -u "zenplus-${svc}" -f --no-pager
        ;;
    update)
        echo "Pulling latest code..."
        cd /opt/zenplus && sudo -u zenplus git pull origin main
        echo "Rebuilding..."
        sudo -u zenplus /opt/zenplus/venv/bin/pip install -r server/requirements.txt
        cd dashboard && sudo -u zenplus npm install && sudo -u zenplus npx vite build && cd ..
        cd poller && sudo -u zenplus PATH=/usr/local/go/bin:$PATH CGO_ENABLED=0 go build -o ../bin/zenplus-poller ./cmd/poller/ && cd ..
        sudo setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
        sudo systemctl restart $SERVICES
        echo "Update complete."
        ;;
    *)
        echo "Usage: zenplus {status|start|stop|restart|logs [api|poller]|update}"
        ;;
esac
```

```bash
sudo chmod 755 /usr/local/bin/zenplus
```

---

## 28. API Endpoints Reference

The FastAPI server exposes these route groups at `http://<IP>/api/v1/`:

| Route Prefix | Purpose | Key Features |
|-------------|---------|--------------|
| `/auth` | Authentication | Login (JWT), current user profile |
| `/users` | User management | CRUD, roles (admin/operator/viewer/read_only), password reset |
| `/devices` | Device inventory | CRUD, bulk import/export, summary, uptime stats, metrics, status history |
| `/service-checks` | Service monitoring | HTTP/TCP/TLS checks, test-now, bulk-delete, export, metrics, uptime stats |
| `/alert-rules` | Alert policies | CRUD, toggle, preview templates, simulate notifications |
| `/alerts` | Incident management | List/filter, stats, acknowledge, resolve |
| `/alert-engine` | Alert evaluation | Internal endpoint for poller status change evaluation |
| `/settings` | System config | Company info/logo, timezone, SMTP/SMS gateways CRUD, notification channels CRUD |
| `/reports` | Report generation | PDF/Excel/CSV (executive, device health, service health, alert analysis, full) |
| `/discovery` | Network discovery | Subnet ICMP scan with reverse DNS (up to 10 subnets) |
| `/stream` | Live updates | SSE streams for metrics, status changes, alerts (via Redis pub/sub) |
| `/subscription` | Licensing | Current plan, usage stats, activate license key |
| `/system` | OTA updates + health | Registration, update check/config, update history, system health |

### Default Login
- **URL**: `http://<appliance-ip>/`
- **Username**: `admin`
- **Password**: Set during installation (default seed: check `init-postgres.sql`)

---

## 29. Complete Appliance Build Sequence

This is the exact order to build a working ZenPlus appliance from scratch:

```
┌─────────────────────────────────────────────────────────────────┐
│ PHASE 1: Base OS (Sections 1-16, done by OS Team)              │
│                                                                 │
│  Ubuntu 24.04 LTS → PostgreSQL 16 → Redis 7 → Docker →        │
│  ClickHouse container → Nginx → Python 3.12 → Node 20 →       │
│  Go 1.22 → sysctl → firewall → zenplus user → directories     │
├─────────────────────────────────────────────────────────────────┤
│ PHASE 2: Application (Sections 20-28, done by App Team)        │
│                                                                 │
│  Git clone → first-boot-init.sh (generates .env + DB) →       │
│  pip install (venv) → go build (poller) → npm build (UI) →    │
│  SQL schemas (PG + CH) → systemd services → polkit rules →    │
│  zenplus CLI → start all services → verify health              │
├─────────────────────────────────────────────────────────────────┤
│ PHASE 3: Export (Section 16)                                    │
│                                                                 │
│  Clean logs → clear history → remove SSH keys →                │
│  zero free space → shutdown → export OVA                       │
└─────────────────────────────────────────────────────────────────┘
```

### Quick Verification After Build
```bash
# All services green
zenplus status

# API responding
curl -s http://localhost/api/v1/system/health | python3 -m json.tool

# Dashboard loading
curl -s -o /dev/null -w "%{http_code}" http://localhost/
# Should return 200

# Poller writing metrics
curl -s 'http://localhost:8123/' -d "SELECT count() FROM zenplus.ping_metrics" \
    --user "default:$(grep CLICKHOUSE_PASSWORD /opt/zenplus/.env | cut -d= -f2)"
# Should return > 0 after a few minutes

# Login works
curl -s http://localhost/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"<password>"}'
# Should return JWT token
```

---

*Document version: 3.0 | Updated: 2026-04-07 | Target: Ubuntu 24.04 LTS | Author: ZenPlus Engineering*
