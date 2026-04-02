#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  ZenPlus Network Monitoring System - Installer / Updater       ║
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
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

# ─── Helpers ──────────────────────────────────────────────────────
log()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
info()  { echo -e "${BLUE}[i]${NC} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (use sudo)"
    fi
}

get_ip() {
    ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1
}

# ─── Detect mode: install or update ──────────────────────────────
detect_mode() {
    if [[ -d "$ZENPLUS_HOME" && -f "$ZENPLUS_HOME/docker-compose.yml" ]]; then
        echo "update"
    else
        echo "install"
    fi
}

# ─── Banner ───────────────────────────────────────────────────────
show_banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║     ______          ____  __                     ║"
    echo "  ║    /_  __/__  ___  / __ \/ /_  _______           ║"
    echo "  ║     / / / _ \/ _ \/ /_/ / / / / / ___/           ║"
    echo "  ║    / / /  __/  __/ ____/ / /_/ (__  )            ║"
    echo "  ║   /_/  \___/\___/_/   /_/\__,_/____/             ║"
    echo "  ║                                                  ║"
    echo "  ║   Network Monitoring System                      ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ═════════════════════════════════════════════════════════════════
# STEP 1: Prerequisites
# ═════════════════════════════════════════════════════════════════
install_prerequisites() {
    step "Installing prerequisites"

    export DEBIAN_FRONTEND=noninteractive

    # Update package list
    info "Updating package lists..."
    apt-get update -qq

    # Core tools
    info "Installing core packages..."
    apt-get install -y -qq \
        curl wget git apt-transport-https ca-certificates \
        gnupg lsb-release software-properties-common \
        python3 python3-pip python3-venv \
        build-essential jq openssl \
        > /dev/null 2>&1
    log "Core packages installed"

    # Docker
    if ! command -v docker &>/dev/null; then
        info "Installing Docker..."
        if curl -fsSL https://get.docker.com | sh; then
            systemctl enable docker --now 2>/dev/null || true
            log "Docker installed"
        else
            err "Docker installation failed. Check network connectivity."
        fi
    else
        systemctl enable docker --now 2>/dev/null || true
        log "Docker already installed ($(docker --version | cut -d' ' -f3))"
    fi

    # Docker Compose plugin
    if ! docker compose version &>/dev/null; then
        info "Installing Docker Compose plugin..."
        apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
        if ! docker compose version &>/dev/null; then
            warn "Docker Compose plugin not available, trying standalone..."
            COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)"
            curl -fsSL "$COMPOSE_URL" -o /usr/local/bin/docker-compose 2>/dev/null && chmod +x /usr/local/bin/docker-compose || true
        fi
        log "Docker Compose installed"
    else
        log "Docker Compose already installed"
    fi

    # Go
    if ! command -v go &>/dev/null || [[ "$(go version 2>/dev/null | grep -oP '\d+\.\d+' | head -1)" < "1.22" ]]; then
        info "Installing Go 1.22..."
        GO_VERSION="1.22.5"
        ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")
        GO_URL="https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz"
        info "Downloading from $GO_URL ..."
        if wget -q --timeout=60 "$GO_URL" -O /tmp/go.tar.gz; then
            rm -rf /usr/local/go
            tar -C /usr/local -xzf /tmp/go.tar.gz
            rm -f /tmp/go.tar.gz
            ln -sf /usr/local/go/bin/go /usr/local/bin/go
            log "Go ${GO_VERSION} installed"
        else
            warn "Failed to download Go from $GO_URL"
            info "Trying apt-get install golang-go as fallback..."
            apt-get install -y -qq golang-go 2>/dev/null || true
            if command -v go &>/dev/null; then
                log "Go installed via apt ($(go version | cut -d' ' -f3))"
            else
                err "Failed to install Go. Please install manually and re-run."
            fi
        fi
    else
        log "Go already installed ($(go version | cut -d' ' -f3))"
    fi

    # Node.js 20+
    if ! command -v node &>/dev/null || [[ "$(node -v 2>/dev/null | cut -d'.' -f1 | tr -d 'v')" -lt 20 ]]; then
        info "Installing Node.js 20..."
        if curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1; then
            apt-get install -y -qq nodejs > /dev/null 2>&1
        else
            warn "NodeSource setup failed, trying apt..."
            apt-get install -y -qq nodejs npm > /dev/null 2>&1 || true
        fi
        if command -v node &>/dev/null; then
            log "Node.js $(node -v) installed"
        else
            err "Failed to install Node.js. Please install manually and re-run."
        fi
    else
        log "Node.js already installed ($(node -v))"
    fi

    # Enable ICMP for non-root
    sysctl -w net.ipv4.ping_group_range="0 2147483647" > /dev/null 2>&1
    echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-zenplus-ping.conf

    log "All prerequisites installed"
}

