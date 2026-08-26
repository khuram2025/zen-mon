#!/usr/bin/env bash
# ╔════════════════════════════════════════════════════════════════════╗
# ║  ZenPlus Network Monitoring Appliance — Installer                  ║
# ║  https://github.com/khuram2025/zen-mon                             ║
# ║                                                                    ║
# ║  Fresh install:  curl -fsSL https://zentryc.com/install.sh | sudo bash
# ║  Update:         sudo zenplus update                               ║
# ║  Status:         sudo zenplus status                               ║
# ╚════════════════════════════════════════════════════════════════════╝
#
# Every phase runs as a numbered, verified step: the step's own output goes
# to the install log, and the console shows a single checked line only after
# the step's post-conditions actually hold. A failed critical step aborts the
# install with the relevant log tail rather than continuing half-configured.

set -uo pipefail

# ─── Configuration (all env-overridable) ──────────────────────────
ZENPLUS_HOME="${ZENPLUS_HOME:-/opt/zenplus}"
ZENPLUS_USER="${ZENPLUS_USER:-zenplus}"
ZENPLUS_REPO="${ZENPLUS_REPO:-https://github.com/khuram2025/zen-mon.git}"
ZENPLUS_BRANCH="${ZENPLUS_BRANCH:-main}"
ZENPLUS_VERSION_FILE="$ZENPLUS_HOME/.version"
INSTALL_LOG="${INSTALL_LOG:-/var/log/zenplus-install.log}"

# Trial licence seeded on a fresh install (see seed_trial_licence).
TRIAL_DAYS="${TRIAL_DAYS:-30}"
TRIAL_MAX_DEVICES=50
TRIAL_MAX_CHECKS=20
TRIAL_MAX_USERS=5

# Minimum host requirements enforced by preflight_checks.
MIN_RAM_MB="${MIN_RAM_MB:-3500}"
MIN_DISK_GB="${MIN_DISK_GB:-20}"

# Database defaults (overridden by .env if exists)
DB_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "zenplus_$(date +%s)")
CH_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "clickhouse_$(date +%s)")
REDIS_PASSWORD=$(openssl rand -hex 16 2>/dev/null || echo "redis_$(date +%s)")
JWT_SECRET=$(openssl rand -hex 32 2>/dev/null || echo "jwt_$(date +%s)_secret")
SNMP_ENC_KEY=$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || echo "")

# ─── Presentation ─────────────────────────────────────────────────
# Colours only when attached to a terminal, so piped/CI logs stay clean.
if [[ -t 1 ]]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
    BLUE=$'\033[0;34m'; CYAN=$'\033[0;36m'; DIM=$'\033[2m'
    NC=$'\033[0m'; BOLD=$'\033[1m'; IS_TTY=1
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; DIM=''; NC=''; BOLD=''; IS_TTY=0
fi

# These write into the per-step log, not the console — the console shows the
# step checklist. They are kept because the whole script narrates through them.
log()   { echo "[ok]   $*"; }
warn()  { echo "[warn] $*"; STEP_WARNING="${STEP_WARNING:+$STEP_WARNING; }$*"; }
err()   { echo "[fail] $*"; exit 1; }
info()  { echo "[info] $*"; }
step()  { echo "--- $* ---"; }

check_root() { [[ $EUID -ne 0 ]] && { echo "This installer must be run as root (use sudo)." >&2; exit 1; }; return 0; }
get_ip() { ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1; }
mark_git_safe() {
    git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
    if id "$ZENPLUS_USER" &>/dev/null; then
        runuser -u "$ZENPLUS_USER" -- git config --global --add safe.directory "$ZENPLUS_HOME" 2>/dev/null || true
    fi
}

# ─── Step framework ───────────────────────────────────────────────
TOTAL_STEPS=16
STEP_NO=0
STEP_WARNING=""
STEP_TITLES=()
STEP_RESULTS=()
STEP_NOTES=()
FAILED_COUNT=0
WARNED_COUNT=0
INSTALL_STARTED=$SECONDS
CURRENT_STEP_TITLE=""
STEP_IN_FLIGHT=0

# Reserve the real console on fd 9. Each step's stdout/stderr is redirected
# into the install log, and a trap firing from inside a step inherits that
# redirection — so anything the operator must see is written to >&9 explicitly.
# Without this, an err()-triggered abort scrolls silently into the log file.
exec 9>&1

# State published by steps and rendered by print_summary.
RESOLVED_VERSION=""
LICENCE_PLAN=""
LICENCE_EXPIRES=""
ADMIN_PASSWORD_IS_DEFAULT="yes"

