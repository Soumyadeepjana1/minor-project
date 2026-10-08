#!/usr/bin/env python3
"""Local observability dashboard for the demo project.

Serves a single page on http://localhost:9983 that embeds the project's
existing Grafana dashboards (Cluster Health, Application SLO) in kiosk mode
and shows live reachability of the local stack components. It is backed by the
*local* (docker-compose) Prometheus / Alertmanager / Loki / Grafana
containers -- no Kubernetes is involved.

Only the Python standard library is required, so it can be started directly:

    python3 dashboard/server.py

Endpoints:
    /             the dashboard UI
    /healthz      liveness for the dashboard server itself
    /api/status   JSON reachability of each backend
"""

import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DASHBOARD_PORT", "9983"))
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100").rstrip("/")
APP_URL = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")

# (key, label, ready-url, dashboard-url)
BACKENDS = [
    ("grafana", "Grafana", f"{GRAFANA_URL}/api/health", f"{GRAFANA_URL}/"),
    ("prometheus", "Prometheus", f"{PROMETHEUS_URL}/-/ready", f"{PROMETHEUS_URL}/targets"),
    ("alertmanager", "Alertmanager", f"{ALERTMANAGER_URL}/-/ready", f"{ALERTMANAGER_URL}/"),
    ("loki", "Loki", f"{LOKI_URL}/ready", f"{LOKI_URL}/ready"),
    ("app", "Demo app", f"{APP_URL}/healthz", f"{APP_URL}/"),
]

DASHBOARDS = [
    ("cluster-health", "Cluster Health", "Stack health: instances, targets, alerts, resources."),
    ("application-slo", "Application SLO", "Request rate, error ratio, latency, SLO burn."),
]

# Where the browser should load Grafana panels from (kiosk = no chrome).
GRAFANA_BASE = GRAFANA_URL


def check_backend(url: str, timeout: float = 2.0) -> dict:
    """Return reachability info for one backend URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "local-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"up": 200 <= resp.status < 400, "status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"up": False, "status": exc.code}
    except Exception as exc:  # noqa: BLE001 - report any failure as "down"
        return {"up": False, "status": 0, "error": type(exc).__name__}


def collect_status() -> dict:
    """Check every backend concurrently so the endpoint stays snappy."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(BACKENDS)) as pool:
        results = pool.map(lambda b: check_backend(b[2]), BACKENDS)
        checks = {b[0]: res for b, res in zip(BACKENDS, results)}
    return {
        "backends": [
            {
                "key": key,
                "label": label,
                "dashboard_url": dashboard_url,
                **checks[key],
            }
            for key, label, _ready, dashboard_url in BACKENDS
        ]
    }