# ═════════════════════════════════════════════════════════════════
# STEP 2: Create system user & directories
# ═════════════════════════════════════════════════════════════════
setup_user() {
    step "Setting up system user"

    if ! id "$ZENPLUS_USER" &>/dev/null; then
        useradd -r -m -d "$ZENPLUS_HOME" -s /bin/bash "$ZENPLUS_USER"
        usermod -aG docker "$ZENPLUS_USER"
        log "Created system user: $ZENPLUS_USER"
    else
        log "User $ZENPLUS_USER already exists"
    fi

    mkdir -p "$ZENPLUS_HOME"/{data,logs,backups}
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
}

# ═════════════════════════════════════════════════════════════════
# STEP 3: Clone / Pull repository
# ═════════════════════════════════════════════════════════════════
fetch_code() {
    step "Fetching ZenPlus source code"

    if [[ -d "$ZENPLUS_HOME/.git" ]]; then
        info "Pulling latest changes..."
        cd "$ZENPLUS_HOME"
        git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
        git fetch origin
        git reset --hard "origin/$ZENPLUS_BRANCH"
        CURRENT_VERSION=$(git rev-parse --short HEAD)
        log "Updated to $CURRENT_VERSION"
    else
        info "Cloning repository..."
        # Save existing data dirs
        for dir in data logs backups; do
            [[ -d "$ZENPLUS_HOME/$dir" ]] && mv "$ZENPLUS_HOME/$dir" "/tmp/zenplus-save-$dir" 2>/dev/null || true
        done
        ENV_BACKUP=""
        [[ -f "$ZENPLUS_HOME/.env" ]] && ENV_BACKUP=$(cat "$ZENPLUS_HOME/.env")

        rm -rf "$ZENPLUS_HOME"
        git clone -b "$ZENPLUS_BRANCH" "$ZENPLUS_REPO" "$ZENPLUS_HOME"

        # Restore saved dirs
        for dir in data logs backups; do
            [[ -d "/tmp/zenplus-save-$dir" ]] && mv "/tmp/zenplus-save-$dir" "$ZENPLUS_HOME/$dir"
        done
        mkdir -p "$ZENPLUS_HOME"/{data,logs,backups,bin}

        [[ -n "$ENV_BACKUP" ]] && echo "$ENV_BACKUP" > "$ZENPLUS_HOME/.env"

        CURRENT_VERSION=$(cd "$ZENPLUS_HOME" && git rev-parse --short HEAD)
        log "Cloned version $CURRENT_VERSION"
    fi

    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
}

# ═════════════════════════════════════════════════════════════════
# STEP 4: Configure environment
# ═════════════════════════════════════════════════════════════════
configure_env() {
    step "Configuring environment"

    ENV_FILE="$ZENPLUS_HOME/.env"

    if [[ -f "$ENV_FILE" ]]; then
        info "Existing .env found, preserving credentials"
        source "$ENV_FILE"
        # Use existing passwords
        DB_PASSWORD="${POSTGRES_PASSWORD:-$DB_PASSWORD}"
        CH_PASSWORD="${CLICKHOUSE_PASSWORD:-$CH_PASSWORD}"
        REDIS_PASSWORD="${REDIS_PASSWORD:-$REDIS_PASSWORD}"
        JWT_SECRET="${JWT_SECRET:-$JWT_SECRET}"
    fi

    cat > "$ENV_FILE" <<ENVEOF
# ZenPlus Configuration - Auto-generated $(date -Iseconds)
# Do NOT delete - contains database credentials

# Database
POSTGRES_PASSWORD=$DB_PASSWORD
CLICKHOUSE_PASSWORD=$CH_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD

# API
JWT_SECRET=$JWT_SECRET
API_HOST=0.0.0.0
API_PORT=8000

# Poller
POLLER_ID=poller-01
ENVEOF

    chmod 600 "$ENV_FILE"
    chown "$ZENPLUS_USER:$ZENPLUS_USER" "$ENV_FILE"
    log "Environment configured"
}

