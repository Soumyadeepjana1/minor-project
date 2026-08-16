"""Tests for the demo Flask app."""

import main


def test_index_returns_service_info():
    client = main.app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "demo-app"
    assert data["version"] == "0.2.0"
    assert "instance" in data
    assert "uptime_seconds" in data


def test_healthz_ok():
    client = main.app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_readyz_ok():
    client = main.app.test_client()
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ready"}


def test_version_endpoint():
    client = main.app.test_client()
    resp = client.get("/version")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "demo-app"
    assert data["version"] == "0.2.0"


def test_metrics_endpoint():
    client = main.app.test_client()
    # Warm the counters so the metric actually exists.
    client.get("/healthz")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_unknown_route_404():
    client = main.app.test_client()
    resp = client.get("/nope")
    assert resp.status_code == 404
