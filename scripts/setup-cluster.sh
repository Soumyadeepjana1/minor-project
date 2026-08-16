#!/usr/bin/env bash
# Bootstrap a local cluster (Minikube or k3s) for the project.
#
# Usage:
#   scripts/setup-cluster.sh [--provider auto|minikube|k3s]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROVIDER="${1:-auto}"
if [[ "$PROVIDER" == "--provider" ]]; then
  PROVIDER="${2:-auto}"
fi

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl is required (see docs/SETUP.md)"; exit 1; }

# --- detect provider ---------------------------------------------------------
if [[ "$PROVIDER" == "auto" ]]; then
  if command -v minikube >/dev/null 2>&1; then
    PROVIDER=minikube
  elif command -v k3d >/dev/null 2>&1 || command -v k3s >/dev/null 2>&1; then
    PROVIDER=k3s
  else
    echo "ERROR: no cluster tool found. Install Minikube or k3d (see docs/SETUP.md)."
    exit 1
  fi
fi
echo "==> Using provider: $PROVIDER"

# --- start the cluster -------------------------------------------------------
case "$PROVIDER" in
  minikube)
    if ! minikube status >/dev/null 2>&1; then
      echo "==> Starting Minikube (driver=docker)..."
      minikube start --driver=docker --cpus=2 --memory=4096
    fi
    # Make locally-built images visible to the cluster.
    eval "$(minikube docker-env)"
    ;;
  k3s)
    if ! kubectl cluster-info >/dev/null 2>&1; then
      echo "ERROR: no cluster reachable via kubectl. Start k3s/k3d first (see docs/SETUP.md)."
      exit 1
    fi
    ;;
  *)
    echo "ERROR: unknown provider '$PROVIDER' (use auto|minikube|k3s)"
    exit 1
    ;;
esac

echo "==> Waiting for cluster to be ready..."
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null 2>&1 || \
  { echo "WARN: node readiness check timed out; continuing anyway"; }

# --- generate raw configs (docker-compose) ----------------------------------
echo "==> Extracting raw configs for docker-compose..."
python3 scripts/extract-configs.py

# --- create namespaces -------------------------------------------------------
echo "==> Creating namespaces..."
kubectl apply -f k8s/namespace.yaml
kubectl apply -f monitoring/namespace.yaml

echo
echo "Cluster ready. Next: make build && make deploy"
