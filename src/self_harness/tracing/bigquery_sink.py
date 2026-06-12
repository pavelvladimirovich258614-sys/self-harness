"""BigQuery streaming sink for execution traces.

Telemetry pipeline:

    Agent → TraceCollector → BigQuerySink (streaming insert) → harness_telemetry.execution_traces

Delivery semantics: at-least-once with bounded retries. Failed batches are
retried with exponential backoff; batches that still fail are appended to a
local NDJSON dead-letter file for offline backfill (`bq load`), never
silently dropped. Telemetry errors are isolated from the agent loop —
observability must not alter agent behavior.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .schema import TraceEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0


class BigQuerySink:
    def __init__(
        self,
        project_id: str,
        dataset: str,
        table: str,
        dead_letter_path: str | Path = "traces/deadletter.ndjson",
    ):
        from google.cloud import bigquery  # Deferred: optional [gcp] dependency

        self.client = bigquery.Client(project=project_id)
        self.table_ref = f"{project_id}.{dataset}.{table}"
        self.dead_letter_path = Path(dead_letter_path)

    def write(self, events: list[TraceEvent]) -> None:
        if not events:
            return
        rows = [e.to_bq_row() for e in events]
        for attempt in range(MAX_RETRIES):
            try:
                errors = self.client.insert_rows_json(self.table_ref, rows)
                if not errors:
                    return
                logger.warning("BigQuery insert errors (attempt %d): %s", attempt + 1, errors[:3])
            except Exception as exc:  # noqa: BLE001 — transport errors are retryable
                logger.warning("BigQuery insert failed (attempt %d): %r", attempt + 1, exc)
            time.sleep(BACKOFF_BASE_S * 2**attempt)
        self._dead_letter(rows)

    def _dead_letter(self, rows: list[dict]) -> None:
        """Persist undeliverable rows for offline backfill via `bq load`."""
        self.dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
        with self.dead_letter_path.open("a") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
        logger.error(
            "BigQuery delivery failed after %d attempts; %d rows written to dead letter %s",
            MAX_RETRIES, len(rows), self.dead_letter_path,
        )

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
