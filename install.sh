#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  ZenPlus Network Monitoring System — Installer / Updater       ║
# ║  https://github.com/khuram2025/zen-mon                        ║
# ║                                                                ║
# ║  Usage:                                                        ║
# ║    Fresh install:  curl -fsSL <url>/install.sh | sudo bash     ║
# ║    Update:         sudo zenplus update                         ║
# ║    Status:         sudo zenplus status                         ║
# ╚══════════════════════════════════════════════════════════════════╝

set -uo pipefail

# ─── Configuration ────────────────────────────────────────────────
ZENPLUS_HOME="/opt/zenplus"
ZENPLUS_USER="zenplus"
ZENPLUS_REPO="https://github.com/khuram2025/zen-mon.git"
ZENPLUS_BRANCH="main"
ZENPLUS_VERSION_FILE="$ZENPLUS_HOME/.version"

# Database defaults (overridden by .env if exists)
DB_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "zenplus_$(date +%s)")
CH_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "clickhouse_$(date +%s)")
REDIS_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "redis_$(date +%s)")
JWT_SECRET=$(openssl rand -hex 32 2>/dev/null || echo "jwt_$(date +%s)_secret")
SNMP_ENC_KEY=$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || echo "")

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
info()  { echo -e "${BLUE}[i]${NC} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n"; }

check_root() { [[ $EUID -ne 0 ]] && err "This script must be run as root (use sudo)"; }
get_ip() { ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1; }
mark_git_safe() {
    git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
    if id "$ZENPLUS_USER" &>/dev/null; then
        runuser -u "$ZENPLUS_USER" -- git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
    fi
}

show_banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║        ZenPlus Network Monitoring System         ║"
    echo "  ║        Full-Stack Installer v2.0                 ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ═══════════════════════════════════════════════════════════════
# STEP 1: Prerequisites
# ═══════════════════════════════════════════════════════════════
install_prerequisites() {
    step "Installing prerequisites"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq

    info "Installing core packages..."
    apt-get install -y -qq \
        curl wget git apt-transport-https ca-certificates \
        gnupg lsb-release software-properties-common \
        python3 python3-pip python3-venv \
        build-essential jq openssl \
        libcap2-bin \
        postgresql postgresql-client redis-server nginx \
        > /dev/null 2>&1
    log "Core packages installed"

    # Docker
    if ! command -v docker &>/dev/null; then
        info "Installing Docker..."
        curl -fsSL https://get.docker.com | sh
        systemctl enable docker --now 2>/dev/null || true
        log "Docker installed"
    else
        systemctl enable docker --now 2>/dev/null || true
        log "Docker already installed"
    fi

    # Docker Compose plugin
    if ! docker compose version &>/dev/null; then
        apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
    fi

    # Go 1.22+
    if ! command -v go &>/dev/null || [[ "$(go version 2>/dev/null | grep -oP '\d+\.\d+' | head -1)" < "1.22" ]]; then
        info "Installing Go 1.22..."
        GO_VERSION="1.22.5"
        ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")
        wget -q --timeout=60 "https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz" -O /tmp/go.tar.gz
        rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tar.gz && rm -f /tmp/go.tar.gz
        ln -sf /usr/local/go/bin/go /usr/local/bin/go
        log "Go ${GO_VERSION} installed"
    else
        log "Go already installed"
    fi

    # Node.js 20+
    if ! command -v node &>/dev/null || [[ "$(node -v 2>/dev/null | cut -d'.' -f1 | tr -d 'v')" -lt 20 ]]; then
        info "Installing Node.js 20..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1
        apt-get install -y -qq nodejs > /dev/null 2>&1
        log "Node.js $(node -v) installed"
    else
        log "Node.js already installed ($(node -v))"
    fi

    # Enable ICMP for non-root
    sysctl -w net.ipv4.ping_group_range="0 2147483647" > /dev/null 2>&1
    echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-zenplus-ping.conf

    # Enable native services
    systemctl enable postgresql redis-server docker --now 2>/dev/null || true
    log "All prerequisites installed"
}

# ═══════════════════════════════════════════════════════════════
# STEP 2: System user & directories
# ═══════════════════════════════════════════════════════════════
setup_user() {
    step "Setting up system user"
    if ! id "$ZENPLUS_USER" &>/dev/null; then
        useradd -r -m -d "$ZENPLUS_HOME" -s /bin/bash "$ZENPLUS_USER"
        usermod -aG docker "$ZENPLUS_USER"
        log "Created system user: $ZENPLUS_USER"
    else
        log "User $ZENPLUS_USER already exists"
    fi
    mkdir -p "$ZENPLUS_HOME"/{data,logs,backups,bin}
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
}

