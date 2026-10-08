"""Local dashboard for the AI-Powered Kubernetes Health Monitoring System.

A production-friendly companion to the demo app: it serves one page on
http://localhost:9983 that embeds the project's existing Grafana dashboards
(Cluster Health, Application SLO) and reports live reachability of the local
stack (Grafana, Prometheus, Alertmanager, Loki, demo app).

It is backed by the *local* docker-compose stack -- no Kubernetes involved.
Configuration is injected via environment variables so the same image runs in
docker-compose and standalone. Run it with gunicorn:

    gunicorn --bind 0.0.0.0:9983 app:app

Endpoints:
    /             dashboard UI (templates/index.html)
    /healthz      liveness  (process is up)
    /readyz       readiness (server can serve requests)
    /api/config   UI configuration (dashboards, refresh interval)
    /api/status   JSON reachability of every backend (short TTL cache)
    /metrics      Prometheus metrics for the dashboard itself
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

VERSION = "0.2.0"

LOG = logging.getLogger("dashboard")

# Live, project-specific numbers pulled from Prometheus for the header cards.
OVERVIEW_QUERIES = [
    {
        "key": "request_rate",
        "label": "Request rate",
        "query": 'sum(rate(http_requests_total{job="demo-app"}[5m]))',
        "unit": "req/s",
        "precision": 3,
    },
    {
        "key": "error_ratio",
        "label": "Error ratio (non-2xx)",
        "query": (
            'sum(rate(http_requests_total{job="demo-app",status!~"2.."}[5m]))'
            ' / clamp_min(sum(rate(http_requests_total{job="demo-app"}[5m])), 0.0001)'
        ),
        "unit": "%",
        "precision": 2,
        "scale": 100,
    },
    {
        "key": "latency_p95",
        "label": "Latency p95",
        "query": (
            'histogram_quantile(0.95, sum('
            'rate(http_request_duration_seconds_bucket{job="demo-app"}[5m])) by (le))'
        ),
        "unit": "s",
        "precision": 4,
    },
    {
        "key": "in_progress",
        "label": "Requests in progress",
        "query": 'sum(http_requests_in_progress{job="demo-app"})',
        "unit": "",
        "precision": 0,
    },
    {
        "key": "uptime",
        "label": "Uptime",
        "query": 'max(app_uptime_seconds{job="demo-app"})',
        "unit": "s",
        "precision": 0,
    },
    {
        "key": "instances",
        "label": "Instances up",
        "query": 'sum(up{job="demo-app"})',
        "unit": "",
        "precision": 0,
    },
    {
        "key": "total_requests",
        "label": "Total requests",
        "query": 'sum(http_requests_total{job="demo-app"})',
        "unit": "",
        "precision": 0,
    },
]

DEFAULT_DASHBOARDS = [
    {
        "uid": "demo-app",
        "title": "Demo App - Live",
        "description": "Real project data: throughput, errors, latency, in-flight, uptime, build.",
    },
    {
        "uid": "cluster-health",
        "title": "Cluster Health",
        "description": "Stack health: instances, scrape targets, firing alerts, resources.",
    },
    {
        "uid": "application-slo",
        "title": "Application SLO",
        "description": "Request rate, error ratio, latency and overall SLO burn.",
    },
]

# Prometheus metrics for the dashboard itself.
STATUS_REQUESTS = Counter(
    "dashboard_status_requests_total",
    "Number of /api/status requests served",
)
BACKEND_UP = Gauge(
    "dashboard_backend_up",
    "Whether a backend responded successfully (1 = up, 0 = down)",
    ["backend"],
)
PROBE_DURATION = Counter(
    "dashboard_probe_seconds_total",
    "Time spent probing backends",
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


class Backend:
    """A local stack component and how to check it."""

    def __init__(self, key: str, label: str, ready_url: str, public_url: str):
        self.key = key
        self.label = label
        self.ready_url = ready_url
        self.public_url = public_url

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "dashboard_url": self.public_url}


def build_backends(cfg: dict) -> list:
    """Build the backend list from the resolved configuration."""
    return [
        Backend("grafana", "Grafana", f"{cfg['grafana']}/api/health", f"{cfg['grafana_public']}/"),
        Backend(
            "prometheus",
            "Prometheus",
            f"{cfg['prometheus']}/-/ready",
            f"{cfg['prometheus_public']}/targets",
        ),
        Backend(
            "alertmanager",
            "Alertmanager",
            f"{cfg['alertmanager']}/-/ready",
            f"{cfg['alertmanager_public']}/",
        ),
        Backend("loki", "Loki", f"{cfg['loki']}/ready", f"{cfg['loki_public']}/ready"),
        Backend("app", "Demo app", f"{cfg['app']}/healthz", f"{cfg['app_public']}/"),
    ]


def probe_backend(url: str, timeout: float) -> dict:
    """Return reachability info for a single URL. Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": "dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"up": 200 <= resp.status < 400, "status": resp.status}
    except urllib.error.HTTPError as exc:
        # A structured HTTP answer still proves the service is reachable.
        return {"up": False, "status": exc.code, "error": "HTTPError"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"up": False, "status": 0, "error": type(exc).__name__}


