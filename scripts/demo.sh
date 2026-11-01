#!/usr/bin/env bash
# One script for demo day: bring everything up, run the failure scenarios,
# approve the fixes, shut down again. The runbook is docs/DEMO.md.
#
#   scripts/demo.sh up [--skip-build]       cluster, Ollama, images, deploy, port-forwards
#   scripts/demo.sh status                  pods, AI service checks, alerts, incidents
#   scripts/demo.sh crash | errors | latency
#                                           start a scenario, wait for the analysed incident
#   scripts/demo.sh incidents               list the incidents
#   scripts/demo.sh approve [ID|latest]     approve the remediation and wait for the result
#   scripts/demo.sh security                show the token and network policy at work
#   scripts/demo.sh forward                 restart the port-forwards
#   scripts/demo.sh down [--stop-cluster]   stop the port-forwards (and the cluster)
#
# Cluster: a k3s container (K3S_CONTAINER, default k3s-demo) if one exists, else
# the current kubectl context if it answers, else a new k3s container in Docker.
# CLUSTER=minikube uses Minikube instead. Settings come from .env (.env.example).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi

STATE="$ROOT/.local/demo"          # kubeconfig, ports, pids, logs (gitignored)
mkdir -p "$STATE"
CLUSTER="${CLUSTER:-auto}"
K3S_CONTAINER="${K3S_CONTAINER:-k3s-demo}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.33.4-k3s1}"
OLLAMA_BIN="${OLLAMA_BIN:-ollama}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"
APP_IMAGE="demo-app:${IMAGE_TAG:-0.2.0}"
AI_IMAGE="ai-service:${AI_IMAGE_TAG:-0.1.0}"

# Values chosen by `up` (cluster kind, kubeconfig, ports) for the other commands
if [[ -f "$STATE/env" ]]; then
  # shellcheck source=/dev/null
  source "$STATE/env"
fi
KIND="${KIND:-existing}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
APP_PORT="${APP_PORT:-8080}"
AI_PORT="${AI_PORT:-8000}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROM_PORT="${PROM_PORT:-9090}"
AM_PORT="${AM_PORT:-9093}"

# Small JSON formatters for the AI service's answers (stdin -> text)
read -r -d '' PY_DIAG <<'PY' || true
import json, sys
d = json.load(sys.stdin)
for name, c in d["checks"].items():
    print("    %-8s %-10s %s" % (name, c["status"], c["detail"]))
k = d["kubernetes"]
print("    %-8s %-10s allowed namespaces %s" % ("k8s", "enabled" if k["enabled"] else "disabled", k["allowed_namespaces"]))
PY
read -r -d '' PY_ALERTS <<'PY' || true
import json, sys
alerts = json.load(sys.stdin)["data"]["alerts"]
for a in alerts:
    print("    %-24s %s" % (a["labels"]["alertname"], a["state"]))
if not alerts:
    print("    none")
PY
read -r -d '' PY_INCIDENTS <<'PY' || true
import json, sys
inc = json.load(sys.stdin)["incidents"]
for i in sorted(inc, key=lambda i: i["created_at"]):
    print("    %-44s %-20s %s" % (i["id"], i["status"], (i.get("remediation") or {}).get("action", "")))
if not inc:
    print("    none yet")
PY
read -r -d '' PY_LATEST <<'PY' || true
import json, sys
inc = [i for i in json.load(sys.stdin)["incidents"] if i["status"] in ("analyzed", "analysis_failed")]
print(max(inc, key=lambda i: i["created_at"])["id"] if inc else "")
PY
read -r -d '' PY_ACTION <<'PY' || true
import json, sys
a = json.load(sys.stdin)["action"]
print("    action : %s on %s in %s" % (a["action"], a.get("pod") or a.get("deployment"), a["namespace"]))
PY
read -r -d '' PY_STATUS <<'PY' || true
import json, sys
print(json.load(sys.stdin)["status"])
PY
read -r -d '' PY_RESULT <<'PY' || true
import json, sys
d = json.load(sys.stdin)
r = (d.get("remediation") or {}).get("result") or {}
v = r.get("verification") or {}
print("    status : %s" % d["status"])
print("    result : %s - %s" % (r.get("status"), r.get("detail")))
print("    check  : %s - %s" % (v.get("status"), v.get("detail")))
PY

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
ai() { curl -s -m 10 "http://localhost:$AI_PORT$1"; }

