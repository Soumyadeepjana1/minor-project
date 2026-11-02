import asyncio
import time
from datetime import datetime

import aiohttp
import pytest
from models.incident import Alert, Incident, IncidentStatus, RootCauseAnalysis
from services.k8s_client import KubernetesClient
from services.loki_client import LokiClient


def _sample_alert(alertname="PodCrashLooping", severity="critical", **extra_labels):
    labels = {"alertname": alertname, "severity": severity, "app": "demo-app"}
    labels.update(extra_labels)
    return Alert(
        status="firing",
        labels=labels,
        annotations={"summary": "Pod crash looping", "description": "Pod restarted"},
        startsAt=datetime.now(),
    )


def _incident(alertname="PodCrashLooping", incident_id="INC-1", **extra_labels):
    return Incident(
        id=incident_id,
        alert=_sample_alert(alertname, **extra_labels),
        created_at=datetime.now(),
    )


REMEDIATION_ENV = (
    "REMEDIATION_ENABLED",
    "REMEDIATION_DRY_RUN",
    "REMEDIATION_ALLOWED_NAMESPACES",
    "REMEDIATION_PROTECTED_WORKLOADS",
    "REMEDIATION_MAX_REPLICAS",
    "REMEDIATION_SCALE_STEP",
    "KUBESAGE_NAMESPACE",
)


def _client(monkeypatch, **env):
    """Build a KubernetesClient with a controlled environment.

    Ambient remediation settings are cleared first so the guardrail tests
    behave the same locally, in CI, and inside the container.
    """
    for key in REMEDIATION_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return KubernetesClient()


# ---------------------------------------------------------------- models

def test_incident_defaults():
    incident = _incident()
    assert incident.status == IncidentStatus.INVESTIGATING
    assert incident.rca is None
    assert incident.remediation is None


def test_root_cause_analysis_model():
    rca = RootCauseAnalysis(
        root_cause="DB timeout",
        impact="Checkout failing",
        recommended_fix="Restart pod",
        confidence_score=85.0,
    )
    assert rca.confidence_score == 85.0


# ------------------------------------------------------- action selection

def test_determine_remediation_mapping():
    from main import determine_remediation

    action = determine_remediation(_incident("HighErrorRate", "INC-2"))
    assert action["action"] == "rollback_deployment"


def test_platform_alert_names_are_mapped():
    # The platform's rules (monitoring/alerts/rules.yaml) use their own names.
    from main import determine_remediation

    expected = {
        "DemoAppDown": "restart_pod",
        "DemoAppCrashLooping": "restart_pod",
        "DemoAppHighErrorRate": "rollback_deployment",
        "DemoAppHighLatency": "scale_deployment",
    }
    for alertname, action in expected.items():
        assert determine_remediation(_incident(alertname, "INC-P"))["action"] == action


def test_platform_alerts_get_the_right_prompt_guidance():
    from services.ollama_client import CRASH_ALERTS, REQUEST_ALERTS

    assert {"DemoAppDown", "DemoAppCrashLooping"} <= CRASH_ALERTS
    assert {"DemoAppHighErrorRate", "DemoAppHighLatency"} <= REQUEST_ALERTS


def test_determine_remediation_default():
    from main import determine_remediation

    action = determine_remediation(_incident("UnknownAlert", "INC-3"))
    assert action["action"] == "notify_sre"


def test_determine_remediation_carries_target():
    from main import determine_remediation

    action = determine_remediation(
        _incident("PodCrashLooping", "INC-4", pod="demo-app-7d9f8b6c5d-x2k9p")
    )
    assert action["pod"] == "demo-app-7d9f8b6c5d-x2k9p"
    assert action["deployment"] == "demo-app"
    assert action["namespace"] == "kubesage"


def test_resolve_target_prefers_alert_namespace():
    from main import resolve_target

    target = resolve_target(_incident("HighLatency", "INC-5", namespace="staging"))
    assert target["namespace"] == "staging"


def test_deployment_from_pod():
    from main import deployment_from_pod

    assert deployment_from_pod("demo-app-7d9f8b6c5d-x2k9p") == "demo-app"
    assert deployment_from_pod("standalone") == "standalone"
    assert deployment_from_pod(None) is None


# ------------------------------------------------------------ guardrails

