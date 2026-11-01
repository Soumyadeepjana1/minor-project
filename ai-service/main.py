import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

import metrics
import uvicorn
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from models.incident import Alert, Incident, IncidentStatus, RootCauseAnalysis
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from services.k8s_client import KubernetesClient, remediation_result
from services.loki_client import LokiClient
from services.ollama_client import OllamaClient
from services.slack_client import SlackClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Release the HTTP sessions held by the Loki and Ollama clients
    await loki_client.close()
    await ollama_client.close()

app = FastAPI(
    title="KubeSage AI Service",
    description="AI-powered incident response system for Kubernetes",
    version="1.0.0",
    lifespan=lifespan
)

# CORS: only for browser front ends listed in CORS_ORIGINS (comma-separated).
# Nothing is listed by default; Alertmanager and kubectl do not need CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def configured_api_token() -> str | None:
    """The shared bearer token: API_TOKEN, or the file named by API_TOKEN_FILE
    (a mounted Secret, so it can be rotated without a restart)."""
    token = os.getenv("API_TOKEN")
    path = os.getenv("API_TOKEN_FILE")
    if not token and path:
        try:
            with open(path) as f:
                token = f.read().strip()
        except OSError as e:
            logger.error(f"Cannot read API_TOKEN_FILE {path}: {e}")
    return token or None


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Guard for the endpoints that open incidents or change the cluster.

    Fails closed: with no token configured the endpoints refuse every request,
    the same way the Slack callback refuses without SLACK_SIGNING_SECRET.
    """
    token = configured_api_token()
    if not token:
        raise HTTPException(status_code=503, detail="API token is not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

# Initialize clients
loki_client = LokiClient()
ollama_client = OllamaClient()
slack_client = SlackClient()
k8s_client = KubernetesClient()

# Log lines fetched from Loki per analysis (condensed before prompting)
LOG_FETCH_LIMIT = int(os.getenv("LOKI_FETCH_LIMIT", "1000"))

# Store active incidents (in production, use a database)
active_incidents: dict[str, Incident] = {}

# Statuses that mean the incident is still open, so a repeat of the same alert
# is a duplicate rather than a new problem
OPEN_STATUSES = (
    IncidentStatus.INVESTIGATING,
    IncidentStatus.ANALYZING,
    IncidentStatus.ANALYZED,
    IncidentStatus.ANALYSIS_FAILED,
    IncidentStatus.REMEDIATING,
    IncidentStatus.REMEDIATION_FAILED,
)

def alert_key(alert: Alert) -> str:
    """Stable identity for an alert across firing/resolved deliveries.

    Alertmanager sends a fingerprint; fall back to the label set when a hand-made
    payload omits it.
    """
    if alert.fingerprint:
        return alert.fingerprint

    return "|".join(f"{k}={v}" for k, v in sorted(alert.labels.items()))

def find_open_incident(alert: Alert) -> Incident | None:
    """Find an already-open incident for this alert.

    Linear over the in-memory store, which is fine at demo scale; a database
    would index on the alert key.
    """
    key = alert_key(alert)
    for incident in active_incidents.values():
        if incident.status in OPEN_STATUSES and alert_key(incident.alert) == key:
            return incident

    return None

class AlertWebhook(BaseModel):
    receiver: str
    status: str
    alerts: list[Alert]
    groupLabels: dict[str, str]
    commonLabels: dict[str, str]
    externalURL: str
    version: str
    groupKey: str

@app.get("/")
async def root():
    return {
        "service": "KubeSage AI Service",
        "status": "operational",
        "version": "1.0.0",
        "incidents": len(active_incidents),
        "remediation": k8s_client.config_summary()
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/api/v1/diagnostics")
async def diagnostics():
    """Check the service's dependencies.

    Deliberately separate from /health: the liveness and readiness probes must
    not restart the service just because Ollama or Loki is down.
    """
    loki, ollama = await asyncio.gather(
        loki_client.health(), ollama_client.health()
    )

    kubernetes = k8s_client.config_summary()
    checks = {"loki": loki, "ollama": ollama}

    return {
        "status": (
            "ok" if all(c["status"] == "healthy" for c in checks.values())
            else "degraded"
        ),
        "checks": checks,
        "kubernetes": kubernetes
    }

@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus scrape endpoint.

    Scraped through the pod annotations in `ai-service-deployment.yaml`
    (by service name in the Compose stack).
    """
    counts: dict[str, int] = {}
    for incident in active_incidents.values():
        status = getattr(incident.status, "value", str(incident.status))
        counts[status] = counts.get(status, 0) + 1

    for status in IncidentStatus:
        metrics.incidents_active.labels(status=status.value).set(
            counts.get(status.value, 0)
        )

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/api/v1/alerts", dependencies=[Depends(require_api_token)])
async def handle_alert(webhook: AlertWebhook, background_tasks: BackgroundTasks):
    """Handle incoming alerts from Alertmanager"""
    logger.info(f"Received alert webhook with {len(webhook.alerts)} alerts")

    created = 0
    resolved = 0
    suppressed = 0

    for alert in webhook.alerts:
        alertname = alert.labels.get("alertname", "unknown")

        # Alertmanager is configured with send_resolved, so the same webhook
        # also carries recoveries. Those close an incident, they do not open one.
        if alert.status == "resolved":
            existing = find_open_incident(alert)
            if existing:
                existing.status = IncidentStatus.RESOLVED
                existing.resolved_at = datetime.now()
                existing.updated_at = datetime.now()
                existing.notes = "Closed by a resolved alert from Alertmanager"
                metrics.incidents_auto_resolved_total.labels(alertname=alertname).inc()
                logger.info(f"Incident {existing.id} closed by resolved alert")
                resolved += 1
            continue

        # A flapping alert re-fires every repeat_interval; one incident is enough
        existing = find_open_incident(alert)
        if existing:
            metrics.alerts_suppressed_total.labels(alertname=alertname).inc()
            logger.info(
                f"Alert '{alertname}' suppressed: incident {existing.id} is already open"
            )
            suppressed += 1
            continue

        # Create incident
        incident = Incident(
            id=f"INC-{datetime.now().strftime('%Y%m%d%H%M%S')}-{alertname}",
            alert=alert,
            status=IncidentStatus.INVESTIGATING,
            created_at=datetime.now()
        )

        # Store incident
        active_incidents[incident.id] = incident
        metrics.incidents_total.labels(
            alertname=alertname,
            severity=alert.labels.get("severity", "unknown")
        ).inc()
        created += 1

        # Trigger AI analysis in background
        background_tasks.add_task(analyze_incident, incident)

        # Send initial Slack notification
        background_tasks.add_task(
            slack_client.send_alert_notification, incident
        )

    return {
        "status": "accepted",
        "incidents_created": created,
        "incidents_resolved": resolved,
        "duplicates_suppressed": suppressed
    }

