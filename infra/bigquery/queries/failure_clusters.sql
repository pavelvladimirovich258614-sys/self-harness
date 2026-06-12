-- Failure clustering for one harness generation.
-- Groups ERROR spans and tool-level failures by error class and step-depth
-- bucket; output feeds the mutation engine's evidence base.
-- Params: @version (harness_version)

WITH errors AS (
  SELECT
    run_id,
    step,
    REGEXP_EXTRACT(JSON_VALUE(payload, '$.error'), r'^([A-Za-z_.]+)') AS error_class,
    CAST(FLOOR(step / 10) * 10 AS INT64) AS depth_bucket
  FROM `harness_telemetry.execution_traces`
  WHERE harness_version = @version AND kind = 'error'

  UNION ALL

  SELECT
    run_id,
    step,
    CONCAT('tool_error:', JSON_VALUE(payload, '$.tool')) AS error_class,
    CAST(FLOOR(step / 10) * 10 AS INT64) AS depth_bucket
  FROM `harness_telemetry.execution_traces`
  WHERE harness_version = @version
    AND kind = 'tool_call'
    AND JSON_VALUE(payload, '$.result_preview') LIKE '%error%'
)
SELECT
  error_class,
  depth_bucket,
  COUNT(*) AS occurrences,
  COUNT(DISTINCT run_id) AS affected_runs,
  ARRAY_AGG(STRUCT(run_id, step) ORDER BY step LIMIT 5) AS exemplars
FROM errors
GROUP BY error_class, depth_bucket
ORDER BY occurrences DESC;