@pytest.mark.asyncio
async def test_remediation_disabled_is_skipped(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_ENABLED="false")

    result = await client.restart_pod(pod="demo-app-abc-123", namespace="kubesage")
    assert result["status"] == "skipped"
    assert "disabled" in result["detail"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_is_skipped(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_ALLOWED_NAMESPACES="kubesage")

    result = await client.scale_deployment(deployment="demo-app", namespace="kube-system")
    assert result["status"] == "skipped"
    assert "allow-list" in result["detail"]


@pytest.mark.asyncio
async def test_protected_workload_is_never_remediated(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_DRY_RUN="true")

    result = await client.restart_pod(pod="ai-service-6f5b9c8d7-qq2xm", namespace="kubesage")
    assert result["status"] == "skipped"
    assert "protected" in result["detail"]


@pytest.mark.asyncio
async def test_kube_state_metrics_is_protected_by_default(monkeypatch):
    # PodCrashLooping alerts are built from kube-state-metrics series; the
    # exporter itself must never be the thing that gets restarted.
    client = _client(monkeypatch, REMEDIATION_DRY_RUN="true")

    result = await client.restart_pod(
        pod="kube-state-metrics-7c9d8f6b5-abcde", namespace="kubesage"
    )
    assert result["status"] == "skipped"
    assert "protected" in result["detail"]


@pytest.mark.asyncio
async def test_missing_target_is_skipped(monkeypatch):
    client = _client(monkeypatch)

    result = await client.rollback_deployment(deployment=None, namespace="kubesage")
    assert result["status"] == "skipped"
    assert "target" in result["detail"]


def test_rollout_is_not_healthy_before_the_controller_sees_the_change(monkeypatch):
    from kubernetes import client as m

    def deployment(generation, observed):
        return m.V1Deployment(
            metadata=m.V1ObjectMeta(name="demo-app", generation=generation),
            spec=m.V1DeploymentSpec(replicas=2, selector=m.V1LabelSelector(), template=m.V1PodTemplateSpec()),
            status=m.V1DeploymentStatus(
                observed_generation=observed, replicas=2, updated_replicas=2, available_replicas=2
            ),
        )

    client = _client(monkeypatch)
    for dep, healthy in ((deployment(3, 2), False), (deployment(3, 3), True)):
        client._apps = type("Apps", (), {"read_namespaced_deployment": lambda self, name, namespace, d=dep: d})()
        assert client._rollout_healthy("demo-app", "demo")[0] is healthy


def test_rollback_restores_the_previous_template_exactly(monkeypatch):
    # Revision 2 only added an annotation (what `kubectl rollout restart`
    # does). Rolling back must drop it, which a merge patch cannot do.
    from kubernetes import client as m

    def template(annotations, pod_hash):
        return m.V1PodTemplateSpec(
            metadata=m.V1ObjectMeta(
                labels={"app": "demo-app", "pod-template-hash": pod_hash},
                annotations=annotations,
            ),
            spec=m.V1PodSpec(containers=[m.V1Container(name="demo-app", image="demo-app:0.2.0")]),
        )

    dep = m.V1Deployment(
        metadata=m.V1ObjectMeta(
            name="demo-app", uid="dep-uid", resource_version="42",
            annotations={"deployment.kubernetes.io/revision": "2"},
        ),
        spec=m.V1DeploymentSpec(
            selector=m.V1LabelSelector(match_labels={"app": "demo-app"}),
            template=template({"kubectl.kubernetes.io/restartedAt": "now"}, "new"),
        ),
    )
    owner = [m.V1OwnerReference(api_version="apps/v1", kind="Deployment", name="demo-app", uid="dep-uid")]

    def rs(revision, annotations, pod_hash):
        return m.V1ReplicaSet(
            metadata=m.V1ObjectMeta(
                annotations={"deployment.kubernetes.io/revision": revision}, owner_references=owner
            ),
            spec=m.V1ReplicaSetSpec(
                selector=m.V1LabelSelector(match_labels={"app": "demo-app"}),
                template=template(annotations, pod_hash),
            ),
        )

    class FakeApps:
        replaced = None

        def read_namespaced_deployment(self, name, namespace):
            return dep

        def list_namespaced_replica_set(self, namespace, label_selector):
            return m.V1ReplicaSetList(items=[rs("1", None, "old"), rs("2", {"kubectl.kubernetes.io/restartedAt": "now"}, "new")])

        def replace_namespaced_deployment(self, name, namespace, body):
            FakeApps.replaced = body

        def patch_namespaced_deployment(self, **kwargs):
            raise AssertionError("rollback must replace the deployment, not patch it")

    client = _client(monkeypatch)
    client._apps = FakeApps()

    assert client._rollback("demo-app", "demo") == 1
    body = FakeApps.replaced
    assert body.metadata.resource_version == "42"
    assert body.spec.template.metadata.annotations is None
    assert body.spec.template.metadata.labels == {"app": "demo-app"}
    assert "rollback to revision 1" in body.metadata.annotations["kubernetes.io/change-cause"]


@pytest.mark.asyncio
async def test_dry_run_never_touches_the_cluster(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_DRY_RUN="true")

    def _fail():
        raise AssertionError("dry run must not connect to the Kubernetes API")

    monkeypatch.setattr(client, "_connect", _fail)

    result = await client.scale_deployment(deployment="demo-app", namespace="kubesage")
    assert result["status"] == "dry_run"
    assert "demo-app" in result["detail"]


@pytest.mark.asyncio
async def test_unreachable_cluster_reports_failure(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_connect", lambda: False)
    client._load_error = "no usable Kubernetes config"

    result = await client.restart_pod(pod="demo-app-abc-123", namespace="kubesage")
    assert result["status"] == "failed"
    assert "Kubernetes API unavailable" in result["detail"]


# -------------------------------------------------------------- dispatch

@pytest.mark.asyncio
async def test_dispatch_notify_sre_is_skipped():
    from main import determine_remediation, dispatch_remediation

    action = determine_remediation(_incident("UnknownAlert", "INC-6"))
    result = await dispatch_remediation(action)

    assert result["status"] == "skipped"
    assert result["action"] == "notify_sre"


@pytest.mark.asyncio
async def test_execute_remediation_records_result_on_incident(monkeypatch):
    import main

    incident = _incident("HighLatency", "INC-7")
    action = main.determine_remediation(incident)

    async def _fake_dispatch(_action):
        return {
            "action": "scale_deployment",
            "status": "success",
            "detail": "Scaled demo-app from 2 to 3 replicas",
            "target": "demo-app",
            "namespace": "kubesage",
            "executed_at": "2026-09-03T10:00:00+00:00",
        }

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "dispatch_remediation", _fake_dispatch)
    monkeypatch.setattr(main.slack_client, "send_remediation_update", _noop)
    monkeypatch.setattr(main.slack_client, "send_remediation_result", _noop)

    await main.execute_remediation(incident, action)

    assert incident.status == IncidentStatus.RESOLVED
    assert incident.resolved_at is not None
    assert incident.remediation["result"]["status"] == "success"


@pytest.mark.asyncio
async def test_failed_remediation_marks_incident_failed(monkeypatch):
    import main

    incident = _incident("PodCrashLooping", "INC-8")
    action = main.determine_remediation(incident)

    async def _fake_dispatch(_action):
        return {
            "action": "restart_pod",
            "status": "failed",
            "detail": "Kubernetes API call failed (403): Forbidden",
            "target": "demo-app",
            "namespace": "kubesage",
            "executed_at": "2026-09-03T10:00:00+00:00",
        }

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "dispatch_remediation", _fake_dispatch)
    monkeypatch.setattr(main.slack_client, "send_remediation_update", _noop)
    monkeypatch.setattr(main.slack_client, "send_remediation_result", _noop)

    await main.execute_remediation(incident, action)

    assert incident.status == IncidentStatus.REMEDIATION_FAILED
    assert incident.resolved_at is None