# Several long-standing helpers abort via err() → `exit 1`. That would skip the
# step framework's reporting entirely, so catch it here and render the same
# failure context the framework would have shown.
_on_exit() {
    local rc=$?
    if (( rc != 0 )) && (( STEP_IN_FLIGHT == 1 )); then
        # Snapshot the tail first: writing to the log below would otherwise
        # make the report quote itself.
        local tail_text; tail_text=$(tail -n 25 "$INSTALL_LOG" 2>/dev/null)
        {
            [[ $IS_TTY -eq 1 ]] && printf '\r'
            printf '%s %s✗%s\n' "$(_step_label "$CURRENT_STEP_TITLE")" "$RED" "$NC"
            echo ""
            echo "${RED}${BOLD}  Installation aborted during: ${CURRENT_STEP_TITLE}${NC}"
            echo "${DIM}  Last lines of ${INSTALL_LOG}:${NC}"
            echo ""
            echo "$tail_text" | sed 's/^/    /'
            echo ""
            echo "  Full log: ${BOLD}${INSTALL_LOG}${NC}"
            echo "  Fix the issue above and re-run the installer — it is idempotent."
            echo ""
        } >&9 2>&9
    fi
}
trap _on_exit EXIT

fmt_dur() {
    local s=$1
    if (( s < 60 )); then printf '%ds' "$s"; else printf '%dm%02ds' $((s / 60)) $((s % 60)); fi
}

_step_label() { printf '  %s[%02d/%02d]%s %-42s' "$DIM" "$STEP_NO" "$TOTAL_STEPS" "$NC" "$1"; }

# run_step <title> <function> [args…]   — abort the install if it fails
# run_soft_step <title> <function> …    — degrade to a warning if it fails
_run_step() {
    local critical="$1" title="$2"; shift 2
    STEP_NO=$((STEP_NO + 1))
    STEP_WARNING=""
    local label; label=$(_step_label "$title")
    [[ $IS_TTY -eq 1 ]] && printf '%s%s…%s' "$label" "$DIM" "$NC"
    local start=$SECONDS rc=0
    {
        echo ""
        echo "===== [$STEP_NO/$TOTAL_STEPS] $title — $(date -Iseconds) ====="
    } >> "$INSTALL_LOG"
    CURRENT_STEP_TITLE="$title"; STEP_IN_FLIGHT=1
    "$@" >> "$INSTALL_LOG" 2>&1 || rc=$?
    STEP_IN_FLIGHT=0
    local dur=$((SECONDS - start))
    [[ $IS_TTY -eq 1 ]] && printf '\r'
    STEP_TITLES+=("$title")

    if [[ $rc -eq 0 ]]; then
        if [[ -n "$STEP_WARNING" ]]; then
            printf '%s %s!%s %s(%s)%s\n' "$label" "$YELLOW" "$NC" "$DIM" "$(fmt_dur "$dur")" "$NC"
            printf '           %s%s%s\n' "$YELLOW" "$STEP_WARNING" "$NC"
            STEP_RESULTS+=("warn"); STEP_NOTES+=("$STEP_WARNING"); WARNED_COUNT=$((WARNED_COUNT + 1))
        else
            printf '%s %s✓%s %s(%s)%s\n' "$label" "$GREEN" "$NC" "$DIM" "$(fmt_dur "$dur")" "$NC"
            STEP_RESULTS+=("ok"); STEP_NOTES+=("")
        fi
        return 0
    fi

    printf '%s %s✗%s %s(%s)%s\n' "$label" "$RED" "$NC" "$DIM" "$(fmt_dur "$dur")" "$NC"
    STEP_RESULTS+=("fail"); STEP_NOTES+=("exit code $rc"); FAILED_COUNT=$((FAILED_COUNT + 1))

    if [[ "$critical" == "critical" ]]; then
        echo ""
        echo "${RED}${BOLD}  Installation failed at step ${STEP_NO}: ${title}${NC}"
        echo "${DIM}  Last lines of ${INSTALL_LOG}:${NC}"
        echo ""
        tail -n 25 "$INSTALL_LOG" | sed 's/^/    /'
        echo ""
        echo "  Full log: ${BOLD}${INSTALL_LOG}${NC}"
        echo "  Re-running the installer is safe — completed steps are skipped or repeated idempotently."
        echo ""
        exit 1
    fi
    printf '           %s%s%s\n' "$YELLOW" "continuing — this component will be unavailable" "$NC"
    return 0
}
run_step()      { _run_step critical "$@"; }
run_soft_step() { _run_step soft "$@"; }

