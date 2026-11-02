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
fi

SKIP_BUILD="${1:-}"
IMAGE_NAME="${IMAGE_NAME:-demo-app}"
IMAGE_TAG="${IMAGE_TAG:-0.2.0}"
AI_IMAGE_TAG="${AI_IMAGE_TAG:-0.1.0}"

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl is required"; exit 1; }

# --- image -------------------------------------------------------------------
if [[ "$SKIP_BUILD" != "--skip-build" ]]; then
  echo "==> Building image $IMAGE_NAME:$IMAGE_TAG..."
  docker build -t "$IMAGE_NAME:$IMAGE_TAG" app/
  echo "==> Building image ai-service:$AI_IMAGE_TAG..."
  docker build -t "ai-service:$AI_IMAGE_TAG" ai-service/
else
  echo "==> Skipping build (--skip-build)"
fi

# --- regenerate generated artifacts ------------------------------------------
echo "==> Regenerating dashboards ConfigMap and raw configs..."
python3 scripts/extract-configs.py

# --- apply manifests ---------------------------------------------------------
echo "==> Creating namespaces..."
kubectl apply -f k8s/namespace.yaml -f k8s/ai/namespace.yaml -f monitoring/namespace.yaml

# Shared bearer token: Alertmanager sends it, the AI service checks it. Made
# once, kept across deploys; the same value is copied to both namespaces.
echo "==> Ensuring the AI service API token..."
if ! kubectl -n ai get secret ai-service-token >/dev/null 2>&1; then
  kubectl -n ai create secret generic ai-service-token \
    --from-literal=token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
kubectl -n monitoring create secret generic ai-service-token --dry-run=client -o yaml \
  --from-literal=token="$(kubectl -n ai get secret ai-service-token -o jsonpath='{.data.token}' | base64 -d)" \
  | kubectl apply -f -

echo "==> Applying app manifests (k8s/)..."
kubectl apply -f k8s/

echo "==> Applying the AI incident service (k8s/ai/)..."
kubectl apply -f k8s/ai/
# The ConfigMap points at Ollama on a Minikube host. Elsewhere (k3s, a remote
# GPU box) set OLLAMA_URL, e.g. in .env.
if [[ -n "${OLLAMA_URL:-}" ]]; then
  echo "==> Pointing the AI service at Ollama on $OLLAMA_URL..."
  kubectl -n ai patch configmap ai-service-config --type merge \
    -p "{\"data\": {\"OLLAMA_URL\": \"$OLLAMA_URL\"}}"
  kubectl -n ai rollout restart deployment/ai-service
fi

echo "==> Applying observability manifests (monitoring/)..."
# One directory per component. Not -R: that would also pick up the dashboard
# JSON files, which reach Grafana through dashboards-configmap.yaml.
for dir in monitoring/*/; do
  kubectl apply -f "$dir"
done
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  # After monitoring/, whose copy of the ConfigMap has the placeholder URL.
  kubectl apply -f /tmp/alertmanager-config.generated.yaml
fi

# --- wait for rollouts -------------------------------------------------------
echo "==> Waiting for rollouts..."
kubectl rollout status deployment/demo-app -n demo --timeout=180s
kubectl rollout status deployment/prometheus -n monitoring --timeout=180s
kubectl rollout status deployment/alertmanager -n monitoring --timeout=180s
kubectl rollout status deployment/loki -n monitoring --timeout=180s
kubectl rollout status deployment/grafana -n monitoring --timeout=180s
kubectl rollout status deployment/kube-state-metrics -n monitoring --timeout=180s
kubectl rollout status deployment/ai-service -n ai --timeout=180s

echo
echo "Deployment complete. Useful commands:"
echo "  scripts/port-forward.sh"
echo "  kubectl get pods -n demo"
echo "  kubectl get pods -n monitoring"
echo "  kubectl get pods -n ai"
