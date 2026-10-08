#!/usr/bin/env bash
# Start the local observability stack and the dashboard on http://localhost:9983.
#
# Uses the single compose entrypoint (compose.yaml) plus, by default, the local
# log-path override. No Kubernetes is involved.
#
# Usage:
#   scripts/start-dashboard.sh [--dev] [--no-stack] [--no-build] [--no-local-logs]
#
#   --dev             run the dashboard locally with gunicorn instead of in a
#                     container (handy while editing dashboard/ code)
#   --no-stack        assume the stack is already running; start the UI only
#   --no-build        reuse existing images instead of building
#   --no-local-logs   do not layer docker-compose.local.yaml (use the host
#                     container-log paths instead of a writable ./logs dir)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEV=false
NO_STACK=false
BUILD=true
LOCAL_LOGS=true

for arg in "$@"; do
  case "$arg" in
    --dev)            DEV=true ;;
    --no-stack)       NO_STACK=true ;;
    --no-build)       BUILD=false ;;
    --no-local-logs)  LOCAL_LOGS=false ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

## optional .env (IMAGE_TAG, SLACK_WEBHOOK_URL, DASHBOARD_PORT, ...)
# shellcheck source=/dev/null
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

DASHBOARD_PORT="${DASHBOARD_PORT:-9983}"

## GitHub repository for the live CI/CD status shown on the dashboard.
## Derived from the git remote when not set explicitly.
if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
  GITHUB_REPOSITORY="$(git remote get-url origin 2>/dev/null \
    | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
fi
export GITHUB_REPOSITORY

## 1) raw configs for compose are generated from the manifests
if [[ ! -d .local ]]; then
  echo "==> Generating .local/ configs from the manifests..."
  python3 scripts/extract-configs.py
fi

COMPOSE=(docker compose -f compose.yaml)
if [[ "$LOCAL_LOGS" == true ]]; then
  COMPOSE+=(-f docker-compose.local.yaml)
fi

## 2) bring up the backing services (app + Prometheus + Alertmanager + Loki
##    + Promtail + Grafana + dashboard)
if [[ "$NO_STACK" == false ]]; then
  echo "==> Starting local stack (app, Prometheus, Alertmanager, Loki, Promtail, Grafana)..."
  UP_ARGS=(up -d)
  if [[ "$BUILD" == true ]]; then
    UP_ARGS+=(--build)
  fi
  if [[ "$DEV" == true ]]; then
    # Only the backing services; the UI runs locally below.
    "${COMPOSE[@]}" "${UP_ARGS[@]}" app prometheus alertmanager loki promtail grafana
  else
    "${COMPOSE[@]}" "${UP_ARGS[@]}"
  fi

  echo "==> Waiting for Grafana..."
  for _ in $(seq 1 60); do
    if curl -fsS http://localhost:3000/api/health >/dev/null 2>&1; then
      echo "    Grafana is up."
      break
    fi
    sleep 1
  done
fi

if [[ "$DEV" == true ]]; then
  if [[ ! -x .venv/bin/gunicorn ]]; then
    echo "ERROR: .venv is missing; run 'make setup' first." >&2
    exit 1
  fi
  echo
  echo "==> Running dashboard locally (gunicorn) on http://localhost:${DASHBOARD_PORT}"
  echo "    Press Ctrl-C to stop."
  echo
  # Server-side probes target the published host ports when running on the host.
  export GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
  export PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
  export ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://localhost:9093}"
  export LOKI_URL="${LOKI_URL:-http://localhost:3100}"
  export APP_URL="${APP_URL:-http://localhost:5000}"
  # PYTHONPATH pins the import to dashboard/app.py (the repo root also holds an
  # app/ directory that must not shadow it).
  PYTHONPATH="$ROOT/dashboard" exec "$ROOT/.venv/bin/gunicorn" \
    --bind "0.0.0.0:${DASHBOARD_PORT}" --workers 2 --threads 4 \
    --access-logfile - --error-logfile - app:app
fi

echo
echo "==> Local dashboard:  http://localhost:${DASHBOARD_PORT}"
echo "    Pipeline status:  ${GITHUB_REPOSITORY:-not configured}"
echo "    Grafana:          http://localhost:3000   (admin/admin)"
echo "    Prometheus:       http://localhost:9090/targets"
echo "    Alertmanager:     http://localhost:9093"
echo "    Loki:             http://localhost:3100/ready"
echo "    Demo app:         http://localhost:5000"
echo
echo "    Stop everything with: make dashboard-stop"
