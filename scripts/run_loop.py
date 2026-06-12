#!/usr/bin/env python3
"""Entry point: run K generations of the self-optimization loop.

Backend wiring (sandbox, trace sink, mutation engine) is selected from the
config profile — `configs/default.yaml` for local iteration,
`configs/gcp.yaml` for the BigQuery + Vertex AI + GKE stack.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from self_harness.mutation.engine import MutationEngine, NoopMutationEngine
from self_harness.orchestrator import Orchestrator, load_config
from self_harness.sandbox.base import Sandbox
from self_harness.sandbox.local_docker import LocalDockerSandbox


def build_mutation_engine(config: dict) -> MutationEngine:
    engine = config["mutation"]["engine"]
    if engine == "vertex_gemini":
        from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine

        gcp = config.get("gcp", {})
        return VertexGeminiMutationEngine(
            project_id=gcp.get("project_id", ""),
            location=gcp.get("location", "us-central1"),
            model=config["mutation"]["model"],
            max_trace_tokens=config["mutation"]["max_trace_tokens"],
            temperature=config["mutation"]["temperature"],
        )
    return NoopMutationEngine()


def build_sandbox(config: dict) -> Sandbox:
    if config["sandbox"]["backend"] == "gke":
        from self_harness.sandbox.gke_sandbox import GKESandbox

        gke = config["sandbox"]["gke"]
        return GKESandbox(
            cluster=gke["cluster"],
            namespace_prefix=gke["namespace_prefix"],
            job_template=gke["job_template"],
        )
    return LocalDockerSandbox()


def main() -> None:
    parser = argparse.ArgumentParser(description="self-harness optimization loop")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--artifacts-dir", default="artifacts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_config(args.config)

    Orchestrator(
        config=config,
        mutation_engine=build_mutation_engine(config),
        sandbox=build_sandbox(config),
        artifacts_dir=Path(args.artifacts_dir),
    ).run()


if __name__ == "__main__":
    main()
