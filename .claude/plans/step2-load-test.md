# Step 2: Run Load Tests & Collect Evidence

## Goal

Execute a sustained load test against the instrumented service, capture comprehensive telemetry (traces, metrics, logs), and document baseline performance characteristics. This evidence will inform Step 3 (diagnosis) and Step 4 (fixes).

---

## Prerequisites

- Step 1 completed: observability stack fully operational
- All services running: `docker compose up --build`
- Verify endpoints are live:
  - Agent service: http://localhost:8080/health
  - Jaeger UI: http://localhost:16686
  - Prometheus UI: http://localhost:9090
  - Grafana UI: http://localhost:3000
  - Metrics endpoint: http://localhost:8080/metrics

---

## 1. Review existing load test

Read `tests/test_load.py` to understand:

- Number of concurrent clients
- Request pattern (tenants, priorities, task descriptions)
- Duration
- Expected behavior

If the test is too short (< 60s), consider extending it to allow metrics to stabilize and patterns to emerge.

---

## 2. Run the load test

```bash
# Start the platform
docker compose up --build -d

# Wait for services to be ready (20-30 seconds)
sleep 30

# Run the load test
python -m tests.test_load
```

Let the test run to completion. Monitor system behavior during the run:

- Check Docker logs: `docker compose logs -f agent-service`
- Watch metrics: http://localhost:8080/metrics (refresh periodically)
- Observe active traces appearing in Jaeger

---

## 3. Capture evidence immediately after test completes

### 3a. Jaeger traces

Open http://localhost:16686 and capture:

1. **Service overview**
   - Screenshot of the service list showing `agent-service` with trace count
   - File: `evidence/jaeger-service-overview.png`

2. **Sample successful trace**
   - Search for traces with no errors, operation `http.create_task`
   - Expand a trace showing full span hierarchy: `task.execute` → `task.plan`, `task.tools`, `task.summarize`, `task.validate`
   - Screenshot showing timing breakdown
   - File: `evidence/jaeger-trace-success.png`

3. **Sample failed trace**
   - Search for traces with errors (filter by tag `error=true` or status code)
   - Expand to show where failure occurred (likely in `llm.call` spans)
   - Screenshot showing error details and retry attempts
   - File: `evidence/jaeger-trace-failure.png`

4. **Tool execution trace**
   - Find a trace, expand `task.tools` span to show child `tool.*` spans
   - Note whether tools run sequentially or in parallel (look at timeline overlap)
   - Screenshot clearly showing tool execution pattern
   - File: `evidence/jaeger-trace-tools.png`

5. **LLM retry trace**
   - Find a trace with multiple `llm.call` spans under the same stage (retry scenario)
   - Screenshot showing retry attempts with increasing backoff
   - File: `evidence/jaeger-trace-llm-retries.png`

6. **Trace statistics**
   - Use Jaeger's "Compare" or aggregation features if available
   - Screenshot showing P50/P95/P99 latencies across all traces
   - File: `evidence/jaeger-trace-statistics.png`

### 3b. Prometheus metrics

Open http://localhost:9090 and run these queries, capturing results:

1. **Request rate by status**

   ```promql
   sum by (status) (rate(task_requests_total[1m]))
   ```

   - Screenshot graph showing completed, failed, cached
   - File: `evidence/prometheus-request-rate-by-status.png`

2. **Error rate**

   ```promql
   sum(rate(task_requests_total{status="failed"}[1m])) / sum(rate(task_requests_total[1m]))
   ```

   - Screenshot showing error rate percentage over time
   - File: `evidence/prometheus-error-rate.png`

3. **Task latency distribution**

   ```promql
   histogram_quantile(0.50, sum(rate(task_duration_seconds_bucket[1m])) by (le))
   histogram_quantile(0.95, sum(rate(task_duration_seconds_bucket[1m])) by (le))
   histogram_quantile(0.99, sum(rate(task_duration_seconds_bucket[1m])) by (le))
   ```

   - Screenshot showing P50/P95/P99 lines
   - File: `evidence/prometheus-task-latency-percentiles.png`

4. **Pipeline stage duration**

   ```promql
   histogram_quantile(0.95, sum(rate(pipeline_stage_duration_seconds_bucket[1m])) by (stage, le))
   ```

   - Screenshot showing P95 latency per stage (plan, execute, summarize, validate)
   - File: `evidence/prometheus-stage-duration.png`

5. **Cache hit rate**

   ```promql
   sum(rate(cache_hits_total[1m])) / (sum(rate(task_requests_total[1m])) - sum(rate(task_requests_total{status="completed_cached"}[1m])))
   ```

   - Screenshot showing cache hit ratio over time
   - File: `evidence/prometheus-cache-hit-rate.png`

6. **Active tasks gauge**

   ```promql
   active_tasks
   ```

   - Screenshot showing concurrent task count (should stay ≤5 due to semaphore)
   - File: `evidence/prometheus-active-tasks.png`

7. **LLM retry rate**

   ```promql
   sum by (reason) (rate(llm_retries_total[1m]))
   ```

   - Screenshot showing retry rate by reason (server_error, rate_limited, timeout)
   - File: `evidence/prometheus-llm-retry-rate.png`

