-- Tool-calling transition graph for one harness generation.
-- Self-join over consecutive tool-call steps within each run yields the
-- edge list (prev_tool -> next_tool, weight) of the agent's behavioral graph.
-- Params: @version (harness_version)

WITH tool_calls AS (
  SELECT
    run_id,
    step,
    JSON_VALUE(payload, '$.tool') AS tool,
    LAG(JSON_VALUE(payload, '$.tool'))
      OVER (PARTITION BY run_id ORDER BY step) AS prev_tool
  FROM `harness_telemetry.execution_traces`
  WHERE harness_version = @version AND kind = 'tool_call'
)
SELECT
  prev_tool,
  tool AS next_tool,
  COUNT(*) AS weight,
  COUNT(DISTINCT run_id) AS runs
FROM tool_calls
WHERE prev_tool IS NOT NULL
GROUP BY prev_tool, next_tool
ORDER BY weight DESC;
