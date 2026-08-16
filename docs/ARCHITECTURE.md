# Architecture

## Overview

The platform consists of a containerized Flask demo app plus a self-hosted
observability stack (Prometheus, Alertmanager, Loki, Promtail, Grafana)
deployed to a local Kubernetes cluster (Minikube or k3s). The app and the
observability stack live in separate namespaces: `demo` and `monitoring`.

```
                        ┌────────────────────────────────────────────┐
                        │                 Kubernetes                 │
                        │                                            │
  ┌──────────────┐      │  ┌─────────────┐   ┌──────────────────┐    │
  │   Browser    │──────┼─▶│  Ingress    │──▶│  demo-app x2     │    │
  └──────────────┘ HTTP  │  └─────────────┘   │  (Flask/gunicorn)│    │
                        │                     └────────┬─────────┘    │
                        │                              │ /metrics     │
                        │   ┌──────────────────────────▼──────────┐   │
                        │   │  Prometheus (scrape + alert rules)  │   │
                        │   └──────┬──────────────────────┬───────┘   │
                        │          │ alerts               │ metrics   │
                        │   ┌──────▼──────┐        ┌──────▼──────┐    │
                        │   │ Alertmanager │        │   Grafana   │    │
                        │   └──┬───────┬──┘        └─────────────┘    │
                        │      │       │                              │
                        │   Slack  AI webhook                         │
                        │      (Devel 2 service, pending)             │
                        │                                            │
                        │   ┌────────────────────────────────────┐   │
                        │   │ Promtail (DaemonSet on each node)  │   │
                        │   └────────────────┬───────────────────┘   │
                        │                    │ logs                  │
                        │              ┌─────▼──────┐                │
                        │              │    Loki    │                │
                        │              └────────────┘                │
                        └────────────────────────────────────────────┘
```

## Components

### Application (`app/`, namespace `demo`)

- **demo-app** — Flask demo service run under gunicorn. Endpoints:
  - `/` service info (name, version, environment, instance, uptime)
  - `/healthz` liveness probe
  - `/readyz` readiness probe
  - `/version` name/version/environment
  - `/metrics` Prometheus metrics (`http_requests_total`,
    `http_request_duration_seconds`)
- Config via environment variables from the `app-config` ConfigMap.
- 2 replicas, liveness/readiness probes, resource requests/limits.

### Observability (namespace `monitoring`)

| Component   | Role                                                              | Port |
|-------------|-------------------------------------------------------------------|------|
| Prometheus  | Scrapes `demo-app` (k8s service discovery), self, Alertmanager; evaluates alert rules | 9090 |
| Alertmanager| Deduplicates alerts; routes to Slack webhook and AI-service webhook  | 9093 |
| Loki        | Log store (single-binary, filesystem storage)                     | 3100 |
| Promtail    | DaemonSet shipping pod logs (from `/var/log/pods`) to Loki         | 9080 |
| Grafana     | Dashboards (Cluster Health, Application SLO); Prometheus + Loki datasources | 3000 |

## Data flow

1. **Metrics** — Prometheus scrapes `demo-app` every 15s (service discovery
   via `role: endpoints`, namespace `demo`). Alert rules (app down, 5xx ratio,
   p95 latency, target down) are evaluated every 15s.
2. **Alerts** — Firing alerts go to Alertmanager, which groups by alertname
   and routes: `critical` severity → Slack webhook (placeholder URL in the
   config; `scripts/deploy-all.sh` substitutes a real one from
   `SLACK_WEBHOOK_URL` when set); all alerts → AI-service webhook
   (`http://ai-service.ai.svc.cluster.local:8000/webhook`, placeholder for
   Developer 2's service).
3. **Logs** — Promtail (one pod per node) tails container logs under
   `/var/log/pods` and pushes them to Loki, labeled by namespace/pod/app.
4. **Dashboards** — Grafana is provisioned at startup with Prometheus and
   Loki datasources and both dashboards (from ConfigMaps).

## Source of truth & generated artifacts

- Kubernetes manifests under `k8s/` and `monitoring/` are the source of
  truth and are applied with `kubectl apply`.
- The observability configs are **wrapped in ConfigMaps** so a single
  `kubectl apply -f monitoring/` deploys everything.
- `scripts/extract-configs.py` unpacks those ConfigMaps into `.local/`
  (gitignored) and rewrites in-cluster service DNS to Docker Compose service
  names, so the same configs run locally with `docker compose`.
- `monitoring/grafana/dashboards-configmap.yaml` is generated from the
  standalone dashboard JSON files by the same script.

## CI/CD

- **CI** (`.github/workflows/ci.yml`) — lint (ruff) + tests on every push/PR;
  builds and pushes the image to GHCR on `main`.
- **CD** (`.github/workflows/cd.yml`) — on `main`: builds/pushes the image,
  then applies manifests and rolls the `demo-app` deployment to the new image
  (kubeconfig supplied via the `KUBECONFIG_B64` secret).

## Known integration point

- The Alertmanager `ai-service` receiver points at Developer 2's AI service,
  which is not deployed yet. Once it exists, update the URL in
  `monitoring/alertmanager/alertmanager-config.yaml` and run
  `kubectl apply -f monitoring/alertmanager/`.