# ═══════════════════════════════════════════════════════════════
# STEP 3: Clone / Pull repository
# ═══════════════════════════════════════════════════════════════
fetch_code() {
    step "Fetching ZenPlus source code"
    if [[ -d "$ZENPLUS_HOME/.git" ]]; then
        info "Pulling latest changes..."
        cd "$ZENPLUS_HOME"
        mark_git_safe
        git fetch origin && git reset --hard "origin/$ZENPLUS_BRANCH"
        log "Updated to $(git rev-parse --short HEAD)"
    else
        info "Cloning repository..."
        ENV_BACKUP=""
        [[ -f "$ZENPLUS_HOME/.env" ]] && ENV_BACKUP=$(cat "$ZENPLUS_HOME/.env")
        for dir in data logs backups bin; do
            [[ -d "$ZENPLUS_HOME/$dir" ]] && mv "$ZENPLUS_HOME/$dir" "/tmp/zenplus-save-$dir" 2>/dev/null || true
        done
        rm -rf "$ZENPLUS_HOME"
        git clone -b "$ZENPLUS_BRANCH" "$ZENPLUS_REPO" "$ZENPLUS_HOME"
        for dir in data logs backups bin; do
            [[ -d "/tmp/zenplus-save-$dir" ]] && mv "/tmp/zenplus-save-$dir" "$ZENPLUS_HOME/$dir"
        done
        mkdir -p "$ZENPLUS_HOME"/{data,logs,backups,bin}
        [[ -n "$ENV_BACKUP" ]] && echo "$ENV_BACKUP" > "$ZENPLUS_HOME/.env"
        log "Cloned version $(cd "$ZENPLUS_HOME" && git rev-parse --short HEAD)"
    fi
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
    mark_git_safe
}

# ═══════════════════════════════════════════════════════════════
# STEP 4: Configure environment
# ═══════════════════════════════════════════════════════════════
configure_env() {
    step "Configuring environment"
    ENV_FILE="$ZENPLUS_HOME/.env"
    if [[ -f "$ENV_FILE" ]]; then
        info "Existing .env found, preserving credentials"
        source "$ENV_FILE"
        DB_PASSWORD="${POSTGRES_PASSWORD:-$DB_PASSWORD}"
        CH_PASSWORD="${CLICKHOUSE_PASSWORD:-$CH_PASSWORD}"
        REDIS_PASSWORD="${REDIS_PASSWORD:-$REDIS_PASSWORD}"
        JWT_SECRET="${JWT_SECRET:-$JWT_SECRET}"
        SNMP_ENC_KEY="${SNMP_ENC_KEY:-$SNMP_ENC_KEY}"
    fi
    if [[ -z "$SNMP_ENC_KEY" ]]; then
        err "Could not generate SNMP_ENC_KEY; install openssl or python3 and rerun installer"
    fi

    cat > "$ENV_FILE" <<ENVEOF
# ZenPlus Configuration - Generated $(date -Iseconds)
POSTGRES_PASSWORD=$DB_PASSWORD
CLICKHOUSE_PASSWORD=$CH_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
JWT_SECRET=$JWT_SECRET
SNMP_ENC_KEY=$SNMP_ENC_KEY
API_HOST=0.0.0.0
API_PORT=8000
POLLER_ID=poller-01
# API uses these
DATABASE_URL=postgresql+asyncpg://zenplus:${DB_PASSWORD}@localhost:5432/zenplus
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=9000
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_DB=zenplus
CLICKHOUSE_USER=default
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0
# Poller uses these
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=zenplus
POSTGRES_USER=zenplus
REDIS_HOST=localhost
REDIS_PORT=6379
ENVEOF
    chmod 640 "$ENV_FILE"
    chown "$ZENPLUS_USER:$ZENPLUS_USER" "$ENV_FILE"
    log "Environment configured"
}