def render_page() -> str:
    tabs = "\n".join(
        f'<button class="tab{" active" if i == 0 else ""}" '
        f'data-dashboard="{uid}" data-title="{title}">{title}</button>'
        for i, (uid, title, _desc) in enumerate(DASHBOARDS)
    )
    frames = "\n".join(
        f'<iframe class="frame{" visible" if i == 0 else ""}" data-dashboard="{uid}" '
        f'title="{title}" loading="lazy" '
        f'src="{GRAFANA_BASE}/d/{uid}/{uid}?kiosk&refresh=10s&from=now-1h&to=now"></iframe>'
        for i, (uid, title, _desc) in enumerate(DASHBOARDS)
    )
    return TEMPLATE.replace("{{TABS}}", tabs).replace("{{FRAMES}}", frames)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo Project - Local Observability</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --border:#2a3038;
          --fg:#e6edf3; --muted:#8b949e; --accent:#1f9cf0; --ok:#3fb950; --bad:#f85149; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5
         -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
           padding: 14px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
           position: sticky; top: 0; z-index: 10; }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  h1 span { color: var(--muted); font-weight: 400; }
  .status { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
          border: 1px solid var(--border); border-radius: 999px; font-size: 12px;
          color: var(--muted); text-decoration: none; }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .pill.up { color: var(--fg); border-color: #2f4f37; }
  .pill.up .dot { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
  .pill.down .dot { background: var(--bad); }
  .tabs { display: flex; gap: 4px; padding: 12px 20px 0; }
  .tab { background: transparent; color: var(--muted); border: 1px solid transparent;
         border-bottom: none; padding: 8px 14px; border-radius: 8px 8px 0 0;
         cursor: pointer; font-size: 13px; }
  .tab:hover { color: var(--fg); }
  .tab.active { background: var(--panel); color: var(--fg); border-color: var(--border); }
  main { padding: 0 20px 20px; }
  .frame { display: none; width: 100%; height: calc(100vh - 150px); border: 1px solid
           var(--border); border-radius: 0 8px 8px 8px; background: var(--panel); }
  .frame.visible { display: block; }
  .hint { color: var(--muted); font-size: 12px; padding: 0 20px 14px; }
  .hint a { color: var(--accent); }
  .offline { margin: 24px 20px; padding: 16px; border: 1px solid var(--border);
             border-radius: 8px; background: var(--panel); color: var(--muted);
             display: none; }
</style>
</head>
<body>
<header>
  <h1>Demo Project <span>&middot; local observability dashboard</span></h1>
  <div class="status" id="status"></div>
</header>
<div class="tabs" id="tabs">{{TABS}}</div>
<main>
  <div class="offline" id="offline">
    Grafana is not reachable, so the dashboards cannot be embedded. Start the
    local stack first: <code>docker compose -f docker-compose.dev.yaml
    -f docker-compose.monitoring.yaml -f docker-compose.local.yaml up -d</code>
  </div>
  {{FRAMES}}
</main>
<p class="hint" id="hint"></p>
<script>
  const DASHBOARDS = __DASHBOARDS__;
  const tabs = document.querySelectorAll(".tab");
  const frames = document.querySelectorAll(".frame");
  const hint = document.getElementById("hint");

  function show(uid) {
    tabs.forEach(t => t.classList.toggle("active", t.dataset.dashboard === uid));
    frames.forEach(f => f.classList.toggle("visible", f.dataset.dashboard === uid));
    const d = DASHBOARDS.find(x => x.uid === uid);
    hint.innerHTML = d ? `${d.title}: ${d.desc} &nbsp;&middot;&nbsp; ` +
      `<a href="${d.url}" target="_blank" rel="noopener">open in Grafana</a>` : "";
  }
  tabs.forEach(t => t.addEventListener("click", () => show(t.dataset.dashboard)));

  async function refresh() {
    try {
      const res = await fetch("/api/status", {cache: "no-store"});
      const data = await res.json();
      const el = document.getElementById("status");
      el.innerHTML = "";
      let grafanaUp = false;
      data.backends.forEach(b => {
        if (b.key === "grafana") grafanaUp = b.up;
        const a = document.createElement("a");
        a.className = "pill " + (b.up ? "up" : "down");
        a.href = b.dashboard_url; a.target = "_blank"; a.rel = "noopener";
        a.title = b.up ? `${b.label} reachable (HTTP ${b.status})`
                       : `${b.label} unreachable` + (b.error ? `: ${b.error}` : "");
        a.innerHTML = `<span class="dot"></span>${b.label}`;
        el.appendChild(a);
      });
      document.getElementById("offline").style.display = grafanaUp ? "none" : "block";
    } catch (e) { /* keep the previous status on a transient failure */ }
  }
  show(DASHBOARDS[0].uid);
  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            dashboards = json.dumps(
                [
                    {"uid": uid, "title": title, "desc": desc,
                     "url": f"{GRAFANA_BASE}/d/{uid}/{uid}"}
                    for uid, title, desc in DASHBOARDS
                ]
            )
            page = render_page().replace("__DASHBOARDS__", dashboards)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b'{"status":"ok"}', "application/json")
        elif path == "/api/status":
            self._send(200, json.dumps(collect_status()).encode(), "application/json")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def log_message(self, fmt: str, *args) -> None:
        print("[dashboard] " + fmt % args)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Local dashboard on http://localhost:{PORT}")
    print(f"  Grafana: {GRAFANA_URL}  |  Prometheus: {PROMETHEUS_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
