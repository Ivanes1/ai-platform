# Step 1: Add Observability Stack

## Goal

Instrument the existing agent platform with distributed tracing, Prometheus metrics, structured logging, and a `/metrics` endpoint — without changing any business logic.

---

## 1. New infrastructure services (`docker-compose.yml`)

Add three services alongside `agent-service` and `mock-llm`:

| Service      | Image                           | Purpose                   | Port                         |
| ------------ | ------------------------------- | ------------------------- | ---------------------------- |
| `jaeger`     | `jaegertracing/all-in-one:1.57` | OTLP collector + trace UI | 4317 (OTLP gRPC), 16686 (UI) |
| `prometheus` | `prom/prometheus:v2.51.0`       | Metrics scraper           | 9090                         |
| `grafana`    | `grafana/grafana:10.4.2`        | Dashboards                | 3000                         |

Wire `agent-service` to Jaeger via env var `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317`.

Create `prometheus.yml` scrape config pointing at `agent-service:8080/metrics`.

---

## 2. New dependencies

Add to `requirements.txt` (and `pyproject.toml` under `[project.dependencies]`):

```
opentelemetry-sdk>=1.24
opentelemetry-api>=1.24
opentelemetry-exporter-otlp-proto-grpc>=1.24
opentelemetry-instrumentation-fastapi>=0.45b0
opentelemetry-instrumentation-httpx>=0.45b0
prometheus-client>=0.20
python-json-logger>=2.0
```

---

## 3. New file: `src/telemetry.py`

Central setup module imported once at app startup. Keeps instrumentation wiring out of business logic files.

**Responsibilities:**

- Initialize `TracerProvider` with OTLP gRPC exporter → Jaeger
- Set global `tracer` instance (`opentelemetry.trace.get_tracer("agent-service")`)
- Initialize Prometheus metrics registry with all counters/histograms/gauges
- Set up structured JSON logger (via `python-json-logger`) that injects `trace_id` and `span_id` into every log record
- Expose a `setup_telemetry(app: FastAPI)` function that: applies FastAPI auto-instrumentation, applies httpx auto-instrumentation

**Prometheus metrics defined here (used by all other modules via import):**

```python
# Counters
task_requests_total        # labels: tenant_id, priority, status
cache_hits_total           # labels: tenant_id
llm_calls_total            # labels: stage, http_status
llm_retries_total          # labels: reason (rate_limited | server_error | timeout)

# Histograms
task_duration_seconds      # labels: tenant_id, priority   (buckets: .1,.5,1,2,5,10,30)
pipeline_stage_duration_seconds  # labels: stage            (plan|execute|summarize|validate)
llm_call_duration_seconds  # labels: stage                  (same stages)
tool_execution_duration_seconds  # labels: tool_name

# Gauges
active_tasks               # (no labels) — tracks semaphore occupancy

# Counters (additional)
token_usage_total          # labels: tenant_id, token_type (prompt|completion) — Counter, not Gauge; tokens only accumulate

# Histograms (additional)
rate_limiter_wait_seconds  # (no labels) — time blocked waiting for a token-bucket slot before each LLM call
```

---

## 4. Modifications to `src/main.py`

### 4a. Startup/shutdown hooks

Call `setup_telemetry(app)` in the `@app.on_event("startup")` handler. Add a `@app.on_event("shutdown")` handler that calls `tracer_provider.shutdown()` to flush any in-flight spans before the process exits.

### 4b. `GET /metrics` endpoint

```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

### 4c. Instrument `create_task`

Wrap handler body in a manual span `"http.create_task"` with attributes:

- `task.id`, `task.tenant_id`, `task.priority`

Record cache hit/miss on `cache_hits_total`. Increment/decrement `active_tasks` gauge around `_guarded_execute()`. Record `task_requests_total` and `task_duration_seconds` at the end with final status.

### 4d. Structured log on each request outcome (info on success, error on failure/timeout)

---

## 5. Modifications to `src/orchestrator.py`

Wrap each pipeline stage in a child span, all nested under a parent `"task.execute"` span created at the top of `run_task`.

```
Span: task.execute            (attrs: task_id, tenant_id, priority)
  ├─ Span: task.plan          (attrs: prompt_length, prompt_tokens, completion_tokens)
  ├─ Span: task.tools         (attrs: tool_count)
  │    ├─ Span: tool.search
  │    ├─ Span: tool.database_lookup
  │    └─ Span: tool.calculator
  ├─ Span: task.summarize     (attrs: prompt_tokens, completion_tokens)
  └─ Span: task.validate      (attrs: prompt_tokens, completion_tokens)
```

Add span events on errors (e.g. `span.record_exception(e)`, `span.set_status(ERROR)`).

Record `pipeline_stage_duration_seconds` for each stage.

Emit one structured log line per stage completion: `{"event": "stage_complete", "stage": "plan", "task_id": ..., "trace_id": ..., "duration_ms": ...}`.

---

## 6. Modifications to `src/llm_client.py`

Create a span `"llm.call"` per **attempt** (not per logical call) with attributes:

- `llm.attempt` (0-indexed), `llm.max_tokens`, `llm.prompt_length`
- `llm.http_status` set on response or error

On each retry, increment `llm_retries_total` with label `reason` = `rate_limited` (429), `server_error` (500), or `timeout`.

Record `llm_call_duration_seconds` per attempt.

Increment `llm_calls_total` with the final `http_status`.

Log at WARNING level on each retry: `{"event": "llm_retry", "attempt": n, "reason": ..., "delay_ms": ..., "trace_id": ...}`.

---

## 7. Modifications to `src/tool_executor.py`

Wrap each `execute_tool` call in a span `"tool.{tool_name}"` with attribute `tool.name`.

Record `tool_execution_duration_seconds{tool_name=...}`.

---

## 8. Prometheus scrape config

New file `prometheus.yml`:

```yaml
global:
  scrape_interval: 10s

scrape_configs:
  - job_name: agent-service
    static_configs:
      - targets: ["agent-service:8080"]
```

---

## 9. Grafana provisioning (optional but included)

Add `grafana/provisioning/datasources/prometheus.yml` pointing at `http://prometheus:9090`.

Add a basic dashboard JSON with panels for:

- Request rate (by tenant, priority)
- Error rate
- P50/P95/P99 task latency
- Cache hit rate
- Active tasks gauge
- LLM retry rate

---

## Implementation order

1. Add `src/telemetry.py` (metrics + tracer definitions)
2. Update `requirements.txt` and `pyproject.toml`
3. Modify `src/config.py` — add `OTEL_EXPORTER_OTLP_ENDPOINT` constant
4. Modify `src/main.py` — startup hook, `/metrics`, span + metric calls
5. Modify `src/orchestrator.py` — stage spans + metrics + logs
6. Modify `src/llm_client.py` — attempt spans + retry metrics + logs
7. Modify `src/tool_executor.py` — per-tool spans + metrics
8. Update `docker-compose.yml` — Jaeger, Prometheus, Grafana services
9. Add `prometheus.yml` and Grafana provisioning files
10. `docker compose up --build` and verify `/metrics` returns data and Jaeger shows traces

---

## Acceptance criteria

- [ ] `GET /metrics` returns Prometheus-format text with all defined metrics
- [ ] A single `POST /tasks` call produces a trace in Jaeger with child spans for each pipeline stage and LLM attempt
- [ ] Log lines contain `trace_id` and `span_id` fields
- [ ] `prometheus` scrapes successfully (no "down" targets in Prometheus UI)
- [ ] Grafana dashboard loads and shows live data after running `python -m tests.test_load`
- [ ] Zero changes to business logic (all instrumentation is additive)