save_state() {
  # %q: the repository path may contain spaces
  printf 'KIND=%q\nexport KUBECONFIG=%q\nAPP_PORT=%q\n' "$KIND" "$KUBECONFIG" "$APP_PORT" > "$STATE/env"
}

# ------------------------------------------------------------------ Ollama
ensure_ollama() {
  say "Ollama ($OLLAMA_MODEL)"
  if curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null; then
    ok "already running"
  else
    command -v "$OLLAMA_BIN" >/dev/null 2>&1 || [[ -x "$OLLAMA_BIN" ]] \
      || die "ollama not found; set OLLAMA_BIN in .env"
    # 0.0.0.0 so pods can reach it through the Docker bridge / Minikube host
    OLLAMA_HOST=0.0.0.0:11434 setsid "$OLLAMA_BIN" serve > "$STATE/ollama.log" 2>&1 < /dev/null &
    echo $! > "$STATE/ollama.pid"
    for _ in $(seq 60); do
      curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null && break
      sleep 1
    done
    curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null || die "Ollama did not start; see $STATE/ollama.log"
    ok "started (log: $STATE/ollama.log)"
  fi
  if ! curl -s -m 5 http://127.0.0.1:11434/api/tags | grep -q "\"$OLLAMA_MODEL\""; then
    say "Pulling $OLLAMA_MODEL (about 4.7 GB, once)"
    "$OLLAMA_BIN" pull "$OLLAMA_MODEL"
  fi
  ok "model $OLLAMA_MODEL present"
}

warm_model() {
  # Load the model into memory now, so the first analysis in the demo is not
  # the slow one. keep_alive holds it for an hour.
  say "Loading the model into memory"
  if curl -s -m 300 http://127.0.0.1:11434/api/generate \
      -d "{\"model\": \"$OLLAMA_MODEL\", \"prompt\": \"Reply with OK.\", \"stream\": false, \"keep_alive\": \"60m\"}" \
      >/dev/null; then
    ok "model loaded"
  else
    warn "could not load the model; the first analysis will be slower"
  fi
}

# ------------------------------------------------------------------ cluster
k3s_kubeconfig() {
  for _ in $(seq 60); do
    docker exec "$K3S_CONTAINER" test -f /etc/rancher/k3s/k3s.yaml 2>/dev/null && break
    sleep 2
  done
  docker exec "$K3S_CONTAINER" cat /etc/rancher/k3s/k3s.yaml > "$STATE/kubeconfig"
  export KUBECONFIG="$STATE/kubeconfig"
}

start_k3s() {
  need docker
  if docker ps -a --format '{{.Names}}' | grep -qx "$K3S_CONTAINER"; then
    docker start "$K3S_CONTAINER" >/dev/null
    ok "k3s container $K3S_CONTAINER started"
  else
    say "Creating k3s container $K3S_CONTAINER ($K3S_IMAGE)"
    # Fixed eviction limits: k3s's percentage defaults evict everything on a
    # nearly full laptop disk. Images are imported, not pulled, so never
    # garbage-collect them.
    docker run -d --name "$K3S_CONTAINER" --hostname "$K3S_CONTAINER" --privileged \
      -p 127.0.0.1:6443:6443 --tmpfs /run --tmpfs /var/run "$K3S_IMAGE" server \
      --disable traefik --disable metrics-server --disable servicelb --tls-san 127.0.0.1 \
      --kubelet-arg "eviction-hard=nodefs.available<300Mi,imagefs.available<300Mi" \
      --kubelet-arg "eviction-minimum-reclaim=nodefs.available=100Mi,imagefs.available=100Mi" \
      --kubelet-arg "image-gc-high-threshold=100" --kubelet-arg "image-gc-low-threshold=99" >/dev/null
  fi
  k3s_kubeconfig
  for _ in $(seq 60); do
    kubectl get nodes 2>/dev/null | grep -q ' Ready' && break
    sleep 2
  done
  kubectl get nodes 2>/dev/null | grep -q ' Ready' || die "k3s node did not become Ready"
  ok "node Ready (KUBECONFIG=$KUBECONFIG)"
}

