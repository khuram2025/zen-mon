#!/usr/bin/env bash
# One-line golden-image provisioner for the full ZenPlus appliance.
#
# Run on a fresh minimal Ubuntu LTS VM:
#   curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/scripts/provision-main-appliance-golden.sh | sudo bash
#
# The script installs ZenPlus, applies appliance hardening, verifies the live
# system, then cleans the VM into export-ready OVA state. Export must still be
# done from the hypervisor while the VM is powered off.
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive

ZENPLUS_REPO="${ZENPLUS_REPO:-https://github.com/khuram2025/zen-mon.git}"
ZENPLUS_BRANCH="${ZENPLUS_BRANCH:-main}"
ZENPLUS_HOME="${ZENPLUS_HOME:-/opt/zenplus}"
ZENPLUS_USER="${ZENPLUS_USER:-zenplus}"
SRC_DIR="${ZENPLUS_PROVISION_SRC:-/root/zenplus-appliance-source}"
LOG_FILE="${ZENPLUS_PROVISION_LOG:-/var/log/zenplus-appliance-provision.log}"
REPORT_FILE="${ZENPLUS_PROVISION_REPORT:-/root/zenplus-appliance-export-checklist.txt}"
ADMIN_TEST_PASSWORD="${ZENPLUS_ADMIN_TEST_PASSWORD:-admin123}"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chmod 0600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

