"""OCI-container sandbox for laptop-scale iteration.

Implements the same contract as the GKE backend so that the orchestrator is
backend-agnostic. Isolation: read-only artifact mount, dropped capabilities,
no network, pids/cpu/memory limits, hard wall-clock timeout enforced from
the host side.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from .base import Sandbox, SandboxLimits, SandboxResult

SANDBOX_IMAGE = "self-harness/evaluator:latest"


class LocalDockerSandbox(Sandbox):
    def evaluate(
        self,
        harness_version: str,
        artifacts_dir: str,
        suite: str,
        episodes_per_task: int,
        limits: SandboxLimits,
    ) -> SandboxResult:
        results_dir = Path(tempfile.mkdtemp(prefix="sh-results-"))
        cmd = [
            "docker", "run", "--rm",
            "--network", "none" if limits.network == "deny" else "bridge",
            "--cpus", limits.cpu,
            "--memory", limits.memory,
            "--pids-limit", "256",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:size=256m",
            "-v", f"{artifacts_dir}:/artifacts:ro",
            "-v", f"{results_dir}:/results",
            SANDBOX_IMAGE,
            "python", "-m", "self_harness.evaluation.pipeline",
            "--harness-version", harness_version,
            "--artifacts-dir", "/artifacts",
            "--suite", suite,
            "--episodes-per-task", str(episodes_per_task),
            "--output", "/results/scores.json",
        ]

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=limits.wall_clock_s
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                completed=False,
                wall_clock_s=limits.wall_clock_s,
                violation="wall_clock_exceeded",
                logs_tail=(exc.stdout or b"").decode(errors="replace")[-4000:]
                if isinstance(exc.stdout, bytes) else str(exc.stdout or "")[-4000:],
            )

        wall = time.monotonic() - t0
        scores_file = results_dir / "scores.json"
        if proc.returncode != 0 or not scores_file.exists():
            return SandboxResult(
                completed=False,
                wall_clock_s=wall,
                violation=f"exit_code_{proc.returncode}",
                logs_tail=(proc.stderr or "")[-4000:],
            )

        return SandboxResult(
            completed=True,
            per_task_scores=json.loads(scores_file.read_text()),
            wall_clock_s=wall,
            logs_tail=(proc.stdout or "")[-4000:],
        )