# ------------------------------------------------------------ Loki query

def test_loki_query_uses_regex_line_filter():
    query = LokiClient().build_query("demo-app")

    # `|= "A" or |= "B"` is not valid LogQL - Loki rejects it with a 400
    assert " or " not in query
    assert '|~' in query
    assert 'app="demo-app"' in query


def test_loki_query_scopes_to_namespace_and_pod():
    query = LokiClient().build_query("demo-app", namespace="kubesage", pod="demo-app-abc-123")

    assert 'namespace="kubesage"' in query
    assert 'pod="demo-app-abc-123"' in query


def test_loki_logs_are_returned_oldest_first():
    """direction=backward returns newest first; callers slice the tail for recent lines"""
    data = {
        "data": {
            "result": [
                {"values": [["300", "third"], ["100", "first"]]},
                {"values": [["200", "second"]]},
            ]
        }
    }

    assert LokiClient()._parse_logs(data) == ["first", "second", "third"]


def test_loki_parse_handles_malformed_payload():
    assert LokiClient()._parse_logs({}) == []


# ----------------------------------------------------- alert de-duplication

def _webhook(alerts):
    return {
        "receiver": "ai-incident-analyzer",
        "status": "firing",
        "alerts": alerts,
        "groupLabels": {},
        "commonLabels": {},
        "externalURL": "",
        "version": "4",
        "groupKey": "{}",
    }


def _raw_alert(alertname="PodCrashLooping", status="firing", fingerprint="abc123"):
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "severity": "critical",
            "app": "demo-app",
            "namespace": "kubesage",
        },
        "annotations": {"summary": "s", "description": "d"},
        "startsAt": "2026-09-03T10:00:00Z",
        "fingerprint": fingerprint,
    }


@pytest.fixture
def api(monkeypatch):
    """TestClient with the incident store emptied and the slow paths stubbed"""
    import main
    from fastapi.testclient import TestClient

    main.active_incidents.clear()

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "analyze_incident", _noop)
    monkeypatch.setattr(main.slack_client, "send_alert_notification", _noop)
    monkeypatch.setenv("API_TOKEN", "test-token")

    with TestClient(main.app, headers={"Authorization": "Bearer test-token"}) as client:
        yield client

    main.active_incidents.clear()


