# AI-Powered Kubernetes Health Monitoring System

A health-monitoring platform for Kubernetes: a Flask demo app containerized and
deployed to a local cluster, with a full observability stack (Prometheus,
Alertmanager, Loki, Promtail, Grafana), a local dashboard, CI/CD pipelines, and
deployment automation.

**Documentation:** [docs/SETUP.md](docs/SETUP.md) (quickstart & install) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (design & data flow)

## Repository layout

```
app/                  Flask demo app (v0.2.0): /, /healthz, /readyz, /version, /metrics
                      + multi-stage Dockerfile (python:3.12-alpine)
dashboard/            Local observability dashboard on :9983 (Flask + gunicorn,
                      templates/ + static/, own tests, multi-stage Dockerfile)
k8s/                  App manifests: namespace, configmap, deployment, service, ingress
monitoring/           Observability: prometheus, alerts, alertmanager, loki, promtail, grafana
compose.yaml          Single entrypoint for the whole local stack (no cluster)
docker-compose.*.yaml Modular local stacks (dev / monitoring / dashboard / local override)
.github/workflows/    ci.yml (lint+test+drift+compose+images), stack-smoke.yml (e2e), cd.yml (cluster deploy)
scripts/              start-dashboard, smoke-test, setup-cluster, deploy-all, simulate-failure,
                      port-forward, extract-configs
.yamllint.yml         yamllint config for the compose files and workflows
docs/                 SETUP.md and ARCHITECTURE.md
Makefile              setup, build, images, test, lint, lint-shell, lint-yaml, dashboard,
                      dashboard-dev, smoke, deploy, port-forward, status, clean
```

## Quickstart

```bash
make setup                                   # venv + deps + generated configs
make test && make lint                       # 23 tests, ruff clean
make images                                  # multi-stage build: app + dashboard
make dashboard                               # whole local stack + UI on :9983
./scripts/setup-cluster.sh --provider auto   # Minikube or k3s
make deploy                                  # apply app + observability manifests
./scripts/port-forward.sh                    # local access to app & stack
```

Full details, verification steps (metrics/logs/alerts end-to-end), and
troubleshooting are in [docs/SETUP.md](docs/SETUP.md).

## Local development without a cluster

One command brings up the whole stack (app, Prometheus, Alertmanager, Loki,
Promtail, Grafana and the dashboard):

```bash
docker compose up -d --build        # picks up compose.yaml automatically
```

Prefer a subset? The modular files still work on their own:

```bash
docker compose -f docker-compose.dev.yaml up -d --build       # app + Prometheus
docker compose -f docker-compose.monitoring.yaml up -d        # observability stack
```

`make setup` generates raw configs into `.local/` (gitignored) from the
ConfigMap manifests via `scripts/extract-configs.py`, so there is a single
source of truth for both Kubernetes and Docker Compose deployments.

## Dashboard (http://localhost:9983)

```bash
make dashboard           # start the stack + dashboard
make dashboard-dev       # run the dashboard locally (gunicorn) while editing it
make dashboard-stop      # stop everything
make smoke               # end-to-end check of the running stack
```

The dashboard is a small Flask app (`dashboard/`) run under gunicorn in the
`dashboard` container. It shows:

- **Live CI/CD status** from the GitHub Actions API: the latest run of every
  workflow (passing / failing / running), refreshed in near real time and
  linked to the run. Set `GITHUB_REPOSITORY` (auto-derived from the git remote
  by `start-dashboard.sh`) and optionally `GITHUB_TOKEN`; results are cached
  to respect the API rate limit (`PIPELINES_TTL`, default 60s).
- **Live project data** read from Prometheus: request rate, error ratio,
  latency p95, in-flight requests, uptime, scraped instances, total requests
  and the running build/version.
- **Backend health** for Grafana, Prometheus, Alertmanager, Loki and the app.
- The Grafana dashboards **embedded in kiosk mode**: *Demo App - Live*,
  *Cluster Health*, *Application SLO*.

Endpoints: `/` (UI), `/healthz`, `/readyz`, `/api/config`, `/api/metrics`,
`/api/status`, `/metrics`. Configuration is environment-driven:
`GRAFANA_URL`/`GRAFANA_PUBLIC_URL` (same for `PROMETHEUS_`, `ALERTMANAGER_`,
`LOKI_`, `APP_`), `DASHBOARD_PORT`, `STATUS_TTL`, `STATUS_TIMEOUT`,
`REFRESH_SECONDS` and `DASHBOARDS` (JSON override).

## Images

Both services use multi-stage builds: dependencies are installed in a
throwaway stage and only the finished virtualenv + code are copied into a
`python:3.12-alpine` runtime, running as a non-root user with a healthcheck.
That cut the app image from **188 MB to ~94 MB**.

```bash
make images      # docker build demo-app:0.2.0 + demo-dashboard:0.2.0
```

## Testing failures & alerts

```bash
./scripts/simulate-failure.sh --traffic 20 --errors 5 --crash
```

## CI/CD

All workflows target the default branch, **`master`**.

**CI — verify** (`ci.yml`)

- `lint` — ruff, shellcheck, yamllint, actionlint.
- `test` — pytest on Python 3.10/3.11/3.12.
- `config-drift` — fails if the generated `dashboards-configmap.yaml` is stale.
- `compose` — validates every compose entrypoint.
- `images` — builds both multi-stage images, reports their sizes and verifies
  they serve `/healthz`.

**CI — integration** (`stack-smoke.yml`)

- starts the real stack with docker-compose and runs `scripts/smoke-test.sh`
  end to end (app, Prometheus, Alertmanager, Loki, Grafana, dashboard).
- runs on PRs, on `master`, nightly and on demand.

**CD — deliver** (`cd-compose.yml`)

- triggered by `workflow_run` once **CI succeeds on `master`**, by a `v*` tag,
  or manually (`workflow_dispatch`).
- builds and pushes both images to GHCR tagged with the commit SHA, the tag
  (if any) and `latest` on the default branch.
- attests the images: build provenance (Sigstore) and SPDX SBOMs.
- on a version tag: creates a GitHub Release with the image digests and a
  `deploy-bundle.tar.gz` (compose files + `.env` + deploy notes).
- optionally deploys on a self-hosted runner (label `compose-host`, environment
  `production`) when the `DEPLOY_ENABLED` repository variable is `true`:
  `docker compose -f compose.yaml -f docker-compose.registry.yaml pull` then
  `up -d --no-build`, followed by the smoke test.

**CD — cluster** (`cd.yml`) — cluster deployment only (unchanged); needs a
kubeconfig from the `KUBECONFIG_B64` secret.

### Deploying a released version

```bash
# images are pulled from GHCR instead of being built locally
docker compose -f compose.yaml -f docker-compose.registry.yaml pull
docker compose -f compose.yaml -f docker-compose.registry.yaml up -d --no-build
```

## Known integration point

The Alertmanager `ai-service` receiver points at
`http://ai-service.ai.svc.cluster.local:8000/webhook` — a placeholder for
Developer 2's AI service (not deployed yet). Update it once that service is
live and re-apply `monitoring/alertmanager/`.