CHECKS=()
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log() { printf '\n[zenplus-ova] %s\n' "$*"; }
info() { printf '[info] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
die() { printf '[fatal] %s\n' "$*" >&2; exit 1; }

record_check() {
  local area="$1" name="$2" status="$3" detail="${4:-}"
  CHECKS+=("${status}|${area}|${name}|${detail}")
  printf '[%s] %s - %s' "$status" "$area" "$name"
  [[ -n "$detail" ]] && printf ' (%s)' "$detail"
  printf '\n'
}

check_cmd() {
  local area="$1" name="$2" cmd="$3" detail="${4:-}"
  if bash -lc "$cmd" >/dev/null 2>&1; then
    record_check "$area" "$name" "PASS" "$detail"
    return 0
  fi
  record_check "$area" "$name" "FAIL" "$detail"
  return 0
}

write_report() {
  local completed_at
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  {
    echo "# ZenPlus Appliance Golden Image Checklist"
    echo
    echo "Started:   $STARTED_AT"
    echo "Completed: $completed_at"
    echo "Repo:      $ZENPLUS_REPO"
    echo "Branch:    $ZENPLUS_BRANCH"
    echo "Host:      $(hostname -f 2>/dev/null || hostname)"
    echo
    echo "## Verification Results"
    echo
    local fail_count=0
    local item status area name detail
    for item in "${CHECKS[@]}"; do
      IFS='|' read -r status area name detail <<<"$item"
      [[ "$status" == "FAIL" ]] && fail_count=$((fail_count + 1))
      if [[ -n "$detail" ]]; then
        printf -- "- [%s] %s: %s - %s\n" "$status" "$area" "$name" "$detail"
      else
        printf -- "- [%s] %s: %s\n" "$status" "$area" "$name"
      fi
    done
    echo
    echo "## Result"
    echo
    if [[ "$fail_count" -eq 0 ]]; then
      echo "PASS: VM is prepared for poweroff and host-side OVA export."
    else
      echo "FAIL: $fail_count verification item(s) failed. Do not export this VM."
    fi
    echo
    echo "## Next Commands"
    echo
    echo "If every item passed:"
    echo
    echo '```bash'
    echo "sudo poweroff"
    echo '```'
    echo
    echo "After shutdown, export the VM from VMware/ESXi/Workstation as:"
    echo
    echo '```text'
    echo "zenplus-appliance-<version>-amd64.ova"
    echo '```'
    echo
    echo "Then generate:"
    echo
    echo '```bash'
    echo "sha256sum zenplus-appliance-<version>-amd64.ova > zenplus-appliance-<version>-amd64.ova.sha256"
    echo '```'
  } > "$REPORT_FILE"
  chmod 0600 "$REPORT_FILE"
}

require_root() {
  [[ $EUID -eq 0 ]] || die "run as root, for example: curl -fsSL <url> | sudo bash"
}

verify_base_os() {
  log "Verifying base OS"
  check_cmd "system" "running as root" 'test "$EUID" -eq 0'
  check_cmd "system" "Ubuntu detected" 'test -f /etc/os-release && . /etc/os-release && test "$ID" = ubuntu'
  check_cmd "system" "supported Ubuntu LTS" '. /etc/os-release && case "$VERSION_ID" in 20.04|22.04|24.04) exit 0 ;; *) exit 1 ;; esac' || true
  check_cmd "system" "x86_64 architecture" 'test "$(uname -m)" = x86_64'
  check_cmd "system" "minimum 4GB RAM" 'test "$(awk "/MemTotal/ {print int(\$2/1024)}" /proc/meminfo)" -ge 3800' || true
  check_cmd "system" "minimum 20GB free root disk" 'test "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" -ge 20' || true
}

install_bootstrap_packages() {
  log "Installing bootstrap packages"
  apt-get update -qq
  apt-get install -y -qq \
    ca-certificates curl git jq openssl sed gawk coreutils \
    lsb-release software-properties-common ufw snmp iputils-ping >/dev/null
  record_check "system" "bootstrap packages installed" "PASS" "curl, git, jq, openssl, ufw, snmp, iputils-ping"
}

fetch_source() {
  log "Fetching ZenPlus source"
  rm -rf "$SRC_DIR"
  git clone -b "$ZENPLUS_BRANCH" "$ZENPLUS_REPO" "$SRC_DIR"
  record_check "architecture" "source cloned" "PASS" "$SRC_DIR"
}

run_installer() {
  log "Running ZenPlus full installer"
  bash "$SRC_DIR/install.sh"
  record_check "application" "base installer completed" "PASS" "$ZENPLUS_HOME"
}

ensure_line() {
  local file="$1" line="$2"
  grep -qxF "$line" "$file" || printf '%s\n' "$line" >> "$file"
}

apply_known_appliance_fixes() {
  log "Applying appliance packaging fixes"

  ensure_line "$ZENPLUS_HOME/server/requirements.txt" "openpyxl==3.1.5"
  "$ZENPLUS_HOME/venv/bin/pip" install -q -r "$ZENPLUS_HOME/server/requirements.txt"
  record_check "application" "Python runtime dependencies installed" "PASS" "requirements.txt including openpyxl"

  python3 - <<'PY'
from pathlib import Path

health = Path("/opt/zenplus/server/app/api/v1/system_updates.py")
if health.exists():
    text = health.read_text()
    text = text.replace(
        '["zenplus-api", "zenplus-poller", "netmon-gunicorn", "nginx"]',
        '["zenplus-api", "zenplus-poller", "nginx"]',
    )
    health.write_text(text)

builder = Path("/opt/zenplus/scripts/build-release.py")
if builder.exists():
    text = builder.read_text()
    text = text.replace(
        '["zenplus-api", "zenplus-poller", "netmon-gunicorn",\n'
        '                                "netmon-celery", "netmon-celery-beat", "nginx"]',
        '["zenplus-api", "zenplus-poller", "nginx"]',
    )
    text = text.replace(
        '["zenplus-api", "zenplus-poller", "netmon-gunicorn",\n'
        '                          "netmon-celery", "netmon-celery-beat", "nginx"]',
        '["zenplus-api", "zenplus-poller", "nginx"]',
    )
    text = text.replace(
        '["go", "build", "-o", str(go_dir / "zenplus-poller"), "./cmd/poller"]',
        '["go", "build", "-buildvcs=false", "-o", str(go_dir / "zenplus-poller"), "./cmd/poller"]',
    )
    builder.write_text(text)
PY
  record_check "application" "stale service names removed" "PASS" "health check and release builder"

  log "Rebuilding Go poller with appliance-safe build flags"
  (
    cd "$ZENPLUS_HOME/poller"
    export PATH=/usr/local/go/bin:$PATH
    CGO_ENABLED=0 go build -buildvcs=false -o "$ZENPLUS_HOME/bin/zenplus-poller" ./cmd/poller
  )
  setcap cap_net_raw+ep "$ZENPLUS_HOME/bin/zenplus-poller" 2>/dev/null || true
  chown "$ZENPLUS_USER:$ZENPLUS_USER" "$ZENPLUS_HOME/bin/zenplus-poller"
  record_check "application" "poller rebuilt" "PASS" "-buildvcs=false"

  log "Resetting current admin test password for live verification"
  local admin_hash
  admin_hash=$("$ZENPLUS_HOME/venv/bin/python3" - "$ADMIN_TEST_PASSWORD" <<'PY'
import sys
from passlib.context import CryptContext
print(CryptContext(schemes=["bcrypt"], deprecated="auto").hash(sys.argv[1]))
PY
)
  sudo -u postgres psql -d zenplus -v ON_ERROR_STOP=1 -v admin_hash="$admin_hash" <<'SQL' >/dev/null
UPDATE users
SET password_hash = :'admin_hash',
    last_login = NULL,
    updated_at = NOW()
WHERE username = 'admin';
SQL
  record_check "security" "admin hash valid for live verification" "PASS" "temporary pre-export test password"
}

configure_https_and_proxy() {
  log "Configuring HTTPS and Nginx reverse proxy"
  install -d -m 0750 -o root -g root /etc/ssl/zenplus
  if [[ ! -f /etc/ssl/zenplus/zenplus-selfsigned.key || ! -f /etc/ssl/zenplus/zenplus-selfsigned.crt ]]; then
    openssl req -x509 -nodes -newkey rsa:3072 -days 825 \
      -keyout /etc/ssl/zenplus/zenplus-selfsigned.key \
      -out /etc/ssl/zenplus/zenplus-selfsigned.crt \
      -subj "/CN=zenplus-appliance" >/dev/null 2>&1
    chmod 0600 /etc/ssl/zenplus/zenplus-selfsigned.key
    chmod 0644 /etc/ssl/zenplus/zenplus-selfsigned.crt
  fi

  cat >/etc/nginx/conf.d/zenplus.conf <<'NGINX'
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name _;

    ssl_certificate /etc/ssl/zenplus/zenplus-selfsigned.crt;
    ssl_certificate_key /etc/ssl/zenplus/zenplus-selfsigned.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

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
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }

    location = /docs {
        proxy_pass http://127.0.0.1:8000/docs;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /redoc {
        proxy_pass http://127.0.0.1:8000/redoc;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /openapi.json {
        proxy_pass http://127.0.0.1:8000/openapi.json;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ~* \.(js|css|png|jpg|ico|svg|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
NGINX

  nginx -t
  record_check "security" "HTTPS reverse proxy configured" "PASS" "self-signed first-boot certificate"
}

bind_api_to_loopback() {
  log "Binding API to loopback"
  python3 - <<'PY'
from pathlib import Path
p = Path("/etc/systemd/system/zenplus-api.service")
text = p.read_text()
text = text.replace("--host 0.0.0.0 --port 8000", "--host 127.0.0.1 --port 8000")
p.write_text(text)
PY
  systemctl daemon-reload
  record_check "security" "API bound to loopback" "PASS" "Nginx remains public entrypoint"
}

configure_firewall() {
  log "Configuring firewall"
  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  record_check "security" "firewall enabled" "PASS" "OpenSSH, 80, 443 allowed"
}

configure_first_boot() {
  log "Installing first-boot service"
  install -m 0755 "$SRC_DIR/bin/first-boot-init.sh" "$ZENPLUS_HOME/bin/first-boot-init.sh"
  install -m 0644 "$SRC_DIR/systemd/zenplus-first-boot.service" /etc/systemd/system/zenplus-first-boot.service

  install -d -m 0755 /etc/systemd/system/zenplus-wait-deps.service.d
  cat >/etc/systemd/system/zenplus-wait-deps.service.d/10-firstboot.conf <<'EOF'
[Unit]
After=zenplus-first-boot.service
Requires=zenplus-first-boot.service
EOF

  systemctl daemon-reload
  systemctl enable zenplus-first-boot.service >/dev/null
  record_check "architecture" "first-boot service installed" "PASS" "secrets generated on imported appliance"
}

restart_runtime_services() {
  log "Restarting runtime services for live verification"
  systemctl restart nginx
  systemctl restart zenplus-wait-deps
  systemctl restart zenplus-api
  systemctl restart zenplus-poller
  sleep 5
}

verify_live_system() {
  log "Verifying live appliance before export cleanup"
  check_cmd "system" "PostgreSQL active" 'systemctl is-active --quiet postgresql'
  check_cmd "system" "Redis active" 'systemctl is-active --quiet redis-server'
  check_cmd "system" "Nginx active" 'systemctl is-active --quiet nginx'
  check_cmd "system" "ClickHouse container healthy" 'docker inspect -f "{{.State.Health.Status}}" zenplus-clickhouse | grep -q healthy'
  check_cmd "application" "API service active" 'systemctl is-active --quiet zenplus-api'
  check_cmd "application" "poller service active" 'systemctl is-active --quiet zenplus-poller'
  check_cmd "application" "HTTPS dashboard responds" 'curl -ksSf https://localhost/ >/dev/null'
  check_cmd "application" "API health ok" 'curl -ksSf https://localhost/api/v1/system/health | jq -e ".status == \"ok\"" >/dev/null'
  check_cmd "application" "poller health ok" 'curl -sSf http://127.0.0.1:8081/health | jq -e ".status == \"ok\"" >/dev/null'
  check_cmd "application" "admin login works before cleanup" "TOKEN=\$(curl -ksSf -X POST https://localhost/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$ADMIN_TEST_PASSWORD\"}' | jq -r '.access_token // empty'); test -n \"\$TOKEN\""
  check_cmd "security" "API listens on loopback" "ss -ltn | awk '{print \$4}' | grep -Eq '127\\.0\\.0\\.1:8000|\\[::1\\]:8000'"
  check_cmd "security" "UFW active" 'ufw status | grep -q "Status: active"'
  check_cmd "architecture" "updater timer enabled" 'systemctl is-enabled zenplus-updater.timer >/dev/null'
  check_cmd "architecture" "release public key present" "test -f '$ZENPLUS_HOME/updater/keys/zentryc-release.pub'"
  check_cmd "security" "no private signing key in appliance" "! find '$ZENPLUS_HOME/updater/keys' -type f \\( -name '*.key' -o -name '*.pem' \\) | grep -q ."
}

prepare_export_state() {
  log "Cleaning VM into OVA export-ready state"
  install -m 0755 "$SRC_DIR/scripts/prepare-main-appliance-ova.sh" /usr/local/sbin/zenplus-prepare-main-appliance-ova
  install -m 0755 "$SRC_DIR/scripts/verify-main-appliance-ova-ready.sh" /usr/local/sbin/zenplus-verify-main-appliance-ova-ready

  # The prep script stops services and removes generated state. It also runs
  # its own readiness verifier.
  ZENPLUS_HOME="$ZENPLUS_HOME" /usr/local/sbin/zenplus-prepare-main-appliance-ova --yes

  check_cmd "export" "OVA readiness verifier passes" "/usr/local/sbin/zenplus-verify-main-appliance-ova-ready"
}

main() {
  require_root
  log "Starting ZenPlus golden appliance provisioning"
  verify_base_os
  install_bootstrap_packages
  fetch_source
  run_installer
  apply_known_appliance_fixes
  configure_https_and_proxy
  bind_api_to_loopback
  configure_firewall
  restart_runtime_services
  verify_live_system
  configure_first_boot
  prepare_export_state
  write_report

  log "Provisioning complete"
  cat "$REPORT_FILE"
  echo
  echo "Checklist written to: $REPORT_FILE"
  echo "Log written to:       $LOG_FILE"
  echo
  echo "If every checklist item is PASS, run:"
  echo "  sudo poweroff"
  echo
  echo "Then export the powered-off VM from VMware/ESXi/Workstation as OVA."
}

main "$@"
