#!/usr/bin/env bash
# Start the local observability stack and the dashboard on http://localhost:9983.
#
# The stack is the docker-compose Prometheus/Alertmanager/Loki/Grafana set plus
# the demo app. No Kubernetes is involved.
#
# Usage:
#   scripts/start-dashboard.sh [--no-stack] [--foreground]
#
#   --no-stack    assume the compose stack is already running; only start the UI
#   --foreground  keep the compose stack attached instead of detaching
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NO_STACK=false
FOREGROUND=false
for arg in "$@"; do
  case "$arg" in
    --no-stack)    NO_STACK=true ;;
    --foreground)  FOREGROUND=true ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

## optional .env (IMAGE_TAG, SLACK_WEBHOOK_URL, ...)
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

## 1) raw configs for compose are generated from the manifests
if [[ ! -d .local ]]; then
  echo "==> Generating .local/ configs from the manifests..."
  python3 scripts/extract-configs.py
fi

COMPOSE=(docker compose
  -f docker-compose.dev.yaml
  -f docker-compose.monitoring.yaml
  -f docker-compose.local.yaml)

## 2) bring up the backing services (app + Prometheus + Alertmanager + Loki + Grafana)
if [[ "$NO_STACK" == false ]]; then
  echo "==> Starting local stack (app, Prometheus, Alertmanager, Loki, Grafana)..."
  if [[ "$FOREGROUND" == true ]]; then
    "${COMPOSE[@]}" up --build &
  else
    "${COMPOSE[@]}" up -d --build
  fi

  echo "==> Waiting for Grafana to become ready..."
  for _ in $(seq 1 60); do
    if curl -fsS http://localhost:3000/api/health >/dev/null 2>&1; then
      echo "    Grafana is up."
      break
    fi
    sleep 1
  done
fi

echo
echo "==> Local dashboard:  http://localhost:${DASHBOARD_PORT:-9983}"
echo "    Grafana:          http://localhost:3000   (admin/admin)"
echo "    Prometheus:       http://localhost:9090/targets"
echo "    Alertmanager:     http://localhost:9093"
echo "    Loki:             http://localhost:3100/ready"
echo "    Demo app:         http://localhost:5000"
echo
echo "    Press Ctrl-C to stop the dashboard."
echo

exec python3 dashboard/server.py