async def analyze_incident(incident: Incident):
    """Analyze incident using AI"""
    started = time.monotonic()
    try:
        logger.info(f"Analyzing incident: {incident.id}")

        # Update status
        incident.status = IncidentStatus.ANALYZING

        # Fetch logs from Loki, scoped to the workload the alert names
        labels = incident.alert.labels
        # A wide window, because the prompt is built from condensed logs: a
        # rare CRITICAL line must not fall outside it behind repeated noise.
        logs = await loki_client.fetch_recent_logs(
            app_label=labels.get("app", "demo-app"),
            minutes=5,
            namespace=labels.get("namespace"),
            pod=labels.get("pod"),
            limit=LOG_FETCH_LIMIT
        )

        metrics.log_lines_fetched.observe(len(logs))

        # Analyze with Ollama
        analysis = await ollama_client.analyze_logs(
            logs=logs,
            alert_info=incident.alert.model_dump()
        )

        # Update incident with analysis
        incident.rca = analysis
        incident.status = IncidentStatus.ANALYZED
        incident.updated_at = datetime.now()

        metrics.analysis_total.labels(outcome="success").inc()
        metrics.analysis_confidence_score.observe(analysis.confidence_score)
        metrics.analysis_duration_seconds.observe(time.monotonic() - started)

        # Send analysis to Slack
        await slack_client.send_analysis(incident)

        logger.info(f"Analysis complete for incident: {incident.id}")

    except Exception as e:  # noqa: BLE001 - a failed analysis is recorded, never raised
        logger.error(f"Failed to analyze incident {incident.id}: {e!s}")
        metrics.analysis_total.labels(outcome="failed").inc()
        metrics.analysis_duration_seconds.observe(time.monotonic() - started)
        incident.status = IncidentStatus.ANALYSIS_FAILED
        incident.rca = RootCauseAnalysis(
            root_cause=f"Analysis failed: {e!s}",
            impact="Unknown",
            recommended_fix="Manual investigation required",
            confidence_score=0.0
        )

