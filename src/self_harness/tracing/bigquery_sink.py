"""BigQuery streaming sink for execution traces.

Telemetry pipeline:

    Agent → TraceCollector → BigQuerySink (streaming insert) → harness_telemetry.execution_traces

BigQuery is the system of record for the analyzer's SQL workloads:
failure clustering, tool-calling-graph extraction, and cross-generation
convergence queries over millions of span rows.
"""

from __future__ import annotations

import logging

from .schema import TraceEvent

logger = logging.getLogger(__name__)


class BigQuerySink:
    def __init__(self, project_id: str, dataset: str, table: str):
        from google.cloud import bigquery  # Deferred: optional [gcp] dependency

        self.client = bigquery.Client(project=project_id)
        self.table_ref = f"{project_id}.{dataset}.{table}"

    def write(self, events: list[TraceEvent]) -> None:
        """Stream a batch of spans. Errors are logged, never raised into the
        agent loop — telemetry must not alter agent behavior."""
        if not events:
            return
        rows = [e.to_bq_row() for e in events]
        errors = self.client.insert_rows_json(self.table_ref, rows)
        if errors:
            logger.error("BigQuery insert errors (%d rows dropped): %s", len(errors), errors[:3])

    def fetch_generation_traces(self, harness_version: str, limit: int = 200_000) -> list[dict]:
        """Pull complete raw traces for one harness generation, ordered for
        replay. This is the corpus handed to the long-context mutation engine."""
        query = f"""
            SELECT run_id, kind, step, duration_s, payload
            FROM `{self.table_ref}`
            WHERE harness_version = @version
            ORDER BY run_id, step
            LIMIT {int(limit)}
        """
        from google.cloud import bigquery

        job = self.client.query(query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("version", "STRING", harness_version)]
        ))
        return [dict(row) for row in job.result()]
