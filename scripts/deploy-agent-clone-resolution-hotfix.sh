#!/usr/bin/env bash
set -euo pipefail

stage_dir="/tmp/zenplus-agent-clone-resolution-hotfix"
dashboard_root="/opt/zenplus/dashboard"
migration_name="migrate-091-agent-clone-resolutions.sql"
migration_checksum="ddc74d3e22f135f26f3eb02235bf1b8308aeb67bd88a8104f100554e8c0f0f20"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="/opt/zenplus/backups/manual-agent-clone-resolution-${stamp}"
previous_dist="${dashboard_root}/dist.pre-agent-clone-resolution-${stamp}"
new_dist="${dashboard_root}/.dist-agent-clone-resolution-${stamp}"

test "$(id -u)" -eq 0
test -s "${stage_dir}/backend/agents.py"
test -s "${stage_dir}/backend/servers.py"
test -s "${stage_dir}/backend/agent.py"
test -s "${stage_dir}/dashboard-src/AgentFleetPage.tsx"
test -s "${stage_dir}/dashboard-src/servers.ts"
test -s "${stage_dir}/dashboard-dist/index.html"
test -s "${stage_dir}/migration/${migration_name}"

actual_checksum="$(sha256sum "${stage_dir}/migration/${migration_name}" | awk '{print $1}')"
test "${actual_checksum}" = "${migration_checksum}"

mkdir -p "${backup_dir}/backend" "${backup_dir}/dashboard" "${backup_dir}/migration"
install -d -o postgres -g postgres -m 0700 "${backup_dir}/database"

cp -a /opt/zenplus/server/app/api/v1/agents.py "${backup_dir}/backend/"
cp -a /opt/zenplus/server/app/api/v1/servers.py "${backup_dir}/backend/"
cp -a /opt/zenplus/server/app/schemas/agent.py "${backup_dir}/backend/"
cp -a /opt/zenplus/dashboard/src/pages/servers/AgentFleetPage.tsx "${backup_dir}/dashboard/"
cp -a /opt/zenplus/dashboard/src/types/servers.ts "${backup_dir}/dashboard/"
cp -a /opt/zenplus/scripts/migrations.lock "${backup_dir}/migration/"
tar -C /opt/zenplus/dashboard -czf "${backup_dir}/dashboard-dist.tar.gz" dist

sudo -u postgres pg_dump -Fc -d zenplus \
  -t agents -t servers -t schema_migrations -t apm_ingest_keys \
  -t agent_apm_credentials -t audit_logs \
  -f "${backup_dir}/database/clone-resolution-tables.dump"
sudo -u postgres pg_dump --schema-only -d zenplus \
  -f "${backup_dir}/database/schema-before.sql"
chown -R root:root "${backup_dir}/database"

sha256sum \
  "${backup_dir}/backend/agents.py" \
  "${backup_dir}/backend/servers.py" \
  "${backup_dir}/backend/agent.py" \
  "${backup_dir}/migration/migrations.lock" \
  "${backup_dir}/dashboard-dist.tar.gz" \
  "${backup_dir}/database/clone-resolution-tables.dump" \
  "${backup_dir}/database/schema-before.sql" \
  > "${backup_dir}/SHA256SUMS"
# Restrict traversal at the backup root while preserving the original source
# file modes needed for an exact rollback.
chmod 0700 "${backup_dir}"

rollback() {
  rc=$?
  trap - ERR
  echo "Activation failed; restoring ${backup_dir}" >&2
  cp -a "${backup_dir}/backend/agents.py" /opt/zenplus/server/app/api/v1/agents.py
  cp -a "${backup_dir}/backend/servers.py" /opt/zenplus/server/app/api/v1/servers.py
  cp -a "${backup_dir}/backend/agent.py" /opt/zenplus/server/app/schemas/agent.py
  cp -a "${backup_dir}/dashboard/AgentFleetPage.tsx" /opt/zenplus/dashboard/src/pages/servers/AgentFleetPage.tsx
  cp -a "${backup_dir}/dashboard/servers.ts" /opt/zenplus/dashboard/src/types/servers.ts
  cp -a "${backup_dir}/migration/migrations.lock" /opt/zenplus/scripts/migrations.lock
  rm -f "/opt/zenplus/scripts/${migration_name}"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d zenplus <<SQL
DROP TABLE IF EXISTS agent_registration_resolutions;
DELETE FROM schema_migrations WHERE filename = '${migration_name}';
SQL
  if [[ -d "${previous_dist}" ]]; then
    if [[ -d "${dashboard_root}/dist" ]]; then
      mv "${dashboard_root}/dist" "${dashboard_root}/dist.failed-${stamp}"
    fi
    mv "${previous_dist}" "${dashboard_root}/dist"
  fi
  systemctl restart zenplus-api.service || true
  exit "${rc}"
}
trap rollback ERR

install -o zen -g zen -m 0644 "${stage_dir}/migration/${migration_name}" "/opt/zenplus/scripts/${migration_name}"
if ! grep -Fq "  ${migration_name}" /opt/zenplus/scripts/migrations.lock; then
  printf '%s  %s\n' "${migration_checksum}" "${migration_name}" >> /opt/zenplus/scripts/migrations.lock
fi
chown zen:zen /opt/zenplus/scripts/migrations.lock

sudo -u postgres /opt/zenplus/venv/bin/python \
  /opt/zenplus/scripts/run-migrations.py \
  --scripts-dir /opt/zenplus/scripts \
  --lock /opt/zenplus/scripts/migrations.lock \
  --user postgres --json

install -o root -g root -m 0644 "${stage_dir}/backend/agents.py" /opt/zenplus/server/app/api/v1/agents.py
install -o root -g root -m 0644 "${stage_dir}/backend/servers.py" /opt/zenplus/server/app/api/v1/servers.py
install -o root -g root -m 0644 "${stage_dir}/backend/agent.py" /opt/zenplus/server/app/schemas/agent.py
install -o zen -g zen -m 0664 "${stage_dir}/dashboard-src/AgentFleetPage.tsx" /opt/zenplus/dashboard/src/pages/servers/AgentFleetPage.tsx
install -o zen -g zen -m 0664 "${stage_dir}/dashboard-src/servers.ts" /opt/zenplus/dashboard/src/types/servers.ts

cp -a "${stage_dir}/dashboard-dist" "${new_dist}"
chown -R zen:zen "${new_dist}"
mv "${dashboard_root}/dist" "${previous_dist}"
mv "${new_dist}" "${dashboard_root}/dist"

systemctl restart zenplus-api.service
for _ in $(seq 1 30); do
  if systemctl is-active --quiet zenplus-api.service && \
     curl -fsS http://127.0.0.1:8000/api/v1/system/health >/dev/null; then
    break
  fi
  sleep 1
done

systemctl is-active --quiet zenplus-api.service
curl -fsS http://127.0.0.1:8000/api/v1/system/health >/dev/null
nginx -t
sudo -u postgres psql -d zenplus -Atc \
  "SELECT filename FROM schema_migrations WHERE filename = '${migration_name}'" | grep -Fx "${migration_name}"
sudo -u postgres psql -d zenplus -Atc \
  "SELECT to_regclass('public.agent_registration_resolutions')" | grep -Fx "agent_registration_resolutions"

trap - ERR
echo "ACTIVATION_OK"
echo "BACKUP_DIR=${backup_dir}"
echo "PREVIOUS_DIST=${previous_dist}"
