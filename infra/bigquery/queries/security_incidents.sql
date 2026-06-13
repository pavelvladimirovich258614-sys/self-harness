-- Agent SIEM feed: Active Defense security incidents.
-- One row per indirect-prompt-injection fast-fail. Drives alerting and
-- post-incident investigation. The offending mutated code is never stored —
-- only the engine's explanation and lineage metadata.

SELECT
  timestamp,
  TIMESTAMP_SECONDS(CAST(timestamp AS INT64)) AS incident_time,
  JSON_VALUE(payload, '$.lineage_id')           AS lineage_id,
  harness_version                               AS parent_harness,
  CAST(JSON_VALUE(payload, '$.generation') AS INT64) AS generation,
  JSON_VALUE(payload, '$.security_explanation') AS security_explanation,
  JSON_VALUE(payload, '$.hypothesis')           AS hypothesis,
  JSON_VALUE(payload, '$.action')               AS action
FROM `harness_telemetry.execution_traces`
WHERE kind = 'security_alert'
ORDER BY timestamp DESC;
