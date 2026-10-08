"""Tests for the local observability dashboard."""

import json
import pathlib

import pytest

import app as dashboard

# The dashboard entry module is imported as `app`, which would be ambiguous if
# pytest resolved the demo app's `app/` directory instead. Guard against it.
assert pathlib.Path(dashboard.__file__).parent.name == "dashboard", dashboard.__file__


CONFIG = {
    "version": "test",
    "grafana": "http://grafana:3000",
    "prometheus": "http://prometheus:9090",
    "alertmanager": "http://alertmanager:9093",
    "loki": "http://loki:3100",
    "app": "http://app:5000",
    "grafana_public": "http://localhost:3000",
    "prometheus_public": "http://localhost:9090",
    "alertmanager_public": "http://localhost:9093",
    "loki_public": "http://localhost:3100",
    "app_public": "http://localhost:5000",
    "timeout": 0.1,
    "ttl": 60.0,
    "refresh_seconds": 10,
    "github_repository": "acme/widgets",
    "github_token": "",
    "github_api_url": "https://api.github.com",
    "pipelines_ttl": 60.0,
}

WORKFLOWS_PAYLOAD = {
    "workflows": [
        {"name": "CI", "path": ".github/workflows/ci.yml", "state": "active"},
        {"name": "CD - Compose Stack", "path": ".github/workflows/cd-compose.yml", "state": "active"},
    ]
}

RUNS_PAYLOAD = {
    "workflow_runs": [
        {
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "master",
            "head_sha": "abcdef1234567890",
            "event": "push",
            "run_number": 42,
            "html_url": "https://github.com/acme/widgets/actions/runs/1",
            "updated_at": "2026-10-08T10:00:00Z",
        },
        {
            "path": ".github/workflows/cd-compose.yml",
            "status": "in_progress",
            "conclusion": None,
            "head_branch": "master",
            "head_sha": "abcdef1234567890",
            "event": "workflow_run",
            "run_number": 7,
            "html_url": "https://github.com/acme/widgets/actions/runs/2",
            "updated_at": "2026-10-08T10:05:00Z",
        },
        {
            # Older run for the same workflow: must not win over the newest one.
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "master",
            "head_sha": "0000000000000000",
            "event": "push",
            "run_number": 41,
            "html_url": "https://github.com/acme/widgets/actions/runs/0",
            "updated_at": "2026-10-08T09:00:00Z",
        },
    ]
}


@pytest.fixture()
def client():
    return dashboard.create_app(config=dict(CONFIG)).test_client()


def _probe(up, calls=None):
    def fake(url, timeout):
        if calls is not None:
            calls.append(url)
        return {"up": up, "status": 200 if up else 0}
    return fake


def test_healthz_and_readyz(client):
    assert client.get("/healthz").get_json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.get_json()["status"] == "ready"
    assert ready.get_json()["backends"] == 5


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AI-Powered Kubernetes Health Monitoring System" in resp.get_data(as_text=True)


def test_security_headers(client):
    headers = client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-src 'self' http://localhost:3000" in headers["Content-Security-Policy"]


def test_config_lists_dashboards(client):
    data = client.get("/api/config").get_json()
    uids = [d["uid"] for d in data["dashboards"]]
    assert uids == ["demo-app", "cluster-health", "application-slo"]
    assert data["refresh_seconds"] == 10
    embed = data["dashboards"][0]["embed_url"]
    assert "kiosk" in embed
    assert embed.startswith("http://localhost:3000/d/demo-app/")


def test_status_reports_backends_up(client, monkeypatch):
    monkeypatch.setattr(dashboard, "probe_backend", _probe(True))
    data = client.get("/api/status").get_json()
    assert data["up"] == 5
    assert data["total"] == 5
    assert all(b["up"] for b in data["backends"])
    assert data["backends"][0]["dashboard_url"] == "http://localhost:3000/"


def test_status_reports_backends_down(client, monkeypatch):
    monkeypatch.setattr(dashboard, "probe_backend", _probe(False))
    data = client.get("/api/status").get_json()
    assert data["up"] == 0
    assert all(not b["up"] for b in data["backends"])


