# Setup

## Prerequisites

- **Docker** — [docs.docker.com/get-docker](https://docs.docker.com/get-docker/)
- **kubectl** — [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/)
- **A local cluster**, one of:
  - **Minikube** — [minikube.sigs.k8s.io/docs/start](https://minikube.sigs.k8s.io/docs/start/)
  - **k3d / k3s** — [k3d.io](https://k3d.io/)
- **Helm** (optional, for the nginx ingress controller) —
  [helm.sh](https://helm.sh/)
- **Python 3.10+** and `python3 -m venv`

## Install checklist

```bash
docker --version          # Docker installed
kubectl version --client  # kubectl installed
minikube version          # or: k3d version
helm version              # optional, for ingress
python3 -m venv --help >/dev/null && echo "venv ok"
```

## Quickstart

```bash
# 1) Local environment (venv, deps, raw configs for compose)
make setup

# 2) Test + lint
make test
make lint

# 3) Build the app image
make build

# 4) Start a local cluster (Minikube or k3s)
./scripts/setup-cluster.sh --provider auto

# 5) Deploy app + observability stack
make deploy

# 6) Forward ports and check things out
./scripts/port-forward.sh
#   App:          http://localhost:8080
#   Prometheus:   http://localhost:9090/targets
#   Alertmanager: http://localhost:9093
#   Loki:         http://localhost:3100/ready
#   Grafana:      http://localhost:3000  (admin/admin)
```

### Slack webhook (optional)

Alertmanager does not expand env vars in its config, so the Slack URL is a
placeholder (`https://hooks.slack.com/services/REPLACE/ME`) in the ConfigMap.
To use a real webhook, put it in `.env`:

```bash
# .env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

`scripts/deploy-all.sh` substitutes it into the `alertmanager-config`
ConfigMap before applying (docker-compose: `make setup` reads it when
generating `.local/alertmanager.yml`).

### Ingress (optional)

Requires the nginx ingress controller:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace
kubectl apply -f k8s/ingress.yaml
echo "127.0.0.1 demo.local" | sudo tee -a /etc/hosts
# then: curl http://demo.local/
```

## Docker Compose (no cluster required)

```bash
make setup            # generates .local/ raw configs from the ConfigMaps
docker compose up -d --build
#   everything: app :5000, Prometheus :9090, Alertmanager :9093, Loki :3100,
#               Grafana :3000 (admin/admin), dashboard :9983

docker compose -f docker-compose.dev.yaml up -d --build      # app + Prometheus only
docker compose -f docker-compose.monitoring.yaml up -d       # observability stack only
```

## Dashboard (http://localhost:9983)

```bash
make dashboard          # whole stack (compose.yaml) + dashboard container
make dashboard-dev      # run the dashboard locally with gunicorn
make dashboard-stop     # stop the stack
make smoke              # end-to-end check of the running stack
```

Or bring everything up with the single compose entrypoint:

```bash
docker compose up -d --build                             # app, metrics, logs, UI
docker compose -f compose.yaml -f docker-compose.local.yaml up -d --build
#   ^ add the local override on hosts where the container log paths are not readable
```

The dashboard is a Flask app served by gunicorn:

| Endpoint        | Purpose                                                          |
|-----------------|------------------------------------------------------------------|
| `/`             | UI: live metrics, CI/CD status, backend health, embedded dashboards |
| `/api/pipelines`| live GitHub Actions status (latest run per workflow)             |
| `/healthz`     | liveness                                                      |
| `/readyz`      | readiness                                                     |
| `/api/metrics` | live project data queried from Prometheus                      |
| `/api/status`  | reachability of Grafana/Prometheus/Alertmanager/Loki/app       |
| `/api/config`  | dashboards + refresh interval for the UI                       |
| `/metrics`     | Prometheus metrics for the dashboard itself                    |

Relevant environment variables: `GRAFANA_URL` and `GRAFANA_PUBLIC_URL` (the
former is the container-network address used for probes, the latter the
browser-facing address used for iframes) — likewise `PROMETHEUS_`,
`ALERTMANAGER_`, `LOKI_`, `APP_`; plus `DASHBOARD_PORT`, `STATUS_TTL`,
`STATUS_TIMEOUT`, `REFRESH_SECONDS`, `LOG_LEVEL` and `DASHBOARDS` (JSON list of
`{uid,title,description}`). Grafana has `GF_SECURITY_ALLOW_EMBEDDING` enabled
in `docker-compose.monitoring.yaml` so the panels can be iframed.

## Continuous integration (no cluster required)

All workflows target the default branch, **`master`**.

| Workflow            | Covers                                                            |
|---------------------|-------------------------------------------------------------------|
| `ci.yml`            | ruff, shellcheck, yamllint, actionlint, pytest (3.10/3.11/3.12), generated-config drift, compose validation, multi-stage image builds + `/healthz` checks |
| `stack-smoke.yml`   | brings up the real compose stack and runs `scripts/smoke-test.sh`  |
| `cd-compose.yml`    | CD: runs after CI succeeds on `master`; pushes both images to GHCR (sha/tag/latest), attests provenance + SBOMs, creates a GitHub Release with a deploy bundle on `v*` tags, and can deploy on a self-hosted runner |
| `cd.yml`            | cluster deployment only (unchanged)                               |

### Deploy a released version (no cluster)

```bash
IMAGE_REGISTRY=ghcr.io/<owner>/<repo> IMAGE_TAG=v0.2.0 \
  docker compose -f compose.yaml -f docker-compose.registry.yaml pull
IMAGE_REGISTRY=ghcr.io/<owner>/<repo> IMAGE_TAG=v0.2.0 \
  docker compose -f compose.yaml -f docker-compose.registry.yaml up -d --no-build
```

### Pipeline status on the dashboard

The dashboard shows the live status of each GitHub Actions workflow. It reads
`GET /repos/{repo}/actions/runs`:

```bash
export GITHUB_REPOSITORY=owner/repo   # optional: auto-derived from the git remote
export GITHUB_TOKEN=ghp_...           # optional: raises the API rate limit
make dashboard
```

The result is cached for `PIPELINES_TTL` seconds (default 60) so polling the UI
does not exhaust the GitHub API rate limit.

Run the same checks locally:

```bash
make lint        # ruff
make lint-shell  # shellcheck (requires shellcheck)
make lint-yaml   # yamllint  (requires yamllint)
make test        # pytest
make smoke       # end-to-end, needs the stack running
```

## Verifying metrics, logs, and alerts end-to-end

```bash
# Metrics: demo-app targets should be UP in Prometheus
open http://localhost:9090/targets

# Logs: query Loki for demo-app streams
curl -G http://localhost:3100/loki/api/v1/labels --data-urlencode 'start=0'

# Alerts: fire synthetic load + failures, then check Alertmanager
./scripts/simulate-failure.sh --traffic 20 --errors 5 --crash
open http://localhost:9093
```

Dashboards provisioned in Grafana:

- **Demo App - Live** — real project data: request rate, error ratio (non-2xx),
  latency p50/p95/p99, in-flight requests, response sizes, uptime and the
  running build (`app_info`).
- **Cluster Health** — stack health (instances, scrape targets, alerts, resources).
- **Application SLO** — request rate, error ratio, latency, instances.

The demo app exposes `http_requests_total`, `http_request_duration_seconds`,
`http_response_size_bytes`, `http_requests_in_progress`, `app_uptime_seconds`
and `app_info`; error responses (including unmatched 404s) are counted so the
error-ratio panels reflect real traffic.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Pods stay `ImagePullBackOff` (Minikube) | Run `eval $(minikube docker-env)` then `make build` so the cluster can pull the local image. |
| `prometheus-config` scrape target for demo-app is DOWN | Confirm the `demo-app` Service exists: `kubectl get svc -n demo`. |
| Grafana dashboards missing | `kubectl delete pod -n monitoring -l app=grafana` to re-provision, or check `kubectl logs -n monitoring deploy/grafana`. |
| Slack alerts not firing | Confirm `SLACK_WEBHOOK_URL` is set in `.env` and the config was re-applied (`make deploy` / `scripts/deploy-all.sh`). |
| Loki shows no logs | Promtail reads `/var/log/pods`; on Docker Desktop with k3d this may be `/var/lib/docker/containers` — see `monitoring/promtail/promtail-config.yaml`. |
| `docker compose` fails on missing `.local/` | Run `make setup` first (generates raw configs). |

## Cleaning up

```bash
make clean            # venv + generated files
kubectl delete ns demo monitoring
minikube delete       # if you want to remove the cluster entirely
```