# ═════════════════════════════════════════════════════════════════
# STEP 5: Build components
# ═════════════════════════════════════════════════════════════════
build_components() {
    step "Building ZenPlus components"

    cd "$ZENPLUS_HOME"
    source .env

    # Build Go poller
    info "Building Go poller..."
    cd "$ZENPLUS_HOME/poller"
    sudo -u "$ZENPLUS_USER" go mod tidy 2>/dev/null
    sudo -u "$ZENPLUS_USER" CGO_ENABLED=0 go build -o "$ZENPLUS_HOME/bin/zenplus-poller" ./cmd/poller
    setcap cap_net_raw+ep "$ZENPLUS_HOME/bin/zenplus-poller" 2>/dev/null || true
    log "Go poller built"

    # Setup Python venv
    info "Setting up Python environment..."
    cd "$ZENPLUS_HOME/server"
    sudo -u "$ZENPLUS_USER" python3 -m venv "$ZENPLUS_HOME/venv"
    sudo -u "$ZENPLUS_USER" "$ZENPLUS_HOME/venv/bin/pip" install -q --upgrade pip
    sudo -u "$ZENPLUS_USER" "$ZENPLUS_HOME/venv/bin/pip" install -q -r requirements.txt
    # Pin bcrypt for passlib compatibility
    sudo -u "$ZENPLUS_USER" "$ZENPLUS_HOME/venv/bin/pip" install -q 'bcrypt==4.2.1'
    log "Python environment ready"

    # Build React dashboard
    info "Building React dashboard..."
    cd "$ZENPLUS_HOME/dashboard"
    sudo -u "$ZENPLUS_USER" npm install 2>&1 | tail -3
    if [[ -f node_modules/.bin/vite ]]; then
        sudo -u "$ZENPLUS_USER" ./node_modules/.bin/vite build 2>&1 | tail -5
    else
        sudo -u "$ZENPLUS_USER" npx vite build 2>&1 | tail -5
    fi
    if [[ -d "$ZENPLUS_HOME/dashboard/dist" ]]; then
        log "Dashboard built ($(ls dist/assets/*.js 2>/dev/null | wc -l) JS bundles)"
    else
        warn "Dashboard build may have failed - dist/ not found"
    fi

    mkdir -p "$ZENPLUS_HOME/bin"
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
}

# ═════════════════════════════════════════════════════════════════
# STEP 6: Start infrastructure (Docker)
# ═════════════════════════════════════════════════════════════════
start_infrastructure() {
    step "Starting infrastructure services"

    cd "$ZENPLUS_HOME"

    # Ensure .env is sourced for docker compose
    source "$ZENPLUS_HOME/.env" 2>/dev/null || true

    # Start PostgreSQL, ClickHouse, Redis
    docker compose up -d postgres clickhouse redis 2>&1 | grep -v "^$\|level=warning"

    info "Waiting for databases to be healthy..."
    local retries=30
    while [[ $retries -gt 0 ]]; do
        if docker compose ps --format json 2>/dev/null | grep -q '"healthy"' || \
           docker compose ps 2>/dev/null | grep -c "healthy" | grep -q "3"; then
            break
        fi
        sleep 2
        retries=$((retries - 1))
    done

    if [[ $retries -eq 0 ]]; then
        warn "Databases may not be fully ready yet, continuing..."
    else
        log "All databases healthy"
    fi
}