start_cluster() {
  say "Cluster"
  if [[ "$CLUSTER" == minikube ]]; then
    KIND=minikube
    ./scripts/setup-cluster.sh --provider minikube
  elif [[ "$CLUSTER" == k3s-docker ]] || docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$K3S_CONTAINER"; then
    KIND=k3s-docker
    start_k3s
  elif [[ "$CLUSTER" == auto ]] && timeout 10 kubectl cluster-info >/dev/null 2>&1; then
    KIND=existing
    ok "using the current context: $(kubectl config current-context)"
  else
    KIND=k3s-docker
    start_k3s
  fi
}

load_images() {
  say "Building $APP_IMAGE and $AI_IMAGE into the cluster"
  case "$KIND" in
    minikube)
      minikube image build -t "$APP_IMAGE" app/
      minikube image build -t "$AI_IMAGE" ai-service/
      ;;
    k3s-docker)
      local avail
      avail=$(df -Pm / | awk 'NR==2 {print $4}')
      (( avail > 1500 )) || warn "only ${avail} MB free on /; the build may push the node into disk pressure"
      for spec in "$APP_IMAGE=app" "$AI_IMAGE=ai-service"; do
        docker build -q -t "${spec%%=*}" "${spec#*=}/" >/dev/null
        docker save "${spec%%=*}" | docker exec -i "$K3S_CONTAINER" ctr -n k8s.io images import - >/dev/null
        ok "${spec%%=*} imported"
        if [[ -n "${PRUNE_HOST_IMAGES:-}" ]]; then
          docker rmi "${spec%%=*}" >/dev/null 2>&1 || true
        fi
      done
      ;;
    *)
      docker build -q -t "$APP_IMAGE" app/ >/dev/null
      docker build -q -t "$AI_IMAGE" ai-service/ >/dev/null
      warn "built with the local Docker; make sure your cluster can use local images"
      ;;
  esac
}

ollama_url() {
  if [[ -n "${OLLAMA_URL:-}" ]]; then
    echo "$OLLAMA_URL"
  elif [[ "$KIND" == k3s-docker ]]; then
    # pods reach the host through the Docker bridge gateway
    echo "http://$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'):11434"
  fi
}

# ------------------------------------------------------------------ port-forwards
stop_forwards() {
  if [[ -f "$STATE/forwards.pid" ]]; then
    while read -r pid; do
      kill -- "-$pid" 2>/dev/null || true
    done < "$STATE/forwards.pid"
    rm -f "$STATE/forwards.pid"
  fi
}

forward() { # name namespace service local-port service-port
  # Restarts itself, so a forward survives the pod restarts the demo causes.
  setsid bash -c "while true; do kubectl port-forward -n $2 svc/$3 $4:$5 >> '$STATE/pf-$1.log' 2>&1; sleep 1; done" \
    < /dev/null > /dev/null 2>&1 &
  echo $! >> "$STATE/forwards.pid"
}

