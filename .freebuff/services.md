# AI services — run guide for the Freebuff agent

This repo's AI service (`ai-service/`) is a FastAPI app that receives Alertmanager
webhooks, pulls the failing pod's logs from Loki, and asks Ollama for a root cause.
Everything runs in Docker Compose; no Kubernetes needed for the local stack.

## 0. Prereqs

```bash
# Generated configs + token written to .local/ (gitignored):
make setup                    # venv, dev deps, scripts/extract-configs.py
# If make is unavailable:
python3 -m venv .venv && .venv/bin/python -m pip install -r app/requirements-dev.txt && .venv/bin/python scripts/extract-configs.py
```

## 1. Ollama (the model backend)

The AI service expects Ollama at `OLLAMA_URL` (default `http://host.docker.internal:11434`) —
`extra_hosts: host-gateway` is already set on the service, so a container on host port 11434 works:

```bash
docker run -d --name ollama -p 11434:11434 -v ollama:/root/.ollama ollama/ollama
docker exec ollama ollama pull "${OLLAMA_MODEL:-qwen2.5:7b}"   # first pull is slow (~4 GB)
```

If the host already runs Ollama natively, skip this. Override the model with an
`OLLAMA_MODEL=...` env var if the default pull is too heavy.

## 2. Start the stack

```bash
docker compose -f compose.yaml -f docker-compose.local.yaml up -d --build
```

That brings up app (:5000), prometheus (:9090), alertmanager (:9093), loki (:3100),
promtail, grafana (:3000), dashboard (:9983) and ai-service (:8000) on one project
network, so `LOKI_URL=http://loki:3100` resolves by service name. The
`docker-compose.local.yaml` override redirects promtail to the writable `./logs`
dir on hosts without `/var/log/pods`.

AI-service-only (no monitoring in front of it) — the token volume must exist first:

```bash
docker compose -f docker-compose.ai.yaml up -d --build
```

## 3. Verify

```bash
curl -sf http://localhost:8000/health          # {"status":"healthy"}
curl -s  http://localhost:8000/api/v1/incidents | head    # empty list [] until alerts arrive
cd ai-service && ../.venv/bin/python tools/eval_analysis.py --runs 1  # analysis quality vs a live Ollama
# incidents then show up with a dashboards view at http://localhost:9983
```

## 4. Exercise it (opens real incidents)

```bash
scripts/simulate-failure.sh --app-crash          # or --error-rate 150 / --latency 150
scripts/demo.sh status                           # incidents, alerts, AI checks
scripts/demo.sh approve latest                   # renders remediation decision
```

## 5. Like-for-like on Kubernetes

`scripts/demo.sh up` handles cluster + Ollama + deploy + port-forwards by itself
(k3s container by default, minikube via `CLUSTER=minikube`); manifests live in
`k8s/ai/`. Local pod logs reach Loki through promtail with the same token in
`.local/ai-service-token`.

## 6. Tests and lint

```bash
make ai-test     # runs ai-service tests in a python:3.11 container (pinned deps need 3.11)
make lint        # ruff over app/ dashboard/ ai-service/ scripts/
```

## Troubleshooting

- `ollama connect error`: Ollama not up or model not pulled — redo step 1, check
  `curl http://localhost:11434/api/tags`.
- 401 on `/api/v1/alerts`: the bearer token sent must match `.local/ai-service-token`
  (written by `scripts/extract-configs.py` — rerun `make setup` after changing MANIFEST configs).
- Empty incidents: alerts only create incidents when their fingerprint maps to a
  workload promtail is shipping; check `curl http://localhost:3100/ready`.