def prom_query(base_url: str, expr: str, timeout: float):
    """Run a Prometheus instant query and return its scalar value, or None.

    Used to surface real project data (throughput, errors, latency, uptime)
    on the dashboard. Never raises: a missing Prometheus just yields None so
    the UI can degrade gracefully.
    """
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, headers={"User-Agent": "dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        result = payload["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def prom_labels(base_url: str, expr: str, timeout: float) -> dict:
    """Return the label set of the first match for an instant query."""
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, headers={"User-Agent": "dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
        return dict(payload["data"]["result"][0]["metric"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        return {}


class MetricsService:
    """Live project metrics for the dashboard header cards.

    Same short-TTL cache strategy as StatusService: the page polls every few
    seconds, Prometheus is queried at most once per TTL window.
    """

    def __init__(self, base_url: str, timeout: float, ttl: float):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def _collect(self) -> dict:
        metrics = []
        for spec in OVERVIEW_QUERIES:
            value = prom_query(self._base, spec["query"], self._timeout)
            if value is not None and spec.get("scale"):
                value *= spec["scale"]
            metrics.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "unit": spec["unit"],
                    "precision": spec["precision"],
                    "value": value,
                    "ok": value is not None,
                }
            )
        build = prom_labels(self._base, 'app_info{job="demo-app"}', self._timeout)
        return {
            "metrics": metrics,
            "build": {
                "version": build.get("version", ""),
                "environment": build.get("environment", ""),
                "name": build.get("name", ""),
            },
            "source": self._base,
            "generated_at": time.time(),
        }

    def overview(self, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            fresh = self._cached is not None and (now - self._cached_at) < self._ttl
            if force or not fresh:
                self._cached = self._collect()
                self._cached_at = now
            result = dict(self._cached)
        result["cached"] = not force and fresh
        return result


def github_api_get(url: str, token: str, timeout: float):
    """GET a GitHub API URL and return parsed JSON, or None on any failure."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "dashboard/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise GitHubError(exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubError(type(exc).__name__) from exc


class GitHubError(Exception):
    """Raised when the GitHub API cannot be reached or answers with an error."""


class PipelinesService:
    """Live CI/CD status from the GitHub Actions API.

    Reports the latest run per workflow so the dashboard can show, in near
    real time, whether each pipeline is passing. Results are cached for a
    generous TTL because the unauthenticated GitHub API allows only a small
    number of requests per hour.
    """

    def __init__(self, repository: str, api_url: str, token: str, timeout: float, ttl: float):
        self._repo = repository.strip().strip("/")
        self._api = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def _collect(self) -> dict:
        if not self._repo:
            return {
                "configured": False,
                "available": False,
                "repository": "",
                "workflows": [],
                "reason": "GITHUB_REPOSITORY is not set",
                "generated_at": time.time(),
            }
        try:
            workflows = github_api_get(
                f"{self._api}/repos/{self._repo}/actions/workflows", self._token, self._timeout
            )
            runs = github_api_get(
                f"{self._api}/repos/{self._repo}/actions/runs?per_page=50",
                self._token,
                self._timeout,
            )
        except GitHubError as exc:
            return {
                "configured": True,
                "available": False,
                "repository": self._repo,
                "workflows": [],
                "reason": f"GitHub API error: {exc}",
                "generated_at": time.time(),
            }

        # Latest run per workflow file.
        latest = {}
        for run in runs.get("workflow_runs", []):
            path = run.get("path", "")
            if path and path not in latest:
                latest[path] = run

        items = []
        for workflow in workflows.get("workflows", []):
            path = workflow.get("path", "")
            run = latest.get(path)
            items.append(
                {
                    "name": workflow.get("name", path),
                    "path": path,
                    "state": workflow.get("state", "unknown"),
                    "status": (run or {}).get("status", "none"),
                    "conclusion": (run or {}).get("conclusion"),
                    "branch": (run or {}).get("head_branch", ""),
                    "sha": ((run or {}).get("head_sha") or "")[:7],
                    "event": (run or {}).get("event", ""),
                    "run_number": (run or {}).get("run_number"),
                    "url": (run or {}).get("html_url", ""),
                    "updated_at": (run or {}).get("updated_at", ""),
                }
            )
        # Workflows that only exist as runs (e.g. dynamic ones) still matter.
        known = {w.get("path") for w in workflows.get("workflows", [])}
        for path, run in latest.items():
            if path not in known:
                items.append(
                    {
                        "name": run.get("name", path),
                        "path": path,
                        "state": "active",
                        "status": run.get("status", "none"),
                        "conclusion": run.get("conclusion"),
                        "branch": run.get("head_branch", ""),
                        "sha": (run.get("head_sha") or "")[:7],
                        "event": run.get("event", ""),
                        "run_number": run.get("run_number"),
                        "url": run.get("html_url", ""),
                        "updated_at": run.get("updated_at", ""),
                    }
                )

        return {
            "configured": True,
            "available": True,
            "repository": self._repo,
            "repository_url": f"https://github.com/{self._repo}/actions",
            "workflows": items,
            "generated_at": time.time(),
        }

    def status(self, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            fresh = self._cached is not None and (now - self._cached_at) < self._ttl
            if force or not fresh:
                self._cached = self._collect()
                self._cached_at = now
            result = dict(self._cached)
        result["cached"] = not force and fresh
        return result


class StatusService:
    """Probes every backend concurrently with a short result cache.

    The cache keeps a busy page (polled every few seconds) from stampeding the
    stack, while still reflecting outages within the TTL window.
    """

    def __init__(self, backends: list, timeout: float, ttl: float):
        self._backends = backends
        self._timeout = timeout
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def _probe_all(self) -> list:
        started = time.time()
        with ThreadPoolExecutor(max_workers=len(self._backends)) as pool:
            results = list(pool.map(lambda b: probe_backend(b.ready_url, self._timeout), self._backends))
        PROBE_DURATION.inc(time.time() - started)
        merged = []
        for backend, result in zip(self._backends, results):
            BACKEND_UP.labels(backend=backend.key).set(1 if result["up"] else 0)
            merged.append({**backend.as_dict(), **result})
        return merged

    def status(self, force: bool = False) -> dict:
        """Return the current status, using the cache unless it has expired."""
        now = time.time()
        with self._lock:
            fresh = self._cached is not None and (now - self._cached_at) < self._ttl
            if force or not fresh:
                backends = self._probe_all()
                self._cached = {
                    "backends": backends,
                    "up": sum(1 for b in backends if b["up"]),
                    "total": len(backends),
                    "generated_at": time.time(),
                }
                self._cached_at = now
            result = dict(self._cached)
        result["cached"] = not force and fresh
        return result


def load_dashboards() -> list:
    """Dashboards to embed, optionally overridden via DASHBOARDS (JSON)."""
    raw = _env("DASHBOARDS", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return parsed
            LOG.warning("DASHBOARDS must be a non-empty JSON list; using defaults")
        except json.JSONDecodeError:
            LOG.warning("DASHBOARDS is not valid JSON; using defaults")
    return DEFAULT_DASHBOARDS


def build_config() -> dict:
    """Resolve all configuration from the environment once at startup."""
    grafana = _env("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    prometheus = _env("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
    alertmanager = _env("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
    loki = _env("LOKI_URL", "http://localhost:3100").rstrip("/")
    demo_app = _env("APP_URL", "http://localhost:5000").rstrip("/")
    return {
        "version": VERSION,
        # Server-side (container network) addresses used for health probes.
        "grafana": grafana,
        "prometheus": prometheus,
        "alertmanager": alertmanager,
        "loki": loki,
        "app": demo_app,
        # Browser-facing addresses, used to build iframe and link URLs.
        "grafana_public": _env("GRAFANA_PUBLIC_URL", grafana).rstrip("/"),
        "prometheus_public": _env("PROMETHEUS_PUBLIC_URL", prometheus).rstrip("/"),
        "alertmanager_public": _env("ALERTMANAGER_PUBLIC_URL", alertmanager).rstrip("/"),
        "loki_public": _env("LOKI_PUBLIC_URL", loki).rstrip("/"),
        "app_public": _env("APP_PUBLIC_URL", demo_app).rstrip("/"),
        "timeout": _env_float("STATUS_TIMEOUT", 2.0),
        "ttl": _env_float("STATUS_TTL", 3.0),
        "refresh_seconds": _env_int("REFRESH_SECONDS", 10),
        # CI/CD status (GitHub Actions). The API is rate limited, so cache it
        # for much longer than the backend probes.
        "github_repository": _env("GITHUB_REPOSITORY", ""),
        "github_token": _env("GITHUB_TOKEN", ""),
        "github_api_url": _env("GITHUB_API_URL", "https://api.github.com"),
        "pipelines_ttl": _env_float("PIPELINES_TTL", 60.0),
    }


def create_app(config: dict | None = None) -> Flask:
    """Application factory: keeps the dashboard importable and testable."""
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = config or build_config()
    app = Flask(__name__)
    app.config.update(cfg)

    backends = build_backends(cfg)
    dashboards = load_dashboards()
    status_service = StatusService(backends, timeout=cfg["timeout"], ttl=cfg["ttl"])
    metrics_service = MetricsService(cfg["prometheus"], timeout=cfg["timeout"], ttl=cfg["ttl"])
    pipelines_service = PipelinesService(
        repository=cfg["github_repository"],
        api_url=cfg["github_api_url"],
        token=cfg["github_token"],
        timeout=max(cfg["timeout"], 5.0),
        ttl=cfg["pipelines_ttl"],
    )

    app.extensions["status_service"] = status_service
    app.extensions["metrics_service"] = metrics_service
    app.extensions["pipelines_service"] = pipelines_service
    app.extensions["dashboards"] = dashboards

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"frame-src 'self' {cfg['grafana_public']}; "
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "connect-src 'self'",
        )
        return response

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            title="AI-Powered Kubernetes Health Monitoring System",
            version=VERSION,
        )

    @app.get("/healthz")
    def healthz():
        # Liveness: the dashboard process is up and serving.
        return jsonify({"status": "ok", "version": VERSION})

    @app.get("/readyz")
    def readyz():
        # Readiness: the dashboard itself can serve. Backend outages are
        # reported through /api/status and the UI, not by failing readiness.
        return jsonify({"status": "ready", "backends": len(backends)})

    @app.get("/api/config")
    def api_config():
        base = cfg["grafana_public"]
        return jsonify(
            {
                "version": VERSION,
                "refresh_seconds": cfg["refresh_seconds"],
                "dashboards": [
                    {
                        "uid": d["uid"],
                        "title": d.get("title", d["uid"]),
                        "description": d.get("description", ""),
                        "embed_url": (
                            f"{base}/d/{d['uid']}/{d['uid']}"
                            f"?kiosk&refresh={cfg['refresh_seconds']}s&from=now-1h&to=now"
                        ),
                        "open_url": f"{base}/d/{d['uid']}/{d['uid']}",
                    }
                    for d in dashboards
                ],
            }
        )

    @app.get("/api/status")
    def api_status():
        STATUS_REQUESTS.inc()
        force = request.args.get("force") in {"1", "true", "yes"}
        payload = status_service.status(force=force)
        payload["version"] = VERSION
        return jsonify(payload)

    @app.get("/api/metrics")
    def api_metrics():
        # Real project data, read from Prometheus instant queries.
        force = request.args.get("force") in {"1", "true", "yes"}
        return jsonify(metrics_service.overview(force=force))

    @app.get("/api/pipelines")
    def api_pipelines():
        # Live CI/CD status from GitHub Actions (cached; rate limited API).
        force = request.args.get("force") in {"1", "true", "yes"}
        return jsonify(pipelines_service.status(force=force))

    @app.get("/metrics")
    def metrics():
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

    app.extensions["cfg"] = cfg
    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover - dev convenience
    app.run(host="0.0.0.0", port=_env_int("DASHBOARD_PORT", 9983), debug=False)
