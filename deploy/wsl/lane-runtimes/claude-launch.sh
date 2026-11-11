#!/usr/bin/env bash
# Native Claude, with the monitoring-usage docs' OTLP environment (read 2026-11-08).
# Carried from Sextant (see ORIGINS.md), which activates it only with its redaction collector.
# Like Sextant's dsh-launch.sh, this changes only the child's environment, never a runtime home.
set -euo pipefail
set +x
# Unset rather than assign false: assistant-response logging falls back to the prompt flag.
unset OTEL_LOG_USER_PROMPTS OTEL_LOG_ASSISTANT_RESPONSES OTEL_LOG_RAW_API_BODIES OTEL_LOG_TOOL_CONTENT
unset BETA_TRACING_ENDPOINT
# Per-signal settings override the generic endpoint/protocol; inherited remote destinations
# and credentials must not redirect a signal or reach the local, unauthenticated receiver.
unset OTEL_EXPORTER_OTLP_HEADERS
for signal in METRICS LOGS TRACES; do
  unset "OTEL_EXPORTER_OTLP_${signal}_ENDPOINT" "OTEL_EXPORTER_OTLP_${signal}_PROTOCOL" \
    "OTEL_EXPORTER_OTLP_${signal}_HEADERS"
done
export DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
export CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export ENABLE_BETA_TRACING_DETAILED=0
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
export OTEL_LOG_TOOL_DETAILS=1
# Attribute values cannot contain OTel's comma/equal delimiters. Omit unavailable joins;
# an absent lane/PR is unknown, never an invented association.
export OTEL_RESOURCE_ATTRIBUTES=sextant.runtime=claude-code
for field in LANE PR; do
  name="SEXTANT_$field"
  value=${!name-}
  case "$value" in *[!a-zA-Z0-9_./:-]*) echo "claude-launch: invalid $name" >&2; exit 2 ;; esac
  [[ -z "$value" ]] || OTEL_RESOURCE_ATTRIBUTES+=",sextant.${field,,}=$value"
done
# Cumulative for the store's Prometheus (code.claude.com/docs/en/monitoring-usage, read 2026-11-10).
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
exec claude "$@"