def test_alerts_need_the_bearer_token(api):
    body = _webhook([_raw_alert()])
    assert api.post("/api/v1/alerts", json=body, headers={"Authorization": ""}).status_code == 401
    assert api.post("/api/v1/alerts", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post("/api/v1/incidents/INC-1/remediate", headers={"Authorization": ""}).status_code == 401
    assert api.post("/api/v1/alerts", json=body).status_code == 200


def test_state_changing_endpoints_fail_closed_without_a_token(api, monkeypatch):
    monkeypatch.delenv("API_TOKEN")
    monkeypatch.delenv("API_TOKEN_FILE", raising=False)
    assert api.post("/api/v1/alerts", json=_webhook([_raw_alert()])).status_code == 503
    assert api.post("/api/v1/incidents/INC-1/remediate").status_code == 503


def test_token_can_come_from_a_mounted_file(api, monkeypatch, tmp_path):
    monkeypatch.delenv("API_TOKEN")
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    monkeypatch.setenv("API_TOKEN_FILE", str(token_file))
    assert api.post("/api/v1/alerts", json=_webhook([_raw_alert()])).status_code == 200


def test_cors_allows_no_origin_by_default(api):
    resp = api.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_alert_key_prefers_fingerprint():
    from main import alert_key

    assert alert_key(_sample_alert()) != ""
    alert = _sample_alert()
    alert.fingerprint = "deadbeef"
    assert alert_key(alert) == "deadbeef"


def test_repeat_firing_alert_is_suppressed(api):
    first = api.post("/api/v1/alerts", json=_webhook([_raw_alert()])).json()
    assert first["incidents_created"] == 1

    second = api.post("/api/v1/alerts", json=_webhook([_raw_alert()])).json()
    assert second["incidents_created"] == 0
    assert second["duplicates_suppressed"] == 1

    assert api.get("/api/v1/incidents").json()["total"] == 1


def test_resolved_alert_closes_the_incident(api):
    api.post("/api/v1/alerts", json=_webhook([_raw_alert()]))

    resolved = api.post(
        "/api/v1/alerts", json=_webhook([_raw_alert(status="resolved")])
    ).json()

    assert resolved["incidents_created"] == 0
    assert resolved["incidents_resolved"] == 1

    incidents = api.get("/api/v1/incidents").json()["incidents"]
    assert incidents[0]["status"] == "resolved"


def test_resolved_alert_frees_the_key_for_a_new_incident(api):
    api.post("/api/v1/alerts", json=_webhook([_raw_alert()]))
    api.post("/api/v1/alerts", json=_webhook([_raw_alert(status="resolved")]))

    again = api.post("/api/v1/alerts", json=_webhook([_raw_alert()])).json()
    assert again["incidents_created"] == 1


def test_different_alerts_are_not_deduplicated(api):
    body = _webhook([
        _raw_alert("PodCrashLooping", fingerprint="aaa"),
        _raw_alert("HighErrorRate", fingerprint="bbb"),
    ])
    assert api.post("/api/v1/alerts", json=body).json()["incidents_created"] == 2


# ------------------------------------------------------------------ metrics

def test_metrics_endpoint_exposes_kubesage_series(api):
    api.post("/api/v1/alerts", json=_webhook([_raw_alert()]))

    response = api.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    body = response.text
    assert "kubesage_incidents_total" in body
    assert 'kubesage_incidents_active{status="investigating"} 1.0' in body


def test_known_series_exist_before_the_first_event():
    # A labelled counter only appears on its first increment, already at 1, and
    # increase()/rate() need an earlier sample to compare with. Found in a live
    # run: the AI Incidents dashboard showed "Incidents Opened: 0" after three
    # incidents. Every series the dashboards query must start at 0.
    import metrics
    from prometheus_client import generate_latest

    body = generate_latest().decode()
    for alertname, severity in metrics.KNOWN_ALERTS.items():
        assert f'kubesage_incidents_total{{alertname="{alertname}",severity="{severity}"}}' in body
        assert f'kubesage_alerts_suppressed_total{{alertname="{alertname}"}}' in body
    for outcome in ("success", "failed"):
        assert f'kubesage_analysis_total{{outcome="{outcome}"}}' in body
    for action in ("restart_pod", "rollback_deployment", "scale_deployment", "notify_sre"):
        assert f'kubesage_remediations_total{{action="{action}",status="dry_run"}}' in body
        assert f'kubesage_remediation_duration_seconds_count{{action="{action}"}}' in body


# ------------------------------------------------- Slack request signing

def _signed_headers(secret, body, timestamp="1757000000"):
    import hashlib
    import hmac

    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}


def test_signature_accepted_for_a_valid_request(monkeypatch):
    from services.slack_client import SlackClient

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "s3cr3t")
    client = SlackClient()
    body = b"payload=%7B%7D"

    ok, reason = client.verify_signature(
        body, _signed_headers("s3cr3t", body), now=1757000000
    )
    assert ok, reason


def test_signature_rejected_for_a_tampered_body(monkeypatch):
    from services.slack_client import SlackClient

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "s3cr3t")
    client = SlackClient()
    headers = _signed_headers("s3cr3t", b"payload=%7B%7D")

    ok, reason = client.verify_signature(b"payload=evil", headers, now=1757000000)
    assert not ok
    assert reason == "Slack signature mismatch"


