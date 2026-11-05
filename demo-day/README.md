# Demo day kit

Everything needed to present the AI-Powered Kubernetes Health Monitoring System
(Minor Project 25CAR-710) on two laptops, prepared and tested on 5 November 2026.
The project's own runbook is `docs/DEMO.md`; this folder adds the scripts and checks
learned while setting up the demo laptop.

## Who shows what

| Part | Laptop | Shown by | What |
|---|---|---|---|
| 1. The web application (about 4 min) | Teammate laptop | Teammates | The demo shop: home page, products, health and metrics endpoints; how it is built (Dockerfile, image) and tested (GitHub Actions) |
| 2. Platform, AI services and the full demo (about 12 min) | Iqbal's laptop | Iqbal | Architecture, Grafana, Prometheus alerts, the AI incident service: crash, error-rate and latency scenarios with approval, the security checks, the results |
| Questions | both | all | `docs/DEMO.md` section 4 lists likely questions and answers |

The two laptops are independent: each runs its own copy, nothing is shared over the network.
The teammate laptop can also run the full platform as a backup (`TEAMMATE-SETUP.md`, part B).

## Order of the demo

| # | Who | Run / show | Say |
|---|---|---|---|
| 1 | Teammate | Web app home page and `/api/products` | The application the platform watches: a small shop API with health probes and Prometheus metrics |
| 2 | Teammate | `app/Dockerfile`, GitHub Actions page | Multi-stage image, non-root user; CI runs lint, tests on Python 3.10-3.12, image builds and a smoke test |
| 3 | Iqbal | `docs/ARCHITECTURE.md` | Three namespaces: demo, monitoring, ai. The model runs locally, so no logs leave the machine |
| 4 | Iqbal | http://localhost:9983 and Grafana "Demo App - Live"; `kubectl get pods -A` | The platform watching the same app on Kubernetes |
| 5 | Iqbal | `scripts/demo.sh crash`, then `scripts/demo.sh approve latest` | Alert, logs from Loki, root cause from the model, approved restart, verified |
| 6 | Iqbal | `scripts/demo.sh errors`, then `approve latest` | About 30% of product requests fail; the model finds the cause in the logs; rollback |
| 7 | Iqbal | `scripts/demo.sh latency`, then `approve latest` | p95 above 1 s; scale from 2 to 3 replicas (never above 5) |
| 8 | Iqbal | `scripts/demo.sh security` | No token: 401. The NetworkPolicy blocks the app's own pods |
| 9 | Iqbal | Grafana "AI Incidents"; report Table 4.2 | Detection in 28-38 s, root cause 2.5-3.5 minutes after the fault, fixes verified in 10-15 s |

Approve promptly: an incident can be approved only while its alert is active.

## Links on the laptop running the platform

| What | URL |
|---|---|
| Demo app home (port 18080 if 8080 is taken; `demo-day-setup.sh health` prints the port) | http://localhost:8080/ or http://localhost:18080/ |
| Demo app product API (about 30% of calls return 500 on purpose: `FAULT_INJECTION=true`) | `/api/products` |
| Demo app health, readiness, version, metrics | `/healthz` · `/readyz` · `/version` · `/metrics` |
| Local monitoring dashboard | http://localhost:9983 |
| Grafana (admin / admin) | http://localhost:3000 |
| Grafana dashboards | `/d/ai-incidents/ai-incidents` · `/d/demo-app/demo-app-live` · `/d/cluster-health/cluster-health` · `/d/application-slo/application-slo` |
| Prometheus | http://localhost:9090/alerts · `/targets` · `/rules` |
| Alertmanager | http://localhost:9093 |
| AI incident service: API docs, incidents, checks | http://localhost:8000/docs · `/api/v1/incidents` · `/api/v1/diagnostics` |
| Loki (API only) | http://localhost:3100/ready |

On the teammate laptop with only the web app running (part A of `TEAMMATE-SETUP.md`): http://localhost:5000/.

## Commands (run from the repository root)

| When | Command |
|---|---|
| First time on a laptop | `demo-day/demo-day-setup.sh check`, then `up`, then `rehearse` |
| 10-15 minutes before presenting | `demo-day/demo-day-setup.sh day` |
| Any time (read-only) | `demo-day/demo-day-setup.sh health` |
| Pods evicted, ErrImagePull, after a reboot | `demo-day/demo-day-setup.sh repair` |
| Keep everything up all day | `demo-day/keep-running.sh start` (`status`, `stop`) |
| Local dashboard against the k3s cluster | `demo-day/dashboard-k3s.sh` |
| Kubernetes checks and Unit 3 topics on the cluster | `demo-day/K8S-COMMANDS.md` |

## Files

| File | Purpose |
|---|---|
| `demo-day-setup.sh` | Machine check, setup, rehearsal, health check, repair, demo-day start |
| `keep-running.sh` | Restarts only what is down (k3s, Ollama, port-forwards, dashboard) and keeps the model loaded; never restarts the AI service |
| `dashboard-k3s.sh` | Runs the 9983 dashboard against the k3s cluster |
| `TEAMMATE-SETUP.md` | Step list for the teammate laptop |
| `K8S-COMMANDS.md` | Tested kubectl commands: health checks, Kubernetes topics on the live cluster, practice commands with undo |

## Lessons from setting up on 5 November 2026

| Problem | Cause | Now handled by |
|---|---|---|
| Pods evicted, then `ErrImagePull` for demo-app and ai-service | Disk fell below k3s's 300 MiB limit while every image downloaded at once; Kubernetes then deleted images; `PRUNE_HOST_IMAGES=1` had already removed Docker's copy | `check` asks for about 3.5 GB free; `PRUNE_HOST_IMAGES` is left empty; images are pulled one at a time and **pinned** in k3s; `repair` restores what is missing |
| Rollback scenario fails with "no earlier revision" | A fresh cluster has only revision 1 of demo-app | A rolling restart (same image) adds revision 2; `up`, `day`, `repair` and `rehearse` check it |
| 9983 dashboard shows every service as down | It was the Compose stack's dashboard, pointing at Compose services | `dashboard-k3s.sh` |
| Model slow on the first analysis | It unloads after inactivity | `day` reloads it; `keep-running.sh` keeps it loaded |
| Clearing incidents brought one straight back | An incident cleared while its alert still fires is re-opened | `day` waits for alerts to end first |
