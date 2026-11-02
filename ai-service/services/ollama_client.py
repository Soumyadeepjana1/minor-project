import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import aiohttp
from models.incident import RootCauseAnalysis

logger = logging.getLogger(__name__)

# Actions the service can actually carry out - naming them in the prompt keeps
# the LLM's "recommended fix" inside what auto-remediation can do
# Measured against qwen2.5:7b: asked to lower its confidence when no logs were
# found, the model still reported 85. Self-reported confidence is not calibrated,
# so cap it here rather than trusting the instruction.
NO_EVIDENCE_CONFIDENCE_CAP = 40.0

AVAILABLE_ACTIONS = (
    "restart_pod (delete the pod so the ReplicaSet recreates it), "
    "rollback_deployment (revert to the previous revision), "
    "scale_deployment (add replicas), "
    "notify_sre (manual intervention)"
)


# How many distinct log messages go into the prompt. A 7B model on CPU slows
# down sharply with long prompts, so this stays small; condensing is what makes
# 20 entries enough.
MAX_PROMPT_LOG_ENTRIES = 20

# Digits vary between otherwise identical lines (timestamps, IDs, IPs, counts)
_VOLATILE = re.compile(r"\d+")
# Lines that usually name the cause rather than a symptom. They are kept even
# when hundreds of newer, noisier lines would otherwise push them out.
_SEVERE = re.compile(r"(?i)\b(critical|fatal|panic|traceback|oomkilled|segfault)\b")


def condense_logs(lines: list[str], max_entries: int = MAX_PROMPT_LOG_ENTRIES) -> list[str]:
    """Collapse repeated lines and keep the rare severe ones, oldest first.

    Found in a live run: with error traffic and a crash at the same time, the
    newest 20 lines were all the same timeout message, the CRITICAL crash line
    never reached the model, and it blamed the timeouts for the crash.

    Lines that differ only in digits are one message, shown once as its newest
    occurrence with an "[xN]" count. Severe messages are kept first; the rest of
    the budget goes to the most recent remaining messages.
    """
    groups: dict[str, list[Any]] = {}  # key -> [count, last_index, newest line]
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        key = _VOLATILE.sub("#", line)
        group = groups.get(key)
        if group:
            group[0] += 1
            group[1] = index
            group[2] = line
        else:
            groups[key] = [1, index, line]

    by_recency = sorted(groups.values(), key=lambda g: g[1])
    severe = [g for g in by_recency if _SEVERE.search(g[2])][-max_entries:]
    others = [g for g in by_recency if not _SEVERE.search(g[2])]
    room = max_entries - len(severe)
    kept = severe + (others[-room:] if room > 0 else [])
    kept.sort(key=lambda g: g[1])

    return [f"[x{count}] {line}" if count > 1 else line for count, _, line in kept]


CRASH_ALERTS = {"PodCrashLooping", "DemoAppDown", "DemoAppCrashLooping"}
REQUEST_ALERTS = {"HighErrorRate", "HighLatency", "DemoAppHighErrorRate", "DemoAppHighLatency"}


def alert_guidance(alertname: str) -> str:
    """Tell the model which evidence answers this kind of alert.

    The same logs can hold unrelated problems (in the live run, a crash and a
    timeout spike at once), and the right cause depends on what was alerted on.
    """
    if alertname in CRASH_ALERTS:
        return (
            "This is a crash/restart alert: explain it with the line logged as the "
            "process stopped. Request errors in the same logs are a separate symptom."
        )
    if alertname in REQUEST_ALERTS:
        return (
            "This is a request error/latency alert: explain it with the errors or slow "
            "operations that requests are hitting. A crash line, if present, is a "
            "separate event unless the request errors point to it."
        )
    return "Explain the alert with the log lines that match it."


