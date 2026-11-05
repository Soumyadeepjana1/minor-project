#!/usr/bin/env bash
# Keeps the demo up all day on this laptop.
#
#   ./keep-running.sh start    start the watcher in the background
#   ./keep-running.sh stop     stop it
#   ./keep-running.sh status   is it running, last log lines
#
# Every 30 s it checks, and fixes only what is down:
#   - the k3s container (docker start)
#   - Ollama on the Docker bridge address (starts it like scripts/demo.sh does)
#   - the port-forwards of scripts/demo.sh (scripts/demo.sh forward) and a Loki
#     forward on 3100 that the local dashboard needs
#   - the local dashboard container on http://localhost:9983
# Every 2 minutes it keeps the model in memory, so no analysis waits for loading.
# It never restarts the AI service or redeploys anything, so it cannot clear
# incidents in the middle of the demo. Log: <repo>/.local/demo/keepalive.log
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${REPO_DIR:-}" ]]; then
  if [[ -x "$PWD/scripts/demo.sh" ]]; then REPO_DIR="$PWD"
  elif [[ -x "$HERE/../scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/.." && pwd)"
  elif [[ -x "$HERE/../minor-project/scripts/demo.sh" ]]; then REPO_DIR="$(cd "$HERE/../minor-project" && pwd)"
  else REPO_DIR="$HOME/minor-project"; fi
fi
STATE="$REPO_DIR/.local/demo"
LOG="$STATE/keepalive.log"
PIDFILE="$STATE/keepalive.pid"
K3S=k3s-demo
DASH=demo-dashboard-k3s
MODEL=qwen2.5:7b

log()  { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
code() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$1"; }
app_port() { local p; p=$(sed -n 's/^APP_PORT=//p' "$STATE/env" 2>/dev/null | tr -d "'\""); echo "${p:-8080}"; }

watch_loop() {
  cd "$REPO_DIR" || exit 1
  set -a; [[ -f .env ]] && source .env; set +a
  export KUBECONFIG="$STATE/kubeconfig"
  local n=0 br port u bad loki_pid=""
  log "watcher started (pid $$)"
  while true; do
    # 1. the cluster
    if ! docker ps --format '{{.Names}}' | grep -qx "$K3S"; then
      log "k3s container not running: starting it"
      docker start "$K3S" >/dev/null 2>&1 && sleep 40
    fi
    # 2. Ollama, where the pods reach it
    br=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null); br=${br:-172.17.0.1}
    if ! curl -sf -m 5 "http://$br:11434/api/tags" >/dev/null; then
      log "Ollama not answering on $br:11434: starting it"
      OLLAMA_HOST="$br:11434" setsid "${OLLAMA_BIN:-ollama}" serve >> "$STATE/ollama.log" 2>&1 < /dev/null &
      echo $! > "$STATE/ollama.pid"
      sleep 10
    fi
    # 3. keep the model loaded (every 2 minutes; loading only, no generation)
    if (( n % 4 == 0 )); then
      curl -s -m 180 "http://$br:11434/api/generate" -d "{\"model\": \"$MODEL\", \"keep_alive\": \"30m\"}" >/dev/null \
        || log "could not load $MODEL"
    fi
    # 4. port-forwards of scripts/demo.sh (they reconnect by themselves; restart them if not)
    port=$(app_port); bad=""
    for u in "http://localhost:$port/healthz" http://localhost:8000/health http://localhost:3000/api/health \
             http://localhost:9090/-/ready http://localhost:9093/-/ready; do
      [[ "$(code "$u")" == 200 ]] || bad="$bad $u"
    done
    if [[ -n "$bad" ]]; then
      sleep 10; bad=""
      for u in "http://localhost:$port/healthz" http://localhost:8000/health http://localhost:3000/api/health \
               http://localhost:9090/-/ready http://localhost:9093/-/ready; do
        [[ "$(code "$u")" == 200 ]] || bad="$bad $u"
      done
      if [[ -n "$bad" ]]; then
        log "not answering:$bad -> scripts/demo.sh forward"
        scripts/demo.sh forward >/dev/null 2>&1
      fi
    fi
    # 5. Loki on 3100 for the local dashboard
    if [[ "$(code http://localhost:3100/ready)" != 200 ]]; then
      [[ -n "$loki_pid" ]] && kill "$loki_pid" 2>/dev/null
      setsid kubectl port-forward -n monitoring svc/loki 3100:3100 --address 127.0.0.1 >> "$STATE/pf-loki.log" 2>&1 < /dev/null &
      loki_pid=$!
      (( n > 0 )) && log "Loki forward (re)started"
    fi
    # 6. the local dashboard
    if docker ps -a --format '{{.Names}}' | grep -qx "$DASH" && ! docker ps --format '{{.Names}}' | grep -qx "$DASH"; then
      log "dashboard container stopped: starting it"
      docker start "$DASH" >/dev/null 2>&1
    fi
    # 7. disk: warn only (nothing is deleted automatically)
    local free; free=$(df -Pm / | awk 'NR==2 {print $4}')
    (( free < 700 )) && log "WARNING: only $free MB free on /; Kubernetes evicts pods below about 300 MB"
    n=$((n + 1))
    sleep 30
  done
}

case "${1:-status}" in
  start)
    mkdir -p "$STATE"
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE")); log: $LOG"; exit 0
    fi
    setsid bash -c "$(declare -f log code app_port watch_loop); REPO_DIR=$(printf '%q' "$REPO_DIR") STATE=$(printf '%q' "$STATE") LOG=$(printf '%q' "$LOG") K3S=$K3S DASH=$DASH MODEL=$MODEL watch_loop" \
      < /dev/null > /dev/null 2>&1 &
    echo $! > "$PIDFILE"
    echo "started (pid $!); log: $LOG"
    ;;
  stop)
    if [[ -f "$PIDFILE" ]] && kill -- "-$(cat "$PIDFILE")" 2>/dev/null; then echo "stopped"; else echo "not running"; fi
    rm -f "$PIDFILE"
    ;;
  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running"; fi
    tail -5 "$LOG" 2>/dev/null
    ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
