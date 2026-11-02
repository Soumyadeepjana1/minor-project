import hashlib
import hmac
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Slack rejects its own requests past this age; so do we, to stop replays
SIGNATURE_MAX_AGE_SECONDS = 60 * 5

logger = logging.getLogger(__name__)

class SlackClient:
    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN")
        self.channel = os.getenv("SLACK_CHANNEL", "#incidents")
        self.signing_secret = os.getenv("SLACK_SIGNING_SECRET")
        self.client = WebClient(token=self.token) if self.token else None

    def verify_signature(
        self, body: bytes, headers: Mapping[str, str], now: float | None = None
    ) -> tuple[bool, str]:
        """Verify a Slack interactivity request signature.

        Slack signs `v0:{timestamp}:{body}` with the app signing secret. Without
        this check anyone who learns the URL could fire remediation actions, so
        an unconfigured secret fails closed.
        """
        if not self.signing_secret:
            return False, "SLACK_SIGNING_SECRET is not configured"

        timestamp = headers.get("X-Slack-Request-Timestamp")
        signature = headers.get("X-Slack-Signature")
        if not timestamp or not signature:
            return False, "Missing Slack signature headers"

        try:
            age = abs((now if now is not None else time.time()) - int(timestamp))
        except ValueError:
            return False, "Malformed Slack timestamp"

        if age > SIGNATURE_MAX_AGE_SECONDS:
            return False, "Slack request is too old"

        basestring = b"v0:" + timestamp.encode() + b":" + body
        expected = "v0=" + hmac.new(
            self.signing_secret.encode(), basestring, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return False, "Slack signature mismatch"

        return True, "ok"

    async def send_alert_notification(self, incident: Any):
        """Send initial alert notification to Slack"""
        if not self.client:
            logger.warning("Slack token not configured")
            return

        try:
            alert = incident.alert
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🚨 New Incident: {incident.id}",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Alert:*\n{alert.labels.get('alertname', 'Unknown')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Severity:*\n{alert.labels.get('severity', 'Unknown')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Status:*\n{incident.status}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Time:*\n{incident.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Description:*\n{alert.annotations.get('description', 'No description')}"
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "🔍 AI analysis in progress..."
                        }
                    ]
                }
            ]

            self.client.chat_postMessage(
                channel=self.channel,
                blocks=blocks,
                text=f"New incident: {incident.id}"
            )

            logger.info(f"Slack notification sent for incident {incident.id}")

        except SlackApiError as e:
            logger.error(f"Failed to send Slack notification: {e.response['error']}")

    async def send_analysis(self, incident: Any):
        """Send AI analysis results to Slack"""
        if not self.client or not incident.rca:
            return

        try:
            rca = incident.rca
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🤖 AI Analysis: {incident.id}",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Confidence Score:*\n{rca.confidence_score:.0f}%"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Status:*\n{incident.status}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*🔍 Root Cause:*\n{rca.root_cause}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*📊 Impact:*\n{rca.impact}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*🔧 Recommended Fix:*\n{rca.recommended_fix}"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Run Auto-Remediation 🔧",
                                "emoji": True
                            },
                            "style": "primary",
                            "value": incident.id,
                            "action_id": "run_remediation"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "View Details 📊",
                                "emoji": True
                            },
                            "url": f"http://localhost:8000/api/v1/incidents/{incident.id}",
                            "action_id": "view_details"
                        }
                    ]
                }
            ]

            self.client.chat_postMessage(
                channel=self.channel,
                blocks=blocks,
                text=f"AI Analysis for incident {incident.id}"
            )

        except SlackApiError as e:
            logger.error(f"Failed to send analysis to Slack: {e.response['error']}")

    async def send_remediation_update(self, incident: Any, action: dict[str, Any]):
        """Send remediation status update to Slack"""
        if not self.client:
            return

        try:
            target = action.get("pod") or action.get("deployment") or "unknown"
            namespace = action.get("namespace", "unknown")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🔧 *Auto-Remediation Started*\nIncident: {incident.id}\nAction: {action['description']}\nTarget: `{namespace}/{target}`"
                    }
                }
            ]

            self.client.chat_postMessage(
                channel=self.channel,
                blocks=blocks,
                text=f"Remediation started for {incident.id}"
            )

        except SlackApiError as e:
            logger.error(f"Failed to send remediation update: {e.response['error']}")

    async def send_remediation_result(
        self, incident: Any, action: dict[str, Any], result: dict[str, Any]
    ):
        """Send the outcome of a remediation attempt to Slack"""
        if not self.client:
            return

        try:
            status = result.get("status", "unknown")
            icon = {
                "success": "✅",
                "dry_run": "🧪",
                "skipped": "⏭️",
                "failed": "❌"
            }.get(status, "ℹ️")

            target = result.get("target") or "unknown"
            namespace = result.get("namespace") or "unknown"

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{icon} *Auto-Remediation {status.replace('_', ' ').title()}*\nIncident: {incident.id}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Action:*\n{result.get('action', action.get('action', 'unknown'))}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Target:*\n`{namespace}/{target}`"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Incident Status:*\n{incident.status}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*At:*\n{result.get('executed_at', 'unknown')}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Detail:*\n{result.get('detail', 'No detail provided')}"
                    }
                }
            ]

            self.client.chat_postMessage(
                channel=self.channel,
                blocks=blocks,
                text=f"Remediation {status} for {incident.id}"
            )

        except SlackApiError as e:
            logger.error(f"Failed to send remediation result: {e.response['error']}")