def test_status_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "probe_backend", _probe(True, calls))
    client.get("/api/status")
    first = len(calls)
    assert first == 5
    second = client.get("/api/status").get_json()
    assert len(calls) == first  # served from cache
    assert second["cached"] is True


def test_status_force_bypasses_cache(client, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "probe_backend", _probe(True, calls))
    client.get("/api/status")
    client.get("/api/status?force=1")
    assert len(calls) == 10


def test_metrics_endpoint(client):
    client.get("/api/status")
    body = client.get("/metrics").get_data(as_text=True)
    assert "dashboard_status_requests_total" in body
    assert "dashboard_backend_up" in body


def test_overview_returns_real_project_metrics(client, monkeypatch):
    monkeypatch.setattr(dashboard, "prom_query", lambda url, expr, timeout: 0.25)
    monkeypatch.setattr(
        dashboard,
        "prom_labels",
        lambda url, expr, timeout: {"version": "0.2.0", "environment": "development"},
    )
    data = client.get("/api/metrics").get_json()
    assert data["source"] == "http://prometheus:9090"
    assert len(data["metrics"]) == len(dashboard.OVERVIEW_QUERIES)
    assert all(m["ok"] for m in data["metrics"])
    # error_ratio is scaled to percent
    ratio = next(m for m in data["metrics"] if m["key"] == "error_ratio")
    assert ratio["value"] == pytest.approx(25.0)
    assert data["build"]["version"] == "0.2.0"


def test_overview_degrades_when_prometheus_is_down(client, monkeypatch):
    monkeypatch.setattr(dashboard, "prom_query", lambda url, expr, timeout: None)
    monkeypatch.setattr(dashboard, "prom_labels", lambda url, expr, timeout: {})
    data = client.get("/api/metrics").get_json()
    assert all(not m["ok"] for m in data["metrics"])
    assert data["build"]["version"] == ""


def _fake_github(payloads):
    def fake(url, token, timeout):
        for needle, payload in payloads.items():
            if needle in url:
                return payload
        raise dashboard.GitHubError("404")
    return fake


def test_pipelines_reports_latest_run_per_workflow(client, monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "github_api_get",
        _fake_github({"actions/workflows": WORKFLOWS_PAYLOAD, "actions/runs": RUNS_PAYLOAD}),
    )
    data = client.get("/api/pipelines").get_json()
    assert data["configured"] is True
    assert data["available"] is True
    assert data["repository"] == "acme/widgets"
    by_name = {w["name"]: w for w in data["workflows"]}
    assert by_name["CI"]["conclusion"] == "success"
    assert by_name["CI"]["run_number"] == 42
    assert by_name["CI"]["sha"] == "abcdef1"
    assert by_name["CD - Compose Stack"]["status"] == "in_progress"


def test_pipelines_degrades_when_api_fails(client, monkeypatch):
    def boom(url, token, timeout):
        raise dashboard.GitHubError("403")

    monkeypatch.setattr(dashboard, "github_api_get", boom)
    data = client.get("/api/pipelines").get_json()
    assert data["configured"] is True
    assert data["available"] is False
    assert "403" in data["reason"]
    assert data["workflows"] == []


def test_pipelines_unconfigured_without_repository(client):
    app = dashboard.create_app(config={**CONFIG, "github_repository": ""})
    data = app.test_client().get("/api/pipelines").get_json()
    assert data["configured"] is False
    assert data["workflows"] == []


def test_load_dashboards_env_override(monkeypatch):
    custom = [{"uid": "custom", "title": "Custom", "description": "d"}]
    monkeypatch.setenv("DASHBOARDS", json.dumps(custom))
    assert dashboard.load_dashboards() == custom


def test_load_dashboards_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv("DASHBOARDS", "not-json")
    assert dashboard.load_dashboards() == dashboard.DEFAULT_DASHBOARDS


def test_build_config_public_url_defaults_to_server_url(monkeypatch):
    for key in ("GRAFANA_URL", "GRAFANA_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GRAFANA_URL", "http://grafana:3000/")
    cfg = dashboard.build_config()
    assert cfg["grafana"] == "http://grafana:3000"
    assert cfg["grafana_public"] == "http://grafana:3000"