# ═══════════════════════════════════════════════════════════════
# STEP 5: Configure databases
# ═══════════════════════════════════════════════════════════════
setup_databases() {
    step "Setting up databases"
    source "$ZENPLUS_HOME/.env"

    # --- PostgreSQL ---
    info "Configuring PostgreSQL..."
    systemctl start postgresql 2>/dev/null || true
    sleep 2

    # Create user and database if not exists
    su - postgres -c "psql -c \"SELECT 1 FROM pg_user WHERE usename='zenplus'\" | grep -q 1" 2>/dev/null || \
        su - postgres -c "psql -c \"CREATE USER zenplus WITH PASSWORD '$DB_PASSWORD'\"" 2>/dev/null
    su - postgres -c "psql -c \"SELECT 1 FROM pg_database WHERE datname='zenplus'\" | grep -q 1" 2>/dev/null || \
        su - postgres -c "psql -c \"CREATE DATABASE zenplus OWNER zenplus\"" 2>/dev/null

    # Update password in case it changed
    su - postgres -c "psql -c \"ALTER USER zenplus WITH PASSWORD '$DB_PASSWORD'\"" 2>/dev/null

    # Run Postgres migrations only — skip the ClickHouse-targeted ones,
    # which use MergeTree syntax that psql can't parse.
    for migration in "$ZENPLUS_HOME"/scripts/init-postgres.sql "$ZENPLUS_HOME"/scripts/seed-devices.sql "$ZENPLUS_HOME"/scripts/migrate-*.sql; do
        [[ -f "$migration" ]] || continue
        case "$(basename "$migration")" in
            *clickhouse*) continue ;;
        esac
        info "Running $(basename "$migration")..."
        su - postgres -c "psql -d zenplus -f '$migration'" 2>/dev/null || true
    done

    # Grant permissions
    su - postgres -c "psql -d zenplus -c 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zenplus; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO zenplus;'" 2>/dev/null
    log "PostgreSQL configured"

    # Set admin password
    ADMIN_HASH=$("$ZENPLUS_HOME/venv/bin/python3" -c "
from passlib.context import CryptContext
print(CryptContext(schemes=['bcrypt'], deprecated='auto').hash('admin123'))
" 2>/dev/null || echo "")
    if [[ -n "$ADMIN_HASH" ]]; then
        runuser -u postgres -- psql -d zenplus -c "UPDATE users SET password_hash = '$ADMIN_HASH' WHERE username = 'admin';" >/dev/null 2>&1
        log "Admin password set (admin123)"
    fi

    # --- Redis ---
    info "Configuring Redis..."
    if ! grep -q "^requirepass" /etc/redis/redis.conf 2>/dev/null; then
        echo "requirepass $REDIS_PASSWORD" >> /etc/redis/redis.conf
    else
        sed -i "s/^requirepass .*/requirepass $REDIS_PASSWORD/" /etc/redis/redis.conf
    fi
    systemctl restart redis-server
    log "Redis configured"

    # --- ClickHouse (Docker) ---
    info "Starting ClickHouse..."
    cd "$ZENPLUS_HOME"
    docker compose up -d clickhouse 2>&1 | grep -v "^$\|level=warning" || true

    # Wait for ClickHouse
    local retries=30
    while [[ $retries -gt 0 ]]; do
        docker exec zenplus-clickhouse clickhouse-client --password "$CH_PASSWORD" --query "SELECT 1" &>/dev/null && break
        sleep 2; retries=$((retries - 1))
    done

    # Run ClickHouse migrations — init/fix first, then any migrate-*-clickhouse.sql
    # in lexical order. (Pre-1.2.2 builds skipped these, leaving fleets without
    # snmp_if_metrics / services_v2 tables.)
    for sql_file in scripts/init-clickhouse.sql scripts/fix-clickhouse.sql scripts/migrate-*-clickhouse.sql; do
        [[ -f "$sql_file" ]] || continue
        info "Running $(basename "$sql_file") on ClickHouse..."
        docker cp "$sql_file" zenplus-clickhouse:/tmp/m.sql 2>/dev/null
        docker exec zenplus-clickhouse clickhouse-client --password "$CH_PASSWORD" --multiquery --queries-file /tmp/m.sql 2>/dev/null || true
    done

    # Service metrics tables (ClickHouse)
    docker exec zenplus-clickhouse clickhouse-client --password "$CH_PASSWORD" --multiquery <<'CHSQL'
CREATE DATABASE IF NOT EXISTS zenplus;
CREATE TABLE IF NOT EXISTS zenplus.service_metrics (service_check_id UUID, device_id Nullable(UUID), timestamp DateTime64(3, 'UTC'), check_type LowCardinality(String), is_up UInt8, response_ms Float64, status_code Nullable(UInt16), tls_days_remaining Nullable(Int32), tls_valid Nullable(UInt8), content_matched Nullable(UInt8), error_message Nullable(String), poller_id String) ENGINE = MergeTree() PARTITION BY toYYYYMM(timestamp) ORDER BY (service_check_id, timestamp) TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE SETTINGS index_granularity = 8192;
CREATE TABLE IF NOT EXISTS zenplus.service_metrics_5m (service_check_id UUID, device_id Nullable(UUID), timestamp DateTime64(3, 'UTC'), check_type LowCardinality(String), avg_response_ms Float64, min_response_ms Float64, max_response_ms Float64, uptime_pct Float32, sample_count UInt32) ENGINE = MergeTree() PARTITION BY toYYYYMM(timestamp) ORDER BY (service_check_id, timestamp) TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE;
CREATE TABLE IF NOT EXISTS zenplus.service_status_log (service_check_id UUID, device_id Nullable(UUID), timestamp DateTime64(3, 'UTC'), check_type LowCardinality(String), old_status String, new_status String, reason String, duration_sec UInt64) ENGINE = MergeTree() PARTITION BY toYYYYMM(timestamp) ORDER BY (service_check_id, timestamp) TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;
CHSQL
    log "ClickHouse configured"
}

