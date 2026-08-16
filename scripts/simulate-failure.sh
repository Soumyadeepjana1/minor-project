#!/usr/bin/env bash
# Simulate load and failures so you can watch metrics, logs, and alerts react.
#
# Usage:
#   scripts/simulate-failure.sh [--traffic N] [--errors N] [--crash] [--scale-down]
#
# Examples:
#   scripts/simulate-failure.sh --traffic 50
#   scripts/simulate-failure.sh --crash
#   scripts/simulate-failure.sh --traffic 20 --errors 10 --crash
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TRAFFIC=0
ERRORS=0
CRASH=false
SCALE_DOWN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --traffic) TRAFFIC="${2:-0}"; shift 2 ;;
    --errors)  ERRORS="${2:-0}";  shift 2 ;;
    --crash)   CRASH=true;        shift ;;
    --scale-down) SCALE_DOWN=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

NS="${NS:-demo}"
SERVICE="${SERVICE:-demo-app}"
PORT="${PORT:-8080}"

# Reach the service via port-forward on the background.
kubectl port-forward -n "$NS" "svc/$SERVICE" "$PORT:80" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
sleep 3

echo "==> Generating $TRAFFIC normal requests to localhost:$PORT/..."
for ((i = 0; i < TRAFFIC; i++)); do
  curl -s -o /dev/null "http://localhost:$PORT/"
done

if [[ "$ERRORS" -gt 0 ]]; then
  echo "==> Generating $ERRORS error requests (404s) to /does-not-exist..."
  for ((i = 0; i < ERRORS; i++)); do
    curl -s -o /dev/null "http://localhost:$PORT/does-not-exist"
  done
fi

if [[ "$CRASH" == true ]]; then
  echo "==> Crashing a demo-app pod (kubectl delete pod)..."
  POD="$(kubectl get pods -n "$NS" -l app=demo-app -o jsonpath='{.items[0].metadata.name}')"
  kubectl delete pod "$POD" -n "$NS"
  echo "    Deleted $POD - watch the DemoAppDown / target-down alerts and pod restart."
fi

if [[ "$SCALE_DOWN" == true ]]; then
  echo "==> Scaling demo-app to 0 replicas for 30s..."
  kubectl scale deployment/demo-app -n "$NS" --replicas=0
  sleep 30
  kubectl scale deployment/demo-app -n "$NS" --replicas=2
  echo "    Restored to 2 replicas."
fi

echo "Done. Check Prometheus alerts and Grafana dashboards."