# ═════════════════════════════════════════════════════════════════
# STEP 7: Run database migrations
# ═════════════════════════════════════════════════════════════════
run_migrations() {
    step "Running database migrations"

    cd "$ZENPLUS_HOME"
    source .env

    # Determine Postgres user - docker-compose uses 'zenplus' but init SQL creates as 'netpulse' schema
    # Try both users
    PG_USER="zenplus"
    if ! docker compose exec -T postgres psql -U "$PG_USER" -c "SELECT 1" &>/dev/null; then
        PG_USER="netpulse"
    fi
    info "Using PostgreSQL user: $PG_USER"

    # Run migration scripts if they exist
    for migration in scripts/migrate-*.sql; do
        [[ -f "$migration" ]] || continue
        info "Running $migration..."
        docker compose exec -T postgres psql -U "$PG_USER" < "$migration" 2>&1 | tail -3 || true
    done

    # Fix ClickHouse tables - run each statement individually
    info "Ensuring ClickHouse schema..."
    CH_PASS="${CLICKHOUSE_PASSWORD:-$CH_PASSWORD}"
    for sql_file in scripts/init-clickhouse.sql scripts/fix-clickhouse.sql; do
        [[ -f "$sql_file" ]] || continue
        # Copy file into container and run
        docker cp "$sql_file" zenplus-clickhouse:/tmp/init.sql 2>/dev/null || true
        docker compose exec -T clickhouse clickhouse-client \
            --password "$CH_PASS" --multiquery \
            --queries-file /tmp/init.sql 2>/dev/null || true
    done

    # Generate proper bcrypt hash for admin user
    info "Setting admin password..."
    ADMIN_HASH=$("$ZENPLUS_HOME/venv/bin/python3" -c "
from passlib.context import CryptContext
print(CryptContext(schemes=['bcrypt'], deprecated='auto').hash('admin123'))
" 2>/dev/null || echo "")

    if [[ -n "$ADMIN_HASH" ]]; then
        docker compose exec -T postgres psql -U "$PG_USER" -c \
            "UPDATE users SET password_hash = '$ADMIN_HASH' WHERE username = 'admin';" 2>/dev/null || true
        log "Admin password set"
    else
        warn "Could not generate admin password hash"
    fi

    log "Migrations complete"
}

# ═════════════════════════════════════════════════════════════════
# STEP 8: Create systemd services
# ═════════════════════════════════════════════════════════════════
create_services() {
    step "Creating systemd services"

    # API Service
    cat > /etc/systemd/system/zenplus-api.service <<SVCEOF
[Unit]
Description=ZenPlus API Server
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$ZENPLUS_USER
WorkingDirectory=$ZENPLUS_HOME/server
EnvironmentFile=$ZENPLUS_HOME/.env
Environment=DATABASE_URL=postgresql+asyncpg://zenplus:\${POSTGRES_PASSWORD}@localhost:5432/zenplus
Environment=CLICKHOUSE_HOST=localhost
Environment=CLICKHOUSE_DB=zenplus
Environment=CLICKHOUSE_USER=default
Environment=CLICKHOUSE_PASSWORD=\${CLICKHOUSE_PASSWORD}
Environment=REDIS_URL=redis://:\${REDIS_PASSWORD}@localhost:6379/0
ExecStart=$ZENPLUS_HOME/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

    # Poller Service
    cat > /etc/systemd/system/zenplus-poller.service <<SVCEOF
[Unit]
Description=ZenPlus Ping Poller
After=zenplus-api.service docker.service
Requires=docker.service

[Service]
Type=simple
User=$ZENPLUS_USER
WorkingDirectory=$ZENPLUS_HOME
EnvironmentFile=$ZENPLUS_HOME/.env
Environment=POSTGRES_DB=zenplus
Environment=POSTGRES_USER=zenplus
Environment=CLICKHOUSE_DB=zenplus
ExecStart=$ZENPLUS_HOME/bin/zenplus-poller
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
SVCEOF

    # Dashboard (serve built files via Python)
    cat > /etc/systemd/system/zenplus-dashboard.service <<SVCEOF
[Unit]
Description=ZenPlus Dashboard
After=zenplus-api.service

[Service]
Type=simple
User=$ZENPLUS_USER
WorkingDirectory=$ZENPLUS_HOME/dashboard/dist
ExecStart=$ZENPLUS_HOME/venv/bin/python3 -m http.server 3000 --bind 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

    # Install Nginx for production reverse proxy
    if ! command -v nginx &>/dev/null; then
        info "Installing Nginx..."
        apt-get install -y -qq nginx > /dev/null 2>&1
    fi

    # Nginx config
    cat > /etc/nginx/sites-available/zenplus <<NGINXEOF
server {
    listen 80;
    server_name _;

    # Dashboard
    root $ZENPLUS_HOME/dashboard/dist;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # API proxy
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }

    # Static assets caching
    location ~* \.(js|css|png|jpg|ico|svg|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
NGINXEOF

    ln -sf /etc/nginx/sites-available/zenplus /etc/nginx/sites-enabled/zenplus
    rm -f /etc/nginx/sites-enabled/default
    nginx -t 2>/dev/null && systemctl reload nginx

    # Enable and start services
    systemctl daemon-reload
    systemctl enable zenplus-api zenplus-poller zenplus-dashboard nginx
    systemctl restart zenplus-api
    sleep 3
    systemctl restart zenplus-poller
    systemctl restart nginx

    log "All services created and started"
}

# ═════════════════════════════════════════════════════════════════
# STEP 9: Create management CLI
# ═════════════════════════════════════════════════════════════════
create_cli() {
    step "Creating management CLI"

    cat > /usr/local/bin/zenplus <<'CLIEOF'
#!/usr/bin/env bash
set -uo pipefail

ZENPLUS_HOME="/opt/zenplus"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

case "${1:-help}" in
    status)
        echo -e "${CYAN}${BOLD}ZenPlus Status${NC}"
        echo ""
        echo -e "Version:    $(cd $ZENPLUS_HOME && git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
        echo -e "IP:         $(ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)"
        echo ""
        for svc in zenplus-api zenplus-poller nginx; do
            status=$(systemctl is-active $svc 2>/dev/null || echo "inactive")
            if [[ "$status" == "active" ]]; then
                echo -e "  ${GREEN}●${NC} $svc: ${GREEN}running${NC}"
            else
                echo -e "  ${RED}●${NC} $svc: ${RED}$status${NC}"
            fi
        done
        echo ""
        cd $ZENPLUS_HOME && docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null
        echo ""
        echo -e "Dashboard: http://$(ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)"
        echo -e "API Docs:  http://$(ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)/docs"
        echo -e "Login:     admin / admin123"
        ;;

    update)
        echo -e "${CYAN}${BOLD}Updating ZenPlus...${NC}"
        cd $ZENPLUS_HOME

        OLD_VERSION=$(git rev-parse --short HEAD)
        echo "Current version: $OLD_VERSION"

        git fetch origin
        git reset --hard origin/main

        NEW_VERSION=$(git rev-parse --short HEAD)
        echo "New version: $NEW_VERSION"

        if [[ "$OLD_VERSION" == "$NEW_VERSION" ]]; then
            echo -e "${GREEN}Already up to date!${NC}"
            exit 0
        fi

        echo "Building..."
        # Rebuild Go poller
        cd $ZENPLUS_HOME/poller && go mod tidy 2>/dev/null && CGO_ENABLED=0 go build -o $ZENPLUS_HOME/bin/zenplus-poller ./cmd/poller
        setcap cap_net_raw+ep $ZENPLUS_HOME/bin/zenplus-poller 2>/dev/null || true

        # Update Python deps
        cd $ZENPLUS_HOME/server && $ZENPLUS_HOME/venv/bin/pip install -q -r requirements.txt

        # Rebuild dashboard
        cd $ZENPLUS_HOME/dashboard && npm install --silent 2>/dev/null && npx vite build 2>/dev/null

        # Run migrations
        source $ZENPLUS_HOME/.env
        for migration in $ZENPLUS_HOME/scripts/migrate-*.sql; do
            [[ -f "$migration" ]] || continue
            cd $ZENPLUS_HOME && docker compose exec -T postgres psql -U netpulse -f "/dev/stdin" < "$migration" 2>/dev/null || true
        done

        # Restart services
        systemctl restart zenplus-api zenplus-poller nginx

        echo -e "${GREEN}Updated: $OLD_VERSION → $NEW_VERSION${NC}"
        echo "Run 'zenplus status' to verify"
        ;;

    restart)
        echo "Restarting ZenPlus services..."
        systemctl restart zenplus-api zenplus-poller nginx
        echo -e "${GREEN}All services restarted${NC}"
        ;;

    stop)
        echo "Stopping ZenPlus services..."
        systemctl stop zenplus-api zenplus-poller
        echo -e "${YELLOW}Services stopped (databases still running)${NC}"
        ;;

    start)
        echo "Starting ZenPlus services..."
        cd $ZENPLUS_HOME && docker compose up -d postgres clickhouse redis
        sleep 5
        systemctl start zenplus-api zenplus-poller nginx
        echo -e "${GREEN}All services started${NC}"
        ;;

    logs)
        SERVICE="${2:-api}"
        case "$SERVICE" in
            api)     journalctl -u zenplus-api -f ;;
            poller)  journalctl -u zenplus-poller -f ;;
            all)     journalctl -u zenplus-api -u zenplus-poller -f ;;
            *)       echo "Usage: zenplus logs [api|poller|all]" ;;
        esac
        ;;

    backup)
        BACKUP_DIR="$ZENPLUS_HOME/backups/$(date +%Y%m%d_%H%M%S)"
        mkdir -p "$BACKUP_DIR"
        echo "Backing up to $BACKUP_DIR..."
        cd $ZENPLUS_HOME
        docker compose exec -T postgres pg_dump -U netpulse netpulse > "$BACKUP_DIR/postgres.sql"
        cp .env "$BACKUP_DIR/.env"
        echo -e "${GREEN}Backup complete: $BACKUP_DIR${NC}"
        ;;

    help|*)
        echo -e "${CYAN}${BOLD}ZenPlus Management CLI${NC}"
        echo ""
        echo "Usage: zenplus <command>"
        echo ""
        echo "Commands:"
        echo "  status    Show service status and URLs"
        echo "  update    Pull latest code and rebuild"
        echo "  restart   Restart all services"
        echo "  start     Start all services"
        echo "  stop      Stop application services"
        echo "  logs      View logs (api|poller|all)"
        echo "  backup    Backup database"
        echo "  help      Show this help"
        ;;
