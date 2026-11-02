# Architecture

## Overview

The platform consists of a containerized Flask demo app, a self-hosted
observability stack (Prometheus, Alertmanager, Loki, Promtail, Grafana) and an
AI incident service, deployed to a local Kubernetes cluster (Minikube or k3s).
They live in separate namespaces: `demo`, `monitoring` and `ai`. A language
model (Ollama, `qwen2.5:7b`) runs on the host, outside the cluster.

```
                        ┌──────────────────────────────────────────────┐
                        │                  Kubernetes                  │
                        │                                              │
  ┌──────────────┐      │  ┌─────────────┐   ┌──────────────────┐      │
  │   Browser    │──────┼─▶│  Ingress    │──▶│  demo-app x2     │◀──┐  │
  └──────────────┘ HTTP │  └─────────────┘   │  (Flask/gunicorn)│   │  │
                        │                    └────────┬─────────┘   │  │
                        │                             │ /metrics    │  │
                        │   ┌─────────────────────────▼──────────┐  │  │
                        │   │ Prometheus (scrape + alert rules)  │  │  │
                        │   │ + kube-state-metrics (restarts)    │  │  │
                        │   └──────┬──────────────────────┬──────┘  │  │
                        │          │ alerts               │ metrics │  │
                        │   ┌──────▼───────┐       ┌──────▼──────┐  │  │
                        │   │ Alertmanager │       │   Grafana   │  │  │
                        │   └──┬────────┬──┘       └─────────────┘  │  │
                        │      │        │ webhook + bearer token    │  │
                        │   Slack  ┌────▼───────────────────────┐   │  │
                        │          │ ai-service (namespace ai)  │───┘  │
                        │          │ incident → logs → analysis │      │
                        │          │ → restart/rollback/scale   │      │
                        │          └────┬───────────────┬───────┘      │
                        │               │ LogQL         │              │
                        │   ┌───────────▼──────┐        │              │
                        │   │ Loki ◀─ Promtail │        │              │
                        │   └──────────────────┘        │              │
                        └───────────────────────────────┼──────────────┘
                                                        │ HTTP
                                               ┌────────▼────────┐
                                               │ Ollama on host  │
                                               │ (qwen2.5:7b)    │
                                               └─────────────────┘
```

## Components

### Application (`app/`, namespace `demo`)

- **demo-app** — Flask demo service run under gunicorn (one worker, eight
  threads, so the Prometheus counters stay in one process). Endpoints:
  - `/` service info (name, version, environment, instance, uptime)
  - `/healthz` liveness probe
  - `/readyz` readiness probe
  - `/version` name/version/environment
  - `/metrics` Prometheus metrics (`http_requests_total`,
    `http_request_duration_seconds`)
  - `/api/products`, `/api/checkout` a small shop API
- With `FAULT_INJECTION=true` (set in the `app-config` ConfigMap) the app
  misbehaves on demand: about 30% of `/api/products` calls fail with a 500,
  checkouts take 0.1–2.0 s and about 20% fail, and `/crash` stops the process.
  `scripts/simulate-failure.sh` uses these to trip each alert.
- Config via environment variables from the `app-config` ConfigMap.
- 2 replicas, liveness/readiness probes, resource requests/limits.

### Observability (namespace `monitoring`)

| Component   | Role                                                              | Port |
|-------------|-------------------------------------------------------------------|------|
| Prometheus  | Scrapes `demo-app` (k8s service discovery, labelled with namespace/pod/app), `ai-service`, kube-state-metrics, self, Alertmanager; evaluates alert rules | 9090 |
| kube-state-metrics | Container restart counts for the crash-loop alert (scoped to `demo`) | 8080 |
| Alertmanager| Groups alerts; sends every alert to the AI service, critical ones to Slack as well | 9093 |
| Loki        | Log store (single-binary, filesystem storage)                     | 3100 |
| Promtail    | DaemonSet shipping pod logs (from `/var/log/pods`) to Loki         | 9080 |
| Grafana     | Dashboards (Cluster Health, Application SLO, Demo App, AI Incidents); Prometheus + Loki datasources | 3000 |

### AI incident service (`ai-service/`, namespace `ai`)

