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
        git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
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
    fi

    cat > "$ENV_FILE" <<ENVEOF
# ZenPlus Configuration - Generated $(date -Iseconds)
POSTGRES_PASSWORD=$DB_PASSWORD
CLICKHOUSE_PASSWORD=$CH_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
JWT_SECRET=$JWT_SECRET
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

    # Run migrations
    for migration in "$ZENPLUS_HOME"/scripts/init-postgres.sql "$ZENPLUS_HOME"/scripts/seed-devices.sql "$ZENPLUS_HOME"/scripts/migrate-*.sql; do
        [[ -f "$migration" ]] || continue
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
        su - postgres -c "psql -d zenplus -c \"UPDATE users SET password_hash = '$ADMIN_HASH' WHERE username = 'admin';\"" 2>/dev/null
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

    # Run ClickHouse migrations
    for sql_file in scripts/init-clickhouse.sql scripts/fix-clickhouse.sql; do
        [[ -f "$sql_file" ]] || continue
        docker cp "$sql_file" zenplus-clickhouse:/tmp/init.sql 2>/dev/null
        docker exec zenplus-clickhouse clickhouse-client --password "$CH_PASSWORD" --multiquery --queries-file /tmp/init.sql 2>/dev/null || true
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
    go mod tidy 2>/dev/null
    CGO_ENABLED=0 go build -o "$ZENPLUS_HOME/bin/zenplus-poller" ./cmd/poller
    setcap cap_net_raw+ep "$ZENPLUS_HOME/bin/zenplus-poller" 2>/dev/null || true
    log "Go poller built"

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
NoNewPrivileges=true
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
# STEP 8: Create management CLI
# ═══════════════════════════════════════════════════════════════
create_cli() {
    step "Creating management CLI"
    cat > /usr/local/bin/zenplus <<'CLIEOF'
#!/usr/bin/env bash
set -uo pipefail
ZENPLUS_HOME="/opt/zenplus"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
get_ip() { ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1; }

case "${1:-help}" in
    status)
        echo -e "${CYAN}${BOLD}ZenPlus Network Monitoring${NC}"
        echo ""
        echo -e "  Version:  $(cd $ZENPLUS_HOME && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        echo -e "  IP:       $(get_ip)"
        echo ""
        echo -e "  ${BOLD}Services:${NC}"
        for svc in zenplus-wait-deps zenplus-api zenplus-poller nginx postgresql redis-server; do
            st=$(systemctl is-active $svc 2>/dev/null || echo "inactive")
            en=$(systemctl is-enabled $svc 2>/dev/null || echo "disabled")
            [[ "$st" == "active" || "$st" == "active" ]] && echo -e "    ${GREEN}●${NC} $svc  ${GREEN}running${NC}  [$en]" || echo -e "    ${RED}●${NC} $svc  ${RED}$st${NC}  [$en]"
        done
        echo ""; echo -e "  ${BOLD}Docker:${NC}"
        cd $ZENPLUS_HOME && docker compose ps --format "    {{.Name}}  {{.Status}}" 2>/dev/null
        echo ""; echo -e "  ${BOLD}Access:${NC}"
        echo -e "    Dashboard:  http://$(get_ip)"
        echo -e "    API Docs:   http://$(get_ip)/docs"
        echo -e "    Login:      admin / admin123"
        ;;
    restart) echo "Restarting..."; systemctl restart zenplus-api zenplus-poller nginx; echo -e "${GREEN}Done${NC}" ;;
    stop)    echo "Stopping..."; systemctl stop zenplus-poller zenplus-api; echo -e "${YELLOW}Stopped${NC}" ;;
    start)   echo "Starting..."; systemctl start docker postgresql redis-server; docker start zenplus-clickhouse 2>/dev/null; systemctl start zenplus-wait-deps zenplus-api zenplus-poller nginx; echo -e "${GREEN}Started${NC}" ;;
    logs)    journalctl -u "zenplus-${2:-api}" -f ;;
    update)  echo "Updating..."; cd $ZENPLUS_HOME; OLD=$(git rev-parse --short HEAD); git fetch origin && git reset --hard origin/main; NEW=$(git rev-parse --short HEAD); [[ "$OLD" == "$NEW" ]] && { echo "Already up to date"; exit 0; }; echo "$OLD -> $NEW"; export PATH=/usr/local/go/bin:$PATH; cd poller && go mod tidy 2>/dev/null && CGO_ENABLED=0 go build -o $ZENPLUS_HOME/bin/zenplus-poller ./cmd/poller; setcap cap_net_raw+ep $ZENPLUS_HOME/bin/zenplus-poller 2>/dev/null; cd $ZENPLUS_HOME/server && $ZENPLUS_HOME/venv/bin/pip install -q -r requirements.txt; cd $ZENPLUS_HOME/dashboard && npm install --silent 2>/dev/null && npx vite build 2>/dev/null; systemctl restart zenplus-api zenplus-poller nginx; echo -e "${GREEN}Updated${NC}" ;;
    help|*)  echo -e "${CYAN}${BOLD}ZenPlus CLI${NC}\n\nUsage: zenplus <command>\n\n  status    Show status\n  start     Start all\n  stop      Stop app services\n  restart   Restart all\n  update    Pull & rebuild\n  logs      View logs (api|poller)" ;;
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
    create_cli
    finalize
}

main "$@"