@app.post("/api/v1/incidents/{incident_id}/remediate", dependencies=[Depends(require_api_token)])
async def auto_remediate(incident_id: str, background_tasks: BackgroundTasks):
    """Trigger auto-remediation for an incident"""
    if incident_id not in active_incidents:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident = active_incidents[incident_id]

    # Implement remediation logic here
    remediation_action = determine_remediation(incident)

    background_tasks.add_task(execute_remediation, incident, remediation_action)

    return {
        "status": "remediation_started",
        "incident_id": incident_id,
        "action": remediation_action
    }

def determine_remediation(incident: Incident) -> dict[str, Any]:
    """Determine appropriate remediation action based on incident type"""
    alert_name = incident.alert.labels.get("alertname", "")

    restart = {"action": "restart_pod", "description": "Restarting crash-looping pod"}
    rollback = {"action": "rollback_deployment", "description": "Rolling back to previous stable version"}
    scale = {"action": "scale_deployment", "description": "Scaling deployment to handle increased load"}
    remediation_map = {
        # KubeSage alert names
        "PodCrashLooping": restart,
        "HighErrorRate": rollback,
        "HighLatency": scale,
        # alert names of the platform's rules (monitoring/alerts/rules.yaml)
        "DemoAppDown": restart,
        "DemoAppCrashLooping": restart,
        "DemoAppHighErrorRate": rollback,
        "DemoAppHighLatency": scale,
    }

    action = dict(remediation_map.get(alert_name, {
        "action": "notify_sre",
        "description": "Manual intervention required"
    }))
    action.update(resolve_target(incident))
    return action

def deployment_from_pod(pod: str | None) -> str | None:
    """Strip the ReplicaSet hash and pod suffix.

    demo-app-7d9f8b6c5d-x2k9p -> demo-app
    """
    if not pod:
        return None

    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod

def resolve_target(incident: Incident) -> dict[str, Any]:
    """Work out which workload an alert points at, from its labels.

    Prometheus attaches `namespace` and `pod` to kube-state-metrics alerts;
    application alerts usually only carry `app`.
    """
    labels = incident.alert.labels
    pod = labels.get("pod")

    return {
        "namespace": labels.get("namespace") or k8s_client.default_namespace,
        "pod": pod,
        "deployment": (
            labels.get("deployment")
            or labels.get("app")
            or deployment_from_pod(pod)
        )
    }

async def dispatch_remediation(action: dict[str, Any]) -> dict[str, Any]:
    """Route a remediation action to the matching Kubernetes operation"""
    name = action.get("action", "notify_sre")
    namespace = action.get("namespace")
    pod = action.get("pod")
    deployment = action.get("deployment")

    try:
        if name == "restart_pod":
            return await k8s_client.restart_pod(
                pod=pod, deployment=deployment, namespace=namespace
            )

        if name == "rollback_deployment":
            return await k8s_client.rollback_deployment(
                deployment=deployment, namespace=namespace
            )

        if name == "scale_deployment":
            return await k8s_client.scale_deployment(
                deployment=deployment, namespace=namespace
            )

        return remediation_result(
            name, "skipped",
            "No automated action is defined for this alert; "
            "escalated to the on-call SRE",
            deployment or pod, namespace
        )

    except Exception as e:
        logger.exception(f"Remediation '{name}' raised an unexpected error")
        return remediation_result(
            name, "failed", f"Unexpected error: {e!s}",
            deployment or pod, namespace
        )

