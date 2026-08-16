#!/usr/bin/env bash
# Full deployment automation: build the app image, apply app + observability
# manifests, and wait for rollouts.
#
# Usage:
#   scripts/deploy-all.sh [--skip-build]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load optional .env (IMAGE_TAG, SLACK_WEBHOOK_URL, ...)
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

# Alertmanager does not expand env vars in its config. If a real Slack
# webhook is configured, regenerate the alertmanager ConfigMap with the URL
# substituted into it before applying (otherwise the placeholder is used).
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  echo "==> Injecting SLACK_WEBHOOK_URL into alertmanager ConfigMap..."
  SLACK_WEBHOOK_URL="$SLACK_WEBHOOK_URL" python3 - <<'PY'
import os, pathlib, sys
import yaml

src = pathlib.Path("monitoring/alertmanager/alertmanager-config.yaml").read_text()
# Substitute inside the inner alertmanager.yml data value.
raw = yaml.safe_load(src)["data"]["alertmanager.yml"]
raw = raw.replace("https://hooks.slack.com/services/REPLACE/ME", os.environ["SLACK_WEBHOOK_URL"])

out = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "alertmanager-config", "namespace": "monitoring", "labels": {"app": "alertmanager"}},
    "data": {"alertmanager.yml": raw},
}
pathlib.Path("/tmp/alertmanager-config.generated.yaml").write_text(
    yaml.safe_dump(out, sort_keys=False)
)
PY
  kubectl apply -f /tmp/alertmanager-config.generated.yaml
fi

SKIP_BUILD="${1:-}"
IMAGE_NAME="${IMAGE_NAME:-demo-app}"
IMAGE_TAG="${IMAGE_TAG:-0.2.0}"

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl is required"; exit 1; }

# --- image -------------------------------------------------------------------
if [[ "$SKIP_BUILD" != "--skip-build" ]]; then
  echo "==> Building image $IMAGE_NAME:$IMAGE_TAG..."
  docker build -t "$IMAGE_NAME:$IMAGE_TAG" app/
else
  echo "==> Skipping build (--skip-build)"
fi

# --- regenerate generated artifacts ------------------------------------------
echo "==> Regenerating dashboards ConfigMap and raw configs..."
python3 scripts/extract-configs.py

# --- apply manifests ---------------------------------------------------------
echo "==> Applying app manifests (k8s/)..."
kubectl apply -f k8s/

echo "==> Applying observability manifests (monitoring/)..."
kubectl apply -f monitoring/

# --- wait for rollouts -------------------------------------------------------
echo "==> Waiting for rollouts..."
kubectl rollout status deployment/demo-app -n demo --timeout=180s
kubectl rollout status deployment/prometheus -n monitoring --timeout=180s
kubectl rollout status deployment/alertmanager -n monitoring --timeout=180s
kubectl rollout status deployment/loki -n monitoring --timeout=180s
kubectl rollout status deployment/grafana -n monitoring --timeout=180s

echo
echo "Deployment complete. Useful commands:"
echo "  scripts/port-forward.sh"
echo "  kubectl get pods -n demo"
echo "  kubectl get pods -n monitoring"
