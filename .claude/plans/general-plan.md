# AI Platform — Observability & Reliability Improvement Plan

## Context
We have a multi-tenant AI agent execution service with reliability, performance, and cost efficiency gaps. The goal is to instrument it with a production-grade observability stack, use that telemetry to surface and diagnose issues, fix the highest-impact ones, and document the system's production readiness posture.

---

## Step 1: Add Observability Stack
Set up the observability infrastructure and instrument the code.

- **Infrastructure**: Add Jaeger (tracing), Prometheus (metrics), and structured logging to `docker-compose.yml`
- **Tracing**: Integrate OpenTelemetry into `main.py`, `orchestrator.py`, `llm_client.py`, and `tool_executor.py` — one span per pipeline stage, with attributes (tenant_id, priority, task_id, retry count, token counts)
- **Metrics**: Add Prometheus counters/histograms for request rate, error rate, latency (by stage/tenant/priority), cache hit rate, concurrent tasks, token usage, retry counts
- **Logging**: Add structured JSON logging correlated to trace/span IDs
- **Endpoint**: Expose `GET /metrics` (Prometheus format)

**Files to modify**: `docker-compose.yml`, `src/main.py`, `src/orchestrator.py`, `src/llm_client.py`, `src/tool_executor.py`, `src/config.py`

---

## Step 2: Run Load Tests & Collect Evidence
Run `python -m tests.test_load` against the instrumented service and capture telemetry.

- Run a sustained load test (not just a short burst)
- Capture traces in Jaeger, metrics from Prometheus, and logs
- Export/screenshot key signals: latency distributions, error rates, cache behavior, per-tenant patterns, retry storms

---

## Step 3: Diagnose Issues & Write DIAGNOSIS.md
Analyze the telemetry to identify and document each issue.

Known candidate issues to investigate (from code inspection):
1. **Unbounded memory** — `_execution_log` and response cache grow forever → OOM risk
2. **Sequential tool execution** — 3 tools run one-by-one despite being independent → unnecessary latency
3. **Priority ignored** — priority field not used in scheduling or caching; urgent tasks may wait behind low-priority ones
4. **Cache key excludes priority** — high-priority tasks get stale low-priority cached results (or vice versa)
5. **Validation runs on failed summaries** — wastes LLM tokens on already-failed tasks
6. **Token cost accounting broken** — cached tasks report 0 tokens; cost constants unused
7. **Global rate limiter (not per-tenant)** — one noisy tenant can starve others
8. **Per-tenant lock serializes all requests** — even independent tasks from the same tenant queue up

Write `DIAGNOSIS.md` with: evidence (trace screenshots, metric values, log excerpts), root cause, discovery path, and proposed fix for each issue.

---

## Step 4: Fix Issues & Show Before/After
Implement fixes for the highest-impact issues (aim for 2–3), re-run the load test, and capture before/after telemetry.

Priority fixes (highest impact):
1. **Parallelize tool execution** — `asyncio.gather` in `tool_executor.py` (quick win, measurable latency drop)
2. **Fix cache eviction** — add TTL or LRU cap to the response cache in `main.py`
3. **Skip validation on failed summaries** — guard in `orchestrator.py`

Add before/after metric comparisons and trace screenshots to `DIAGNOSIS.md`.

---

## Step 5: Write Production Readiness Doc
Add a `PRODUCTION.md` (or section in README) covering:

- **SLIs/SLOs**: P99 latency < Xs, error rate < Y%, cache hit rate, token cost per task
- **Alerts**: error rate spike, latency breach, memory growth, retry storm, semaphore saturation
- **GCP/Kubernetes changes**: persistent task store (Cloud Spanner/Redis), horizontal scaling, distributed rate limiting, proper secrets management, health probes, PodDisruptionBudgets

---

## Step 6: Update README.md
- How to run the instrumented system (`docker compose up`)
- How to reproduce the load test
- How to view traces (Jaeger UI), metrics (Prometheus/Grafana), logs
- Observability design choices
- **AI Tool Usage** section