# ═══════════════════════════════════════════════════════════════
# STEP 6: Build components
# ═══════════════════════════════════════════════════════════════
build_components() {
    step "Building ZenPlus components"
    cd "$ZENPLUS_HOME"
    source .env

    # Go poller
    info "Building Go poller..."
    cd "$ZENPLUS_HOME/poller"
    export PATH=/usr/local/go/bin:$PATH
    go mod tidy || err "Go dependency resolution failed"
    CGO_ENABLED=0 go build -buildvcs=false -o "$ZENPLUS_HOME/bin/zenplus-poller" ./cmd/poller || err "Go poller build failed"
    [[ -x "$ZENPLUS_HOME/bin/zenplus-poller" ]] || err "Go poller binary was not created"
    setcap cap_net_raw+ep "$ZENPLUS_HOME/bin/zenplus-poller" 2>/dev/null || true
    log "Go poller built"

    # Remote sensor binary served by /api/v1/sensor/install.sh.
    info "Building Go remote sensor..."
    SENSOR_ARTIFACT_DIR="$ZENPLUS_HOME/artifacts/sensors/bin/linux-amd64"
    mkdir -p "$SENSOR_ARTIFACT_DIR"
    SENSOR_COMMIT=$(cd "$ZENPLUS_HOME" && git rev-parse --short HEAD 2>/dev/null || echo unknown)
    SENSOR_BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false \
        -ldflags "-X main.version=sensor-0.1.0 -X main.commit=$SENSOR_COMMIT -X main.buildDate=$SENSOR_BUILD_DATE" \
        -o "$SENSOR_ARTIFACT_DIR/zenplus-sensor" ./cmd/sensor || err "Go sensor build failed"
    [[ -x "$SENSOR_ARTIFACT_DIR/zenplus-sensor" ]] || err "Go sensor binary was not created"
    ( cd "$SENSOR_ARTIFACT_DIR" && sha256sum zenplus-sensor > zenplus-sensor.sha256 )
    cat > "$SENSOR_ARTIFACT_DIR/manifest.json" <<EOF
{"product":"ZenPlus Remote Sensor","platform":"linux-amd64","version":"sensor-0.1.0","commit":"$SENSOR_COMMIT","built_at":"$SENSOR_BUILD_DATE","binary":"zenplus-sensor","sha256_file":"zenplus-sensor.sha256"}
EOF
    log "Go remote sensor built"

    # Python venv
    info "Setting up Python environment..."
    cd "$ZENPLUS_HOME/server"
    python3 -m venv "$ZENPLUS_HOME/venv"
    "$ZENPLUS_HOME/venv/bin/pip" install -q --upgrade pip
    "$ZENPLUS_HOME/venv/bin/pip" install -q -r requirements.txt
    "$ZENPLUS_HOME/venv/bin/pip" install -q 'bcrypt==4.2.1' fpdf2 matplotlib Pillow
    log "Python environment ready"

    # React dashboard
    info "Building React dashboard..."
    cd "$ZENPLUS_HOME/dashboard"
    npm install --silent 2>&1 | tail -3
    npx vite build 2>&1 | tail -3
    log "Dashboard built"

    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
}

