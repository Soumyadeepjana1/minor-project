"""Demo Flask application for the Developer 1 platform/infrastructure project.

Serves a small JSON API with liveness/readiness endpoints suitable for
Kubernetes probes, plus Prometheus metrics on /metrics. Configuration is
injected via environment variables so the same image can run in dev
(docker-compose) and in the cluster (ConfigMap).
"""

import os
import socket
import time

from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

START_TIME = time.time()

APP_NAME = os.environ.get("APP_NAME", "demo-app")
APP_VERSION = os.environ.get("APP_VERSION", "0.2.0")
APP_ENV = os.environ.get("APP_ENV", "development")
PORT = int(os.environ.get("PORT", "5000"))

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)


def create_app():
    """Application factory: keeps the app importable and testable."""
    app = Flask(__name__)

    @app.before_request
    def start_timer():
        request.environ["_start_time"] = time.time()

    @app.after_request
    def record_metrics(response):
        start = request.environ.get("_start_time", time.time())
        elapsed = time.time() - start
        LATENCY.labels(method=request.method, path=request.path).observe(elapsed)
        REQUESTS.labels(
            method=request.method,
            path=request.path,
            status=response.status_code,
        ).inc()
        return response

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": APP_NAME,
                "version": APP_VERSION,
                "environment": APP_ENV,
                "instance": socket.gethostname(),
                "uptime_seconds": round(time.time() - START_TIME, 3),
                "message": "Hello from the demo app!",
            }
        )

    @app.get("/healthz")
    def healthz():
        # Liveness probe: the process is up and serving requests.
        return jsonify({"status": "ok"}), 200

    @app.get("/readyz")
    def readyz():
        # Readiness probe: the app is ready to receive traffic.
        # Add dependency checks here (DB, cache, ...) as the app grows.
        return jsonify({"status": "ready"}), 200

    @app.get("/version")
    def version():
        return jsonify(
            {
                "name": APP_NAME,
                "version": APP_VERSION,
                "environment": APP_ENV,
            }
        )

    @app.get("/metrics")
    def metrics():
        # Prometheus scrape endpoint: request counters and latency histograms.
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
