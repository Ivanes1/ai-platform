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

Requirements: full stack running (`docker compose up`).

```bash
uv sync                      # install dependencies (uv required)
python -m tests.test_load    # 250 requests, 15 concurrent, 3 tenants, ~10-12 min
```

Key parameters: 250 total requests, 15 concurrent clients, 3 tenants (`tenant-alpha`, `tenant-beta`, `tenant-gamma`), 30% repeat rate (produces ~20% cache hit rate). Exercises all priority levels (`urgent`, `normal`, `low`).

## Viewing Telemetry

### Traces — Jaeger

Open `http://localhost:16686`. Select service `agent-execution-service`.

Each task creates a root span `task.execute` with child spans per pipeline stage (`task.plan`, `task.tools`, `task.summarise`, `task.validate`). LLM attempts appear as nested `llm.call` spans. Every span carries `tenant_id`, `priority`, and `task_id` attributes for filtering.

### Metrics — Prometheus

Open `http://localhost:9090`. Raw metrics also available at `http://localhost:8080/metrics`.

Example queries:

```promql
rate(task_requests_total{status="failed"}[5m]) / rate(task_requests_total[5m])  # error rate
histogram_quantile(0.95, sum(rate(task_duration_seconds_bucket[5m])) by (le))   # P95 latency
active_tasks                                                                    # concurrency gauge
rate(cache_hits_total[5m]) / rate(task_requests_total[5m])                      # cache hit rate
```

### Dashboards — Grafana

Open `http://localhost:3000` (no login required). A pre-provisioned **Agent Service Overview** dashboard shows request rate, error rate, latency percentiles, concurrency, LLM retries, cache hits, token usage by tenant, and pipeline stage breakdown.

### Logs

```bash
docker compose logs -f agent-service
```

Structured JSON with `trace_id` and `span_id` fields for log-to-trace correlation.

## API

| Endpoint             | Description                                                       |
| -------------------- | ----------------------------------------------------------------- |
| `POST /tasks`        | Submit a task (`task_description`, `tenant_id`, `priority`)       |
| `GET /tasks/{id}`    | Poll result — `pending`, `running`, `completed`, or `failed`      |
| `GET /health`        | Liveness probe                                                    |
| `GET /metrics`       | Prometheus-format metrics                                         |

## Observability Design

### Stack

- **OpenTelemetry + Jaeger** — vendor-neutral distributed tracing; swapping Jaeger for Cloud Trace requires only changing the exporter
- **Prometheus + Grafana** — pull-based metrics with pre-provisioned dashboards; reproducible without manual setup
- **Structured JSON logging** — stdout with `trace_id`/`span_id` fields for log-to-trace correlation without a dedicated log backend

### Instrumentation strategy

Spans per unit of work: `task.execute` (root) → `task.plan`, `task.tools`, `task.summarise`, `task.validate` → `llm.call` (per attempt) → `tool.<name>` (per tool). All spans carry `tenant_id`, `priority`, `task_id`.

Metrics follow the RED method (Rate, Errors, Duration) at the task level with explicit histogram buckets tuned to expected latency ranges. All metrics carry `tenant_id` and `priority` labels for per-tenant slicing.

### What the instrumentation revealed

The observability stack directly enabled discovery of all five issues documented in `DIAGNOSIS.md`:

- `active_tasks` gauge plateauing at 3 (not 5) → Issue 1 (concurrency underutilization)
- Zero-token failed requests per tenant → Issue 2 (timeout placement)
- Stacked (not overlapping) tool spans in Jaeger → Issue 3 (sequential tool execution)
- Error rate 93% concentrated on one tenant → Issue 4 (unfair retry behavior)
- `process_resident_memory_bytes` trending upward → Issue 5 (unbounded caches)

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
