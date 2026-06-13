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

from .schema import SpanKind, TraceEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0


def record_security_incident(
    sink,
    *,
    lineage_id: str,
    harness_version: str,
    generation: int,
    security_explanation: str,
    hypothesis: str = "",
    detail: dict | None = None,
) -> TraceEvent:
    """Write one Active Defense security incident to the telemetry sink.

    The incident is a SECURITY_ALERT span streamed through the same path as
    every other event, so it lands in the `execution_traces` table and is
    queryable as the agent SIEM feed (see
    `infra/bigquery/queries/security_incidents.sql`). `lineage_id` ties the
    incident to the rejected candidate; the offending mutated code is NOT
    stored — only the engine's explanation and metadata — so a hostile payload
    is never persisted verbatim.

    Works with any TraceSink (BigQuerySink in production, MemorySink in tests),
    so the Active Defense path is uniform across backends.
    """
    event = TraceEvent(
        run_id=lineage_id,
        harness_version=harness_version,
        kind=SpanKind.SECURITY_ALERT,
        payload={
            "event_type": "security_alert",
            "lineage_id": lineage_id,
            "generation": generation,
            "security_explanation": security_explanation,
            "hypothesis": hypothesis,
            "action": "fast_fail_before_sandbox",
            **(detail or {}),
        },
    )
    sink.write([event])
    return event


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
