# Demo day runbook

Everything for showing the project is driven by one script, `scripts/demo.sh`.
This page says what to prepare, what to run, what to show and say, and what to
do when something misbehaves. The whole demo takes about 15 minutes.

**The story in two sentences:** the platform watches a demo app on Kubernetes
and raises an alert within about 30 seconds of a fault. The AI incident service
reads the app's logs, asks a local language model for the root cause, and after
we approve it restarts, rolls back or scales the app and checks that it is
healthy again.

## 1. One-time preparation (the days before)

**Machine:** Linux with Docker, `kubectl`, `python3` (with `pyyaml`) and `curl`;
Ollama with the `qwen2.5:7b` model (about 4.7 GB); at least 6 GB of free disk
and 8 GB of free RAM while the demo runs.

**Settings** go in `.env` in the repository root (not committed):

| Setting | Meaning | Example |
|---------|---------|---------|
| `OLLAMA_BIN` | Ollama binary, if it is not on the PATH | `/mnt/nvme_drive/ollama/dist/bin/ollama` |
| `OLLAMA_MODELS` | where the model files are stored | `/mnt/nvme_drive/ollama-models` |
| `PRUNE_HOST_IMAGES` | `1` deletes Docker's copy of the images after loading them into k3s (saves disk) | `1` |
| `CLUSTER` | `auto` (default), `k3s-docker` or `minikube` | `auto` |
| `K3S_CONTAINER` | name of the k3s container | `k3s-demo` |

**First run** (10–15 minutes: builds the images, creates the cluster, deploys
everything):

```bash
scripts/demo.sh up
```

The cluster is k3s running in a Docker container called `k3s-demo`; it keeps
everything between runs. With `CLUSTER=minikube` the script uses Minikube
instead. If Ollama is not running, the script starts it (listening on
`0.0.0.0`, so the pods can reach it).

**Rehearse once:** `crash` → `approve`, `errors` → `approve`, `latency` →
`approve`, `security`. Note how long each step takes on your machine.

## 2. On the day: 30 minutes before

```bash
cd minor-project
scripts/demo.sh up --skip-build      # about 2.5 minutes; images are already in the cluster
scripts/demo.sh status
```

Check before you start:

- [ ] `status` shows every pod `Running`, and `loki` and `ollama` as `healthy`.
- [ ] "Alerts in Prometheus" is `none` and there are no incidents (a fresh start).
- [ ] Browser tabs open (Grafana login `admin` / `admin`):
      Grafana → *AI Incidents* and *Demo App - Live*,
      Prometheus → <http://localhost:9090/alerts>,
      Alertmanager → <http://localhost:9093>.
- [ ] Laptop on power; heavy programs closed (the model analyses on the CPU).
- [ ] Terminal font enlarged; two terminals open in the repository.
- [ ] `docs/ARCHITECTURE.md` (the diagram) open for the first slide.

`up` loads the model into memory and keeps it there for an hour, so the first
analysis is not the slow one. If more than an hour passes before the demo, run
`up --skip-build` again.

## 3. The demo (about 15 minutes)

| # | Time | Run / show | Say |
|---|------|------------|-----|
| 1 | 2 min | The diagram in `docs/ARCHITECTURE.md` | Three namespaces: `demo` (app), `monitoring` (Prometheus, Alertmanager, Loki, Grafana), `ai` (incident service). The model runs locally, so no logs leave the machine. |
| 2 | 2 min | Grafana *Demo App - Live*; `kubectl get pods -A` | The platform: app with probes and metrics, alert rules, log shipping, dashboards, CI/CD. |
| 3 | 3–4 min | `scripts/demo.sh crash` | We crash the app process. kube-state-metrics sees the restart, the alert goes *pending* then *firing* (show the Prometheus alerts page), Alertmanager sends it to the AI service with a token, the service reads the logs from Loki and asks the model. |
| 4 | 1 min | `scripts/demo.sh approve latest` | A person approves; the action comes from the alert type (crash → restart), not from the model. The service deletes the pod, waits until the replacement is ready, and only then closes the incident. |
| 5 | 3–4 min | `scripts/demo.sh errors`, then `approve latest` | About 30% of product requests now fail with a 500 (a broken database). Error-rate alert → the model finds the database timeouts in the logs → rollback to the previous revision. |
| 6 | 4 min | `scripts/demo.sh latency`, then `approve latest` | Checkouts take up to 2 s; the 95th percentile passes 1 s → scale-up from 2 to 3 replicas (never above 5). |
| 7 | 1 min | `scripts/demo.sh security` | Without the token the service answers 401; the NetworkPolicy blocks the app's own pods; only Alertmanager and Prometheus get in. |
| 8 | 1 min | Grafana *AI Incidents* | Incidents by alert, analysis time and confidence, remediation outcomes. |
| 9 | 1 min | Results (report, Table 4.2) | Detection in 28–38 s, root cause 2.5–3.5 minutes after the fault, fixes verified in 10–15 s; eight defects found by running it end to end. |

