"""GKE sandbox: one ephemeral namespace + Job per candidate harness.

Isolation profile (see infra/gke/sandbox-job.yaml):
  * gVisor RuntimeClass — user-space kernel between candidate code and node;
  * no service-account token automounted — zero GCP credentials in-pod;
  * NetworkPolicy deny-all egress except the model endpoint;
  * resource requests == limits (Guaranteed QoS) + activeDeadlineSeconds.

Lifecycle: create namespace → apply NetworkPolicy → submit Job with the
candidate artifact baked in via ConfigMap → poll to completion → harvest the
scores from the terminated pod's logs (the evaluator prints them behind the
`##SCORES##` marker) → delete the namespace (deleting everything in it).

Result harvesting via pod logs is deliberate: the candidate pod has no
service-account token, so it cannot write ConfigMaps or call any API to
report results. Logs are the only channel that requires zero credentials
on the untrusted side.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from .base import Sandbox, SandboxLimits, SandboxResult

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 15


class GKESandbox(Sandbox):
    def __init__(self, cluster: str, namespace_prefix: str = "mutation-",
                 job_template: str = "infra/gke/sandbox-job.yaml"):
        from kubernetes import client, config

        config.load_kube_config()  # In-cluster: config.load_incluster_config()
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()
        self.cluster = cluster
        self.namespace_prefix = namespace_prefix
        self.job_template = job_template

    def evaluate(
        self,
        harness_version: str,
        artifacts_dir: str,
        suite: str,
        episodes_per_task: int,
        limits: SandboxLimits,
        seed: int = 0,
    ) -> SandboxResult:
        namespace = f"{self.namespace_prefix}{uuid.uuid4().hex[:8]}"
        t0 = time.monotonic()
        try:
            self._create_namespace(namespace)
            self._mount_candidate(namespace, harness_version, artifacts_dir)
            self._submit_job(namespace, harness_version, suite, episodes_per_task, limits, seed)
            return self._await_result(namespace, limits, t0)
        finally:
            self._teardown(namespace)

    # -- lifecycle steps -------------------------------------------------

    def _create_namespace(self, namespace: str) -> None:
        from kubernetes import client

        self.core.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(
                name=namespace,
                labels={"app": "self-harness", "role": "mutation-sandbox"},
            ))
        )
        self._apply_network_policies(namespace)

    def _apply_network_policies(self, namespace: str) -> None:
        """Apply the deny-all + model-endpoint-allowlist policies from the
        template BEFORE the Job is admitted — the pod must never be schedulable
        in a namespace without egress restrictions."""
        import yaml
        from kubernetes import client

        networking = client.NetworkingV1Api()
        with open(self.job_template) as f:
            for doc in yaml.safe_load_all(f):
                if doc.get("kind") == "NetworkPolicy":
                    networking.create_namespaced_network_policy(namespace, doc)

    def _mount_candidate(self, namespace: str, version: str, artifacts_dir: str) -> None:
        """Ship the candidate harness artifact into the namespace as a
        ConfigMap; the Job mounts it read-only at /artifacts."""
        from pathlib import Path

        from kubernetes import client

        version_dir = Path(artifacts_dir) / version
        data = {p.name: p.read_text() for p in version_dir.iterdir() if p.is_file()}
        self.core.create_namespaced_config_map(
            namespace,
            client.V1ConfigMap(metadata=client.V1ObjectMeta(name="candidate"), data=data),
        )

    def _submit_job(self, namespace: str, version: str, suite: str,
                    episodes: int, limits: SandboxLimits, seed: int) -> None:
        import yaml

        with open(self.job_template) as f:
            job = next(doc for doc in yaml.safe_load_all(f) if doc.get("kind") == "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["args"] = [
            "--harness-version", version,
            "--artifacts-dir", "/artifacts",
            "--suite", suite,
            "--episodes-per-task", str(episodes),
            "--seed", str(seed),
            "--output", "/results/scores.json",
        ]
        container["resources"] = {
            "requests": {"cpu": limits.cpu, "memory": limits.memory},
            "limits": {"cpu": limits.cpu, "memory": limits.memory},
        }
        job["spec"]["activeDeadlineSeconds"] = limits.wall_clock_s
        self.batch.create_namespaced_job(namespace, job)

    def _await_result(self, namespace: str, limits: SandboxLimits, t0: float) -> SandboxResult:
        deadline = t0 + limits.wall_clock_s + 120  # Grace for scheduling
        while time.monotonic() < deadline:
            status = self.batch.read_namespaced_job_status("evaluator", namespace).status
            if status.succeeded:
                scores = self._harvest_scores(namespace)
                return SandboxResult(
                    completed=True,
                    per_task_scores=scores,
                    wall_clock_s=time.monotonic() - t0,
                )
            if status.failed:
                return SandboxResult(
                    completed=False,
                    wall_clock_s=time.monotonic() - t0,
                    violation="job_failed",
                    logs_tail=self._pod_logs_tail(namespace),
                )
            time.sleep(POLL_INTERVAL_S)
        return SandboxResult(
            completed=False,
            wall_clock_s=time.monotonic() - t0,
            violation="wall_clock_exceeded",
        )

    def _harvest_scores(self, namespace: str) -> dict[str, float]:
        """Parse per-task scores from the terminated pod's logs. The evaluator
        prints them behind SCORES_MARKER; the credential-free pod has no other
        channel to report results."""
        from ..evaluation.pipeline import SCORES_MARKER

        logs = self._pod_logs_tail(namespace, tail_lines=2000)
        for line in reversed(logs.splitlines()):
            if line.startswith(SCORES_MARKER):
                return json.loads(line[len(SCORES_MARKER):])
        logger.error("no scores marker found in evaluator logs for %s", namespace)
        return {}

    def _pod_logs_tail(self, namespace: str, tail_lines: int = 80) -> str:
        pods = self.core.list_namespaced_pod(namespace, label_selector="job-name=evaluator")
        if not pods.items:
            return ""
        try:
            return self.core.read_namespaced_pod_log(
                pods.items[0].metadata.name, namespace, tail_lines=tail_lines
            )
        except Exception:  # noqa: BLE001 — logs are best-effort diagnostics
            return ""

    def _teardown(self, namespace: str) -> None:
        try:
            self.core.delete_namespace(namespace)
        except Exception:  # noqa: BLE001
            logger.exception("failed to delete sandbox namespace %s", namespace)
