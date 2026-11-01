#!/usr/bin/env bash
# Simulate load and failures so you can watch metrics, logs, and alerts react.
#
# Usage:
#   scripts/simulate-failure.sh [--traffic N] [--errors N] [--crash] [--scale-down]
#                               [--app-crash] [--error-rate SECS] [--latency SECS]
#
# The last three need FAULT_INJECTION=true in the app (k8s/configmap.yaml) and
# are sized to trip their alert in monitoring/alerts/rules.yaml, which hands the
# incident to the AI service:
#   --app-crash         the app process exits once           -> DemoAppCrashLooping
#   --error-rate SECS   RATE req/s to /api/products (30% 500) -> DemoAppHighErrorRate
#   --latency SECS      RATE req/s to /api/checkout (slow)    -> DemoAppHighLatency
#
# Examples:
#   scripts/simulate-failure.sh --traffic 50
#   scripts/simulate-failure.sh --crash
#   scripts/simulate-failure.sh --traffic 20 --errors 10 --crash
#   scripts/simulate-failure.sh --error-rate 150
#   RATE=5 scripts/simulate-failure.sh --latency 180
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TRAFFIC=0
ERRORS=0
CRASH=false
SCALE_DOWN=false
APP_CRASH=false
ERROR_RATE=0
LATENCY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --traffic) TRAFFIC="${2:-0}"; shift 2 ;;
    --errors)  ERRORS="${2:-0}";  shift 2 ;;
    --crash)   CRASH=true;        shift ;;
    --scale-down) SCALE_DOWN=true; shift ;;
    --app-crash)  APP_CRASH=true;  shift ;;
    --error-rate) ERROR_RATE="${2:-0}"; shift 2 ;;
    --latency)    LATENCY="${2:-0}";    shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

NS="${NS:-demo}"
SERVICE="${SERVICE:-demo-app}"
PORT="${PORT:-8080}"
RATE="${RATE:-10}"

# load <METHOD> <path> <seconds>: RATE requests per second, in parallel.
load() {
  local method=$1 path=$2 secs=$3 sent=0 pids=()
  local end=$((SECONDS + secs))
  while [[ "$SECONDS" -lt "$end" ]]; do
    for ((i = 0; i < RATE; i++)); do
      curl -s -o /dev/null -m 5 -X "$method" "http://localhost:$PORT$path" &
      pids+=($!)
    done
    sent=$((sent + RATE))
    sleep 1
  done
  # Only the requests: a bare `wait` would also wait for the port-forward.
  # kubectl port-forward drops some connections under load; count them
  # rather than abort, the alert only needs part of the traffic.
  local failed=0
  for pid in "${pids[@]}"; do
    wait "$pid" || failed=$((failed + 1))
  done
  echo "    sent $sent $method $path requests over ${secs}s ($failed could not connect)"
}

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

if [[ "$APP_CRASH" == true ]]; then
  echo "==> Crashing the app process in one demo-app pod (GET /crash)..."
  # The image has no curl, so use the Python that runs the app.
  kubectl exec -n "$NS" deploy/demo-app -- python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:5000/crash')" \
    >/dev/null 2>&1 || true
  echo "    The container exits and restarts; DemoAppCrashLooping fires within ~2 minutes."
fi

if [[ "$ERROR_RATE" -gt 0 ]]; then
  echo "==> ${RATE} req/s to /api/products (about 30% fail) for ${ERROR_RATE}s..."
  load GET /api/products "$ERROR_RATE"
fi

if [[ "$LATENCY" -gt 0 ]]; then
  echo "==> ${RATE} req/s to /api/checkout (0.1-2.0s each) for ${LATENCY}s..."
  load POST /api/checkout "$LATENCY"
fi

echo "Done. Check Prometheus alerts and Grafana dashboards."
echo "Incidents: kubectl port-forward -n ai svc/ai-service 8000:8000, then"
echo "           curl -s localhost:8000/api/v1/incidents | python3 -m json.tool"
