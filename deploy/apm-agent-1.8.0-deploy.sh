#!/usr/bin/env bash
set -euo pipefail

expected_sha="631be21679787965abb392f482290103a20349d7e4344e145aa61d84b5821d69"
actual_sha="$(sha256sum /tmp/zenplus-agent-1.8.0.exe | cut -d' ' -f1)"
test "$actual_sha" = "$expected_sha"
test "$(readlink -f /opt/zenplus/dashboard/dist)" = "/opt/zenplus/dashboard/dist"
test "$(readlink -f /opt/zenplus/artifacts/agents/windows)" = "/opt/zenplus/artifacts/agents/windows"
/opt/zenplus/venv/bin/python -m py_compile /tmp/agents.py.apm-1.8.0

snapshot="/opt/zenplus/updater/backups/pre-apm-agent-180-20260823T142000Z"
test ! -e "$snapshot"
mkdir -p "$snapshot"
cp -a /opt/zenplus/server/app/api/v1/agents.py "$snapshot/agents.py"
cp -a /opt/zenplus/dashboard/dist "$snapshot/dashboard-dist"

install -o root -g root -m 0644 \
  /tmp/agents.py.apm-1.8.0 \
  /opt/zenplus/server/app/api/v1/agents.py
tar -xzf /tmp/zenplus-apm-ui-1.8.0.tar.gz -C /opt/zenplus/dashboard/dist
install -o root -g root -m 0644 \
  /tmp/zenplus-agent-1.8.0.exe \
  /opt/zenplus/artifacts/agents/windows/zenplus-agent-1.8.0.exe
install -o root -g root -m 0644 \
  /tmp/agent-manifest-1.8.0.json \
  /opt/zenplus/artifacts/agents/windows/zenplus-agent-1.8.0.manifest.json

systemctl restart zenplus-api
systemctl reload nginx
systemctl is-active zenplus-api nginx
echo DEPLOY_OK
