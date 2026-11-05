"""Demo Flask application for the Developer 1 platform/infrastructure project.

Serves a small JSON API with liveness/readiness endpoints suitable for
Kubernetes probes, plus Prometheus metrics on /metrics. Configuration is
injected via environment variables so the same image can run in dev
(docker-compose) and in the cluster (ConfigMap).

With FAULT_INJECTION=true the app also misbehaves on demand, so the alerts and
the AI incident service have real failures to work on (see
scripts/simulate-failure.sh):

  GET  /api/products   about 30% of requests fail with a 500 (database error)
  POST /api/checkout   0.1-2.0s of payment latency, about 20% fail with a 500
  GET  /crash          the process exits, the container restarts
"""

import logging
import os
import random
import signal
import socket
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request
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
FAULT_INJECTION = os.environ.get("FAULT_INJECTION", "false").lower() == "true"

PRODUCTS = [
    {"id": 1, "name": "Laptop", "price": 999.99},
    {"id": 2, "name": "Mouse", "price": 29.99},
    {"id": 3, "name": "Keyboard", "price": 79.99},
]

# Logs go to stdout, where promtail picks them up for Loki. The AI service
# reads these lines when it analyses an incident.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(APP_NAME)

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

PRODUCTS = [
    {
        "id": "arc-headphones",
        "name": "Arc Wireless Headphones",
        "category": "Tech",
        "price": 7499,
        "compare_at": 9999,
        "rating": "4.9",
        "reviews": 284,
        "badge": "BESTSELLER",
        "description": "Immersive sound, all-day comfort, and 40 hours of battery life.",
        "image": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=900&q=85",
        "tone": "#e9e3d9",
    },
    {
        "id": "everyday-tote",
        "name": "Everyday Canvas Tote",
        "category": "Style",
        "price": 1899,
        "compare_at": 2499,
        "rating": "4.8",
        "reviews": 126,
        "badge": "ECO PICK",
        "description": "A roomy, durable carryall made from 100% recycled cotton.",
        "image": "https://images.unsplash.com/photo-1590874103328-eac38a683ce7?auto=format&fit=crop&w=900&q=85",
        "tone": "#e8dfd1",
    },
    {
        "id": "ceramic-set",
        "name": "Sunday Ceramic Set",
        "category": "Home",
        "price": 3299,
        "compare_at": 3999,
        "rating": "4.9",
        "reviews": 98,
        "badge": "HANDMADE",
        "description": "Two hand-finished stoneware mugs for slower mornings.",
        "image": "https://images.unsplash.com/photo-1514228742587-6b1558fcca3d?auto=format&fit=crop&w=900&q=85",
        "tone": "#e6e2d8",
    },
    {
        "id": "studio-lamp",
        "name": "Studio Table Lamp",
        "category": "Home",
        "price": 5299,
        "compare_at": 6499,
        "rating": "4.7",
        "reviews": 73,
        "badge": "DESIGNER PICK",
        "description": "Soft, warm light and a sculptural silhouette for your space.",
        "image": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=900&q=85",
        "tone": "#e8dfd3",
    },
    {
        "id": "daily-bottle",
        "name": "Daily Glass Bottle",
        "category": "Living",
        "price": 1299,
        "compare_at": 1599,
        "rating": "4.8",
        "reviews": 211,
        "badge": "LOW WASTE",
        "description": "Your new everyday essential, wrapped in protective silicone.",
        "image": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=900&q=85",
        "tone": "#dfe8e4",
    },
    {
        "id": "weekend-sneakers",
        "name": "Weekend Knit Sneakers",
        "category": "Style",
        "price": 4599,
        "compare_at": 5899,
        "rating": "4.8",
        "reviews": 167,
        "badge": "JUST LANDED",
        "description": "Lightweight everyday sneakers made for wherever you wander.",
        "image": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=900&q=85",
        "tone": "#eee3df",
    },
]

PRODUCTS_BY_ID = {product["id"]: product for product in PRODUCTS}


def stop_gunicorn_master():
    """Under gunicorn a dead worker is just replaced. In the container the
    master is PID 1, so stop it too: the container exits and restarts."""
    try:
        with open("/proc/1/cmdline", "rb") as f:
            pid1_is_gunicorn = b"gunicorn" in f.read()
    except OSError:
        return
    if pid1_is_gunicorn and os.getppid() == 1:
        os.kill(1, signal.SIGTERM)


def create_app(fault_injection=FAULT_INJECTION):
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
        return render_template(
            "shop.html",
            products=PRODUCTS,
            categories=sorted({product["category"] for product in PRODUCTS}),
            service=APP_NAME,
            version=APP_VERSION,
        )

    @app.get("/api")
    def api_index():
        return jsonify(
            {
                "service": APP_NAME,
                "version": APP_VERSION,
                "environment": APP_ENV,
                "instance": socket.gethostname(),
                "uptime_seconds": round(time.time() - START_TIME, 3),
                "message": "Hello from the demo app! Visit /shop for the storefront.",
            }
        )

    @app.post("/checkout")
    def checkout():
        payload = request.get_json(silent=True) if request.is_json else request.form
        payload = payload or {}
        items = payload.get("items", [])
        if isinstance(items, str):
            try:
                import json

                items = json.loads(items)
            except (TypeError, ValueError):
                items = []

        if not isinstance(items, list) or not items:
            return jsonify({"error": "Your cart is empty."}), 400

        quantity_by_id = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            product_id = item.get("id")
            quantity = item.get("quantity")
            if product_id not in PRODUCTS_BY_ID or not isinstance(quantity, int) or isinstance(quantity, bool):
                continue
            if quantity < 1 or quantity > 10:
                continue
            quantity_by_id[product_id] = min(10, quantity_by_id.get(product_id, 0) + quantity)

        if not quantity_by_id:
            return jsonify({"error": "No valid products found in your cart."}), 400

        order_items = [
            {
                "name": PRODUCTS_BY_ID[product_id]["name"],
                "quantity": quantity,
                "unit_price": PRODUCTS_BY_ID[product_id]["price"],
            }
            for product_id, quantity in quantity_by_id.items()
        ]
        total = sum(item["quantity"] * item["unit_price"] for item in order_items)
        order = {
            "id": "DEMO-" + uuid.uuid4().hex[:8].upper(),
            "items": order_items,
            "total": total,
            "payment_status": "demo_paid",
            "notice": "Demo payment only. No real payment was taken.",
        }
        if request.is_json:
            return jsonify(order), 201
        return render_template("order.html", order=order), 201

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

    @app.get("/api/products")
    def products():
        if fault_injection and random.random() < 0.3:
            logger.error("Database connection failed: timeout after 5000ms reading products")
            return jsonify({"error": "Database connection failed"}), 500
        logger.info("Returned %d products", len(PRODUCTS))
        return jsonify({"products": PRODUCTS})

    @app.post("/api/checkout")
    def api_checkout():
        if fault_injection:
            time.sleep(random.uniform(0.1, 2.0))
            if random.random() < 0.2:
                logger.error("Payment processing failed: gateway timeout")
                return jsonify({"error": "Payment processing failed"}), 500
        order_id = f"ORD-{random.randint(1000, 9999)}"
        logger.info("Checkout confirmed, order %s", order_id)
        return jsonify({"order_id": order_id, "status": "confirmed"})

    if fault_injection:

        @app.get("/crash")
        def crash():
            logger.critical("Simulated application crash triggered")
            stop_gunicorn_master()
            os._exit(1)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