def test_signature_rejected_when_replayed(monkeypatch):
    from services.slack_client import SlackClient

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "s3cr3t")
    client = SlackClient()
    body = b"payload=%7B%7D"

    ok, reason = client.verify_signature(
        body, _signed_headers("s3cr3t", body), now=1757000000 + 3600
    )
    assert not ok
    assert reason == "Slack request is too old"


def test_signature_fails_closed_without_a_secret(monkeypatch):
    from services.slack_client import SlackClient

    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    client = SlackClient()

    ok, reason = client.verify_signature(b"", {})
    assert not ok
    assert "not configured" in reason


# -------------------------------------------------- Slack actions endpoint

def test_slack_action_rejected_without_a_signature(api):
    response = api.post("/api/v1/slack/actions", content=b"payload=%7B%7D")
    assert response.status_code == 401


def test_slack_button_triggers_remediation(api, monkeypatch):
    import json as _json
    import urllib.parse

    import main

    api.post("/api/v1/alerts", json=_webhook([_raw_alert()]))
    incident_id = api.get("/api/v1/incidents").json()["incidents"][0]["id"]

    triggered = {}

    async def _capture(incident, action):
        triggered["incident"] = incident.id
        triggered["action"] = action["action"]

    monkeypatch.setattr(main, "execute_remediation", _capture)
    monkeypatch.setattr(main.slack_client, "signing_secret", "s3cr3t")

    payload = {
        "user": {"username": "soumyadeep"},
        "actions": [{"action_id": "run_remediation", "value": incident_id}],
    }
    body = urllib.parse.urlencode({"payload": _json.dumps(payload)}).encode()

    response = api.post(
        "/api/v1/slack/actions",
        content=body,
        headers=_signed_headers("s3cr3t", body, timestamp=str(int(time.time()))),
    )

    assert response.status_code == 200
    assert triggered["incident"] == incident_id
    assert triggered["action"] == "restart_pod"
    assert main.active_incidents[incident_id].assigned_to == "soumyadeep"


def test_slack_button_for_unknown_incident_is_ignored(api, monkeypatch):
    import json as _json
    import urllib.parse

    import main

    monkeypatch.setattr(main.slack_client, "signing_secret", "s3cr3t")

    payload = {"actions": [{"action_id": "run_remediation", "value": "INC-nope"}]}
    body = urllib.parse.urlencode({"payload": _json.dumps(payload)}).encode()

    response = api.post(
        "/api/v1/slack/actions",
        content=body,
        headers=_signed_headers("s3cr3t", body, timestamp=str(int(time.time()))),
    )
    assert response.status_code == 200


# ------------------------------------------------- rollout verification

@pytest.mark.asyncio
async def test_verification_skipped_when_disabled(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_VERIFY="false")

    verification = await client.wait_for_rollout("demo-app", "kubesage")
    assert verification["status"] == "skipped"


@pytest.mark.asyncio
async def test_verification_reports_healthy(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_connect", lambda: True)
    monkeypatch.setattr(
        client, "_rollout_healthy", lambda d, n: (True, "2/2 available, 2 updated, 0 unavailable")
    )

    verification = await client.wait_for_rollout("demo-app", "kubesage")
    assert verification["status"] == "healthy"
    assert "demo-app" in verification["detail"]


@pytest.mark.asyncio
async def test_restart_is_not_verified_until_the_old_pod_is_gone(monkeypatch):
    client = _client(monkeypatch, REMEDIATION_VERIFY_INTERVAL="0")
    monkeypatch.setattr(client, "_connect", lambda: True)
    checks = iter([False, False, True])  # the deleted pod is still terminating twice
    monkeypatch.setattr(client, "_pod_gone", lambda pod, ns: next(checks))
    looked_at_status = []

    def healthy(d, n):
        looked_at_status.append(d)
        return True, "2/2 available, 2 updated, 0 unavailable"

    monkeypatch.setattr(client, "_rollout_healthy", healthy)

    verification = await client.wait_for_rollout("demo-app", "demo", gone_pod="demo-app-abc-123")
    assert verification["status"] == "healthy"
    assert looked_at_status == ["demo-app"]  # only read once the old pod was gone


@pytest.mark.asyncio
async def test_verification_times_out_on_a_stuck_rollout(monkeypatch):
    client = _client(
        monkeypatch, REMEDIATION_VERIFY_TIMEOUT="0", REMEDIATION_VERIFY_INTERVAL="0"
    )
    monkeypatch.setattr(client, "_connect", lambda: True)
    monkeypatch.setattr(
        client, "_rollout_healthy", lambda d, n: (False, "0/2 available, 2 updated, 2 unavailable")
    )

    verification = await client.wait_for_rollout("demo-app", "kubesage")
    assert verification["status"] == "timeout"
    assert "0/2 available" in verification["detail"]


@pytest.mark.asyncio
async def test_stuck_rollout_marks_the_incident_failed(monkeypatch):
    import main

    incident = _incident("PodCrashLooping", "INC-9")
    action = main.determine_remediation(incident)

    async def _fake_dispatch(_action):
        return {
            "action": "restart_pod", "status": "success",
            "detail": "Deleted pod 'demo-app-abc-123'",
            "target": "demo-app-abc-123", "namespace": "kubesage",
            "executed_at": "2026-09-03T10:00:00+00:00",
        }

    async def _stuck(deployment, namespace=None, gone_pod=None):
        return {"status": "timeout", "detail": "it never became healthy"}

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "dispatch_remediation", _fake_dispatch)
    monkeypatch.setattr(main.k8s_client, "wait_for_rollout", _stuck)
    monkeypatch.setattr(main.slack_client, "send_remediation_update", _noop)
    monkeypatch.setattr(main.slack_client, "send_remediation_result", _noop)

    await main.execute_remediation(incident, action)

    assert incident.status == IncidentStatus.REMEDIATION_FAILED
    assert incident.remediation["result"]["verification"]["status"] == "timeout"