# ═══════════════════════════════════════════════════════════════
# STEP 7: Create systemd services
# ═══════════════════════════════════════════════════════════════
create_services() {
    step "Creating systemd services"

    # Wait-for-deps service
    cat > /etc/systemd/system/zenplus-wait-deps.service <<'SVCEOF'
[Unit]
Description=ZenPlus - Wait for all dependencies
After=postgresql.service redis-server.service docker.service
Requires=postgresql.service redis-server.service docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=/opt/zenplus/.env
ExecStart=/opt/zenplus/bin/wait-for-deps.sh
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
SVCEOF

    # Wait script
    cat > "$ZENPLUS_HOME/bin/wait-for-deps.sh" <<'SCRIPT'
#!/usr/bin/env bash
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
SCRIPT
    chmod +x "$ZENPLUS_HOME/bin/wait-for-deps.sh"

    # API Service
    cat > /etc/systemd/system/zenplus-api.service <<'SVCEOF'
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
Environment=MPLCONFIGDIR=/tmp/matplotlib
ExecStart=/opt/zenplus/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60
# NoNewPrivileges must remain false: the API uses `sudo -n /bin/systemctl ...`
# (per /etc/sudoers.d/zenplus-updater) to manage the OTA updater service and
# write a timer drop-in. NoNewPrivileges=true would strip the sudo setuid bit.
NoNewPrivileges=false
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SVCEOF

    # Poller Service
    cat > /etc/systemd/system/zenplus-poller.service <<'SVCEOF'
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
SVCEOF

    # Nginx config
    if ! command -v nginx &>/dev/null; then
        apt-get install -y -qq nginx > /dev/null 2>&1
    fi

    cat > /etc/nginx/conf.d/zenplus.conf <<'NGINXEOF'
server {
    listen 80;
    server_name _;
    root /opt/zenplus/dashboard/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }

    location ~* \.(js|css|png|jpg|ico|svg|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
NGINXEOF

    # Remove default configs
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf 2>/dev/null
    chmod o+x /opt/zenplus /opt/zenplus/dashboard /opt/zenplus/dashboard/dist
    chmod o+r -R /opt/zenplus/dashboard/dist/

    nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null

    # Enable and start everything
    systemctl daemon-reload
    systemctl enable zenplus-wait-deps zenplus-api zenplus-poller nginx docker postgresql redis-server
    systemctl restart zenplus-wait-deps
    sleep 2
    systemctl restart zenplus-api
    sleep 3
    systemctl restart zenplus-poller
    systemctl restart nginx
    log "All services created and started"
}

# ═══════════════════════════════════════════════════════════════
# STEP 8: OTA update agent (systemd, polkit, sudoers, agent.conf)
#
# Installs everything the dashboard needs to manage updates without a
# follow-up manual script. After this runs:
#   - zenplus-updater.service / .timer are installed and enabled.
#   - The zenplus user can manage the updater unit via polkit (D-Bus
#     calls from the FastAPI process).
#   - /etc/sudoers.d/zenplus-updater grants narrow NOPASSWD entries
#     for the exact systemctl invocations in
#     server/app/api/v1/system_updates.py.
#   - The agent.conf is created with empty appliance creds; the
#     customer registers from the dashboard by pasting a license key.
# ═══════════════════════════════════════════════════════════════
setup_updater() {
    step "Installing OTA update agent"

    # Updater config / log / backup / key directories
    mkdir -p "$ZENPLUS_HOME/updater/"{config,logs,backups,keys}
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME/updater"

    # Default agent.conf — keep existing creds across re-installs.
    AGENT_CONF="$ZENPLUS_HOME/updater/config/agent.conf"
    if [[ ! -f "$AGENT_CONF" ]]; then
        cat > "$AGENT_CONF" <<'CONF'
[server]
url = https://zentryc.com
check_interval_seconds = 14400
download_timeout_seconds = 600

[appliance]
id =
api_key =

[security]
public_key_path = /opt/zenplus/updater/keys/zentryc-release.pub
max_manifest_age_days = 30
verify_tls = true

[update]
backup_dir = /opt/zenplus/updater/backups
max_backups = 3
auto_update = true
maintenance_window_start =
maintenance_window_end =

[logging]
log_file = /opt/zenplus/updater/logs/update.log
log_level = INFO
max_log_size_mb = 10
log_rotate_count = 5
CONF
        chmod 600 "$AGENT_CONF"
        chown "$ZENPLUS_USER:$ZENPLUS_USER" "$AGENT_CONF"
        log "Created default agent.conf"
    else
        log "Preserved existing agent.conf (credentials kept)"
    fi

    # Updater systemd units (shipped with the repo at updater/systemd/)
    if [[ -f "$ZENPLUS_HOME/updater/systemd/zenplus-updater.service" ]]; then
        cp "$ZENPLUS_HOME/updater/systemd/zenplus-updater.service" /etc/systemd/system/
        cp "$ZENPLUS_HOME/updater/systemd/zenplus-updater.timer"   /etc/systemd/system/
        log "Updater systemd units installed"
    else
        warn "updater/systemd/zenplus-updater.service missing in repo — skipped"
    fi

    # Polkit rule — lets the API (running as zenplus) manage the updater unit
    # via D-Bus without needing sudo for every call. Format depends on polkit
    # version; install both and let the daemon pick whichever it understands.
    if [[ -d "$ZENPLUS_HOME/updater/polkit" ]]; then
        POLKIT_VER=$(pkaction --version 2>/dev/null | grep -oP '[\d.]+' | head -1 || echo "0.0")
        POLKIT_MINOR=$(echo "$POLKIT_VER" | cut -d. -f2)
        if [[ "${POLKIT_MINOR:-0}" -ge 106 ]] 2>/dev/null; then
            mkdir -p /etc/polkit-1/rules.d
            cp "$ZENPLUS_HOME/updater/polkit/50-zenplus-updater.rules" \
               /etc/polkit-1/rules.d/ 2>/dev/null || true
        else
            mkdir -p /etc/polkit-1/localauthority/50-local.d
            cp "$ZENPLUS_HOME/updater/polkit/zenplus-updater.pkla" \
               /etc/polkit-1/localauthority/50-local.d/ 2>/dev/null || true
        fi
        systemctl restart polkit 2>/dev/null || true
        log "Polkit rule installed"
    fi

    # Sudoers — narrow NOPASSWD entries for the dashboard's "Check for update"
    # button and the "Set check interval" form. Each line below corresponds to
    # an exact subprocess.run() call in server/app/api/v1/system_updates.py;
    # do not broaden these without updating that file.
    cat > /etc/sudoers.d/zenplus-updater <<'SUDOEOF'
# Generated by ZenPlus installer — do not edit by hand.
# Permits the zenplus service account to manage the OTA updater unit
# and write the timer drop-in used to change the check interval.
zenplus ALL=(root) NOPASSWD: /bin/systemctl --no-block start zenplus-updater.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl start zenplus-updater.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl restart zenplus-updater.timer
zenplus ALL=(root) NOPASSWD: /bin/systemctl daemon-reload
zenplus ALL=(root) NOPASSWD: /bin/mkdir -p /etc/systemd/system/zenplus-updater.timer.d
zenplus ALL=(root) NOPASSWD: /usr/bin/tee /etc/systemd/system/zenplus-updater.timer.d/override.conf
SUDOEOF
    chmod 0440 /etc/sudoers.d/zenplus-updater
    if visudo -cf /etc/sudoers.d/zenplus-updater >/dev/null 2>&1; then
        log "Sudoers rule installed and validated"
    else
        warn "sudoers validation failed — removing"
        rm -f /etc/sudoers.d/zenplus-updater
    fi

    # Reload + enable + arm the timer.
    systemctl daemon-reload
    systemctl enable zenplus-updater.timer 2>/dev/null || true
    systemctl restart zenplus-updater.timer 2>/dev/null || true

    # Restart the API so it picks up the new NoNewPrivileges=false setting
    # (no-op on a fresh install, but matters for re-runs over an old install).
    systemctl restart zenplus-api 2>/dev/null || true

    log "OTA timer armed (checks every 4h, ±5m randomized; first check 5m after boot)"
}

# ═══════════════════════════════════════════════════════════════
# STEP 8b: Tech-support bundle generator
#
# Installs the systemd template, narrow sudoers grant, and runtime
# directories that the Settings → Support tab uses to generate a
# diagnostic .tar.gz. Idempotent — safe to re-run.
# ═══════════════════════════════════════════════════════════════
setup_support_bundles() {
    step "Installing tech-support bundle generator"
    if [[ -x "$ZENPLUS_HOME/scripts/setup-support.sh" ]]; then
        bash "$ZENPLUS_HOME/scripts/setup-support.sh"
    else
        echo "  ! setup-support.sh missing — skipping"
    fi
}

# ═══════════════════════════════════════════════════════════════
# STEP 9: Create management CLI
# ═══════════════════════════════════════════════════════════════
create_cli() {
    step "Creating management CLI"
    cat > /usr/local/bin/zenplus <<'CLIEOF'
#!/usr/bin/env bash
set -uo pipefail
ZENPLUS_HOME="/opt/zenplus"
ZENPLUS_USER="zenplus"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
get_ip() { ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1; }

run_migrations() {
    # Apply any pending migrate-*.sql files. Postgres migrations are
    # expected to be idempotent (CREATE TABLE IF NOT EXISTS, etc.).
    set +e
    [[ -f "$ZENPLUS_HOME/.env" ]] && set -a && . "$ZENPLUS_HOME/.env" && set +a
    for sql in "$ZENPLUS_HOME"/scripts/migrate-*.sql; do
        [[ -f "$sql" ]] || continue
        case "$(basename "$sql")" in *clickhouse*) continue ;; esac
        echo "  pg: $(basename "$sql")"
        sudo -u postgres psql -d zenplus -f "$sql" >/dev/null 2>&1
    done
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^zenplus-clickhouse$'; then
        for sql in "$ZENPLUS_HOME"/scripts/migrate-*-clickhouse.sql; do
            [[ -f "$sql" ]] || continue
            echo "  ch: $(basename "$sql")"
            docker cp "$sql" zenplus-clickhouse:/tmp/m.sql >/dev/null 2>&1
            docker exec zenplus-clickhouse clickhouse-client \
                --password "${CLICKHOUSE_PASSWORD:-}" \
                --multiquery --queries-file /tmp/m.sql >/dev/null 2>&1
        done
    fi
    set -e
}

reinstall_units() {
    # Re-copy systemd unit files in case a release shipped an updated copy.
    # Idempotent: overwrites with the in-tree version, then daemon-reload.
    for u in zenplus-updater.service zenplus-updater.timer; do
        src="$ZENPLUS_HOME/updater/systemd/$u"
        [[ -f "$src" ]] && cp "$src" "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
}

case "${1:-help}" in
    status)
        echo -e "${CYAN}${BOLD}ZenPlus Network Monitoring${NC}"
        echo ""
        echo -e "  Version:  $(cat $ZENPLUS_HOME/.version 2>/dev/null | head -1 || echo unknown)"
        echo -e "  Commit:   $(cd $ZENPLUS_HOME && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        echo -e "  IP:       $(get_ip)"
        echo ""
        echo -e "  ${BOLD}Services:${NC}"
        for svc in zenplus-wait-deps zenplus-api zenplus-poller zenplus-updater.timer nginx postgresql redis-server; do
            st=$(systemctl is-active $svc 2>/dev/null || echo "inactive")
            en=$(systemctl is-enabled $svc 2>/dev/null || echo "disabled")
            if [[ "$st" == "active" || "$st" == "waiting" ]]; then
                echo -e "    ${GREEN}●${NC} $svc  ${GREEN}$st${NC}  [$en]"
            else
                echo -e "    ${RED}●${NC} $svc  ${RED}$st${NC}  [$en]"
            fi
        done
        echo ""; echo -e "  ${BOLD}Docker:${NC}"
        cd $ZENPLUS_HOME && docker compose ps --format "    {{.Name}}  {{.Status}}" 2>/dev/null
        echo ""; echo -e "  ${BOLD}OTA Registration:${NC}"
        APP_ID=$(grep -Po '(?<=^id = ).*' $ZENPLUS_HOME/updater/config/agent.conf 2>/dev/null || echo "")
        if [[ -n "$APP_ID" ]]; then
            echo -e "    ${GREEN}registered${NC} as $APP_ID"
        else
            echo -e "    ${YELLOW}not registered${NC} — paste a license key in Settings → Subscription"
        fi
        echo ""; echo -e "  ${BOLD}Access:${NC}"
        echo -e "    Dashboard:  http://$(get_ip)"
        echo -e "    API Docs:   http://$(get_ip)/docs"
        echo -e "    Login:      admin / admin123"
        ;;
    restart) echo "Restarting..."; systemctl restart zenplus-api zenplus-poller nginx; echo -e "${GREEN}Done${NC}" ;;
    stop)    echo "Stopping..."; systemctl stop zenplus-poller zenplus-api; echo -e "${YELLOW}Stopped${NC}" ;;
    start)   echo "Starting..."; systemctl start docker postgresql redis-server; docker start zenplus-clickhouse 2>/dev/null; systemctl start zenplus-wait-deps zenplus-api zenplus-poller nginx zenplus-updater.timer; echo -e "${GREEN}Started${NC}" ;;
    logs)    journalctl -u "zenplus-${2:-api}" -f ;;
    update)
        echo "Updating..."
        cd "$ZENPLUS_HOME" || exit 1
        git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null
        OLD=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
        git fetch origin && git reset --hard origin/main
        NEW=$(git rev-parse --short HEAD)
        if [[ "$OLD" == "$NEW" ]]; then echo "Already up to date ($OLD)"; exit 0; fi
        echo "  $OLD -> $NEW"
        export PATH=/usr/local/go/bin:$PATH
        echo "  building poller..."
        ( cd "$ZENPLUS_HOME/poller" && go mod tidy && CGO_ENABLED=0 go build -buildvcs=false -o "$ZENPLUS_HOME/bin/zenplus-poller" ./cmd/poller ) || { echo "Poller build failed"; exit 1; }
        [[ -x "$ZENPLUS_HOME/bin/zenplus-poller" ]] || { echo "Poller binary was not created"; exit 1; }
        setcap cap_net_raw+ep "$ZENPLUS_HOME/bin/zenplus-poller" 2>/dev/null || true
        echo "  building remote sensor..."
        SENSOR_ARTIFACT_DIR="$ZENPLUS_HOME/artifacts/sensors/bin/linux-amd64"
        mkdir -p "$SENSOR_ARTIFACT_DIR"
        SENSOR_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
        SENSOR_BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        ( cd "$ZENPLUS_HOME/poller" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false \
            -ldflags "-X main.version=sensor-0.1.0 -X main.commit=$SENSOR_COMMIT -X main.buildDate=$SENSOR_BUILD_DATE" \
            -o "$SENSOR_ARTIFACT_DIR/zenplus-sensor" ./cmd/sensor ) || { echo "Sensor build failed"; exit 1; }
        [[ -x "$SENSOR_ARTIFACT_DIR/zenplus-sensor" ]] || { echo "Sensor binary was not created"; exit 1; }
        ( cd "$SENSOR_ARTIFACT_DIR" && sha256sum zenplus-sensor > zenplus-sensor.sha256 )
        cat > "$SENSOR_ARTIFACT_DIR/manifest.json" <<EOF
{"product":"ZenPlus Remote Sensor","platform":"linux-amd64","version":"sensor-0.1.0","commit":"$SENSOR_COMMIT","built_at":"$SENSOR_BUILD_DATE","binary":"zenplus-sensor","sha256_file":"zenplus-sensor.sha256"}
EOF
        echo "  installing python deps..."
        "$ZENPLUS_HOME/venv/bin/pip" install -q -r "$ZENPLUS_HOME/server/requirements.txt"
        echo "  building dashboard..."
        ( cd "$ZENPLUS_HOME/dashboard" && npm install --silent 2>/dev/null && npx vite build 2>/dev/null )
        echo "  running migrations..."
        run_migrations
        echo "  reloading systemd..."
        reinstall_units
        chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME" 2>/dev/null
        echo "  restarting services..."
        systemctl restart zenplus-api zenplus-poller nginx
        systemctl restart zenplus-updater.timer 2>/dev/null || true
        echo "$NEW" > "$ZENPLUS_HOME/.version"
        date -Iseconds >> "$ZENPLUS_HOME/.version"
        echo -e "${GREEN}Updated to $NEW${NC}"
        ;;
    backup)
        TS=$(date +%Y%m%d-%H%M%S)
        OUT="$ZENPLUS_HOME/backups/zenplus-$TS.sql.gz"
        mkdir -p "$ZENPLUS_HOME/backups"
        sudo -u postgres pg_dump zenplus | gzip > "$OUT" && echo -e "${GREEN}Backup:${NC} $OUT"
        ;;
    help|*)  echo -e "${CYAN}${BOLD}ZenPlus CLI${NC}\n\nUsage: zenplus <command>\n\n  status    Show services + OTA registration\n  start     Start all\n  stop      Stop app services\n  restart   Restart app services\n  update    Pull, migrate, rebuild, reload, restart\n  backup    Dump postgres to backups/\n  logs      Tail logs (api|poller|updater)" ;;