esac
CLIEOF

    chmod +x /usr/local/bin/zenplus
    log "CLI installed: zenplus"
}

# ═════════════════════════════════════════════════════════════════
# STEP 10: Save version & show summary
# ═════════════════════════════════════════════════════════════════
finalize() {
    step "Finalizing installation"

    cd "$ZENPLUS_HOME"
    CURRENT_VERSION=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    echo "$CURRENT_VERSION" > "$ZENPLUS_VERSION_FILE"
    echo "$(date -Iseconds)" >> "$ZENPLUS_VERSION_FILE"

    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"

    local IP=$(get_ip)

    # Wait for API to be ready
    info "Waiting for API to be ready..."
    local retries=15
    while [[ $retries -gt 0 ]]; do
        if curl -sf http://localhost:8000/api/v1/system/health > /dev/null 2>&1; then
            break
        fi
        sleep 2
        retries=$((retries - 1))
    done

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║         ZenPlus Installation Complete!           ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "  ${BOLD}Dashboard:${NC}  http://${IP}"
    echo -e "  ${BOLD}API Docs:${NC}   http://${IP}/docs"
    echo -e "  ${BOLD}Login:${NC}      admin / admin123"
    echo -e "  ${BOLD}Version:${NC}    ${CURRENT_VERSION}"
    echo ""
    echo -e "  ${BOLD}Management:${NC}"
    echo -e "    zenplus status     Show service status"
    echo -e "    zenplus update     Update to latest version"
    echo -e "    zenplus restart    Restart services"
    echo -e "    zenplus logs api   View API logs"
    echo -e "    zenplus backup     Backup database"
    echo ""
    echo -e "  ${YELLOW}⚠  Change the default password after first login!${NC}"
    echo ""
}

# ═════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════
main() {
    check_root
    show_banner

    MODE=$(detect_mode)

    if [[ "$MODE" == "update" ]]; then
        step "Update detected - upgrading ZenPlus"
        fetch_code
        build_components
        run_migrations
        systemctl restart zenplus-api zenplus-poller nginx
        finalize
    else
        step "Fresh installation starting"
        install_prerequisites
        setup_user
        fetch_code
        configure_env
        start_infrastructure
        build_components
        run_migrations
        create_services
        create_cli
        finalize
    fi
}

main "$@"
