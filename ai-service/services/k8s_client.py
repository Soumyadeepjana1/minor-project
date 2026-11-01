import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException
    KUBERNETES_SDK_AVAILABLE = True
except ImportError:  # SDK is optional so unit tests can run without a cluster
    k8s = None
    k8s_config = None
    ApiException = Exception
    KUBERNETES_SDK_AVAILABLE = False

POD_TEMPLATE_HASH = "pod-template-hash"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"


def remediation_result(
    action: str,
    status: str,
    detail: str,
    target: str | None = None,
    namespace: str | None = None,
    **extra: Any
) -> dict[str, Any]:
    """Build the outcome record attached to an incident and posted to Slack.

    status is one of: success, dry_run, skipped, failed.
    """
    result = {
        "action": action,
        "status": status,
        "detail": detail,
        "target": target,
        "namespace": namespace,
        "executed_at": datetime.now(UTC).isoformat()
    }
    result.update(extra)
    return result


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning(f"Invalid integer for {name}, falling back to {default}")
        return default


def _csv(name: str, default: str) -> set:
    return {item.strip() for item in os.getenv(name, default).split(",") if item.strip()}


class NoPreviousRevision(Exception):
    """Raised when a deployment has no earlier ReplicaSet to roll back to"""


class KubernetesClient:
    """Executes remediation actions against the Kubernetes API.

    The official Kubernetes SDK is synchronous, so every call is pushed onto a
    worker thread to keep the FastAPI event loop free. Actions are guarded: they
    run only when remediation is enabled, the namespace is allow-listed, and the
    target is not one of KubeSage's own workloads.
    """

    def __init__(self):
        self.enabled = _flag("REMEDIATION_ENABLED", True)
        self.dry_run = _flag("REMEDIATION_DRY_RUN", False)
        self.default_namespace = os.getenv("KUBESAGE_NAMESPACE", "kubesage")
        self.allowed_namespaces = _csv(
            "REMEDIATION_ALLOWED_NAMESPACES", self.default_namespace
        )
        self.protected_workloads = _csv(
            "REMEDIATION_PROTECTED_WORKLOADS",
            "ai-service,prometheus,alertmanager,loki,promtail,grafana,kube-state-metrics"
        )
        self.max_replicas = _int("REMEDIATION_MAX_REPLICAS", 5)
        self.scale_step = _int("REMEDIATION_SCALE_STEP", 1)
        self.verify = _flag("REMEDIATION_VERIFY", True)
        self.verify_timeout = _int("REMEDIATION_VERIFY_TIMEOUT", 120)
        self.verify_interval = _int("REMEDIATION_VERIFY_INTERVAL", 5)
        self._core = None
        self._apps = None
        self._load_error: str | None = None

    def config_summary(self) -> dict[str, Any]:
        """Expose the active guardrails (surfaced on GET / for demos and debugging)"""
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "default_namespace": self.default_namespace,
            "allowed_namespaces": sorted(self.allowed_namespaces),
            "protected_workloads": sorted(self.protected_workloads),
            "max_replicas": self.max_replicas,
            "scale_step": self.scale_step,
            "verify": self.verify,
            "verify_timeout": self.verify_timeout,
            "sdk_available": KUBERNETES_SDK_AVAILABLE
        }

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> bool:
        """Load cluster credentials once. Blocking - always call via to_thread."""
        if self._core is not None and self._apps is not None:
            return True

        if not KUBERNETES_SDK_AVAILABLE:
            self._load_error = "the kubernetes SDK is not installed"
            return False

        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except Exception:  # noqa: BLE001 - no in-cluster config: try kubeconfig
            try:
                k8s_config.load_kube_config()
                logger.info("Loaded local kubeconfig")
            except Exception as e:  # noqa: BLE001 - no cluster: remediation reports unavailable
                self._load_error = f"no usable Kubernetes config ({e!s})"
                logger.warning(f"Kubernetes client unavailable: {self._load_error}")
                return False

        self._core = k8s.CoreV1Api()
        self._apps = k8s.AppsV1Api()
        self._load_error = None
        return True

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    def _is_protected(self, target: str) -> bool:
        return any(
            target == workload or target.startswith(f"{workload}-")
            for workload in self.protected_workloads
        )

    def _guard(
        self, action: str, namespace: str, target: str | None
    ) -> dict[str, Any] | None:
        """Return a skip result if the action must not run, otherwise None"""
        if not self.enabled:
            return remediation_result(
                action, "skipped",
                "Remediation is disabled (REMEDIATION_ENABLED=false)",
                target, namespace
            )

        if not target:
            return remediation_result(
                action, "skipped",
                "Could not determine a target workload from the alert labels",
                target, namespace
            )

        if namespace not in self.allowed_namespaces:
            return remediation_result(
                action, "skipped",
                f"Namespace '{namespace}' is not in the allow-list "
                f"{sorted(self.allowed_namespaces)}",
                target, namespace
            )

        if self._is_protected(target):
            return remediation_result(
                action, "skipped",
                f"'{target}' is a protected KubeSage workload and is never remediated",
                target, namespace
            )

        return None

    async def _ensure_connected(
        self, action: str, namespace: str, target: str | None
    ) -> dict[str, Any] | None:
        connected = await asyncio.to_thread(self._connect)
        if connected:
            return None
        return remediation_result(
            action, "failed",
            f"Kubernetes API unavailable: {self._load_error}",
            target, namespace
        )

    @staticmethod
    def _api_failure(
        action: str, namespace: str, target: str | None, e: Exception
    ) -> dict[str, Any]:
        status = getattr(e, "status", None)
        reason = getattr(e, "reason", None) or str(e)
        detail = f"Kubernetes API call failed: {reason}"
        if status:
            detail = f"Kubernetes API call failed ({status}): {reason}"
        logger.error(f"{action} on {namespace}/{target} failed: {detail}")
        return remediation_result(action, "failed", detail, target, namespace)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def restart_pod(
        self,
        pod: str | None = None,
        deployment: str | None = None,
        namespace: str | None = None
    ) -> dict[str, Any]:
        """Delete the offending pod, or roll the whole deployment if no pod is known.

        Deleting a pod is the least disruptive fix for a crash loop: the
        ReplicaSet immediately recreates it with fresh state.
        """
        action = "restart_pod"
        namespace = namespace or self.default_namespace
        target = pod or deployment

        guard = self._guard(action, namespace, target)
        if guard:
            return guard

        if self.dry_run:
            kind = "pod" if pod else "deployment"
            return remediation_result(
                action, "dry_run",
                f"Dry run: would restart {kind} '{target}' in namespace '{namespace}'",
                target, namespace
            )

        failure = await self._ensure_connected(action, namespace, target)
        if failure:
            return failure

        try:
            if pod:
                await asyncio.to_thread(
                    self._core.delete_namespaced_pod,
                    name=pod, namespace=namespace, grace_period_seconds=30
                )
                detail = (
                    f"Deleted pod '{pod}' in '{namespace}'; "
                    "the ReplicaSet will recreate it"
                )
            else:
                await asyncio.to_thread(self._rollout_restart, deployment, namespace)
                detail = (
                    f"Triggered a rolling restart of deployment "
                    f"'{deployment}' in '{namespace}'"
                )

            logger.info(detail)
            return remediation_result(action, "success", detail, target, namespace)

        except Exception as e:  # noqa: BLE001 - API failures become remediation results
            return self._api_failure(action, namespace, target, e)

    def _rollout_restart(self, deployment: str, namespace: str) -> None:
        """Equivalent of `kubectl rollout restart deployment/<name>`"""
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            RESTART_ANNOTATION: datetime.now(UTC).isoformat()
                        }
                    }
                }
            }
        }
        self._apps.patch_namespaced_deployment(
            name=deployment, namespace=namespace, body=body
        )

    async def rollback_deployment(
        self, deployment: str | None = None, namespace: str | None = None
    ) -> dict[str, Any]:
        """Roll a deployment back to its previous ReplicaSet revision"""
        action = "rollback_deployment"
        namespace = namespace or self.default_namespace

        guard = self._guard(action, namespace, deployment)
        if guard:
            return guard

        if self.dry_run:
            return remediation_result(
                action, "dry_run",
                f"Dry run: would roll deployment '{deployment}' in '{namespace}' "
                "back to its previous revision",
                deployment, namespace
            )

        failure = await self._ensure_connected(action, namespace, deployment)
        if failure:
            return failure

        try:
            revision = await asyncio.to_thread(self._rollback, deployment, namespace)
            detail = (
                f"Rolled deployment '{deployment}' in '{namespace}' "
                f"back to revision {revision}"
            )
            logger.info(detail)
            return remediation_result(
                action, "success", detail, deployment, namespace, revision=revision
            )

        except NoPreviousRevision as e:
            return remediation_result(
                action, "skipped", str(e), deployment, namespace
            )
        except Exception as e:  # noqa: BLE001 - API failures become remediation results
            return self._api_failure(action, namespace, deployment, e)

    def _rollback(self, deployment: str, namespace: str) -> int:
        """Find the newest ReplicaSet below the current revision and restore its template"""
        dep = self._apps.read_namespaced_deployment(
            name=deployment, namespace=namespace
        )
        current_revision = int(
            (dep.metadata.annotations or {}).get(REVISION_ANNOTATION, 0)
        )

        match_labels = (dep.spec.selector.match_labels or {}) if dep.spec.selector else {}
        selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        replica_sets = self._apps.list_namespaced_replica_set(
            namespace=namespace, label_selector=selector
        ).items

        candidates = []
        for rs in replica_sets:
            owners = rs.metadata.owner_references or []
            if not any(ref.uid == dep.metadata.uid for ref in owners):
                continue
            raw_revision = (rs.metadata.annotations or {}).get(REVISION_ANNOTATION)
            if raw_revision is None:
                continue
            revision = int(raw_revision)
            if current_revision and revision >= current_revision:
                continue
            candidates.append((revision, rs))

        if not candidates:
            raise NoPreviousRevision(
                f"Deployment '{deployment}' has no earlier revision to roll back to"
            )

        revision, previous = max(candidates, key=lambda item: item[0])

        template = previous.spec.template
        if template.metadata and template.metadata.labels:
            template.metadata.labels.pop(POD_TEMPLATE_HASH, None)

        dep.spec.template = template
        annotations = dep.metadata.annotations or {}
        annotations[CHANGE_CAUSE_ANNOTATION] = (
            f"KubeSage auto-remediation: rollback to revision {revision}"
        )
        dep.metadata.annotations = annotations

        # Replace, not patch: a merge patch cannot remove what the newer
        # revision added (an env var, an annotation), so the "rollback" could
        # leave the template unchanged. The resourceVersion from the read makes
        # a concurrent change fail instead of being overwritten.
        self._apps.replace_namespaced_deployment(
            name=deployment, namespace=namespace, body=dep
        )
        return revision

    async def scale_deployment(
        self,
        deployment: str | None = None,
        namespace: str | None = None,
        replicas: int | None = None
    ) -> dict[str, Any]:
        """Scale a deployment up to absorb load, capped at REMEDIATION_MAX_REPLICAS"""
        action = "scale_deployment"
        namespace = namespace or self.default_namespace

        guard = self._guard(action, namespace, deployment)
        if guard:
            return guard

        if self.dry_run:
            return remediation_result(
                action, "dry_run",
                f"Dry run: would scale deployment '{deployment}' in '{namespace}' "
                f"up by {self.scale_step} (cap {self.max_replicas})",
                deployment, namespace
            )

        failure = await self._ensure_connected(action, namespace, deployment)
        if failure:
            return failure

        try:
            current, target = await asyncio.to_thread(
                self._scale, deployment, namespace, replicas
            )

            if target == current:
                return remediation_result(
                    action, "skipped",
                    f"Deployment '{deployment}' is already at {current} replicas "
                    f"(cap {self.max_replicas}); scaling would have no effect",
                    deployment, namespace, replicas=current
                )

            detail = (
                f"Scaled deployment '{deployment}' in '{namespace}' "
                f"from {current} to {target} replicas"
            )
            logger.info(detail)
            return remediation_result(
                action, "success", detail, deployment, namespace,
                previous_replicas=current, replicas=target
            )

        except Exception as e:  # noqa: BLE001 - API failures become remediation results
            return self._api_failure(action, namespace, deployment, e)

    def _scale(
        self, deployment: str, namespace: str, replicas: int | None
    ) -> tuple[int, int]:
        scale = self._apps.read_namespaced_deployment_scale(
            name=deployment, namespace=namespace
        )
        current = scale.spec.replicas or 0
        target = replicas if replicas is not None else current + self.scale_step
        target = min(target, self.max_replicas)

        if target <= current:
            return current, current

        self._apps.patch_namespaced_deployment_scale(
            name=deployment, namespace=namespace, body={"spec": {"replicas": target}}
        )
        return current, target

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def wait_for_rollout(
        self, deployment: str | None, namespace: str | None = None
    ) -> dict[str, Any]:
        """Poll a deployment until every replica is updated and available.

        Issuing an action is not the same as fixing the problem, so an incident
        is only closed once the workload is actually healthy again.
        """
        namespace = namespace or self.default_namespace

        if not self.verify:
            return {"status": "skipped", "detail": "Rollout verification is disabled"}

        if not deployment:
            return {
                "status": "skipped",
                "detail": "No deployment to verify (the alert named a bare pod)"
            }

        connected = await asyncio.to_thread(self._connect)
        if not connected:
            return {
                "status": "unknown",
                "detail": f"Could not verify rollout: {self._load_error}"
            }

        deadline = asyncio.get_running_loop().time() + self.verify_timeout
        last_seen = "no status reported yet"

        while True:
            try:
                healthy, last_seen = await asyncio.to_thread(
                    self._rollout_healthy, deployment, namespace
                )
            except Exception as e:  # noqa: BLE001 - verification failure is reported, not raised
                return {
                    "status": "unknown",
                    "detail": f"Could not verify rollout: {getattr(e, 'reason', None) or str(e)}"
                }

            if healthy:
                return {
                    "status": "healthy",
                    "detail": f"Deployment '{deployment}' is healthy ({last_seen})"
                }

            if asyncio.get_running_loop().time() >= deadline:
                return {
                    "status": "timeout",
                    "detail": (
                        f"Deployment '{deployment}' was still not healthy after "
                        f"{self.verify_timeout}s ({last_seen})"
                    )
                }

            await asyncio.sleep(self.verify_interval)

    def _rollout_healthy(self, deployment: str, namespace: str) -> tuple[bool, str]:
        dep = self._apps.read_namespaced_deployment(
            name=deployment, namespace=namespace
        )
        desired = dep.spec.replicas or 0
        status = dep.status
        # As `kubectl rollout status`: until the controller has seen the new
        # spec, the status still describes the old one and looks healthy.
        generation = dep.metadata.generation or 0
        if (status.observed_generation or 0) < generation:
            return False, f"waiting for the controller to pick up generation {generation}"
        updated = status.updated_replicas or 0
        available = status.available_replicas or 0
        unavailable = status.unavailable_replicas or 0

        summary = (
            f"{available}/{desired} available, {updated} updated, "
            f"{unavailable} unavailable"
        )
        healthy = (
            desired > 0
            and updated == desired
            and available == desired
            and unavailable == 0
        )
        return healthy, summary
