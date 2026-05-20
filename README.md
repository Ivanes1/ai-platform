# AI Platform

A multi-tenant agent execution service that accepts task descriptions, runs them through a plan → execute → summarise → validate pipeline backed by an LLM, and returns structured results.

## Services

| Service                 | Port  | Description                                              |
| ----------------------- | ----- | -------------------------------------------------------- |
| Agent Execution Service | 8080  | HTTP API for submitting and querying tasks               |
| Mock LLM Server         | 8081  | Simulated inference endpoint (intentional unreliability) |
| Jaeger                  | 16686 | Distributed tracing UI                                   |
| Prometheus              | 9090  | Metrics scrape target and query UI                       |
| Grafana                 | 3000  | Pre-provisioned dashboards (anonymous access enabled)    |

## Quickstart

```bash
docker compose up --build
```

Wait for all services to report healthy, then submit a task:

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_description": "Summarise Q3 revenue", "tenant_id": "acme", "priority": "normal"}'
```

Retrieve the result by task ID:

```bash
curl http://localhost:8080/tasks/<task_id>
```

Check service health:

```bash
curl http://localhost:8080/health
```

## Reproducing the Load Test

The load test sends 250 requests across 3 tenants (`tenant-alpha`, `tenant-beta`, `tenant-gamma`) with 15 concurrent clients. It runs for approximately 10–12 minutes and exercises all priority levels (`urgent`, `normal`, `low`).

Requirements: the full stack must be running (`docker compose up`).

```bash
# Install dependencies (uv required: https://github.com/astral-sh/uv)
uv sync

# Run the load test
python -m tests.test_load
```

The script prints per-request status, token usage, latency, and a summary table at the end. Key parameters in `tests/test_load.py`:

| Parameter        | Value | Meaning                                       |
| ---------------- | ----- | --------------------------------------------- |
| `TOTAL_REQUESTS` | 250   | Number of task submissions                    |
| `CONCURRENCY`    | 15    | Simultaneous in-flight requests from the test |
| `TENANTS`        | 3     | Distinct tenant IDs                           |
| Repeat rate      | 30%   | Fraction of requests that reuse a description |

The 30% repeat rate produces a realistic cache hit pattern (~20% observed hit rate in our runs).

## Viewing Telemetry

### Traces — Jaeger UI

Open `http://localhost:16686`.

- Select service `agent-execution-service`
- Each task creates a root span `task.execute` with child spans for each pipeline stage: `task.plan`, `task.tools`, `task.summarise`, `task.validate`
- LLM attempts appear as nested `llm.call` spans with attributes `llm.attempt`, `llm.status_code`, `llm.tokens.prompt`, `llm.tokens.completion`
- Tool execution spans (`tool.search`, `tool.database_lookup`, `tool.calculator`) overlap in time — confirming parallel execution
- Every span carries `tenant_id`, `priority`, and `task_id` attributes for filtering
- To find a specific request, search by `task_id` tag (returned in the `POST /tasks` response)

### Metrics — Prometheus

Open `http://localhost:9090`.

Key metrics and example queries:

```promql
# Request rate by status
rate(task_requests_total[1m])

# Error rate
rate(task_requests_total{status="failed"}[5m]) / rate(task_requests_total[5m])

# P95 end-to-end latency
histogram_quantile(0.95, sum(rate(task_duration_seconds_bucket[5m])) by (le))

# Active concurrency (0–5)
active_tasks

# Cache hit rate
rate(cache_hits_total[5m]) / rate(task_requests_total[5m])

# LLM retry rate
rate(llm_retries_total[5m])

# Tool execution time by tool
rate(tool_execution_duration_seconds_sum[5m]) / rate(tool_execution_duration_seconds_count[5m])
```

The raw metrics endpoint is also available at `http://localhost:8080/metrics` (Prometheus text format).

### Dashboards — Grafana

Open `http://localhost:3000` (no login required — anonymous admin access).

A pre-provisioned dashboard **Agent Service Overview** is available under Dashboards. It shows:

- Request rate and error rate over time
- P50/P95/P99 latency percentiles
- Active task concurrency
- LLM call duration and retry rate
- Cache hit rate
- Token usage by tenant
- Pipeline stage duration breakdown

### Logs

Logs are emitted as structured JSON to stdout, correlated with the active trace and span IDs:

```bash
# Follow logs from the agent service
docker compose logs -f agent-service
```

Each log line includes `trace_id` and `span_id` fields. To correlate a log line with a Jaeger trace, copy the `trace_id` value and paste it into the Jaeger search box.

Example log entry:

```json
{
  "timestamp": "2026-05-19T08:21:34.123Z",
  "level": "info",
  "message": "Task completed",
  "task_id": "a1b2c3d4-...",
  "tenant_id": "tenant-alpha",
  "priority": "normal",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7"
}
```

## API

### `POST /tasks`

Submit a new task. Returns immediately with a task ID; use `GET /tasks/{task_id}` to poll for the result.

