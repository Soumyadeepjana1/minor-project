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


def test_metrics_expose_project_gauges():
    client = main.app.test_client()
    # Warm the request path so the in-flight/size metrics exist.
    client.get("/healthz")
    body = client.get("/metrics").get_data(as_text=True)
    assert "app_info" in body
    assert "app_uptime_seconds" in body
    assert "http_requests_in_progress" in body
    assert "http_response_size_bytes" in body
    assert 'name="demo-app"' in body


def test_unknown_route_404():
    client = main.app.test_client()
    resp = client.get("/nope")
    assert resp.status_code == 404


def test_unknown_route_is_counted_as_an_error():
    client = main.app.test_client()
    client.get("/nope")
    body = client.get("/metrics").get_data(as_text=True)
    assert 'path="/nope",status="404"' in body


def test_error_response_is_json():
    client = main.app.test_client()
    data = client.get("/nope").get_json()
    assert data["status"] == 404
    assert data["error"]


def test_products_and_checkout_work_without_fault_injection():
    client = main.create_app(fault_injection=False).test_client()
    resp = client.get("/api/products")
    assert resp.status_code == 200
    assert len(resp.get_json()["products"]) == 3
    resp = client.post("/api/checkout")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "confirmed"


def test_crash_endpoint_only_exists_with_fault_injection():
    assert main.create_app(fault_injection=False).test_client().get("/crash").status_code == 404
    rules = {r.rule for r in main.create_app(fault_injection=True).url_map.iter_rules()}
    assert "/crash" in rules


def test_fault_injection_fails_products_with_a_500(monkeypatch):
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    resp = main.create_app(fault_injection=True).test_client().get("/api/products")
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Database connection failed"}


def test_fault_injection_slows_and_fails_checkout(monkeypatch):
    slept = []
    monkeypatch.setattr(main.time, "sleep", slept.append)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: b)
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    resp = main.create_app(fault_injection=True).test_client().post("/api/checkout")
    assert slept == [2.0]
    assert resp.status_code == 500


def test_stop_gunicorn_master_leaves_other_init_processes_alone(monkeypatch):
    # Outside the container (or under the Flask dev server) PID 1 is not
    # gunicorn, so a crash must not signal it.
    killed = []
    monkeypatch.setattr(main.os, "kill", lambda pid, sig: killed.append(pid))
    main.stop_gunicorn_master()
    assert killed == []