8. **Token usage by tenant**

   ```promql
   sum by (tenant_id, token_type) (rate(token_usage_total[1m]))
   ```

   - Screenshot showing prompt/completion token consumption per tenant
   - File: `evidence/prometheus-token-usage.png`

9. **Rate limiter wait time**
   ```promql
   histogram_quantile(0.95, sum(rate(rate_limiter_wait_seconds_bucket[1m])) by (le))
   ```

   - Screenshot showing P95 wait time before LLM calls
   - File: `evidence/prometheus-rate-limiter-wait.png`

### 3c. Grafana dashboard

Open http://localhost:3000, navigate to the provisioned dashboard:

1. **Full dashboard overview**
   - Screenshot showing all panels after load test
   - File: `evidence/grafana-dashboard-overview.png`

### 3d. Raw metrics snapshot

```bash
curl -s http://localhost:8080/metrics > evidence/metrics-snapshot.txt
```

Capture the full Prometheus text output immediately after the test for offline analysis.

### 3e. Structured logs

```bash
docker compose logs agent-service > evidence/agent-service.log
```

Extract key log patterns:

- Count of cache hits: `grep "completed from cache" evidence/agent-service.log | wc -l`
- Count of task failures: `grep "task failed" evidence/agent-service.log | wc -l`
- Count of LLM retries: `grep "llm_retry" evidence/agent-service.log | wc -l`

Create a summary file:

```bash
cat > evidence/log-summary.txt <<EOF
Cache hits: $(grep "completed from cache" evidence/agent-service.log | wc -l)
Task failures: $(grep "task failed" evidence/agent-service.log | wc -l)
LLM retries: $(grep "llm_retry" evidence/agent-service.log | wc -l)
Total log lines: $(wc -l < evidence/agent-service.log)
EOF
```

---

## 4. Document test configuration

Create `evidence/test-config.md` with:

```markdown
# Load Test Configuration

**Date**: YYYY-MM-DD HH:MM UTC
**Test duration**: X seconds
**Concurrent clients**: N
**Request pattern**: [describe tenant distribution, priority mix, task variety]
**Platform**:

- Docker Compose version: X.Y.Z
- Python version: 3.12
- Total containers: 5 (agent-service, mock-llm, jaeger, prometheus, grafana)

**System resources** (from `docker stats` during test):

- agent-service: X% CPU, Y MB RAM
- mock-llm: X% CPU, Y MB RAM

**Key findings** (high-level):

- Total requests: N
- Success rate: X%
- P95 latency: Y seconds
- Cache hit rate: Z%
```

---

## 5. Organize evidence directory

Create the following structure:

```
evidence/
├── test-config.md                        # Test setup and high-level results
├── metrics-snapshot.txt                  # Raw Prometheus metrics after test
├── agent-service.log                     # Full agent service logs
├── log-summary.txt                       # Parsed log statistics
├── jaeger-service-overview.png
├── jaeger-trace-success.png
├── jaeger-trace-failure.png
├── jaeger-trace-tools.png
├── jaeger-trace-llm-retries.png
├── jaeger-trace-statistics.png
├── prometheus-request-rate-by-status.png
├── prometheus-error-rate.png
├── prometheus-task-latency-percentiles.png
├── prometheus-stage-duration.png
├── prometheus-cache-hit-rate.png
├── prometheus-active-tasks.png
├── prometheus-llm-retry-rate.png
├── prometheus-token-usage.png
├── prometheus-rate-limiter-wait.png
└── grafana-dashboard-overview.png
```

---

## 6. Initial observations

After capturing evidence, create `evidence/initial-observations.md` with:

- Which pipeline stage is the slowest?
- Are tools executing sequentially or in parallel?
- What % of LLM calls require retries?
- What is the most common retry reason?
- Is the cache being used effectively?
- Are all tenants getting equal service, or is one dominating?
- Does priority affect task execution order?
- Are there any anomalies in the metrics or traces?

**Do not diagnose or propose fixes yet** — just note what the data shows. Step 3 will analyze root causes.

---

## Acceptance criteria

- [ ] Load test runs to completion without crashes
- [ ] Evidence directory contains all 20+ artifacts listed above
- [ ] Jaeger shows traces with full span hierarchy
- [ ] Prometheus graphs display data from the test window
- [ ] Grafana dashboard renders with live data
- [ ] Logs contain trace_id/span_id on every line
- [ ] `test-config.md` and `initial-observations.md` are written
- [ ] All screenshots are clearly labeled and timestamped
- [ ] Raw metrics and logs are saved for offline analysis

---

## Notes

- If the load test reveals a crash or deadlock, document it in `initial-observations.md` but do not fix it yet
- If Jaeger/Prometheus is not collecting data, troubleshoot before proceeding (check `docker compose logs jaeger` and `docker compose logs prometheus`)
- If the test is too short to reveal patterns, increase duration in `tests/test_load.py` and re-run
- Keep the platform running after evidence collection so you can explore interactively if needed
