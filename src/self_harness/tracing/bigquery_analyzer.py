"""BigQuery-backed trace analyzer: SQL analytics at corpus scale.

Runs the parameterized queries in `infra/bigquery/queries/` against the
telemetry dataset and assembles an AnalysisReport. The in-memory
`TraceAnalyzer` covers single-generation, single-process runs; this class is
the scale path — the same analytics expressed as SQL over the full span
corpus, where in-process aggregation stops being feasible.
"""

from __future__ import annotations

from pathlib import Path

from .analyzer import AnalysisReport

QUERIES_DIR = Path("infra/bigquery/queries")


class BigQueryTraceAnalyzer:
    def __init__(self, project_id: str, dataset: str, queries_dir: Path = QUERIES_DIR):
        from google.cloud import bigquery

        self.client = bigquery.Client(project=project_id)
        self.dataset = dataset
        self.queries_dir = Path(queries_dir)

    def analyze(self, harness_version: str) -> AnalysisReport:
        report = AnalysisReport(harness_version=harness_version)

        for row in self._run("failure_clusters.sql", harness_version):
            key = f"{row['error_class']}@depth_{row['depth_bucket']}"
            report.failure_clusters[key] = int(row["occurrences"])

        for row in self._run("tool_transition_graph.sql", harness_version):
            report.tool_transition_matrix.setdefault(row["prev_tool"], {})[
                row["next_tool"]
            ] = int(row["weight"])

        for row in self._run("convergence_by_generation.sql"):
            if row["harness_version"] == harness_version:
                report.total_runs = int(row["runs"])
                report.success_rate = float(row["success_rate"])
                report.mean_steps = float(row["mean_steps"])

        return report

    def _run(self, query_file: str, version: str | None = None) -> list[dict]:
        from google.cloud import bigquery

        sql = (self.queries_dir / query_file).read_text().replace(
            "harness_telemetry.", f"{self.dataset}."
        )
        params = (
            [bigquery.ScalarQueryParameter("version", "STRING", version)]
            if version is not None else []
        )
        job = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        return [dict(row) for row in job.result()]
