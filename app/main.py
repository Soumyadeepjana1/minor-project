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
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from werkzeug.exceptions import HTTPException

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
# Project-level gauges: what this build is, how long it has been up, how busy
# it is right now, and how large its responses are. These feed the live panels
# in Grafana and the local dashboard.
APP_INFO = Gauge(
    "app_info",
    "Static information about the running build (always 1)",
    ["name", "version", "environment"],
)
UPTIME = Gauge(
    "app_uptime_seconds",
    "Seconds since this process started",
)
IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being handled",
    ["method"],
)
RESPONSE_SIZE = Histogram(
    "http_response_size_bytes",
    "HTTP response size in bytes",
    ["method", "path"],
    buckets=(100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000),
)


def create_app():
    """Application factory: keeps the app importable and testable."""
    app = Flask(__name__)

    # Publish the build identity once at startup: this is real, addressable
    # project data (the version/environment actually running), not a constant.
    APP_INFO.labels(name=APP_NAME, version=APP_VERSION, environment=APP_ENV).set(1)

    @app.before_request
    def start_timer():
        request.environ["_start_time"] = time.time()
        IN_PROGRESS.labels(method=request.method).inc()
        request.environ["_inflight"] = True

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
        size = response.calculate_content_length() or 0
        RESPONSE_SIZE.labels(method=request.method, path=request.path).observe(size)
        # Keep the in-flight counter balanced with before_request.
        if request.environ.pop("_inflight", False):
            IN_PROGRESS.labels(method=request.method).dec()
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        # Unmatched routes never reach before/after_request, so their status
        # would otherwise be invisible to metrics. Record them here so the
        # error-ratio panels reflect real failed requests. (Matched routes that
        # abort still have the in-flight marker set and are counted once by
        # after_request.)
        if not request.environ.get("_inflight"):
            REQUESTS.labels(
                method=request.method,
                path=request.path,
                status=error.code,
            ).inc()
        return jsonify({"error": error.name, "status": error.code}), error.code

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
        # Prometheus scrape endpoint: request counters, latency histograms and
        # the live project gauges (uptime, in-flight requests, build info).
        UPTIME.set(time.time() - START_TIME)
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
