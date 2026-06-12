-- Convergence curve across harness generations.
-- One row per harness version: success rate, step efficiency, latency.
-- The headline figure of the research program is this query plotted.

WITH outcomes AS (
  SELECT
    harness_version,
    run_id,
    CAST(JSON_VALUE(payload, '$.success') AS BOOL) AS success,
    CAST(JSON_VALUE(payload, '$.steps') AS INT64) AS steps
  FROM `harness_telemetry.execution_traces`
  WHERE kind = 'run_end'
),
latency AS (
  SELECT harness_version, run_id, SUM(duration_s) AS run_seconds
  FROM `harness_telemetry.execution_traces`
  WHERE kind IN ('model_call', 'tool_call')
  GROUP BY harness_version, run_id
)
SELECT
  o.harness_version,
  COUNT(*) AS runs,
  AVG(CAST(o.success AS INT64)) AS success_rate,
  AVG(o.steps) AS mean_steps,
  APPROX_QUANTILES(o.steps, 100)[OFFSET(50)] AS median_steps,
  AVG(l.run_seconds) AS mean_run_seconds
FROM outcomes o
JOIN latency l USING (harness_version, run_id)
GROUP BY o.harness_version
-- Version names embed the generation index (g0000-, g0001-, ...):
ORDER BY o.harness_version;
