# Demo App — Platform & Infrastructure (Developer 1)

Kubernetes platform project: a Flask demo app containerized and deployed to a
local cluster, with a full observability stack (Prometheus, Alertmanager,
Loki, Promtail, Grafana), CI/CD pipelines, and deployment automation.

**Documentation:** [docs/SETUP.md](docs/SETUP.md) (quickstart & install) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (design & data flow)

## Repository layout

```
app/                  Flask demo app (v0.2.0): /, /healthz, /readyz, /version, /metrics
k8s/                  App manifests: namespace, configmap, deployment, service, ingress
monitoring/           Observability: prometheus, alerts, alertmanager, loki, promtail, grafana
.github/workflows/    ci.yml (lint/test/build-push) and cd.yml (deploy pipeline)
scripts/              setup-cluster, deploy-all, simulate-failure, port-forward, extract-configs
docker-compose.*.yaml Local stacks (app+prometheus / full monitoring) without a cluster
docs/                 SETUP.md and ARCHITECTURE.md
Makefile              setup, build, test, lint, deploy, port-forward, status, clean
```

## Quickstart

```bash
make setup                                   # venv + deps + generated configs
make test && make lint                       # 6 tests, ruff clean
make build                                   # docker build demo-app:0.2.0
./scripts/setup-cluster.sh --provider auto   # Minikube or k3s
make deploy                                  # apply app + observability manifests
./scripts/port-forward.sh                    # local access to app & stack
```

Full details, verification steps (metrics/logs/alerts end-to-end), and
troubleshooting are in [docs/SETUP.md](docs/SETUP.md).

## Local development without a cluster

```bash
make setup
docker compose -f docker-compose.dev.yaml up -d --build       # app + Prometheus
docker compose -f docker-compose.monitoring.yaml up -d        # full monitoring stack
```

`make setup` generates raw configs into `.local/` (gitignored) from the
ConfigMap manifests via `scripts/extract-configs.py`, so there is a single
source of truth for both Kubernetes and Docker Compose deployments.

## Testing failures & alerts

```bash
./scripts/simulate-failure.sh --traffic 20 --errors 5 --crash
```

## CI/CD

- **CI** — ruff lint + pytest on every push/PR; builds/pushes the image to
  GHCR on `main`.
- **CD** — on `main`: builds/pushes, then applies manifests and rolls
  `demo-app` to the new image (kubeconfig from the `KUBECONFIG_B64` secret).

## Known integration point

The Alertmanager `ai-service` receiver points at
`http://ai-service.ai.svc.cluster.local:8000/webhook` — a placeholder for
Developer 2's AI service (not deployed yet). Update it once that service is
live and re-apply `monitoring/alertmanager/`.
