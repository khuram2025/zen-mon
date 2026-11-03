#!/usr/bin/env bash
# Run only on a disposable Linux builder. Dependencies must be cached first.
# A fresh network namespace has only loopback: tests cannot contact controllers,
# production databases, notification providers or real monitoring targets.
set -euo pipefail
if [[ ${1:-} != --inside ]]; then
  if [[ $EUID == 0 ]]; then
    echo 'Run as the non-root build user; sudo is used only to create the namespace.' >&2
    exit 2
  fi
  : "${HA_PYTHON:?Set HA_PYTHON to the disposable build virtualenv Python}"
  exec sudo -n unshare --net -- bash "$0" --inside "$(id -un)" "$PWD" "$HA_PYTHON" "$PATH"
fi
build_user=$2
source_root=$3
python_bin=$4
build_path=$5
ip link set lo up
if [[ $(ip -o link show | wc -l) != 1 ]] || [[ -n $(ip route show) ]]; then
  echo 'Refusing tests: namespace is not isolated.' >&2
  exit 2
fi
cd "$source_root"
echo 'Isolation verified: loopback only; no route to any external service.'
exec runuser -u "$build_user" -- env \
  PATH="$build_path" HA_PYTHON="$python_bin" \
  ZENPLUS_DIR="$source_root" ZENPLUS_API=http://127.0.0.1:1 \
  DATABASE_URL=postgresql+asyncpg://fixture@127.0.0.1:1/fixture \
  REDIS_URL=redis://127.0.0.1:1/15 CLICKHOUSE_HOST=127.0.0.1 CLICKHOUSE_HTTP_PORT=1 \
  ZENPLUS_REQUIRE_PG=1 GOPROXY=off GOSUMDB=off \
  bash -euc '
    "$HA_PYTHON" -m pip check
    (cd poller && go test -race ./internal/sensorspool ./cmd/sensor)
    "$HA_PYTHON" -m pytest server/tests/test_sensor_event_replay.py \
      server/tests/test_sensor_fabric_contract.py server/tests/test_sensor_service_checks.py \
      server/tests/test_sensor_appliance_api.py server/tests/test_sensor_appliance_clock.py server/tests/test_sensor_runtime_metadata.py server/tests/test_sensor_snmp.py \
      server/tests/test_sensor_device_status_machine.py \
      server/tests/test_job_lock_keys.py server/tests/test_evaluator_worker_locks.py \
      server/tests/test_netflow_rollup.py server/tests/test_udt_gateway_collection.py \
      server/tests/test_udt_managed_integration.py -q --disable-warnings
  '