show_banner() {
    echo ""
    echo "${CYAN}${BOLD}  ╔══════════════════════════════════════════════════════════╗${NC}"
    echo "${CYAN}${BOLD}  ║   ZenPlus — Network Monitoring Appliance                  ║${NC}"
    echo "${CYAN}${BOLD}  ║   Automated Installer                                    ║${NC}"
    echo "${CYAN}${BOLD}  ╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  ${DIM}Host      ${NC}$(hostname) ${DIM}($(get_ip 2>/dev/null || echo 'no address'))${NC}"
    echo "  ${DIM}Source    ${NC}${ZENPLUS_REPO##*/} ${DIM}branch${NC} ${ZENPLUS_BRANCH}"
    echo "  ${DIM}Target    ${NC}${ZENPLUS_HOME}"
    echo "  ${DIM}Log       ${NC}${INSTALL_LOG}"
    echo ""
    echo "  ${DIM}This takes 8–15 minutes on a fresh server. Safe to re-run.${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# Preflight: fail fast on hosts that cannot run the appliance
# ═══════════════════════════════════════════════════════════════
# Every check here is cheap and happens before anything is written, so an
# unsuitable host is rejected in seconds rather than 10 minutes into a build.
preflight_checks() {
    step "Validating host"
    local fatal=0

    # --- OS family and release ---
    if [[ ! -r /etc/os-release ]]; then
        echo "[fail] /etc/os-release missing — cannot identify the operating system"
        fatal=1
    else
        . /etc/os-release
        info "Detected: ${PRETTY_NAME:-unknown}"
        case "${ID:-}" in
            ubuntu)
                local major="${VERSION_ID%%.*}"
                if (( major < 20 )); then
                    echo "[fail] Ubuntu ${VERSION_ID} is not supported — 20.04 LTS or newer required"
                    fatal=1
                elif [[ "$VERSION_ID" != "20.04" && "$VERSION_ID" != "22.04" && "$VERSION_ID" != "24.04" ]]; then
                    warn "Ubuntu ${VERSION_ID} is untested; 22.04 or 24.04 LTS recommended"
                fi
                ;;
            debian) warn "Debian ${VERSION_ID:-?} is untested — Ubuntu LTS is the supported platform" ;;
            *)
                echo "[fail] ${PRETTY_NAME:-this OS} is not supported — Ubuntu 20.04/22.04/24.04 LTS required"
                fatal=1
                ;;
        esac
    fi

    # --- CPU architecture ---
    local arch; arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
    if [[ "$arch" != "amd64" && "$arch" != "x86_64" ]]; then
        echo "[fail] Architecture '${arch}' is not supported — x86_64/amd64 required"
        fatal=1
    else
        info "Architecture: ${arch}"
    fi

    # --- Memory ---
    local ram_mb; ram_mb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))
    if (( ram_mb > 0 && ram_mb < MIN_RAM_MB )); then
        echo "[fail] ${ram_mb} MB RAM detected — at least ${MIN_RAM_MB} MB (4 GB) required"
        echo "       ClickHouse and the build toolchain will fail or thrash below this."
        fatal=1
    else
        info "Memory: ${ram_mb} MB"
    fi

    # --- Disk on the target filesystem ---
    local target_fs="$ZENPLUS_HOME"
    [[ -d "$target_fs" ]] || target_fs="$(dirname "$ZENPLUS_HOME")"
    local free_gb; free_gb=$(df -BG --output=avail "$target_fs" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [[ -n "$free_gb" ]] && (( free_gb < MIN_DISK_GB )); then
        echo "[fail] ${free_gb} GB free on ${target_fs} — at least ${MIN_DISK_GB} GB required"
        fatal=1
    else
        info "Disk free: ${free_gb:-unknown} GB on ${target_fs}"
    fi

    # --- Port availability (only flag ports not already ours) ---
    local busy=""
    for port in 80 8000 5432 6379 8123 9000; do
        if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
            local owner; owner=$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -oP 'users:\(\("\K[^"]+' | head -1)
            case "$owner" in
                nginx|postgres|redis-server|docker-proxy|python3|uvicorn|clickhouse*) : ;;
                *) busy="${busy}${busy:+, }${port}${owner:+ ($owner)}" ;;
            esac
        fi
    done
    [[ -n "$busy" ]] && warn "Ports already in use by other software: ${busy}"

    # --- Outbound reachability for the things we must download ---
    local unreachable=""
    for host in github.com go.dev deb.nodesource.com download.docker.com; do
        curl -fsS --max-time 8 -o /dev/null "https://${host}" 2>/dev/null || \
            unreachable="${unreachable}${unreachable:+, }${host}"
    done
    if [[ -n "$unreachable" ]]; then
        echo "[fail] Cannot reach: ${unreachable}"
        echo "       The installer downloads Docker, Go, Node.js and the application source."
        echo "       Configure DNS/proxy/firewall egress, then re-run."
        fatal=1
    else
        info "Outbound HTTPS reachable"
    fi

    if (( fatal )); then
        echo "[fail] Host does not meet the requirements above"
        return 1
    fi
    log "Host validated"
    return 0
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
        libcap2-bin snmp iputils-ping \
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
        info "Existing checkout found — updating in place"
        cd "$ZENPLUS_HOME" || { echo "[fail] cannot enter $ZENPLUS_HOME"; return 1; }
        mark_git_safe
        git fetch origin || { echo "[fail] git fetch failed — check network access to $ZENPLUS_REPO"; return 1; }
        git reset --hard "origin/$ZENPLUS_BRANCH" || { echo "[fail] cannot reset to origin/$ZENPLUS_BRANCH"; return 1; }
    else
        # Clone into a staging path and move it in. The previous implementation
        # did `rm -rf $ZENPLUS_HOME` first, which destroyed venv/, updater keys
        # and OTA registration on any appliance whose .git had gone missing —
        # and left the box unrecoverable if the clone then failed.
        info "Cloning $ZENPLUS_REPO ($ZENPLUS_BRANCH)"
        local staging; staging=$(mktemp -d /tmp/zenplus-clone-XXXXXX)
        if ! git clone --depth 1 -b "$ZENPLUS_BRANCH" "$ZENPLUS_REPO" "$staging/repo"; then
            rm -rf "$staging"
            echo "[fail] git clone failed — check network access to $ZENPLUS_REPO"
            return 1
        fi
        mkdir -p "$ZENPLUS_HOME"
        # Copy the tree over the existing home, preserving runtime state
        # (.env, data/, logs/, backups/, venv/, updater/config, artifacts/).
        cp -a "$staging/repo/." "$ZENPLUS_HOME/"
        rm -rf "$staging"
        mkdir -p "$ZENPLUS_HOME"/{data,logs,backups,bin}
        cd "$ZENPLUS_HOME" || return 1
        mark_git_safe
    fi

    local sha ver
    sha=$(git -C "$ZENPLUS_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)
    ver=$(head -1 "$ZENPLUS_VERSION_FILE" 2>/dev/null || echo unknown)
    info "Source at ${sha} (version ${ver})"
    # Surface the stale-branch trap loudly: if the branch we cloned does not
    # carry a semantic version, OTA update matching upstream will not work.
    if [[ ! "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        warn "branch '$ZENPLUS_BRANCH' has no semantic .version — OTA updates may not match"
    fi
    RESOLVED_VERSION="$ver"
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

    # Base schema first: these two are not migrations and are not tracked.
    for base in init-postgres.sql seed-devices.sql; do
        [[ -f "$ZENPLUS_HOME/scripts/$base" ]] || continue
        info "Running $base..."
        runuser -u postgres -- psql -q -d zenplus -f "$ZENPLUS_HOME/scripts/$base" >/dev/null 2>&1 || true
    done

    # Migrations go through the tracked runner — the single authoritative path,
    # the same one the OTA updater uses.
    #
    # This used to be a blind `psql -f` loop over migrate-*.sql with errors
    # discarded, which recorded nothing in the ledger. The tracked runner then
    # ran a second time over an already-migrated database and re-applied every
    # migration it could not probe, and the ones that rebuild
    # alert_rules_metric_check failed against rows the later migrations had just
    # inserted — leaving a fresh appliance reporting "schema does not match the
    # installed version" on day one. Applying each migration exactly once, in
    # lockfile order, cannot produce that.
    info "Applying migrations via the tracked runner..."
    if runuser -u postgres -- python3 "$ZENPLUS_HOME/scripts/run-migrations.py" \
            --scripts-dir "$ZENPLUS_HOME/scripts"; then
        log "Migrations applied and recorded"
    else
        warn "migration runner reported problems — see $ZENPLUS_HOME/.schema-status.json"
    fi

    # Grant permissions
    su - postgres -c "psql -d zenplus -c 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zenplus; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO zenplus; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO zenplus;'" 2>/dev/null
    log "PostgreSQL configured"

    # Repair the admin password hash only when it is missing or corrupt.
    # Historically this ran unconditionally, which silently reset a customer's
    # chosen password back to admin123 on every re-run of the installer.
    local stored
    stored=$(runuser -u postgres -- psql -d zenplus -tAc \
        "SELECT COALESCE(password_hash,'') FROM users WHERE username='admin';" 2>/dev/null | head -1)
    if [[ -z "$stored" ]]; then
        info "No admin user row found — init-postgres.sql seeds it; skipping hash repair"
    elif [[ "$stored" == '$2'*'$'* ]]; then
        ADMIN_PASSWORD_IS_DEFAULT=$( [[ "$stored" == '$2b$12$vjHI8XBgL.dCyn.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai' ]] && echo yes || echo no )
        info "Admin password hash present and well-formed — left untouched"
    else
        # A malformed hash makes every login return HTTP 500 (passlib
        # UnknownHashError). Rewrite it via runuser so the shell cannot expand
        # the '$2b$' prefix, which is what corrupted it in the first place.
        warn "admin password hash was corrupt — reset to the default 'admin123'"
        ADMIN_HASH=$("$ZENPLUS_HOME/venv/bin/python3" -c "
from passlib.context import CryptContext
print(CryptContext(schemes=['bcrypt'], deprecated='auto').hash('admin123'))
" 2>/dev/null || echo "")
        if [[ -n "$ADMIN_HASH" ]]; then
            runuser -u postgres -- psql -d zenplus -c \
                "UPDATE users SET password_hash = '$ADMIN_HASH', is_active = true WHERE username = 'admin';" >/dev/null 2>&1
            ADMIN_PASSWORD_IS_DEFAULT=yes
        fi
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

    # Seed the migration ledgers. The loops above apply every migration but
    # record nothing, so without this a freshly installed appliance looks
    # untracked to the first OTA update. sync-schema.py probes what actually
    # exists and baselines accordingly, then applies anything genuinely
    # missing. Non-fatal: a fresh install must not abort here, and the same
    # script runs again on the first update.
    if [[ -x "$ZENPLUS_HOME/scripts/sync-schema.py" ]]; then
        info "Recording applied migrations..."
        CLICKHOUSE_PASSWORD="$CH_PASSWORD" \
            python3 "$ZENPLUS_HOME/scripts/sync-schema.py" \
            --scripts-dir "$ZENPLUS_HOME/scripts" \
            && log "Migration ledgers seeded" \
            || warn "Schema ledger incomplete — see $ZENPLUS_HOME/.schema-status.json"
    fi
}

# ═══════════════════════════════════════════════════════════════
# Trial licence
# ═══════════════════════════════════════════════════════════════
# The API lazily creates a 30-day trial the first time someone opens the
# Subscription page, but that leaves a brand-new appliance reporting no plan
# at all until then. Seed the row here so the appliance is licensed the
# moment it boots, and so `zenplus status` can show the trial immediately.
#
# This is deliberately local: license keys are issued by zentryc.com and are
# single-use, so there is no API an appliance can call to mint its own trial.
# The trial gates nothing — it is the evaluation entitlement, and a key is
# only needed to unlock OTA updates.
seed_trial_licence() {
    step "Provisioning trial licence"

    if ! runuser -u postgres -- psql -d zenplus -tAc \
        "SELECT to_regclass('public.subscriptions');" 2>/dev/null | grep -q subscriptions; then
        warn "subscriptions table missing — the API will create a trial on first use"
        return 0
    fi

    local existing
    existing=$(runuser -u postgres -- psql -d zenplus -tAc \
        "SELECT count(*) FROM subscriptions;" 2>/dev/null | tr -dc '0-9')
    if [[ "${existing:-0}" != "0" ]]; then
        local plan expires
        plan=$(runuser -u postgres -- psql -d zenplus -tAc \
            "SELECT plan FROM subscriptions ORDER BY created_at DESC LIMIT 1;" 2>/dev/null | tr -d ' ')
        expires=$(runuser -u postgres -- psql -d zenplus -tAc \
            "SELECT to_char(expires_at,'YYYY-MM-DD') FROM subscriptions ORDER BY created_at DESC LIMIT 1;" 2>/dev/null | tr -d ' ')
        info "Existing subscription preserved: plan=${plan:-?} expires=${expires:-?}"
        LICENCE_PLAN="${plan:-unknown}"; LICENCE_EXPIRES="${expires:-unknown}"
        return 0
    fi

    runuser -u postgres -- psql -d zenplus -v ON_ERROR_STOP=1 -c \
        "INSERT INTO subscriptions (plan, status, started_at, expires_at,
                                    max_devices, max_service_checks, max_users,
                                    license_key, activated_by)
         VALUES ('trial', 'active', now(), now() + INTERVAL '${TRIAL_DAYS} days',
                 ${TRIAL_MAX_DEVICES}, ${TRIAL_MAX_CHECKS}, ${TRIAL_MAX_USERS}, NULL, 'installer');" \
        >/dev/null || { echo "[fail] could not seed the trial subscription"; return 1; }

    runuser -u postgres -- psql -d zenplus -c \
        "GRANT ALL ON subscriptions TO zenplus;" >/dev/null 2>&1 || true

    LICENCE_PLAN="trial"
    LICENCE_EXPIRES=$(date -u -d "+${TRIAL_DAYS} days" +%Y-%m-%d 2>/dev/null || echo "+${TRIAL_DAYS}d")
    log "Trial licence active for ${TRIAL_DAYS} days (${TRIAL_MAX_DEVICES} devices, ${TRIAL_MAX_CHECKS} service checks, ${TRIAL_MAX_USERS} users)"
}

# ═══════════════════════════════════════════════════════════════
# Security hardening helpers
# ═══════════════════════════════════════════════════════════════
# Installs the root-owned TLS helper + sudoers grant that back
# Settings → General → Security. Without this the Security tab cannot install
# certificates or switch the appliance to HTTPS.
setup_security_hardening() {
    step "Installing TLS/security helpers"
    if [[ -f "$ZENPLUS_HOME/scripts/setup-security.sh" ]]; then
        bash "$ZENPLUS_HOME/scripts/setup-security.sh" || {
            echo "[fail] setup-security.sh failed"; return 1; }
        log "TLS certificate store and privileged helper installed"
    else
        warn "scripts/setup-security.sh missing — Settings → Security will be unavailable"
    fi
    return 0
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

    # OTLP ingest lives at the appliance ROOT (/v1/traces) per the OTel
    # endpoint convention, not under /api/. Without this location an
    # instrumented app POSTing traces gets the SPA's index.html back.
    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        client_max_body_size 32m;
        proxy_read_timeout 120s;
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

    # Storage management (Settings -> Storage): root-owned helper scripts,
    # sudoers grant, backup dirs, and the ClickHouse backups-disk config.
    # Same script the OTA updater runs as a hook, so fresh installs and
    # updated appliances converge on an identical layout.
    if [ -f /opt/zenplus/scripts/setup-storage.sh ]; then
        if bash /opt/zenplus/scripts/setup-storage.sh; then
            log "Storage management configured"
        else
            warn "storage management setup failed — Settings > Storage will be limited"
        fi
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
        warn "setup-support.sh missing — Settings → Support will be unavailable"
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
    # Converge both databases through the same tracked path the OTA updater
    # uses, so an appliance updated by hand and one updated over the air end up
    # with identical schema. The old loop re-ran every file blind and discarded
    # every error, which recorded nothing and hid failures.
    set +e
    [[ -f "$ZENPLUS_HOME/.env" ]] && set -a && . "$ZENPLUS_HOME/.env" && set +a
    if [[ -x "$ZENPLUS_HOME/scripts/sync-schema.py" ]]; then
        python3 "$ZENPLUS_HOME/scripts/sync-schema.py" --scripts-dir "$ZENPLUS_HOME/scripts"
        local rc=$?
        [[ $rc -eq 0 ]] || echo -e "${YELLOW}  Schema drift remains — see $ZENPLUS_HOME/.schema-status.json${NC}"
        return $rc
    fi

    # Fallback for an appliance that predates sync-schema.py.
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
        # Converge and verify the schema before replacing any generated runtime
        # artifacts or restarting services.  The previous order restarted the
        # newly checked-out API even when migration failed, then returned an
        # error; that left the running code ahead of PostgreSQL/ClickHouse and
        # caused only some telemetry modules to fail.  Keep the already-loaded
        # old services healthy and restore their source checkout on failure.
        echo "  running migrations..."
        if ! run_migrations; then
            echo -e "${RED}Schema drift — update aborted before service restart.${NC}"
            echo -e "${YELLOW}Details: $ZENPLUS_HOME/.schema-status.json${NC}"
            if git reset --hard "$OLD" >/dev/null 2>&1; then
                echo -e "${YELLOW}Restored source checkout $OLD; existing services were not restarted.${NC}"
            else
                echo -e "${RED}Could not restore source checkout $OLD; existing processes are still running but manual repair is required.${NC}"
            fi
            exit 1
        fi
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
        echo "  reloading systemd..."
        reinstall_units
        chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME" 2>/dev/null
        echo "  restarting services..."
        systemctl restart zenplus-api zenplus-poller nginx
        systemctl restart zenplus-updater.timer 2>/dev/null || true
        # The pre-restart schema gate above proved this checkout safe to run.
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
    cd "$ZENPLUS_HOME" || return 1

    # Stamp the semantic version, NOT a git SHA. updater/inventory.py sends
    # line 1 of this file to zentryc.com as current_version, and the release
    # server matches on semver — a SHA here silently breaks OTA matching.
    local ver
    ver=$(git show HEAD:.version 2>/dev/null | head -1)
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || ver="${RESOLVED_VERSION:-0.0.0}"
    {
        echo "$ver"
        date -Iseconds
    } > "$ZENPLUS_VERSION_FILE"
    RESOLVED_VERSION="$ver"
    chown -R "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME"
    log "Version stamped: $ver"
}

# ═══════════════════════════════════════════════════════════════
# Verification — assert the appliance actually came up
# ═══════════════════════════════════════════════════════════════
# The installer previously printed "Installation Complete!" regardless of
# whether anything worked. This step fails loudly instead.
verify_installation() {
    step "Verifying appliance health"
    local failures=0

    info "Waiting for the API to answer..."
    local retries=30 up=0
    while (( retries > 0 )); do
        if curl -sf --max-time 5 http://127.0.0.1:8000/api/v1/system/health >/dev/null 2>&1; then
            up=1; break
        fi
        sleep 2; retries=$((retries - 1))
    done
    if (( up )); then log "API healthy on :8000"; else
        echo "[fail] API did not become healthy — journalctl -u zenplus-api -n 100"
        failures=$((failures + 1))
    fi

    # Dashboard must be served by nginx, not just built.
    if curl -sfI --max-time 5 http://127.0.0.1/ >/dev/null 2>&1; then
        log "Dashboard served on :80"
    else
        echo "[fail] nginx is not serving the dashboard on port 80"
        failures=$((failures + 1))
    fi

    for unit in zenplus-api zenplus-poller nginx postgresql redis-server; do
        if systemctl is-active --quiet "$unit"; then
            log "service active: $unit"
        else
            warn "service not active: $unit"
        fi
    done

    if docker ps --filter name=zenplus-clickhouse --format '{{.Status}}' 2>/dev/null | grep -qi up; then
        log "ClickHouse container up"
    else
        warn "ClickHouse container is not running — metrics history will be unavailable"
    fi

    (( failures == 0 )) || { echo "[fail] ${failures} core check(s) failed"; return 1; }
    return 0
}

# ═══════════════════════════════════════════════════════════════
# Completion report
# ═══════════════════════════════════════════════════════════════
print_summary() {
    local ip; ip=$(get_ip)
    local total=$((SECONDS - INSTALL_STARTED))
    local i

    echo ""
    echo "  ${BOLD}Installation summary${NC}"
    echo "  ${DIM}──────────────────────────────────────────────────────────${NC}"
    for i in "${!STEP_TITLES[@]}"; do
        local mark colour
        case "${STEP_RESULTS[$i]}" in
            ok)   mark="✓"; colour="$GREEN"  ;;
            warn) mark="!"; colour="$YELLOW" ;;
            *)    mark="✗"; colour="$RED"    ;;
        esac
        printf '   %s%s%s  %-44s%s%s\n' "$colour" "$mark" "$NC" "${STEP_TITLES[$i]}" \
            "$DIM" "${STEP_NOTES[$i]:+${STEP_NOTES[$i]}}${NC}"
    done
    echo "  ${DIM}──────────────────────────────────────────────────────────${NC}"
    printf '   %d steps · %d warning(s) · completed in %s\n' \
        "${#STEP_TITLES[@]}" "$WARNED_COUNT" "$(fmt_dur "$total")"

    echo ""
    if (( FAILED_COUNT > 0 )); then
        echo "  ${YELLOW}${BOLD}Appliance installed with ${FAILED_COUNT} degraded component(s).${NC}"
    else
        echo "  ${GREEN}${BOLD}✓ ZenPlus ${RESOLVED_VERSION:-} is installed and running.${NC}"
    fi
    echo ""
    echo "  ${BOLD}Access${NC}"
    echo "    Dashboard    ${BOLD}http://${ip}${NC}"
    echo "    API docs     http://${ip}/docs"
    if [[ "${ADMIN_PASSWORD_IS_DEFAULT:-yes}" == "yes" ]]; then
        echo "    Sign in      ${BOLD}admin${NC} / ${BOLD}admin123${NC}   ${YELLOW}(change this immediately)${NC}"
    else
        echo "    Sign in      ${BOLD}admin${NC} / ${DIM}(your existing password — unchanged)${NC}"
    fi
    echo ""
    echo "  ${BOLD}Licence${NC}"
    if [[ "${LICENCE_PLAN:-}" == "trial" ]]; then
        echo "    ${GREEN}Trial active${NC} — expires ${BOLD}${LICENCE_EXPIRES:-?}${NC}"
        echo "    ${DIM}${TRIAL_MAX_DEVICES} devices · ${TRIAL_MAX_CHECKS} service checks · ${TRIAL_MAX_USERS} users${NC}"
        echo "    ${DIM}All features are usable during the trial. A licence key is only${NC}"
        echo "    ${DIM}required to receive over-the-air updates.${NC}"
    else
        echo "    Plan: ${LICENCE_PLAN:-unknown}${LICENCE_EXPIRES:+ (expires ${LICENCE_EXPIRES})}"
    fi
    echo ""
    echo "  ${BOLD}Recommended next steps${NC}"
    echo "    1. Sign in and change the admin password"
    echo "    2. Enable HTTPS — Settings → General → Security"
    echo "       ${DIM}(generate a self-signed certificate, or issue one from your CA)${NC}"
    echo "    3. Register for updates — Settings → General → Licenses"
    echo "       ${DIM}(paste the licence key supplied with your subscription)${NC}"
    echo "    4. Add devices — Discovery, or Devices → Add Device"
    echo ""
    echo "  ${BOLD}Management${NC}"
    echo "    sudo zenplus status      Service and registration status"
    echo "    sudo zenplus update      Apply the latest release"
    echo "    sudo zenplus restart     Restart all services"
    echo "    sudo zenplus logs api    Tail the API log"
    echo ""
    echo "  ${DIM}Install log: ${INSTALL_LOG}${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