| Field              | Type   | Required | Description                            |
| ------------------ | ------ | -------- | -------------------------------------- |
| `task_description` | string | yes      | Natural language task                  |
| `tenant_id`        | string | yes      | Tenant identifier                      |
| `priority`         | string | no       | `urgent`, `normal` (default), or `low` |

### `GET /tasks/{task_id}`

Returns the task result. `status` is one of `pending`, `running`, `completed`, or `failed`.

### `GET /health`

Returns `{"status": "ok"}`. Used as a liveness probe.

### `GET /metrics`

Prometheus-format metrics. Scraped automatically by Prometheus; also queryable directly.

## Environment Variables

| Variable                      | Default                | Description                 |
| ----------------------------- | ---------------------- | --------------------------- |
| `LLM_SERVER_URL`              | `http://mock-llm:8081` | Inference endpoint base URL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://jaeger:4317`   | OTLP gRPC exporter target   |

## Observability Design

### Stack choice

The observability stack uses three well-established open-source tools that cover the three telemetry pillars (traces, metrics, logs) with minimal operational overhead in a Docker Compose environment:

- **OpenTelemetry + Jaeger** for distributed tracing. OTel's vendor-neutral SDK means the instrumentation code is portable — swapping Jaeger for Cloud Trace in production requires only changing the exporter, not the instrumentation. Jaeger's all-in-one image makes local setup a single-service addition to `docker-compose.yml`.
- **Prometheus + Grafana** for metrics and dashboards. Prometheus's pull model integrates cleanly with FastAPI via the `prometheus-client` library. A single `GET /metrics` endpoint is all the service needs to expose; Prometheus handles scheduling, retention, and alerting. Grafana with provisioned datasources and dashboards means the observability UI is reproducible without manual setup steps.
- **Structured JSON logging** (stdlib `logging` + custom `JsonFormatter`) rather than a third log aggregation service. In the Docker Compose environment, logs are written to stdout and visible via `docker compose logs`. The structured format (with `trace_id` and `span_id` fields) makes log-to-trace correlation possible without a dedicated log backend like Loki or Cloud Logging — those are production additions documented in `PRODUCTION.md`.

### Instrumentation strategy

Every meaningful unit of work gets its own span:

- `task.execute` — root span for the full pipeline, carries `tenant_id`, `priority`, `task_id`, `cache_hit`
- `task.plan`, `task.tools`, `task.summarise`, `task.validate` — one span per pipeline stage, carries stage-level duration and token counts
- `llm.call` — one span per LLM HTTP call (including retries), carries attempt number, status code, and per-call token counts
- `tool.<name>` — one span per tool execution, making the parallel vs. sequential comparison directly visible in Jaeger

Metrics follow the RED method (Rate, Errors, Duration) at the task level, plus USE method (Utilisation, Saturation, Errors) for the concurrency semaphore. Every histogram uses explicit bucket boundaries tuned to the expected latency range (0.1s–30s for tasks, 0.01s–10s for LLM calls) rather than default buckets, so P95/P99 quantiles are accurate.

All metrics carry `tenant_id` and `priority` labels so every aggregate can be sliced to expose per-tenant and per-priority patterns — a necessary capability for diagnosing the fairness issues documented in `DIAGNOSIS.md`.

### What the instrumentation revealed

The observability stack directly enabled discovery of all five issues documented in `DIAGNOSIS.md`:

- The `active_tasks` gauge plateauing at 3 (not 5) was the first signal pointing to Issue 1
- Zero-token failed requests in the per-tenant metrics breakdown surfaced Issue 2 without any code inspection
- Jaeger traces showing stacked (not overlapping) tool spans confirmed Issue 3
- Prometheus error-rate-by-tenant labels exposed the 93% concentration on tenant-gamma for Issue 4
- `process_resident_memory_bytes` trending upward identified Issue 5

---

## AI Tool Usage

### Tools used

**Claude Code** was used as the primary engineering assistant throughout all tasks. Opus 4.6 was used for thinking/planning and Sonnet for execution.

### Setup

- Created a detailed `CLAUDE.md` with project architecture, commands, and design constraints
- Created a code writing skill for coding style consistency
- Created a general plan with a brief description of every step

### Workflow per step

Each step followed this process:

1. **Plan** — create a plan for the step, refine it, and save it to the `.claude/plans/` directory
2. **Implement** — execute the plan using Claude Code
3. **Manual verification** — verify everything was done correctly and make manual changes as needed
4. **AI verification** — in a separate session, ask Claude to verify that everything was done correctly and nothing was missed
5. **Next step** — proceed to the next step

### Pro tips

- Use Opus (4.6 is better than 4.7 imo) for thinking and Sonnet for execution
- Ask Claude to point you in the right direction when viewing logs, traces, and metrics
- Save logs in raw format (JSON) for Claude to easily check
- Ask Claude to start with something verbose and detailed, then reduce it as needed
- Start a new session or compact the current one to avoid context rot/overflow/quota limits
