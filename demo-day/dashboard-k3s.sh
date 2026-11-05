#!/usr/bin/env bash
# Runs the local monitoring dashboard (http://localhost:9983, report section 3.6)
# against the k3s demo cluster instead of the Docker Compose stack.
#
#   demo-day/dashboard-k3s.sh          start it (or say that it already runs)
#
# Needs the demo running (scripts/demo.sh up) and demo-day/keep-running.sh for
# the Loki forward on port 3100. The dashboard container uses host networking,
# so it reaches the port-forwards on 127.0.0.1. Grafana gets
# GF_SECURITY_ALLOW_EMBEDDING=true so the dashboard's Grafana tabs can show.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${REPO_DIR:-}" ]]; then
  if [[ -x "$PWD/scripts/demo.sh" ]]; then REPO_DIR="$PWD"
  elif [[ -x "$HERE/../scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/.." && pwd)"
  elif [[ -x "$HERE/../minor-project/scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/../minor-project" && pwd)"
  else REPO_DIR="$HOME/minor-project"; fi
fi
cd "$REPO_DIR"
export KUBECONFIG="$REPO_DIR/.local/demo/kubeconfig"
NAME=demo-dashboard-k3s
IMAGE=demo-dashboard:0.2.0

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "already running: http://localhost:9983"; exit 0
fi
if (exec 3<>/dev/tcp/127.0.0.1/9983) 2>/dev/null; then
  echo "Port 9983 is already used by: $(docker ps --filter publish=9983 --format '{{.Names}}' | tr '\n' ' ')"
  echo "Stop that first (for the Compose dashboard: docker stop demo-platform-dashboard-1), then run this again."
  exit 1
fi
[[ -f "$KUBECONFIG" ]] || { echo "No demo cluster found: run scripts/demo.sh up first."; exit 1; }

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -q -t "$IMAGE" dashboard/ >/dev/null
echo "Allowing Grafana panels to be embedded"
kubectl -n monitoring set env deploy/grafana GF_SECURITY_ALLOW_EMBEDDING=true >/dev/null
kubectl -n monitoring rollout status deploy/grafana --timeout=180s >/dev/null

APP_PORT=$(sed -n 's/^APP_PORT=//p' .local/demo/env 2>/dev/null | tr -d "'\""); APP_PORT=${APP_PORT:-8080}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network host --restart unless-stopped \
  -e DASHBOARD_PORT=9983 -e GITHUB_REPOSITORY=Soumyadeepjana1/minor-project \
  -e APP_URL="http://127.0.0.1:$APP_PORT" -e APP_PUBLIC_URL="http://localhost:$APP_PORT" \
  -e PROMETHEUS_URL=http://127.0.0.1:9090 -e PROMETHEUS_PUBLIC_URL=http://localhost:9090 \
  -e ALERTMANAGER_URL=http://127.0.0.1:9093 -e ALERTMANAGER_PUBLIC_URL=http://localhost:9093 \
  -e LOKI_URL=http://127.0.0.1:3100 -e LOKI_PUBLIC_URL=http://localhost:3100 \
  -e GRAFANA_URL=http://127.0.0.1:3000 -e GRAFANA_PUBLIC_URL=http://localhost:3000 \
  "$IMAGE" \
  gunicorn --bind 127.0.0.1:9983 --workers 2 --threads 4 --access-logfile - --error-logfile - \
           --graceful-timeout 20 app:app >/dev/null   # host networking: listen on this laptop only, not the LAN

for _ in $(seq 30); do
  curl -sf -m 2 http://localhost:9983/healthz >/dev/null && break
  sleep 1
done
echo "Dashboard: http://localhost:9983"
curl -s -m 20 http://localhost:9983/api/status | python3 -c '
import json, sys
for b in json.load(sys.stdin)["backends"]:
    print("  %-13s %s" % (b["label"], "up" if b["up"] else "down (start demo-day/keep-running.sh for Loki, or scripts/demo.sh forward)"))
' 2>/dev/null || true