FastAPI service that turns an alert into an analysed incident and, once a
person approves it, a remediation on the cluster.

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/alerts` | Alertmanager webhook; opens incidents, suppresses duplicates, resolves on `resolved` (bearer token) |
| `GET /api/v1/incidents`, `GET /api/v1/incidents/{id}` | Incidents with the model's root cause, impact, fix and confidence |
| `POST /api/v1/incidents/{id}/remediate` | Runs the remediation for the incident (bearer token) |
| `POST /api/v1/slack/actions` | The same, from a Slack button (Slack-signed requests only) |
| `GET /health`, `GET /api/v1/diagnostics`, `GET /metrics` | Health, dependency check (Loki, Ollama, Kubernetes), Prometheus metrics |

Which action an alert gets is fixed by the alert, not by the model:

| Alert | Remediation |
|-------|-------------|
| `DemoAppCrashLooping`, `DemoAppDown` | `restart_pod` (delete the pod; the ReplicaSet recreates it) |
| `DemoAppHighErrorRate` | `rollback_deployment` (restore the previous ReplicaSet's template) |
| `DemoAppHighLatency` | `scale_deployment` (+1 replica, at most 5) |
| anything else | `notify_sre` |

After each action the service waits for the rollout to be healthy (as
`kubectl rollout status` does) and records the result on the incident.

## Data flow

1. **Metrics** — Prometheus scrapes `demo-app` every 15s (service discovery
   via `role: endpoints`, namespace `demo`). Alert rules (app down, crash
   loop, 5xx ratio, p95 latency, target down) are evaluated every 15s and
   grouped by namespace and app, so one problem opens one incident.
2. **Alerts** — Firing alerts go to Alertmanager, which groups by alertname
   and sends every alert to the AI service
   (`http://ai-service.ai.svc.cluster.local:8000/api/v1/alerts`, with the
   bearer token) and `critical` ones to Slack as well (placeholder URL in the
   config; `scripts/deploy-all.sh` substitutes a real one from
   `SLACK_WEBHOOK_URL` when set).
3. **Logs** — Promtail (one pod per node) tails container logs under
   `/var/log/pods` (Docker JSON or CRI format) and pushes them to Loki,
   labelled by namespace/pod/app/container.
4. **Analysis** — For each new incident the AI service reads the last five
   minutes of the workload's error logs from Loki, condenses them and asks
   the model for a root cause, impact, fix and confidence. Without supporting
   logs the confidence is capped.
5. **Remediation** — An engineer approves the action (API call or Slack
   button). The service applies it through the Kubernetes API and verifies
   the rollout. Resolved alerts close the incident.
6. **Dashboards** — Grafana is provisioned at startup with Prometheus and
   Loki datasources and all dashboards (from ConfigMaps), including
   **AI Incidents** (open incidents, analysis time and confidence,
   remediation outcomes, AI service and app error logs).

## Security

- **Bearer token** — `POST /api/v1/alerts` and `/remediate` require
  `Authorization: Bearer <token>`. The token lives in the `ai-service-token`
  Secret (created once by `scripts/deploy-all.sh`, copied to `monitoring`
  for Alertmanager's `credentials_file`). Without a token the endpoints
  refuse every request.
- **NetworkPolicy** — only Alertmanager and Prometheus pods may connect to
  the AI service (`k8s/ai/networkpolicy.yaml`); operators use
  `kubectl port-forward`.
- **RBAC** — the service account in `ai` holds a Role in `demo` only: delete
  pods, patch/update deployments and their scale. Monitoring components are
  on a protected list and are never touched.
- **CORS** — off unless `CORS_ORIGINS` lists origins; never with credentials.
- The container runs as a non-root user with a read-only root filesystem.

## Source of truth & generated artifacts

- Kubernetes manifests under `k8s/` and `monitoring/` are the source of
  truth and are applied with `kubectl apply` (one directory per component;
  `scripts/deploy-all.sh` does it in order).
- The observability configs are **wrapped in ConfigMaps** so a plain
  `kubectl apply` deploys them.
- `scripts/extract-configs.py` unpacks those ConfigMaps into `.local/`
  (gitignored), rewrites in-cluster service DNS to Docker Compose service
  names and writes a local AI service token, so the same configs run locally
  with `docker compose`.
- `monitoring/grafana/dashboards-configmap.yaml` is generated from the
  standalone dashboard JSON files by the same script.

## CI/CD

- **CI** (`.github/workflows/ci.yml`) — lint (ruff, including `ai-service/`)
  + tests on every push/PR, plus a separate job for the AI service's tests on
  Python 3.11; builds and pushes the image to GHCR on `main`.
- **CD** (`.github/workflows/cd.yml`) — on `main`: builds/pushes the image,
  then applies manifests and rolls the `demo-app` deployment to the new image
  (kubeconfig supplied via the `KUBECONFIG_B64` secret).
