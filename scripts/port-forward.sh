#!/usr/bin/env bash
# Forward local ports to the app and observability services.
# Kills all forwards on Ctrl-C / exit.
#
# Usage:
#   scripts/port-forward.sh [--app-only]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_ONLY="${1:-}"

PIDS=()
cleanup() {
  echo
  echo "==> Stopping port-forwards..."
  kill "${PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

forward() { # namespace service local:remote
  echo "  $2 -> http://localhost:$3"
  kubectl port-forward -n "$1" "svc/$2" "$3:$4" >/dev/null 2>&1 &
  PIDS+=("$!")
}

echo "==> Starting port-forwards:"

forward demo demo-app 8080 80

if [[ "$APP_ONLY" != "--app-only" ]]; then
  forward monitoring prometheus 9090 9090
  forward monitoring alertmanager 9093 9093
  forward monitoring loki 3100 3100
  forward monitoring grafana 3000 80
fi

echo
echo "Available locally:"
echo "  App:          http://localhost:8080"
if [[ "$APP_ONLY" != "--app-only" ]]; then
  echo "  Prometheus:   http://localhost:9090/targets"
  echo "  Alertmanager: http://localhost:9093"
  echo "  Loki:         http://localhost:3100/ready"
  echo "  Grafana:      http://localhost:3000  (admin/admin)"
fi
echo
echo "Press Ctrl-C to stop all forwards."

wait