async def execute_remediation(incident: Incident, action: dict[str, Any]):
    """Execute the remediation action against the cluster"""
    logger.info(f"Executing remediation for {incident.id}: {action['description']}")

    incident.status = IncidentStatus.REMEDIATING
    incident.updated_at = datetime.now()
    await slack_client.send_remediation_update(incident, action)

    started = time.monotonic()
    result = await dispatch_remediation(action)

    # Issuing the action is not the same as fixing the problem: wait for the
    # workload to come back healthy before closing the incident.
    if result["status"] == "success":
        verification = await k8s_client.wait_for_rollout(
            action.get("deployment"), action.get("namespace")
        )
        result["verification"] = verification
        if verification["status"] == "timeout":
            result["status"] = "failed"
            result["detail"] = f"{result['detail']}, but {verification['detail']}"

    metrics.remediations_total.labels(
        action=result["action"], status=result["status"]
    ).inc()
    metrics.remediation_duration_seconds.labels(action=result["action"]).observe(
        time.monotonic() - started
    )

    action["result"] = result
    incident.remediation = action
    incident.updated_at = datetime.now()

    if result["status"] in ("success", "dry_run"):
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = datetime.now()
    elif result["status"] == "skipped":
        # Nothing was changed in the cluster, so the incident stays open
        incident.status = IncidentStatus.ANALYZED
    else:
        incident.status = IncidentStatus.REMEDIATION_FAILED

    logger.info(
        f"Remediation for {incident.id} finished: "
        f"{result['status']} - {result['detail']}"
    )

    await slack_client.send_remediation_result(incident, action, result)

@app.post("/api/v1/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    """Receive Slack interactive component callbacks.

    This is the request URL to configure under *Interactivity & Shortcuts* in the
    Slack app; it is what makes the "Run Auto-Remediation" button work.
    """
    body = await request.body()

    verified, reason = slack_client.verify_signature(body, request.headers)
    if not verified:
        logger.warning(f"Rejected Slack callback: {reason}")
        raise HTTPException(status_code=401, detail=reason)

    try:
        form = parse_qs(body.decode())
        payload = json.loads(form["payload"][0])
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed Slack payload: {e}")

    actions = payload.get("actions") or []
    if not actions:
        return Response(status_code=200)

    action_id = actions[0].get("action_id")
    incident_id = actions[0].get("value")
    user = (payload.get("user") or {}).get("username", "unknown")

    if action_id != "run_remediation":
        logger.info(f"Ignoring unhandled Slack action '{action_id}'")
        return Response(status_code=200)

    incident = active_incidents.get(incident_id)
    if not incident:
        logger.warning(f"Slack remediation requested for unknown incident {incident_id}")
        return Response(status_code=200)

    logger.info(f"Slack user '{user}' triggered remediation for {incident_id}")
    incident.assigned_to = user

    remediation_action = determine_remediation(incident)
    background_tasks.add_task(execute_remediation, incident, remediation_action)

    # Slack only needs a 200; progress and outcome are posted to the channel
    # by execute_remediation.
    return Response(status_code=200)

@app.get("/api/v1/incidents")
async def list_incidents():
    """List all active incidents"""
    return {
        "total": len(active_incidents),
        "incidents": [
            {
                "id": inc.id,
                "status": inc.status,
                "alert_name": inc.alert.labels.get("alertname"),
                "severity": inc.alert.labels.get("severity"),
                "created_at": inc.created_at.isoformat(),
                "has_rca": inc.rca is not None,
                "remediation": (
                    (inc.remediation.get("result") or {}).get("status")
                    if inc.remediation else None
                )
            }
            for inc in active_incidents.values()
        ]
    }

@app.get("/api/v1/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """Get details of a specific incident"""
    if incident_id not in active_incidents:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident = active_incidents[incident_id]
    return incident

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