esac
CLIEOF
    chmod +x /usr/local/bin/zenplus
    log "CLI installed"
}

# ═══════════════════════════════════════════════════════════════
# STEP 9: Finalize
# ═══════════════════════════════════════════════════════════════
finalize() {
    step "Finalizing installation"
    cd "$ZENPLUS_HOME"
    echo "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" > "$ZENPLUS_VERSION_FILE"
    echo "$(date -Iseconds)" >> "$ZENPLUS_VERSION_FILE"
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"

    local IP=$(get_ip)
    info "Waiting for API..."
    local retries=15
    while [[ $retries -gt 0 ]]; do
        curl -sf http://localhost:8000/api/v1/system/health > /dev/null 2>&1 && break
        sleep 2; retries=$((retries - 1))
    done

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║        ZenPlus Installation Complete!            ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "  ${BOLD}Dashboard:${NC}  http://${IP}"
    echo -e "  ${BOLD}API Docs:${NC}   http://${IP}/docs"
    echo -e "  ${BOLD}Login:${NC}      admin / admin123"
    echo ""
    echo -e "  ${BOLD}Features:${NC}"
    echo -e "    - Device Monitoring (ICMP Ping)"
    echo -e "    - Service Checks (HTTP, TCP, TLS/SSL)"
    echo -e "    - Alert Rules & Notifications (Email, SMS, Webhook)"
    echo -e "    - PDF Reporting System"
    echo -e "    - Real-time Dashboard with SSE"
    echo ""
    echo -e "  ${BOLD}Management:${NC}"
    echo -e "    sudo zenplus status     Show status"
    echo -e "    sudo zenplus update     Update to latest"
    echo -e "    sudo zenplus restart    Restart services"
    echo -e "    sudo zenplus logs api   View API logs"
    echo ""
    echo -e "  ${YELLOW}Change the default password after first login!${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
main() {
    check_root
    show_banner
    install_prerequisites
    setup_user
    fetch_code
    configure_env
    build_components
    setup_databases
    create_services
    setup_updater
    setup_support_bundles
    create_cli
    finalize
}

main "$@"
