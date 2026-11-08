#!/usr/bin/env bash
# Cron watchdog for the existing deqing-data container. No container recreation.
set -euo pipefail

exec 9>/tmp/deqing-dataset-service.lock
flock -n 9 || exit 0

if [[ "$(docker inspect -f '{{.State.Running}}' deqing-data 2>/dev/null || true)" != true ]]; then
  exit 0
fi

if docker exec deqing-data pgrep -f '^/workspace/.venv/bin/python /workspace/.venv/bin/uvicorn dataset_service.main:app' >/dev/null 2>&1; then
  exit 0
fi

docker exec -d \
  -e 'DEQING_SOURCE_ALIASES={"garden":"/workspace/imports/garden","garden_update":"/workspace/imports/garden-update"}' \
  -e DEQING_LEGACY_OUT=/workspace/legacy/out \
  -e DEQING_POSTGIS_DSN_FILE=/workspace/data/postgis_dsn \
  -e PYTHONPATH=/workspace/data-service-source \
  -w /workspace/data-service-source \
  deqing-data bash -lc \
  'exec /workspace/.venv/bin/uvicorn dataset_service.main:app --host 0.0.0.0 --port 7110 --workers 1 >> /workspace/data/service.log 2>&1'