start_forwards() {
  say "Port-forwards"
  stop_forwards
  if port_busy "$APP_PORT"; then
    APP_PORT=18080
    warn "port 8080 is taken, the app goes to $APP_PORT"
  fi
  forward app demo demo-app "$APP_PORT" 80
  forward grafana monitoring grafana "$GRAFANA_PORT" 80
  forward prometheus monitoring prometheus "$PROM_PORT" 9090
  forward alertmanager monitoring alertmanager "$AM_PORT" 9093
  forward ai ai ai-service "$AI_PORT" 8000
  for _ in $(seq 30); do
    curl -sf -m 2 "http://localhost:$AI_PORT/health" >/dev/null \
      && curl -sf -m 2 "http://localhost:$PROM_PORT/-/ready" >/dev/null && break
    sleep 1
  done
  save_state
  ok "running in the background (logs: $STATE/pf-*.log)"
}

# ------------------------------------------------------------------ commands
cmd_up() {
  local skip_build=""
  [[ "${1:-}" == "--skip-build" ]] && skip_build=1
  need kubectl; need curl; need python3
  ensure_ollama
  start_cluster
  [[ -z "$skip_build" ]] && load_images
  say "Deploying the app, the monitoring stack and the AI service"
  OLLAMA_URL="$(ollama_url)" ./scripts/deploy-all.sh --skip-build
  save_state
  start_forwards
  warm_model
  say "AI service checks"
  ai /api/v1/diagnostics | python3 -c "$PY_DIAG"
  cat <<EOF

Ready. Open:
  Grafana        http://localhost:$GRAFANA_PORT   (admin / admin)   dashboard "AI Incidents"
  Prometheus     http://localhost:$PROM_PORT/alerts
  Alertmanager   http://localhost:$AM_PORT
  AI incidents   http://localhost:$AI_PORT/api/v1/incidents
  Demo app       http://localhost:$APP_PORT

Next: scripts/demo.sh crash   (then: errors, latency, approve, status)
EOF
}

cmd_status() {
  say "Pods"
  kubectl get pods -n demo; kubectl get pods -n ai; kubectl get pods -n monitoring
  say "AI service"
  ai /api/v1/diagnostics | python3 -c "$PY_DIAG"
  say "Alerts in Prometheus"
  curl -s -m 10 "http://localhost:$PROM_PORT/api/v1/alerts" | python3 -c "$PY_ALERTS"
  cmd_incidents
}

cmd_incidents() {
  say "Incidents"
  ai /api/v1/incidents | python3 -c "$PY_INCIDENTS"
}

wait_incident() { # alertname started-at(epoch) timeout-seconds
  python3 - "http://localhost:$AI_PORT" "$1" "$2" "$3" <<'PY'
import json, sys, time, urllib.request
from datetime import datetime, timezone
base, alert, started, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
seen = None
while time.time() < started + timeout:
    try:
        incidents = json.load(urllib.request.urlopen(base + "/api/v1/incidents", timeout=5))["incidents"]
    except Exception:
        incidents = []
    for i in incidents:
        created = datetime.fromisoformat(i["created_at"]).replace(tzinfo=timezone.utc).timestamp()
        if i.get("alert_name") != alert or created < started - 5:
            continue
        if i["status"] != seen:
            seen = i["status"]
            print(f"    +{int(time.time() - started):3d}s  {i['id']}  {seen}", flush=True)
        if seen in ("analyzed", "analysis_failed"):
            d = json.load(urllib.request.urlopen(f"{base}/api/v1/incidents/{i['id']}", timeout=5))
            rca = d.get("rca") or {}
            print(f"\n    Root cause : {rca.get('root_cause')}")
            print(f"    Impact     : {rca.get('impact')}")
            print(f"    Model's fix: {rca.get('recommended_fix')}   confidence {rca.get('confidence_score')}")
            # The action itself is fixed per alert in ai-service/main.py, not taken from the model.
            actions = {"DemoAppCrashLooping": "restart_pod", "DemoAppDown": "restart_pod",
                       "DemoAppHighErrorRate": "rollback_deployment", "DemoAppHighLatency": "scale_deployment"}
            print(f"    On approval: {actions.get(alert, 'notify_sre')} (fixed for {alert})")
            print(f"\n    Approve with: scripts/demo.sh approve {i['id']}")
            sys.exit(0)
    time.sleep(3)
print(f"    no analysed {alert} incident after {timeout}s; check scripts/demo.sh status")
sys.exit(1)
PY
}