**Saving time:** while step 3 is waiting, explain the alert rules or show
Alertmanager. Start `errors` (step 5) in the second terminal while you approve
and explain the crash. Each scenario prints the root cause as soon as the
model is done and tells you the exact `approve` command.

**Suggested split for three presenters:** the platform member takes steps 1–2,
the AI service member steps 3–7, the third member steps 8–9 and leads the
questions.

## 4. Questions you may get

- **Why a local model and not a cloud API?** Logs can contain customer data;
  with Ollama nothing leaves the cluster's host, and there is no per-request cost.
- **What if the model is wrong?** It only explains; the action is fixed per
  alert type, a person approves it, and guardrails apply (allowed namespace
  `demo` only, protected monitoring workloads, at most 5 replicas). Without
  supporting logs the confidence is capped at 40.
- **Who can trigger a remediation?** Only callers with the bearer token from the
  `ai-service-token` Secret, and the NetworkPolicy only lets Alertmanager and
  Prometheus connect. The service account can only touch pods and deployments
  in `demo`.
- **How fast is it?** About 30 s to detect, 20–90 s for the analysis on a laptop
  CPU, 10–15 s to apply and verify the fix (measured, report chapter 4).
- **Does the rollback fix the errors here?** It restores the previous
  ReplicaSet, which is what fixes a bad release. In the demo the fault is
  injected by configuration (`FAULT_INJECTION=true`), so the errors stop when
  the test traffic stops, not because of the rollback.
- **Why k3s and not Minikube?** Both are supported; Minikube's node image alone
  needed 1.9 GB, more than the test laptop had free.
- **What did testing find?** Logs never reached Loki, critical alerts never
  reached the AI service, request rates were inflated twentyfold, and
  rollbacks reported success without changing anything. All fixed.

## 5. If something goes wrong

| Symptom | Do this |
|---------|---------|
| A URL stops loading | `scripts/demo.sh forward` (the forwards also reconnect by themselves within seconds) |
| Port 8080 is taken | Nothing; the script moves the app to 18080 and says so |
| No incident after 4 minutes | `scripts/demo.sh status`: is the alert *firing*? Then `kubectl -n ai logs deploy/ai-service --tail=30` |
| Analysis takes minutes | The model was unloaded or the CPU is busy; it still finishes (timeout 180 s, two retries). Close other programs |
| Root cause says "no logs were available" | `kubectl -n monitoring rollout restart ds/promtail`, then run the scenario again |
| `approve` says no incident is waiting | `scripts/demo.sh incidents`, then `scripts/demo.sh approve <ID>` |
| Pods `Pending` with `disk-pressure` | Free disk space (`docker image prune`); the node recovers within five minutes |
| Something is badly stuck | `scripts/demo.sh up --skip-build` redeploys and starts the incident list fresh |

## 6. Afterwards

```bash
scripts/demo.sh down                  # stop the port-forwards
scripts/demo.sh down --stop-cluster   # also stop k3s, and Ollama if the script started it
```

Everything stays in the k3s container; next time `scripts/demo.sh up --skip-build`
brings it back.

## Command reference

| Command | What it does |
|---------|--------------|
| `scripts/demo.sh up [--skip-build]` | Ollama, cluster, images, deployment, port-forwards, model warm-up, checks |
| `scripts/demo.sh status` | Pods, AI service checks, active alerts, incidents |
| `scripts/demo.sh crash` | Crash the app process → `DemoAppCrashLooping` → restart |
| `scripts/demo.sh errors` | 10 requests/s with 30% failures for 3 minutes → `DemoAppHighErrorRate` → rollback |
| `scripts/demo.sh latency` | 10 slow checkouts/s for 4 minutes → `DemoAppHighLatency` → scale up |
| `scripts/demo.sh approve [ID\|latest]` | Approve the remediation and show the verified result |
| `scripts/demo.sh incidents` | List incidents |
| `scripts/demo.sh security` | Token and NetworkPolicy checks |
| `scripts/demo.sh forward` | Restart the port-forwards |
| `scripts/demo.sh down [--stop-cluster]` | Stop the forwards (and the cluster) |

The latency scenario also produces some failed checkouts (about 20%), so an
error-rate incident can appear alongside the latency one; that is the system
working, not a fault.
