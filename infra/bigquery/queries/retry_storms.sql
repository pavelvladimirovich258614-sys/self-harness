-- Retry-storm detection: runs where the same tool is called with identical
-- arguments 3+ times consecutively — the signature of a harness retry policy
-- masking a deterministic failure. Prime mutation target.
-- Params: @version (harness_version)

WITH calls AS (
  SELECT
    run_id,
    step,
    JSON_VALUE(payload, '$.tool') AS tool,
    TO_JSON_STRING(JSON_QUERY(payload, '$.args')) AS args,
    LAG(JSON_VALUE(payload, '$.tool'), 1)
      OVER (PARTITION BY run_id ORDER BY step) AS prev_tool,
    LAG(TO_JSON_STRING(JSON_QUERY(payload, '$.args')), 1)
      OVER (PARTITION BY run_id ORDER BY step) AS prev_args
  FROM `harness_telemetry.execution_traces`
  WHERE harness_version = @version AND kind = 'tool_call'
),
repeats AS (
  SELECT run_id, step, tool,
         COUNTIF(tool = prev_tool AND args = prev_args)
           OVER (PARTITION BY run_id ORDER BY step
                 ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS repeat_window
  FROM calls
)
SELECT run_id, MIN(step) AS storm_start, tool, COUNT(*) AS storm_length
FROM repeats
WHERE repeat_window >= 2
GROUP BY run_id, tool
ORDER BY storm_length DESC;