run_scenario() { # alertname timeout simulate-args...
  local alert="$1" timeout="$2" started
  shift 2
  started=$(date +%s)
  # Traffic runs in the background so the incident shows up while it lasts.
  PORT="$APP_PORT" setsid ./scripts/simulate-failure.sh "$@" > "$STATE/scenario.log" 2>&1 < /dev/null &
  say "Waiting for Prometheus -> Alertmanager -> AI service ($alert, usually 2-4 minutes)"
  wait_incident "$alert" "$started" "$timeout"
}

cmd_approve() {
  local id="${1:-latest}" token status
  if [[ "$id" == latest ]]; then
    id=$(ai /api/v1/incidents | python3 -c "$PY_LATEST")
    [[ -n "$id" ]] || die "no analysed incident is waiting for approval"
  fi
  token=$(kubectl -n ai get secret ai-service-token -o jsonpath='{.data.token}' | base64 -d)
  say "Approving the remediation for $id"
  curl -s -m 10 -X POST -H "Authorization: Bearer $token" "http://localhost:$AI_PORT/api/v1/incidents/$id/remediate" \
    | python3 -c "$PY_ACTION"
  for _ in $(seq 60); do
    status=$(ai "/api/v1/incidents/$id" | python3 -c "$PY_STATUS")
    [[ "$status" == resolved || "$status" == remediation_failed ]] && break
    sleep 2
  done
  ai "/api/v1/incidents/$id" | python3 -c "$PY_RESULT"
  kubectl get pods -n demo
}

cmd_security() {
  say "Without the token the AI service refuses to open incidents or remediate"
  printf '    POST /api/v1/alerts, no token     -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"alerts": []}' "http://localhost:$AI_PORT/api/v1/alerts")"
  printf '    POST /api/v1/alerts, wrong token  -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d '{"alerts": []}' "http://localhost:$AI_PORT/api/v1/alerts")"
  say "NetworkPolicy: a demo-app pod cannot reach the AI service, Alertmanager can"
  kubectl -n demo exec deploy/demo-app -- python -c "
import urllib.request
try: urllib.request.urlopen('http://ai-service.ai.svc.cluster.local:8000/health', timeout=5); print('    demo-app     -> reachable (unexpected)')
except Exception as e: print('    demo-app     -> blocked:', e)"
  printf '    alertmanager -> %s\n' "$(kubectl -n monitoring exec deploy/alertmanager -- wget -qO- -T 5 http://ai-service.ai.svc.cluster.local:8000/health)"
}

cmd_down() {
  say "Stopping the port-forwards"
  stop_forwards
  if [[ "${1:-}" == "--stop-cluster" ]]; then
    case "${KIND:-}" in
      k3s-docker) docker stop "$K3S_CONTAINER" >/dev/null && ok "k3s container stopped" ;;
      minikube)   minikube stop ;;
      *)          warn "not stopping a cluster this script did not start" ;;
    esac
    if [[ -f "$STATE/ollama.pid" ]]; then
      if kill "$(cat "$STATE/ollama.pid")" 2>/dev/null; then ok "Ollama stopped"; fi
      rm -f "$STATE/ollama.pid"
    fi
  fi
}

case "${1:-}" in
  up)        shift; cmd_up "$@" ;;
  status)    cmd_status ;;
  incidents) cmd_incidents ;;
  crash)     run_scenario DemoAppCrashLooping 360 --app-crash ;;
  errors)    run_scenario DemoAppHighErrorRate 420 --error-rate 180 ;;
  latency)   run_scenario DemoAppHighLatency 480 --latency 240 ;;
  approve)   shift; cmd_approve "$@" ;;
  security)  cmd_security ;;
  forward)   start_forwards ;;
  down)      shift; cmd_down "$@" ;;
  *)         sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