# ------------------------------------------------- LLM response parsing
#
# Small models do not reliably return bare JSON. These are the shapes seen in
# practice from llama3.2:3b; every one of them must yield a usable analysis
# rather than the "Failed to parse" fallback.

from services.ollama_client import (
    OllamaClient,
    condense_logs,
    extract_json_object,
    normalize_confidence,
)

CLEAN_JSON = '{"root_cause": "OOMKilled", "impact": "Checkout down", "recommended_fix": "restart_pod", "confidence_score": 85}'

RESPONSE_SHAPES = {
    "bare json": CLEAN_JSON,
    "markdown fence": f"```json\n{CLEAN_JSON}\n```",
    "unlabelled fence": f"```\n{CLEAN_JSON}\n```",
    "prose preamble": f"Here is my analysis:\n\n{CLEAN_JSON}",
    "prose either side": f"Analysis follows.\n{CLEAN_JSON}\nLet me know if you need more.",
    "leading newlines": f"\n\n\n{CLEAN_JSON}",
    "nested object": '{"root_cause": "OOMKilled", "impact": "Checkout down", "recommended_fix": "restart_pod", "confidence_score": 85, "evidence": {"container": "demo-app", "exit_code": 137}}',
    "braces in strings": '{"root_cause": "Template {{ .Values.x }} unresolved", "impact": "Boot fails", "recommended_fix": "restart_pod", "confidence_score": 70}',
}


@pytest.mark.parametrize("shape", list(RESPONSE_SHAPES))
def test_parser_handles_real_llm_response_shapes(shape):
    analysis = OllamaClient()._parse_analysis_response(RESPONSE_SHAPES[shape])

    assert analysis.root_cause != "Failed to parse AI response", shape
    assert analysis.confidence_score > 0, shape
    assert analysis.analyzed_at is not None, shape


def test_parser_keeps_braces_inside_strings():
    analysis = OllamaClient()._parse_analysis_response(RESPONSE_SHAPES["braces in strings"])
    assert analysis.root_cause == "Template {{ .Values.x }} unresolved"


def test_parser_falls_back_on_unparseable_text():
    analysis = OllamaClient()._parse_analysis_response(
        "The pod is crash looping because of memory pressure."
    )

    assert analysis.root_cause == "Failed to parse AI response"
    assert analysis.confidence_score == 0.0
    # the raw text is kept so a human can see what the model actually said
    assert "memory pressure" in analysis.additional_context["raw_response"]


def test_parser_records_missing_fields():
    analysis = OllamaClient()._parse_analysis_response(
        '{"root_cause": "OOMKilled", "confidence_score": 60}'
    )

    assert analysis.impact == "Unknown"
    assert analysis.recommended_fix == "Manual investigation required"
    assert set(analysis.additional_context["missing_fields"]) == {"impact", "recommended_fix"}


def test_extract_json_object_ignores_trailing_garbage():
    assert extract_json_object('{"a": 1} }}} nonsense') == {"a": 1}


def test_extract_json_object_returns_none_for_arrays():
    assert extract_json_object('[1, 2, 3]') is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (85, 85.0),
        ("85", 85.0),
        ("85%", 85.0),
        (0.85, 85.0),        # models often answer with a fraction
        (1, 100.0),
        (0, 0.0),
        (150, 100.0),        # clamped, so the Pydantic bound never trips
        (-10, 0.0),
        ("high", 0.0),
        (None, 0.0),
    ],
)
def test_confidence_normalization(raw, expected):
    assert normalize_confidence(raw) == expected


# ------------------------------------------------------- prompt contents

def test_prompt_includes_workload_and_available_actions():
    alert_info = {
        "labels": {
            "alertname": "PodCrashLooping", "severity": "critical",
            "app": "demo-app", "namespace": "kubesage",
            "pod": "demo-app-7d9f8b6c5d-x2k9p",
        },
        "annotations": {"description": "Pod restarted 5 times"},
    }
    prompt = OllamaClient()._create_analysis_prompt(["ERROR: OOMKilled"], alert_info)

    assert "demo-app-7d9f8b6c5d-x2k9p" in prompt
    assert "kubesage" in prompt
    assert "ERROR: OOMKilled" in prompt
    # the fix the model suggests should be one the service can actually run
    for action in ("restart_pod", "rollback_deployment", "scale_deployment"):
        assert action in prompt


