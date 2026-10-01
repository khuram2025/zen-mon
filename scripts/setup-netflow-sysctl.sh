#!/bin/bash
# Let the NetFlow collector have the socket receive buffer it asks for.
#
# zenplus-netflow-collector requests NETFLOW_RCVBUF bytes (8 MiB by default)
# for its UDP sockets so a burst of export datagrams (template refresh, cache
# flush, many exporters at the top of a minute) waits in the kernel instead of
# being dropped. Linux silently caps that request at net.core.rmem_max, which
# is ~208 KiB on a stock host, so without this the collector runs with a
# fraction of its buffer and loses bursts (kernel RcvbufErrors, sequence
# loss on exporters that lose nothing on the wire).
#
# This raises net.core.rmem_max to at least NETFLOW_RCVBUF, now and at boot
# (/etc/sysctl.d/99-zenplus-netflow.conf). It never lowers a larger value an
# operator configured: the persisted value is the larger of the current one
# and the request. Idempotent; run by install.sh, `zenplus update` and the OTA
# updater (run_hook). A host that refuses the change (a container) is
# reported, not failed: the collector's /health then says the buffer is
# smaller than requested.
set -u

ZENPLUS_DIR="${ZENPLUS_DIR:-/opt/zenplus}"
CONF="${ZENPLUS_NETFLOW_SYSCTL_CONF:-/etc/sysctl.d/99-zenplus-netflow.conf}"
SYSTEMD_UNIT="${ZENPLUS_NETFLOW_UNIT:-/etc/systemd/system/zenplus-netflow-collector.service}"
SYSCTL="${ZENPLUS_SYSCTL:-sysctl}"
DEFAULT_RCVBUF=8388608

requested_rcvbuf() {
    local value="" unit
    for unit in "$SYSTEMD_UNIT" "$ZENPLUS_DIR/poller/systemd/zenplus-netflow-collector.service"; do
        if [[ -f "$unit" ]]; then
            value=$(sed -n 's/^[[:space:]]*Environment=NETFLOW_RCVBUF=\([0-9][0-9]*\)[[:space:]]*$/\1/p' "$unit" | tail -1)
            break
        fi
    done
    # EnvironmentFile= settings override Environment= ones in systemd.
    if [[ -f "$ZENPLUS_DIR/.env" ]]; then
        local env_value
        env_value=$(sed -n 's/^[[:space:]]*NETFLOW_RCVBUF=["'\'']\{0,1\}\([0-9][0-9]*\)["'\'']\{0,1\}[[:space:]]*$/\1/p' "$ZENPLUS_DIR/.env" | tail -1)
        [[ -n "$env_value" ]] && value="$env_value"
    fi
    echo "${value:-$DEFAULT_RCVBUF}"
}

want=$(requested_rcvbuf)
if [[ ! "$want" =~ ^[0-9]+$ ]] || (( want <= 0 )); then
    echo "setup-netflow-sysctl: NETFLOW_RCVBUF is not set to a size; nothing to do"
    exit 0
fi

current=$("$SYSCTL" -n net.core.rmem_max 2>/dev/null || cat /proc/sys/net/core/rmem_max 2>/dev/null || echo 0)
[[ "$current" =~ ^[0-9]+$ ]] || current=0
target=$(( current > want ? current : want ))

if (( current < want )); then
    if "$SYSCTL" -q -w net.core.rmem_max="$target" >/dev/null 2>&1; then
        echo "setup-netflow-sysctl: net.core.rmem_max raised from $current to $target (NETFLOW_RCVBUF=$want)"
    else
        echo "setup-netflow-sysctl: WARNING: could not raise net.core.rmem_max from $current to $target;" \
             "the NetFlow collector will run with a smaller receive buffer than requested" >&2
        exit 0
    fi
else
    echo "setup-netflow-sysctl: net.core.rmem_max is $current (>= NETFLOW_RCVBUF=$want)"
fi

line="net.core.rmem_max = $target"
if [[ ! -f "$CONF" ]] || ! grep -qxF "$line" "$CONF"; then
    if ! printf '# ZenPlus NetFlow collector: socket receive buffer (NETFLOW_RCVBUF) for flow bursts.\n# Written by scripts/setup-netflow-sysctl.sh; never lower than the value in force when written.\n%s\n' \
            "$line" > "$CONF" 2>/dev/null; then
        echo "setup-netflow-sysctl: WARNING: could not write $CONF; the setting will not survive a reboot" >&2
    fi
fi
exit 0
