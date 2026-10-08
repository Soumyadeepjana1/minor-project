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
docker compose -f docker-compose.dev.yaml up -d --build
#   App: http://localhost:5000 , Prometheus: http://localhost:9090

docker compose -f docker-compose.monitoring.yaml up -d
#   Grafana: http://localhost:3000 (admin/admin) with Loki + Prometheus datasources
```

## Local dashboard (http://localhost:9983)

```bash
make dashboard          # starts the stack above + the dashboard on :9983
make dashboard-stop     # stops the stack
```

One page at <http://localhost:9983> that embeds the Grafana dashboards
(Cluster Health, Application SLO) and shows live status of the stack. Only the
Python standard library is required; the Grafana service has
`GF_SECURITY_ALLOW_EMBEDDING` enabled in `docker-compose.monitoring.yaml` so
the panels can be iframed.

Run just the UI against an already-running stack:

```bash
./scripts/start-dashboard.sh --no-stack
# or: python3 dashboard/server.py   (DASHBOARD_PORT to change the port)
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

Dashboards: **Cluster Health** (stack health) and **Application SLO**
(request rate, error ratio, latency, instances) are provisioned in Grafana.

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