def test_prompt_tells_the_model_to_lower_confidence_without_logs():
    prompt = OllamaClient()._create_analysis_prompt([], {"labels": {}, "annotations": {}})
    assert "No error logs found" in prompt
    assert "lower your confidence" in prompt


def _distinct_messages(n):
    """n log lines that differ in words, not just digits (digits are normalized)"""
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    return [f"ERROR event {words[i % 8]} {words[(i // 8) % 8]}" for i in range(n)]


def test_prompt_only_sends_the_most_recent_logs():
    logs = _distinct_messages(50)
    prompt = OllamaClient()._create_analysis_prompt(logs, {"labels": {}, "annotations": {}})

    assert logs[49] in prompt
    assert logs[30] in prompt
    assert logs[29] not in prompt


# ------------------------------------------------------- log condensing
# Found in a live run: with error traffic and a crash at the same time, the
# 20 newest lines were all copies of one timeout message, the CRITICAL crash
# line never reached the model, and it blamed the timeouts for the crash.

def test_condense_collapses_repeats_with_counts():
    logs = ["ERROR: db timeout"] * 5 + ["ERROR: payment timeout"] * 2
    assert condense_logs(logs) == ["[x5] ERROR: db timeout", "[x2] ERROR: payment timeout"]


def test_condense_treats_lines_differing_only_in_numbers_as_repeats():
    logs = [
        "2026-10-19 08:47:30 ERROR order ORD-1234 failed",
        "2026-10-19 08:47:31 ERROR order ORD-9876 failed",
    ]
    # the newest occurrence is the one shown
    assert condense_logs(logs) == ["[x2] 2026-10-19 08:47:31 ERROR order ORD-9876 failed"]


def test_condense_keeps_a_rare_critical_line_behind_many_newer_repeats():
    logs = ["CRITICAL:__main__:Simulated application crash triggered"]
    for _ in range(150):
        logs += ["ERROR: Database connection timeout", "ERROR: Payment gateway timeout"]
    logs += _distinct_messages(30)

    condensed = condense_logs(logs, max_entries=10)

    assert len(condensed) == 10
    assert "CRITICAL:__main__:Simulated application crash triggered" in condensed
    # severe lines survive, the rest of the budget goes to the newest messages
    assert condensed[-1] == logs[-1]


def test_condense_preserves_chronological_order():
    logs = ["ERROR: first", "CRITICAL: second", "ERROR: third"]
    assert condense_logs(logs) == logs


def test_condense_ignores_blank_lines():
    assert condense_logs(["", "   ", "ERROR: real"]) == ["ERROR: real"]


def test_prompt_shows_the_crash_line_despite_newer_noise():
    logs = ["CRITICAL:__main__:Simulated application crash triggered"]
    logs += ["ERROR:__main__:Failed to fetch products: Database connection timeout"] * 300

    prompt = OllamaClient()._create_analysis_prompt(logs, {"labels": {}, "annotations": {}})

    assert "Simulated application crash triggered" in prompt
    assert "[x300]" in prompt


CRASH_AND_NOISE = ["CRITICAL:__main__:Simulated application crash triggered"] + [
    "ERROR:__main__:Checkout failed: Payment gateway timeout"
] * 150


def _alert(alertname):
    return {"labels": {"alertname": alertname}, "annotations": {}}


def test_crash_alert_prompt_separates_severe_lines_from_repeated_noise():
    # Showing the crash line was not enough: with it in one list next to
    # "[x150] ...timeout", qwen2.5:7b blamed the timeouts 3 times out of 3.
    # A separate section fixed it (eval scenario "crash amid error noise").
    prompt = OllamaClient()._create_analysis_prompt(CRASH_AND_NOISE, _alert("PodCrashLooping"))
    severe, others = prompt.split("Other recent errors", 1)

    assert "Severe log lines" in severe
    assert "Simulated application crash triggered" in severe
    assert "Payment gateway timeout" not in severe
    assert "[x150] ERROR:__main__:Checkout failed: Payment gateway timeout" in others
    assert "crash/restart alert" in prompt
    assert "frequent, not that it caused this alert" in prompt


def test_request_alert_prompt_keeps_one_list_so_a_crash_line_is_not_singled_out():
    # The mirror case: with a separate severe section, the same model blamed an
    # unrelated crash for an error spike 3 times out of 3 (eval scenario
    # "error spike despite a crash"). Request alerts get one chronological list.
    for alertname in ("HighErrorRate", "HighLatency"):
        prompt = OllamaClient()._create_analysis_prompt(CRASH_AND_NOISE, _alert(alertname))

        assert "Severe log lines" not in prompt
        assert "Simulated application crash triggered" in prompt  # still visible
        assert "[x150] ERROR:__main__:Checkout failed: Payment gateway timeout" in prompt
        assert "request error/latency alert" in prompt


