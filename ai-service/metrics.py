"""Prometheus metrics for the AI service.

Prometheus scrapes `GET /metrics` through the pod annotations in
`ai-service-deployment.yaml` (the Compose stack scrapes it by service name),
served from the default registry below.
"""

from prometheus_client import Counter, Gauge, Histogram

# --- Incidents -------------------------------------------------------------

incidents_total = Counter(
    "kubesage_incidents_total",
    "Incidents opened from Alertmanager webhooks",
    ["alertname", "severity"]
)

incidents_active = Gauge(
    "kubesage_incidents_active",
    "Incidents currently held in memory, by status",
    ["status"]
)

alerts_suppressed_total = Counter(
    "kubesage_alerts_suppressed_total",
    "Firing alerts dropped because an incident for them is already open",
    ["alertname"]
)

incidents_auto_resolved_total = Counter(
    "kubesage_incidents_auto_resolved_total",
    "Incidents closed by a resolved alert from Alertmanager",
    ["alertname"]
)

# --- AI analysis -----------------------------------------------------------

analysis_total = Counter(
    "kubesage_analysis_total",
    "Root cause analyses attempted",
    ["outcome"]
)

analysis_duration_seconds = Histogram(
    "kubesage_analysis_duration_seconds",
    "Wall-clock time from incident creation to completed analysis",
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120)
)

analysis_confidence_score = Histogram(
    "kubesage_analysis_confidence_score",
    "Confidence score reported by the LLM (0-100)",
    buckets=(0, 20, 40, 60, 70, 80, 90, 100)
)

log_lines_fetched = Histogram(
    "kubesage_log_lines_fetched",
    "Log lines returned by Loki per analysis",
    buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000)
)

# --- Remediation -----------------------------------------------------------

remediations_total = Counter(
    "kubesage_remediations_total",
    "Remediation attempts by action and outcome",
    ["action", "status"]
)

remediation_duration_seconds = Histogram(
    "kubesage_remediation_duration_seconds",
    "Time spent executing a remediation action, including rollout verification",
    ["action"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
)

# --- Start every known series at 0 -----------------------------------------
# A labelled series only exists after its first increment, and then it already
# reads 1. increase() and rate() need an earlier sample to compare with, so the
# first incident of each kind never showed on the AI Incidents dashboard
# ("Incidents Opened: 0" after three incidents). Creating the series up front
# fixes that. Alert names and severities mirror monitoring/alerts/rules.yaml;
# an alert not listed here still works, it just appears from its second event.

KNOWN_ALERTS = {
    "PodCrashLooping": "critical",
    "HighErrorRate": "critical",
    "HighLatency": "warning",
}
REMEDIATION_ACTIONS = ("restart_pod", "rollback_deployment", "scale_deployment", "notify_sre")
REMEDIATION_STATUSES = ("success", "dry_run", "skipped", "failed")

for _alertname, _severity in KNOWN_ALERTS.items():
    incidents_total.labels(alertname=_alertname, severity=_severity)
    alerts_suppressed_total.labels(alertname=_alertname)
    incidents_auto_resolved_total.labels(alertname=_alertname)

for _outcome in ("success", "failed"):
    analysis_total.labels(outcome=_outcome)

for _action in REMEDIATION_ACTIONS:
    remediation_duration_seconds.labels(action=_action)
    for _status in REMEDIATION_STATUSES:
        remediations_total.labels(action=_action, status=_status)