def normalize_confidence(value: Any) -> float:
    """Coerce whatever the model returned into a 0-100 score.

    Models variously answer 85, "85", "85%", or 0.85 - all mean the same thing.
    """
    if value is None:
        return 0.0

    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()

    try:
        score = float(value)
    except (TypeError, ValueError):
        logger.warning(f"Unparseable confidence score: {value!r}")
        return 0.0

    # A fraction is the other common convention
    if 0 < score <= 1:
        score *= 100

    return max(0.0, min(100.0, score))


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first complete JSON object out of an LLM response.

    Even in JSON mode a model may wrap its answer in ```json fences or add a
    sentence of preamble, so scan for a balanced object rather than trusting
    the whole string to parse.
    """
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    depth = 0
    start = None
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(parsed, dict):
                    return parsed
                start = None

    return None


class OllamaClient:
    def __init__(self):
        self.base_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
        self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.timeout = int(os.getenv("OLLAMA_TIMEOUT", "60"))
        self.retries = int(os.getenv("OLLAMA_RETRIES", "2"))
        self.session = None

    async def _get_session(self):
        if self.session is None:
            # Without an explicit timeout a hung Ollama would pin the analysis
            # background task forever
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self.session

    async def analyze_logs(
        self,
        logs: list[str],
        alert_info: dict[str, Any]
    ) -> RootCauseAnalysis:
        """Analyze logs using Ollama LLM"""
        prompt = self._create_analysis_prompt(logs, alert_info)
        last_error = "unknown error"

        for attempt in range(1, self.retries + 2):
            try:
                session = await self._get_session()

                async with session.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        # Ask Ollama itself to constrain the output to JSON
                        "format": "json",
                        "options": {
                            "temperature": 0.1,
                            "top_p": 0.9,
                            # Ollama's option is num_predict; max_tokens is
                            # silently ignored
                            "num_predict": 500
                        }
                    }
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        analysis = self._parse_analysis_response(
                            result.get("response", "")
                        )
                        return self._cap_unevidenced_confidence(analysis, logs)

                    body = await response.text()
                    last_error = f"HTTP {response.status}: {body[:200]}"
                    logger.error(f"Ollama API error ({attempt}/{self.retries + 1}): {last_error}")

            except TimeoutError:
                last_error = f"timed out after {self.timeout}s"
                logger.error(f"Ollama request timed out ({attempt}/{self.retries + 1})")
            except aiohttp.ClientError as e:
                last_error = str(e)
                logger.error(f"Ollama request failed ({attempt}/{self.retries + 1}): {e}")
            except Exception as e:
                last_error = str(e)
                logger.exception("Ollama analysis raised an unexpected error")

            if attempt <= self.retries:
                await asyncio.sleep(2 ** (attempt - 1))

        return self._fallback_analysis(last_error)

    @staticmethod
    def _cap_unevidenced_confidence(
        analysis: RootCauseAnalysis, logs: list[str]
    ) -> RootCauseAnalysis:
        """Hold down the confidence score when there were no logs to reason over.

        An analysis built from the alert text alone is a guess, however sure the
        model sounds about it.
        """
        if logs or analysis.confidence_score <= NO_EVIDENCE_CONFIDENCE_CAP:
            return analysis

        logger.info(
            f"Capping confidence {analysis.confidence_score:.0f} -> "
            f"{NO_EVIDENCE_CONFIDENCE_CAP:.0f}: no logs were available"
        )
        context = dict(analysis.additional_context or {})
        context["confidence_capped"] = {
            "reported": analysis.confidence_score,
            "reason": "no logs were available to support the analysis"
        }

        return analysis.model_copy(
            update={
                "confidence_score": NO_EVIDENCE_CONFIDENCE_CAP,
                "additional_context": context
            }
        )

    async def health(self) -> dict[str, Any]:
        """Check Ollama is reachable and the configured model is pulled"""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/api/tags") as response:
                if response.status != 200:
                    return {
                        "status": "unhealthy",
                        "detail": f"Ollama returned HTTP {response.status}"
                    }

                data = await response.json()
                models = [m.get("name", "") for m in data.get("models", [])]

                if self.model in models:
                    return {"status": "healthy", "detail": f"Model '{self.model}' is available"}

                return {
                    "status": "degraded",
                    "detail": (
                        f"Model '{self.model}' is not pulled; run "
                        f"`ollama pull {self.model}`. Available: {models or 'none'}"
                    )
                }

        except Exception as e:  # noqa: BLE001 - health check: any failure is unreachable
            return {"status": "unreachable", "detail": f"{self.base_url}: {e!s}"}

    def _create_analysis_prompt(self, logs: list[str], alert_info: dict) -> str:
        """Create analysis prompt for the LLM"""
        entries = condense_logs(logs)
        severe = "\n".join(e for e in entries if _SEVERE.search(e))
        others = "\n".join(e for e in entries if not _SEVERE.search(e))
        labels = alert_info.get("labels", {})
        annotations = alert_info.get("annotations", {})

        workload = labels.get("pod") or labels.get("app", "unknown")
        namespace = labels.get("namespace", "unknown")

        prompt = f"""You are an SRE analyzing a Kubernetes incident. Determine the root cause from the alert and logs below.

Alert Information:
- Alert Name: {labels.get('alertname', 'Unknown')}
- Severity: {labels.get('severity', 'Unknown')}
- Workload: {workload} (namespace: {namespace})
- Description: {annotations.get('description', 'No description')}

{self._log_sections(severe, others, labels.get('alertname', ''))}

The automated remediation available is: {AVAILABLE_ACTIONS}.
Keep "recommended_fix" to one concrete action, preferring one of those.

Respond with ONLY this JSON object, no other text:
{{
    "root_cause": "what is actually broken, in one or two sentences",
    "impact": "what this means for users or the service",
    "recommended_fix": "the single next action to take",
    "confidence_score": 85
}}

confidence_score is an integer from 0 to 100. Use a low score when the logs are
empty or unrelated to the alert.
"""
        return prompt

    @staticmethod
    def _log_sections(severe: str, others: str, alertname: str = "") -> str:
        """The log part of the prompt, shaped by the kind of alert.

        Measured with `make eval` against qwen2.5:7b, two scenarios from one live
        run where a crash and a timeout spike happened together:
          - crash alert, one list: the model blamed the frequent timeouts (0/3).
            A separate section for severe lines fixed it (3/3).
          - error-rate alert, separate severe section: the model blamed the
            unrelated crash (0/3), even when told not to. One list fixed it (3/3).
        So the structure, not just the wording, follows the alert type.
        """
        if not severe and not others:
            return (
                "Recent Error Logs:\n"
                "No error logs found - reason about the alert alone and lower "
                "your confidence accordingly"
            )

        if alertname in REQUEST_ALERTS:
            combined = "\n".join(part for part in (severe, others) if part)
            logs = f"""Recent errors (oldest first; "[xN]" means the line repeated N times):
{combined}"""
        else:
            logs = f"""Severe log lines (CRITICAL, FATAL, panics, tracebacks):
{severe or 'None'}

Other recent errors (oldest first; "[xN]" means the line repeated N times):
{others or 'None'}"""

        return f"""{logs}

{alert_guidance(alertname)}
A high repeat count shows that an error is frequent, not that it caused this alert."""

    def _parse_analysis_response(self, response: str) -> RootCauseAnalysis:
        """Parse LLM response into structured analysis"""
        data = extract_json_object(response)

        if data is None:
            logger.error(
                f"Could not extract JSON from Ollama response: {response[:300]!r}"
            )
            return RootCauseAnalysis(
                root_cause="Failed to parse AI response",
                impact="Unknown",
                recommended_fix="Manual investigation required",
                confidence_score=0.0,
                additional_context={"raw_response": response[:1000]},
                analyzed_at=datetime.now(UTC)
            )

        missing = [
            field for field in ("root_cause", "impact", "recommended_fix")
            if not data.get(field)
        ]
        if missing:
            logger.warning(f"Ollama response missing fields: {missing}")

        return RootCauseAnalysis(
            root_cause=str(data.get("root_cause") or "Unknown"),
            impact=str(data.get("impact") or "Unknown"),
            recommended_fix=str(
                data.get("recommended_fix") or "Manual investigation required"
            ),
            confidence_score=normalize_confidence(data.get("confidence_score")),
            additional_context={"missing_fields": missing} if missing else None,
            analyzed_at=datetime.now(UTC)
        )

    async def close(self):
        """Release the shared HTTP session"""
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _fallback_analysis(self, reason: str = "AI analysis unavailable") -> RootCauseAnalysis:
        """Return fallback analysis when AI fails"""
        return RootCauseAnalysis(
            root_cause=f"AI analysis unavailable ({reason})",
            impact="Unknown",
            recommended_fix="Check application logs manually and verify service health",
            confidence_score=0.0,
            analyzed_at=datetime.now(UTC)
        )