main() {
    check_root

    # Start a fresh log for this run; everything each step prints lands here.
    : > "$INSTALL_LOG" 2>/dev/null || INSTALL_LOG=/tmp/zenplus-install.log
    : > "$INSTALL_LOG"
    chmod 0640 "$INSTALL_LOG" 2>/dev/null || true
    echo "ZenPlus install started $(date -Iseconds)" >> "$INSTALL_LOG"

    show_banner

    # Order note: build_components must precede setup_databases — the admin
    # hash repair and the trial seed both need the Python venv it creates.
    run_step      "Validating host"              preflight_checks
    run_step      "Installing system packages"   install_prerequisites
    run_step      "Creating service account"     setup_user
    run_step      "Fetching application source"  fetch_code
    run_step      "Generating configuration"     configure_env
    run_step      "Building application"         build_components
    run_step      "Initialising databases"       setup_databases
    run_step      "Provisioning trial licence"   seed_trial_licence
    run_step      "Configuring services"         create_services
    run_soft_step "Installing TLS/security tools" setup_security_hardening
    run_soft_step "Configuring OTA updater"      setup_updater
    run_step      "Installing support tooling"   setup_support_bundles
    run_step      "Installing management CLI"    create_cli
    run_step      "Finalising installation"      finalize
    run_step      "Verifying appliance health"   verify_installation
    run_soft_step "Collecting system report"     collect_report

    print_summary
    (( FAILED_COUNT > 0 )) && exit 1
    return 0
}

# A tiny post-install snapshot in the log, so a support bundle taken later
# still shows what the box looked like the moment it was built.
collect_report() {
    step "Recording install report"
    echo "--- versions ---"
    cat "$ZENPLUS_VERSION_FILE" 2>/dev/null
    git -C "$ZENPLUS_HOME" rev-parse HEAD 2>/dev/null
    echo "--- services ---"
    systemctl is-active zenplus-api zenplus-poller nginx postgresql redis-server 2>&1 | paste -sd' '
    echo "--- containers ---"
    docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null
    echo "--- disk ---"
    df -h "$ZENPLUS_HOME" 2>/dev/null | tail -1
    log "Report recorded"
}

main "$@"