def test_unknown_alert_gets_generic_guidance():
    prompt = OllamaClient()._create_analysis_prompt(CRASH_AND_NOISE, _alert("SomethingElse"))
    assert "Severe log lines" in prompt
    assert "Explain the alert with the log lines that match it" in prompt


def test_prompt_marks_an_empty_section_instead_of_dropping_it():
    prompt = OllamaClient()._create_analysis_prompt(
        ["ERROR: db timeout"], {"labels": {}, "annotations": {}}
    )
    severe = prompt.split("Severe log lines", 1)[1].split("Other recent errors", 1)[0]
    assert "None" in severe


# --------------------------------------------------- Ollama request shape

@pytest.mark.asyncio
async def test_request_uses_ollama_option_names(monkeypatch):
    """`max_tokens` is silently ignored by Ollama; the option is `num_predict`"""
    client = OllamaClient()
    captured = {}

    class _Response:
        status = 200

        async def json(self):
            return {"response": CLEAN_JSON}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _Response()

    async def _session():
        return _Session()

    monkeypatch.setattr(client, "_get_session", _session)

    analysis = await client.analyze_logs(["ERROR"], {"labels": {}, "annotations": {}})

    assert analysis.confidence_score == 85.0
    assert captured["body"]["format"] == "json"
    assert captured["body"]["options"]["num_predict"] == 500
    assert "max_tokens" not in captured["body"]["options"]


@pytest.mark.asyncio
async def test_analysis_retries_then_falls_back(monkeypatch):
    monkeypatch.setenv("OLLAMA_RETRIES", "2")
    client = OllamaClient()
    attempts = []

    class _Session:
        def post(self, url, json=None):
            attempts.append(url)
            raise aiohttp.ClientError("connection refused")

    async def _session():
        return _Session()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_get_session", _session)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    analysis = await client.analyze_logs([], {"labels": {}, "annotations": {}})

    assert len(attempts) == 3  # initial try + 2 retries
    assert analysis.confidence_score == 0.0
    assert "connection refused" in analysis.root_cause


@pytest.mark.asyncio
async def test_timeout_is_reported_in_the_fallback(monkeypatch):
    monkeypatch.setenv("OLLAMA_RETRIES", "0")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "30")
    client = OllamaClient()

    class _Session:
        def post(self, url, json=None):
            raise TimeoutError()

    async def _session():
        return _Session()

    monkeypatch.setattr(client, "_get_session", _session)

    analysis = await client.analyze_logs([], {"labels": {}, "annotations": {}})
    assert "timed out after 30s" in analysis.root_cause


# ------------------------------------------------------------ diagnostics

def test_diagnostics_reports_dependency_health(api, monkeypatch):
    import main

    async def _loki():
        return {"status": "healthy", "detail": "ready"}

    async def _ollama():
        return {"status": "degraded", "detail": "Model 'llama3.2:3b' is not pulled"}

    monkeypatch.setattr(main.loki_client, "health", _loki)
    monkeypatch.setattr(main.ollama_client, "health", _ollama)

    body = api.get("/api/v1/diagnostics").json()

    assert body["status"] == "degraded"
    assert body["checks"]["loki"]["status"] == "healthy"
    assert body["checks"]["ollama"]["status"] == "degraded"
    assert body["kubernetes"]["enabled"] is True


# ------------------------------------------- confidence without evidence
#
# `make eval` against qwen2.5:7b: given no logs at all, the model still reported
# 85% confidence despite the prompt asking it to hedge. Self-reported confidence
# is not calibrated, so the cap is enforced in code.

@pytest.mark.asyncio
async def test_confidence_is_capped_when_no_logs_were_found(monkeypatch):
    from services.ollama_client import NO_EVIDENCE_CONFIDENCE_CAP

    client = OllamaClient()

    class _Response:
        status = 200

        async def json(self):
            return {"response": CLEAN_JSON}  # claims 85

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, url, json=None):
            return _Response()

    async def _session():
        return _Session()

    monkeypatch.setattr(client, "_get_session", _session)

    analysis = await client.analyze_logs([], {"labels": {}, "annotations": {}})

    assert analysis.confidence_score == NO_EVIDENCE_CONFIDENCE_CAP
    assert analysis.additional_context["confidence_capped"]["reported"] == 85.0
    # the analysis itself is kept - only the certainty is walked back
    assert analysis.root_cause == "OOMKilled"


@pytest.mark.asyncio
async def test_confidence_is_untouched_when_logs_were_found(monkeypatch):
    client = OllamaClient()

    class _Response:
        status = 200

        async def json(self):
            return {"response": CLEAN_JSON}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, url, json=None):
            return _Response()

    async def _session():
        return _Session()

    monkeypatch.setattr(client, "_get_session", _session)

    analysis = await client.analyze_logs(
        ["ERROR OOMKilled"], {"labels": {}, "annotations": {}}
    )
    assert analysis.confidence_score == 85.0
    assert analysis.additional_context is None
