#!/usr/bin/env bash
# End-to-end smoke test for the local stack (docker-compose) and dashboard.
# Verifies the demo app, Prometheus, Alertmanager, Loki, Grafana and the
# dashboard on :9983. No Kubernetes is involved.
#
# Usage:
#   scripts/smoke-test.sh [--wait SECONDS] [--no-dashboard]
#
# Exit status is non-zero if any check fails.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

WAIT=60
CHECK_DASHBOARD=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait)         WAIT="${2:-60}"; shift 2 ;;
    --no-dashboard) CHECK_DASHBOARD=false; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

APP_URL="${APP_URL:-http://localhost:5000}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://localhost:9093}"
LOKI_URL="${LOKI_URL:-http://localhost:3100}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:9983}"

FAILURES=0
PASSES=0

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASSES=$((PASSES + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
section() { printf '\n== %s ==\n' "$1"; }

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true; }

check_status() { # name expected-code url
  local name="$1" expected="$2" url="$3" code
  code="$(http_code "$url")"
  if [[ "$code" == "$expected" ]]; then
    pass "$name (HTTP $code)"
  else
    fail "$name (got ${code:-no response}, want $expected)"
  fi
}

wait_ready() { # name url seconds
  local name="$1" url="$2" seconds="${3:-60}" i=0
  while ! curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; do
    i=$((i + 1))
    if (( i >= seconds )); then
      fail "$name did not become ready within ${seconds}s"
      return 1
    fi
    sleep 1
  done
  pass "$name ready"
}

check_body() { # name url needle
  local name="$1" url="$2" needle="$3" body
  body="$(curl -s --max-time 10 "$url" 2>/dev/null || true)"
  if grep -q "$needle" <<<"$body"; then
    pass "$name contains '$needle'"
  else
    fail "$name does not contain '$needle'"
  fi
}

# --- demo app ----------------------------------------------------------------
section "Demo app"
wait_ready "demo app" "$APP_URL/healthz" "$WAIT"
check_status "GET /healthz" 200 "$APP_URL/healthz"
check_status "GET /readyz" 200 "$APP_URL/readyz"
check_status "GET /metrics" 200 "$APP_URL/metrics"
check_body "metrics" "$APP_URL/metrics" "http_requests_total"

# --- Prometheus --------------------------------------------------------------
section "Prometheus"
wait_ready "prometheus" "$PROMETHEUS_URL/-/ready" "$WAIT"
target_health="$(curl -s --max-time 10 "$PROMETHEUS_URL/api/v1/targets?state=active" 2>/dev/null | python3 -c '
import json, sys
try:
    targets = json.load(sys.stdin)["data"]["activeTargets"]
except Exception:
    print(""); raise SystemExit
print(next((t["health"] for t in targets if t["labels"].get("job") == "demo-app"), ""))
')"
if [[ "$target_health" == "up" ]]; then
  pass "prometheus target demo-app is up"
else
  fail "prometheus target demo-app health is '${target_health:-unknown}'"
fi
check_body "alert rules loaded" "$PROMETHEUS_URL/api/v1/rules" "DemoAppDown"

# --- Alertmanager ------------------------------------------------------------
section "Alertmanager"
wait_ready "alertmanager" "$ALERTMANAGER_URL/-/ready" "$WAIT"
check_status "GET /-/ready" 200 "$ALERTMANAGER_URL/-/ready"

# --- Loki --------------------------------------------------------------------
section "Loki"
wait_ready "loki" "$LOKI_URL/ready" "$WAIT"
check_status "GET /ready" 200 "$LOKI_URL/ready"

# --- Grafana -----------------------------------------------------------------
section "Grafana"
wait_ready "grafana" "$GRAFANA_URL/api/health" "$WAIT"
check_status "GET /api/health" 200 "$GRAFANA_URL/api/health"

dash_uids="$(curl -s --max-time 10 "$GRAFANA_URL/api/search?type=dash-db" 2>/dev/null | python3 -c '
import json, sys
try:
    print(",".join(sorted(d["uid"] for d in json.load(sys.stdin))))
except Exception:
    print("")
')"
if [[ "$dash_uids" == "application-slo,cluster-health,demo-app" ]]; then
  pass "dashboards provisioned ($dash_uids)"
else
  fail "provisioned dashboards are '${dash_uids:-none}'"
fi

check_status "kiosk dashboard" 200 "$GRAFANA_URL/d/cluster-health/cluster-health?kiosk"
if curl -sI --max-time 10 "$GRAFANA_URL/d/cluster-health/cluster-health?kiosk" 2>/dev/null \
    | tr -d '\r' | grep -qi '^x-frame-options:'; then
  fail "grafana sends X-Frame-Options (embedding would break)"
else
  pass "grafana is embeddable (no X-Frame-Options)"
fi

# --- Dashboard ---------------------------------------------------------------
if [[ "$CHECK_DASHBOARD" == true ]]; then
  section "Dashboard ($DASHBOARD_URL)"
  wait_ready "dashboard" "$DASHBOARD_URL/healthz" "$WAIT"
  check_status "GET /" 200 "$DASHBOARD_URL/"
  check_status "GET /readyz" 200 "$DASHBOARD_URL/readyz"
  check_body "GET /api/config" "$DASHBOARD_URL/api/config" "cluster-health"
  check_body "GET /api/config" "$DASHBOARD_URL/api/config" "kiosk"
  check_body "GET /metrics" "$DASHBOARD_URL/metrics" "dashboard_backend_up"

  backend_state="$(curl -s --max-time 15 "$DASHBOARD_URL/api/status" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("0/0"); raise SystemExit
print("%s/%s" % (d.get("up", 0), d.get("total", 0)))
')"
  if [[ "$backend_state" == "5/5" ]]; then
    pass "all backends reported up ($backend_state)"
  else
    fail "dashboard reports ${backend_state:-unknown} backends up (expected 5/5)"
  fi
fi

# --- summary -----------------------------------------------------------------
printf '\n== Summary ==\n'
printf '  %d passed, %d failed\n' "$PASSES" "$FAILURES"
if (( FAILURES > 0 )); then
  echo "  SMOKE TEST FAILED"
  exit 1
fi
echo "  SMOKE TEST PASSED"
