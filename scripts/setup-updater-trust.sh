#!/bin/bash
# ============================================================================
# ZenPlus OTA updater trust setup
#
# The OTA updater runs as root, so whatever it trusts must be out of reach of
# the unprivileged `zenplus` service account. This hook makes that true:
#
#   1. /etc/zenplus/updater-trust.conf  root-owned trust settings (server URL,
#      TLS verification, manifest age, legacy-package policy, revoked keys)
#   2. /etc/zenplus/updater-keys.d/     extra release public keys (rotation)
#   3. Everything the root updater executes or imports is owned by root:
#      /opt/zenplus itself (so `updater` cannot be swapped by renaming),
#      updater/ (only updater/config, written by the API, stays with
#      zenplus), scripts/ (the updater loads scripts/ch_migrate.py), bin/ and
#      the Python virtualenv the updater runs under
#   4. docker group                     `zenplus` is removed from it: membership
#      is root-equivalent through the Docker socket, and no ZenPlus service
#      running as zenplus calls Docker (ClickHouse is reached over HTTP; the
#      support-bundle unit already blocks the socket)
#
# Idempotent, never overwrites an administrator's trust file, and safe to run
# on every OTA update.
#   sudo bash scripts/setup-updater-trust.sh
# ============================================================================
set -eu

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
ETC_DIR="${ZENPLUS_ETC_DIR:-/etc/zenplus}"
SERVICE_USER="${ZENPLUS_USER:-zenplus}"
TRUST_FILE="${ETC_DIR}/updater-trust.conf"
UPDATER_DIR="${ZENPLUS_DIR}/updater"

log()  { echo "[setup-updater-trust] $1"; }
warn() { echo "[setup-updater-trust] WARNING: $1" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must run as root (sudo bash scripts/setup-updater-trust.sh)" >&2
    exit 1
fi

# ── 1. Trust settings and key directory ─────────────────────────────────────
install -d -m 0755 "${ETC_DIR}" "${ETC_DIR}/updater-keys.d"
chown root:root "${ETC_DIR}" "${ETC_DIR}/updater-keys.d"

if [ ! -e "${TRUST_FILE}" ]; then
    tmp="${TRUST_FILE}.tmp.$$"
    cat > "${tmp}" <<'TRUSTEOF'
# ZenPlus OTA updater trust settings. Root-owned: the updater refuses to run if
# this file or its directory is writable by anyone else.
#
# Values below are the built-in defaults. Uncomment only to change them.
# agent.conf is NOT consulted for any of these.
[trust]
# server_url = https://zentryc.com
# verify_tls = true
# max_manifest_age_days = 30
# Accept packages built before signed file inventories (releases before 1.23.32).
# allow_legacy_packages = false
# Stop trusting release keys by SHA-256 fingerprint of the raw public key
# (comma separated), e.g. after a signing key is retired.
# revoked_key_fingerprints =
TRUSTEOF
    chmod 0644 "${tmp}"
    chown root:root "${tmp}"
    mv "${tmp}" "${TRUST_FILE}"
    log "Created ${TRUST_FILE} with the default trust settings"
else
    chown root:root "${TRUST_FILE}"
    chmod go-w "${TRUST_FILE}"
    log "Kept existing ${TRUST_FILE}"
fi

# agent.conf used to be where these settings lived. Report, but never import,
# anything in it that differs: it is writable by the service account.
AGENT_CONF="${UPDATER_DIR}/config/agent.conf"
if [ -f "${AGENT_CONF}" ]; then
    old_url=$(sed -n 's/^[[:space:]]*url[[:space:]]*=[[:space:]]*//p' "${AGENT_CONF}" | head -n 1)
    if [ -n "${old_url}" ] && [ "${old_url}" != "https://zentryc.com" ]; then
        warn "agent.conf sets the update server to ${old_url}; that is now ignored. " \
             "Set server_url in ${TRUST_FILE} if it is intended."
    fi
    if grep -qiE '^[[:space:]]*verify_tls[[:space:]]*=[[:space:]]*(false|0|no|off)' "${AGENT_CONF}"; then
        warn "agent.conf disables TLS verification; that is now ignored."
    fi
fi

# ── 2. Updater code, keys, logs and backups belong to root ──────────────────
if [ -d "${UPDATER_DIR}" ]; then
    # chmod follows a symlink given on its command line, and the service account
    # may have owned this tree on older installs, so remove any link first. The
    # updater tree contains none.
    stray=$(find "${UPDATER_DIR}" -path "${UPDATER_DIR}/config" -prune -o -type l -print)
    if [ -n "${stray}" ]; then
        warn "removing unexpected symlinks under ${UPDATER_DIR}: ${stray}"
        find "${UPDATER_DIR}" -path "${UPDATER_DIR}/config" -prune -o -type l -exec rm -f {} +
    fi
    chown root:root "${UPDATER_DIR}"
    chmod go-w "${UPDATER_DIR}"
    for entry in "${UPDATER_DIR}"/* "${UPDATER_DIR}"/.[!.]*; do
        [ -e "${entry}" ] || continue
        [ "$(basename "${entry}")" = "config" ] && continue
        chown -h -R root:root "${entry}"
        chmod -R go-w "${entry}"
    done
    # Enrollment state is written by the API as the service account.
    if [ -d "${UPDATER_DIR}/config" ]; then
        chown "${SERVICE_USER}:${SERVICE_USER}" "${UPDATER_DIR}/config" 2>/dev/null || true
    fi
    log "Updater code, keys, logs and backups are owned by root (config/ stays with ${SERVICE_USER})"
else
    warn "${UPDATER_DIR} not found; skipping ownership fix"
fi

# The updater is started with PYTHONPATH=/opt/zenplus and cwd /opt/zenplus, so
# the directory itself must not be writable by the service account either.
if [ -d "${ZENPLUS_DIR}" ] && [ ! -L "${ZENPLUS_DIR}" ]; then
    chown root:root "${ZENPLUS_DIR}"
    chmod go-w "${ZENPLUS_DIR}"
fi

# Other code the root updater runs. A symlink here is resolved first, and
# nothing is ever followed while walking a tree (chown -h, chmod -R).
harden_tree() {
    local target real
    target="$1"
    [ -e "${target}" ] || return 0
    real=$(readlink -f "${target}")
    case "${real}" in
        "${ZENPLUS_DIR}"/*) ;;
        *) warn "${target} resolves outside ${ZENPLUS_DIR} (${real}); leaving it alone"; return 0 ;;
    esac
    [ -d "${real}" ] || return 0
    chown -h -R root:root "${real}"
    chmod -R go-w "${real}"
    log "${real} is owned by root"
}
harden_tree "${ZENPLUS_DIR}/scripts"
harden_tree "${ZENPLUS_DIR}/bin"
harden_tree "${ZENPLUS_DIR}/venv"

# ── 3. The service account must not be able to reach the Docker socket ──────
if getent group docker >/dev/null 2>&1; then
    if id -nG "${SERVICE_USER}" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        gpasswd -d "${SERVICE_USER}" docker >/dev/null
        log "Removed ${SERVICE_USER} from the docker group (takes effect when its services restart)"
    else
        log "${SERVICE_USER} is not in the docker group"
    fi
fi

log "Done"
