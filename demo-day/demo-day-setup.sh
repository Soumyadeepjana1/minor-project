#!/usr/bin/env bash
# Demo day helper for the minor project
# "AI-Powered Kubernetes Health Monitoring System with Automated Incident Response" (25CAR-710).
#
# Works on either laptop. It checks the machine, gets the code and drives the
# project's own scripts/demo.sh (runbook: docs/DEMO.md in the repository), and
# adds the fixes learned while setting up on 5 November 2026: images are pinned
# in k3s so disk pressure cannot delete them, monitoring images are pulled one
# at a time, demo-app always has a revision to roll back to, and old incidents
# are cleared only after the alerts have ended. It never prunes Docker, never
# deletes project data and never pushes to GitHub.
#
#   demo-day/demo-day-setup.sh check   (from the repository; or ./demo-day-setup.sh here)
#   ./demo-day-setup.sh check      check the machine only; changes nothing
#   ./demo-day-setup.sh up         check, get or update the code, write .env,
#                                  build and start everything, harden, health
#   ./demo-day-setup.sh rehearse   crash, errors and latency (each approved) and
#                                  the security check, with timings
#   ./demo-day-setup.sh day        10-15 minutes before presenting: restart
#                                  without rebuilding (reloads the model), clear
#                                  old incidents, health check, checklist
#   ./demo-day-setup.sh health     read-only health check of the running demo
#   ./demo-day-setup.sh repair     after evictions, ErrImagePull or a reboot:
#                                  restore images, pods and port-forwards
#   ./demo-day-setup.sh status     scripts/demo.sh status
#   ./demo-day-setup.sh all        up, then rehearse
#
# Optional settings (environment variables):
#   REPO_DIR    where the code is or goes   (default: $HOME/minor-project)
#   REPO_URL    git URL                     (default: the team repository)
#   BRANCH      branch to use               (default: the clone's current branch, or master)
#   OLLAMA_BIN  path to the ollama binary if it is not on PATH
#   SKIP_BUILD  1 = "up" does not rebuild the images
#
# Everything is also written to demo-day-report.txt next to this script.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT="$HERE/demo-day-report.txt"
if [[ -z "${REPO_DIR:-}" ]]; then
  # The repository is the current folder, the folder above this one (demo-day/ inside the
  # repository), minor-project next to this folder, or ~/minor-project
  if [[ -x "$PWD/scripts/demo.sh" ]]; then REPO_DIR="$PWD"
  elif [[ -x "$HERE/../scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/.." && pwd)"
  elif [[ -x "$HERE/../minor-project/scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/../minor-project" && pwd)"
  else REPO_DIR="$HOME/minor-project"; fi
fi
REPO_URL="${REPO_URL:-https://github.com/Soumyadeepjana1/minor-project.git}"
BRANCH="${BRANCH:-}"     # empty: the branch an existing clone is on, master for a new clone
MODEL="qwen2.5:7b"
MODEL_DIR=""
FAILS=0
WARNS=0
# Free space needed on the Docker disk: images inside k3s, pod data and a
# margin above k3s's 300 MiB eviction limit (measured on 5 Nov 2026).
NEED_DOCKER_MB=3500
PROJECT_IMAGES=(demo-app:0.2.0 ai-service:0.1.0)
MONITORING_IMAGES=(prom/alertmanager:v0.27.0 registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0
                   grafana/loki:3.2.1 grafana/promtail:3.2.1 prom/prometheus:v2.53.0 grafana/grafana:11.2.0)

# The report should be plain text: drop the colour codes when the script ends
trap 'sed -i "s/\x1b\[[0-9;]*m//g" "$REPORT" 2>/dev/null' EXIT

log()  { printf '%s\n' "$*" | tee -a "$REPORT"; }
say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; printf '\n==> %s\n' "$*" >> "$REPORT"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; printf '    ok    %s\n' "$*" >> "$REPORT"; }
warn() { WARNS=$((WARNS + 1)); printf '    \033[33mwarn\033[0m  %s\n' "$*"; printf '    warn  %s\n' "$*" >> "$REPORT"; }
fail() { FAILS=$((FAILS + 1)); printf '    \033[31mFAIL\033[0m  %s\n' "$*"; printf '    FAIL  %s\n' "$*" >> "$REPORT"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; printf 'ERROR: %s\n' "$*" >> "$REPORT"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
free_mb() { df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
bridge_ip() { docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null; }
wait_s() { timeout "$1" tail -f /dev/null 2>/dev/null; true; }
ollama_bin() {
  if [[ -n "${OLLAMA_BIN:-}" ]]; then echo "$OLLAMA_BIN"
  elif have ollama; then command -v ollama
  fi
}
header() { log "Demo day report, $(date '+%Y-%m-%d %H:%M %Z'), host $(hostname), command: $*"; }
docker_free() {
  local root
  root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
  free_mb "${root:-/var/lib/docker}" || free_mb /
}

# Run a command from the repository, show its output and keep it in the report
in_repo() {
  printf '\n$ %s\n' "$*" >> "$REPORT"
  (cd "$REPO_DIR" && "$@") 2>&1 | tee -a "$REPORT"
  return "${PIPESTATUS[0]}"
}
need_code() { [[ -x "$REPO_DIR/scripts/demo.sh" ]] || die "no code in $REPO_DIR; run: $0 up"; }

# Values that scripts/demo.sh saved (cluster kind, kubeconfig, app port) and .env
demo_env() { # prints KEY of .local/demo/env or .env
  local key=$1 v=""
  [[ -f "$REPO_DIR/.local/demo/env" ]] && v=$(sed -n "s/^\(export \)\{0,1\}$key=//p" "$REPO_DIR/.local/demo/env" | tail -1)
  [[ -z "$v" && -f "$REPO_DIR/.env" ]] && v=$(sed -n "s/^$key=//p" "$REPO_DIR/.env" | tail -1)
  v=${v#\'}; v=${v%\'}; v=${v#\"}; v=${v%\"}
  printf '%s' "$v"
}
kc() { # kubectl against the demo cluster
  (cd "$REPO_DIR" && {
    if [[ -f .local/demo/env ]]; then source .local/demo/env
    elif [[ -f .local/demo/kubeconfig ]]; then export KUBECONFIG="$PWD/.local/demo/kubeconfig"; fi
    kubectl "$@"; })
}
k3s_name() { local n; n=$(demo_env K3S_CONTAINER); echo "${n:-k3s-demo}"; }
# demo.sh always uses an existing k3s container first, so a running one is the demo cluster
is_k3s() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$(k3s_name)"; }
app_port() { local p; p=$(demo_env APP_PORT); echo "${p:-8080}"; }

# ------------------------------------------------------------------ check
check_machine() {
  say "Machine"
  if [[ "$(uname -s)" == Linux ]]; then ok "Linux, kernel $(uname -r)"
  else fail "this needs Linux (scripts/demo.sh runs k3s inside Docker)"; fi
  local cores mem_total mem_avail
  cores=$(nproc 2>/dev/null || echo 0)
  mem_total=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  mem_avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  log "          CPU: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ //')"
  if (( cores >= 4 )); then ok "$cores CPU threads"
  else warn "$cores CPU threads; the model's analysis will be slow"; fi
  if (( mem_avail >= 8000 )); then ok "$mem_avail MB RAM available of $mem_total MB"
  else warn "$mem_avail MB RAM available of $mem_total MB (8000 MB recommended): close browsers, IDEs and other containers before the demo"; fi

  say "Tools"
  local t
  for t in git docker kubectl curl python3; do
    if have "$t"; then ok "$t ($(command -v "$t"))"; else fail "$t is missing"; fi
  done
  if have python3 && python3 -c 'import yaml' 2>/dev/null; then ok "python3 has pyyaml"
  else fail "python3 module pyyaml is missing (Ubuntu: sudo apt install python3-yaml)"; fi
  if have docker && docker info >/dev/null 2>&1; then
    ok "Docker $(docker version -f '{{.Server.Version}}' 2>/dev/null) is running and usable without sudo"
  elif have docker; then
    fail "Docker is installed but not usable: start it, and add this user to the docker group (sudo usermod -aG docker \$USER, then log out and in)"
  fi

  say "Disk"
  local dfree models m d mfree need=$NEED_DOCKER_MB
  dfree=$(docker_free)
  # An existing cluster already holds its images
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx k3s-demo && need=1500
  if (( ${dfree:-0} >= need )); then ok "$dfree MB free for Docker (needs about $need MB)"
  else fail "$dfree MB free for Docker; about $need MB is needed, or Kubernetes evicts pods and deletes images. Free space first (ask before deleting anything)"; fi
  models="${OLLAMA_MODELS:-$HOME/.ollama/models}"
  for m in "$models" /usr/share/ollama/.ollama/models; do
    if [[ -e "$m/manifests/registry.ollama.ai/library/qwen2.5/7b" ]]; then MODEL_DIR="$m"; break; fi
  done
  if [[ -n "$MODEL_DIR" ]]; then
    ok "model $MODEL is already downloaded ($MODEL_DIR)"
  else
    d="$models"; while [[ ! -d "$d" ]]; do d=$(dirname "$d"); done
    mfree=$(free_mb "$d")
    if (( mfree >= 5000 )); then ok "model $MODEL not downloaded yet; $mfree MB free for it under $d (needs about 4.7 GB, downloaded by 'up')"
    else fail "model $MODEL not downloaded and only $mfree MB free under $d (needs about 4.7 GB); free space or set OLLAMA_MODELS to a disk with room"; fi
  fi

  say "Ollama"
  local ob br
  ob=$(ollama_bin)
  if [[ -z "$ob" ]]; then
    fail "ollama is not installed. Install it from https://ollama.com/download/linux (curl -fsSL https://ollama.com/install.sh | sh), or set OLLAMA_BIN"
  else
    ok "ollama $("$ob" --version 2>&1 | tail -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) at $ob"
    br=$(bridge_ip); br=${br:-172.17.0.1}
    if curl -sf -m 3 "http://$br:11434/api/tags" >/dev/null; then
      ok "Ollama answers on $br:11434, where the pods reach it"
    elif curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null; then
      fail "Ollama runs on 127.0.0.1 only; the pods cannot reach it and scripts/demo.sh will stop. Either stop it (sudo systemctl stop ollama; demo.sh then starts its own on $br) or set Environment=\"OLLAMA_HOST=$br:11434\" in the ollama service and restart it"
    else
      ok "Ollama is not running; scripts/demo.sh starts it on $br:11434"
    fi
  fi

  say "Ports"
  local running="" p
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx k3s-demo && running=1
  for p in 8000 3000 9090 9093; do
    if ! port_busy "$p"; then ok "port $p free"
    elif [[ -n "$running" ]]; then ok "port $p in use (the demo is already running)"
    else warn "port $p is in use; demo.sh forwards a service there, stop what uses it"; fi
  done
  if port_busy 8080; then ok "port 8080 in use: the demo app goes to http://localhost:18080 instead"; else ok "port 8080 free"; fi

  say "Network"
  if curl -sf -m 8 -o /dev/null https://github.com; then ok "github.com reachable"
  else warn "github.com not reachable (needed to get the code)"; fi
  if curl -s -m 8 -o /dev/null -w '%{http_code}' https://registry-1.docker.io/v2/ 2>/dev/null | grep -q 401; then ok "Docker Hub reachable"
  else warn "Docker Hub not reachable (needed on the first run to pull images)"; fi

  say "Cluster"
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx k3s-demo; then
    ok "k3s-demo container exists ($(docker ps -a --filter name='^k3s-demo$' --format '{{.Status}}'))"
  else
    ok "no k3s-demo container yet; the first 'up' creates it"
  fi
}

check_summary() {
  say "Summary"
  if (( FAILS == 0 )); then log "    No problems ($WARNS warning(s)). Next: $0 up"
  else log "    $FAILS problem(s), $WARNS warning(s). Fix the FAIL lines and run '$0 check' again."; fi
}

# ------------------------------------------------------------------ code and .env
get_code() {
  if [[ -z "$BRANCH" ]]; then
    if [[ -d "$REPO_DIR/.git" ]]; then BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD); else BRANCH=master; fi
  fi
  say "Code ($REPO_URL, branch $BRANCH)"
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    [[ -e "$REPO_DIR" ]] && die "$REPO_DIR exists but is not a git repository; set REPO_DIR to another folder"
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR" 2>&1 | tee -a "$REPORT"
    [[ ${PIPESTATUS[0]} -eq 0 ]] || die "git clone failed"
  else
    local cur
    cur=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)
    if ! git -C "$REPO_DIR" fetch --quiet origin "$BRANCH" 2>/dev/null; then
      warn "git fetch failed; using the local copy"
    elif [[ -n "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ]]; then
      warn "$REPO_DIR has local changes; not updating it"
    elif [[ "$cur" != "$BRANCH" ]]; then
      warn "$REPO_DIR is on branch $cur, not $BRANCH; not switching"
    elif git -C "$REPO_DIR" merge --ff-only --quiet "origin/$BRANCH" 2>/dev/null; then
      ok "up to date with origin/$BRANCH"
    else
      warn "could not fast-forward to origin/$BRANCH; using the local copy"
    fi
  fi
  [[ -x "$REPO_DIR/scripts/demo.sh" ]] || die "$REPO_DIR/scripts/demo.sh not found: wrong repository or branch"
  ok "commit $(git -C "$REPO_DIR" log -1 --format='%h %s (%cs)')"
}

set_env() { # KEY VALUE in $REPO_DIR/.env
  local f="$REPO_DIR/.env"
  if grep -q "^$1=" "$f"; then
    python3 - "$f" "$1" "$2" <<'PY'
import sys
path, key, val = sys.argv[1:4]
lines = open(path).read().splitlines()
lines = [key + "=" + val if l.startswith(key + "=") else l for l in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$f"
  fi
}

write_env() {
  say ".env"
  if [[ -f "$REPO_DIR/.env" ]]; then ok ".env exists; keeping its values"
  else cp "$REPO_DIR/.env.example" "$REPO_DIR/.env" && ok "created .env from .env.example"; fi
  [[ -n "${OLLAMA_BIN:-}" ]] && set_env OLLAMA_BIN "$OLLAMA_BIN"
  if [[ -n "$MODEL_DIR" && "$MODEL_DIR" != "$HOME/.ollama/models" ]] && ! grep -q '^OLLAMA_MODELS=.' "$REPO_DIR/.env"; then
    set_env OLLAMA_MODELS "$MODEL_DIR"
    ok "OLLAMA_MODELS=$MODEL_DIR (reuses the model that is already downloaded)"
  fi
  # Keep Docker's copy of the project images: if k3s ever deletes its copy
  # under disk pressure, they can be imported again without a rebuild.
  if grep -q '^PRUNE_HOST_IMAGES=.' "$REPO_DIR/.env"; then
    set_env PRUNE_HOST_IMAGES ""
    ok "PRUNE_HOST_IMAGES cleared (Docker keeps a backup copy of the project images)"
  fi
  grep -E '^(CLUSTER|K3S_CONTAINER|OLLAMA_BIN|OLLAMA_MODELS|OLLAMA_BIND|PRUNE_HOST_IMAGES)=' "$REPO_DIR/.env" | sed 's/^/          /' | tee -a "$REPORT"
}

# ------------------------------------------------------------------ hardening
k3s_has() { docker exec "$(k3s_name)" ctr -n k8s.io images ls -q 2>/dev/null | grep -qx "$1"; }
full_ref() { # crictl/ctr name of an image
  case "$1" in
    */*/*) echo "$1" ;;              # registry.k8s.io/...
    */*)   echo "docker.io/$1" ;;    # grafana/grafana:...
    *)     echo "docker.io/library/$1" ;;
  esac
}

ensure_images() {
  is_k3s || { ok "not a k3s-in-Docker cluster; image pinning skipped"; return 0; }
  local k3s img ref
  k3s=$(k3s_name)
  say "Images inside k3s (pinned, so disk pressure cannot delete them)"
  for img in "${PROJECT_IMAGES[@]}"; do
    ref=$(full_ref "$img")
    if ! k3s_has "$ref"; then
      if ! docker image inspect "$img" >/dev/null 2>&1; then
        local dir; dir=${img%%:*}; [[ "$dir" == demo-app ]] && dir=app
        docker build -q -t "$img" "$REPO_DIR/$dir/" >/dev/null || { fail "could not build $img"; continue; }
      fi
      docker save "$img" | docker exec -i "$k3s" ctr -n k8s.io images import --local --discard-unpacked-layers - >/dev/null 2>&1 \
        || docker save "$img" | docker exec -i "$k3s" ctr -n k8s.io images import - >/dev/null 2>&1 \
        || { fail "could not import $img into k3s"; continue; }
    fi
    docker exec "$k3s" ctr -n k8s.io images label "$ref" io.cri-containerd.pinned=pinned >/dev/null 2>&1 \
      && ok "$img present and pinned" || fail "$img missing in k3s"
  done
  for img in "${MONITORING_IMAGES[@]}"; do   # one at a time: parallel pulls caused the disk spike
    ref=$(full_ref "$img")
    k3s_has "$ref" || docker exec "$k3s" crictl pull "$img" >/dev/null 2>&1 || { fail "could not pull $img"; continue; }
    docker exec "$k3s" ctr -n k8s.io images label "$ref" io.cri-containerd.pinned=pinned >/dev/null 2>&1 \
      && ok "$img present and pinned" || fail "$img missing in k3s"
  done
  ok "$(docker_free) MB free for Docker"
}

ensure_revision() {
  # The error-rate scenario ends with a rollback to the previous revision. A
  # fresh cluster has only revision 1, and the rollback would fail with "no
  # earlier revision". A rolling restart (same image and config) adds one.
  local n
  n=$(kc -n demo get rs -l app=demo-app -o jsonpath='{range .items[*]}{.metadata.annotations.deployment\.kubernetes\.io/revision}{"\n"}{end}' 2>/dev/null | grep -c .)
  if (( n >= 2 )); then ok "demo-app has $n revisions, so the rollback has one to return to"; return; fi
  say "Adding a second demo-app revision for the rollback scenario (rolling restart, no downtime)"
  kc -n demo rollout restart deployment/demo-app >/dev/null && kc -n demo rollout status deployment/demo-app --timeout=180s 2>&1 | tail -1 | tee -a "$REPORT"
  wait_forwards
}

clean_failed_pods() {
  local n
  n=$(kc get pods -A --no-headers 2>/dev/null | awk '$4=="Evicted" || $4=="Error" || $4=="Completed" || $4=="ContainerStatusUnknown" || $4=="ErrImagePull" || $4=="ImagePullBackOff"' | wc -l)
  (( n == 0 )) && return
  say "Deleting $n evicted, failed or stuck pods (their Deployments recreate them)"
  kc delete pods -A --field-selector=status.phase==Failed --wait=false >/dev/null 2>&1
  kc delete pods -A --field-selector=status.phase==Succeeded --wait=false >/dev/null 2>&1
  kc get pods -A --no-headers 2>/dev/null | awk '$4=="ErrImagePull" || $4=="ImagePullBackOff" {print $1, $2}' \
    | while read -r ns pod; do kc -n "$ns" delete pod "$pod" --wait=false >/dev/null 2>&1; done
}

wait_disk_pressure() {
  local i s
  for i in $(seq 80); do
    s=$(kc get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="DiskPressure")].status}' 2>/dev/null)
    [[ "$s" == "False" ]] && { ok "node has no disk pressure"; return 0; }
    (( i == 1 )) && say "Waiting for the node's disk pressure to clear (Kubernetes keeps it for 5 minutes)"
    wait_s 5
  done
  fail "node still reports disk pressure: free space on the Docker disk (df -h), then run '$0 repair' again"
  return 1
}

wait_forwards() {
  local port i
  port=$(app_port)
  for i in $(seq 20); do
    [[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://localhost:$port/healthz")" == 200 ]] && return 0
    wait_s 2
  done
  (cd "$REPO_DIR" && scripts/demo.sh forward >/dev/null 2>&1)
}

wait_rollouts() {
  local d
  for d in demo/demo-app ai/ai-service monitoring/prometheus monitoring/alertmanager monitoring/loki monitoring/grafana monitoring/kube-state-metrics; do
    kc -n "${d%%/*}" rollout status "deployment/${d#*/}" --timeout=240s >/dev/null 2>&1 \
      || warn "deployment ${d#*/} in ${d%%/*} is not rolled out yet"
  done
}

# ------------------------------------------------------------------ health
cmd_health() {
  need_code
  say "Health of the running demo (read-only)"
  local out notready dp ready port u code
  ready=$(kc get nodes --no-headers 2>/dev/null | awk '{print $2}' | head -1)
  if [[ "$ready" == Ready ]]; then ok "node Ready"; else fail "node not Ready (is the k3s container running? docker ps)"; fi
  dp=$(kc get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="DiskPressure")].status}' 2>/dev/null)
  if [[ "$dp" == False ]]; then ok "no disk pressure"; else fail "node reports disk pressure: run '$0 repair'"; fi
  out=$(kc get pods -A --no-headers 2>/dev/null | awk '$1=="demo" || $1=="ai" || $1=="monitoring"')
  if [[ -z "$out" ]]; then fail "no pods in demo, ai, monitoring"
  else
    notready=$(awk '{split($3, r, "/"); if ($4 != "Running" || r[1] != r[2]) print "          " $1 "/" $2 "  " $3 "  " $4}' <<<"$out")
    if [[ -z "$notready" ]]; then ok "all $(wc -l <<<"$out") pods in demo, ai and monitoring are Running and ready"
    else fail "pods not ready (run '$0 repair' if they are Evicted or ErrImagePull):"; log "$notready"; fi
  fi
  ensure_revision_check
  port=$(app_port)
  for u in "demo app|http://localhost:$port/healthz" "AI service|http://localhost:8000/health" "Grafana|http://localhost:3000/api/health" \
           "Prometheus|http://localhost:9090/-/ready" "Alertmanager|http://localhost:9093/-/ready"; do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${u#*|}")
    if [[ "$code" == 200 ]]; then ok "${u%%|*} ${u#*|} answers 200"
    else fail "${u%%|*} ${u#*|} answers $code: run 'scripts/demo.sh forward' in the repository"; fi
  done
  local alerts incidents
  alerts=$(curl -s -m 5 http://localhost:9090/api/v1/alerts | python3 -c 'import json,sys; a=json.load(sys.stdin)["data"]["alerts"]; print(", ".join(x["labels"]["alertname"]+" "+x["state"] for x in a) or "none")' 2>/dev/null)
  incidents=$(curl -s -m 5 http://localhost:8000/api/v1/incidents | python3 -c 'import json,sys; i=json.load(sys.stdin)["incidents"]; print(len(i))' 2>/dev/null)
  log "          alerts: ${alerts:-unknown}; incidents: ${incidents:-unknown}"
  if curl -s -m 5 http://localhost:8000/api/v1/diagnostics | grep -q '"ollama"'; then
    curl -s -m 5 http://localhost:8000/api/v1/diagnostics | python3 -c 'import json,sys; d=json.load(sys.stdin)["checks"]; [print("          %-7s %s" % (k, v["status"])) for k, v in d.items()]' 2>/dev/null | tee -a "$REPORT"
  fi
  local df
  df=$(docker_free)
  if (( ${df:-0} >= 1000 )); then ok "$df MB free for Docker"
  else warn "only $df MB free for Docker: close IntelliJ and other heavy programs, keep it above about 1000 MB"; fi
  log "          Demo app: http://localhost:$port   Grafana: http://localhost:3000 (admin / admin)"
}
ensure_revision_check() {
  local n
  n=$(kc -n demo get rs -l app=demo-app -o jsonpath='{range .items[*]}{.metadata.annotations.deployment\.kubernetes\.io/revision}{"\n"}{end}' 2>/dev/null | grep -c .)
  if (( n >= 2 )); then ok "demo-app has $n revisions (the rollback scenario will work)"
  else fail "demo-app has only $n revision: the rollback scenario would fail; run '$0 repair'"; fi
}

# ------------------------------------------------------------------ status
cmd_status() {
  need_code
  say "Status"
  in_repo scripts/demo.sh status
}

urls() {
  cat <<EOF | tee -a "$REPORT"

Open in the browser:
  Grafana        http://localhost:3000   (admin / admin): "AI Incidents" and "Demo App - Live"
  Prometheus     http://localhost:9090/alerts
  Alertmanager   http://localhost:9093
  AI incidents   http://localhost:8000/api/v1/incidents
  Demo app       http://localhost:$(app_port)
For plain kubectl in a new terminal:
  export KUBECONFIG="$REPO_DIR/.local/demo/kubeconfig"
EOF
}

# ------------------------------------------------------------------ commands
cmd_up() {
  check_machine
  (( FAILS == 0 )) || { check_summary; die "the machine check found $FAILS problem(s); fix them first"; }
  get_code
  write_env
  say "scripts/demo.sh up ${SKIP_BUILD:+--skip-build }(first run: 10-15 minutes, plus the model download if needed)"
  local t0=$SECONDS
  if ! in_repo scripts/demo.sh up ${SKIP_BUILD:+--skip-build}; then
    warn "scripts/demo.sh up did not finish (often disk pressure while images download); repairing"
    cmd_repair
    return
  fi
  ok "up finished in $((SECONDS - t0)) s"
  ensure_images
  ensure_revision
  cmd_health
  urls
  log ""
  log "Next: $0 rehearse"
}

cmd_repair() {
  need_code
  say "Repair"
  if is_k3s; then :; elif docker ps -a --format '{{.Names}}' | grep -qx "$(k3s_name)"; then
    docker start "$(k3s_name)" >/dev/null && ok "k3s container started"
    wait_s 15
  fi
  wait_disk_pressure || return 1
  ensure_images
  clean_failed_pods
  say "scripts/demo.sh up --skip-build (re-applies the manifests, port-forwards, reloads the model)"
  in_repo scripts/demo.sh up --skip-build || warn "scripts/demo.sh up --skip-build reported a problem; see above"
  wait_rollouts
  ensure_revision
  cmd_health
  urls
}

cmd_rehearse() {
  need_code
  ensure_revision
  say "Rehearsal: crash, errors and latency, each approved, then the security check"
  local s t0 t1 line results=()
  for s in crash errors latency; do
    t0=$SECONDS
    if ! in_repo scripts/demo.sh "$s"; then
      fail "$s: no analysed incident; see the output above"
      results+=("$s: no analysed incident")
      continue
    fi
    t1=$SECONDS
    if in_repo scripts/demo.sh approve latest; then
      line="$s: analysed incident after $((t1 - t0)) s; fix approved and verified $((SECONDS - t1)) s later"
      ok "$line"
    else
      line="$s: analysed after $((t1 - t0)) s, but approve failed"
      fail "$line"
    fi
    results+=("$line")
  done
  if in_repo scripts/demo.sh security; then ok "security check ran"; else fail "security check failed"; fi
  say "Rehearsal summary"
  for line in "${results[@]}"; do log "    $line"; done
  log "    These are the times to expect on this laptop during the demo."
  log "    10-15 minutes before presenting: $0 day"
}

cmd_day() {
  need_code
  say "Demo day start (no rebuild; reloads the model into memory for the next hour)"
  in_repo scripts/demo.sh up --skip-build || { warn "up --skip-build reported a problem; repairing"; cmd_repair; }
  ensure_images
  ensure_revision
  say "Clearing old incidents"
  local i n
  for i in $(seq 96); do   # an incident cleared while its alert still fires would come straight back
    n=$(curl -s -m 5 http://localhost:9090/api/v1/alerts | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]["alerts"]))' 2>/dev/null)
    [[ "$n" == 0 ]] && break
    (( i == 1 )) && log "          waiting for $n active alert(s) to end first (up to 8 minutes)"
    wait_s 5
  done
  printf '\n$ kubectl -n ai rollout restart deployment/ai-service\n' >> "$REPORT"
  kc -n ai rollout restart deployment/ai-service >/dev/null && kc -n ai rollout status deployment/ai-service --timeout=180s 2>&1 | tail -1 | tee -a "$REPORT"
  in_repo scripts/demo.sh forward >/dev/null
  for i in $(seq 15); do curl -sf -m 3 http://localhost:8000/health >/dev/null && break; wait_s 2; done
  wait_forwards
  cmd_health
  urls
  cat <<'EOF' | tee -a "$REPORT"

Before you start (docs/DEMO.md):
  [ ] health above shows no FAIL lines; alerts none, incidents 0
  [ ] browser tabs open: Grafana (AI Incidents, Demo App - Live), Prometheus alerts, Alertmanager, demo app
  [ ] laptop on power, IntelliJ and unneeded browser tabs closed, terminal font enlarged, two terminals in the repository
  [ ] docs/ARCHITECTURE.md open for the first slide
Demo order: crash -> approve latest, errors -> approve latest, latency -> approve latest, security.
Approve promptly: an incident can only be approved while its alert is active.
EOF
}

case "${1:-help}" in
  check)    : > "$REPORT"; header "$@"; check_machine; check_summary; (( FAILS == 0 )) ;;
  up)       : > "$REPORT"; header "$@"; cmd_up ;;
  rehearse) header "$@"; cmd_rehearse ;;
  day)      header "$@"; cmd_day ;;
  health)   header "$@"; cmd_health; (( FAILS == 0 )) ;;
  repair)   header "$@"; cmd_repair ;;
  status)   header "$@"; cmd_status ;;
  all)      : > "$REPORT"; header "$@"; cmd_up && cmd_rehearse ;;
  *)        sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
