"""Central telemetry setup: OpenTelemetry tracing, Prometheus metrics, structured logging."""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram
from pythonjsonlogger import jsonlogger

# ── Prometheus metrics ──────────────────────────────────────────────────────

task_requests_total = Counter(
    "task_requests_total",
    "Total task requests",
    ["tenant_id", "priority", "status"],
)
cache_hits_total = Counter(
    "cache_hits_total",
    "Cache hits for identical (tenant, description) pairs",
    ["tenant_id"],
)
llm_calls_total = Counter(
    "llm_calls_total",
    "Total LLM call completions",
    ["stage", "http_status"],
)
llm_retries_total = Counter(
    "llm_retries_total",
    "LLM retry attempts",
    ["reason"],
)
task_duration_seconds = Histogram(
    "task_duration_seconds",
    "End-to-end task duration in seconds",
    ["tenant_id", "priority"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)
pipeline_stage_duration_seconds = Histogram(
    "pipeline_stage_duration_seconds",
    "Duration of each pipeline stage in seconds",
    ["stage"],
)
llm_call_duration_seconds = Histogram(
    "llm_call_duration_seconds",
    "Duration of each LLM call attempt in seconds",
    ["stage"],
)
tool_execution_duration_seconds = Histogram(
    "tool_execution_duration_seconds",
    "Duration of each tool execution in seconds",
    ["tool_name"],
)
active_tasks = Gauge(
    "active_tasks",
    "Number of tasks currently executing through the pipeline",
)
token_usage_total = Counter(
    "token_usage_total",
    "Cumulative token usage (prompt and completion)",
    ["tenant_id", "token_type"],
)
rate_limiter_wait_seconds = Histogram(
    "rate_limiter_wait_seconds",
    "Time blocked waiting for a token-bucket slot before each LLM call",
)

# ── Tracer ─────────────────────────────────────────────────────────────────

_tracer_provider: TracerProvider | None = None


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("agent-service")


# ── Structured logging ─────────────────────────────────────────────────────


class _TraceContextFilter(logging.Filter):
    """Inject active span's trace_id and span_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16
        return True


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter(  # type: ignore[attr-defined]
            fmt="%(asctime)s %(name)s %(levelname)s %(message)s %(trace_id)s %(span_id)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(_TraceContextFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# ── Public setup API ───────────────────────────────────────────────────────


def setup_telemetry(app) -> None:
    """Wire up tracing, metrics, and logging. Call once at app startup."""
    global _tracer_provider

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    _tracer_provider = TracerProvider()
    _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_tracer_provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

    _setup_logging()


def shutdown_telemetry() -> None:
    """Flush in-flight spans before process exit."""
    if _tracer_provider:
        _tracer_provider.shutdown()
