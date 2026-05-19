# Step 3: Diagnose Issues & Write DIAGNOSIS.md

## Context

Step 1 (observability stack) and Step 2 (load test + evidence collection) are complete. We have comprehensive telemetry from a 250-request, 12-minute sustained load test against the multi-tenant agent execution service. The goal of Step 3 is to analyze the collected evidence and source code to independently identify, diagnose, and document performance/reliability issues — without being told what to look for in advance.

The output is a `DIAGNOSIS.md` file at the project root that documents each discovered issue with supporting evidence, root cause analysis, and proposed fixes.

---

## Inputs (what to examine)

### Source code (read for root-cause analysis)

- `src/main.py` — task execution flow, caching, concurrency, timeouts
- `src/orchestrator.py` — pipeline stages, error handling, token accounting
- `src/llm_client.py` — retry logic, rate limiting, token tracking
- `src/tool_executor.py` — tool execution patterns
- `src/config.py` — configuration constants and their usage

### Evidence artifacts (read/view for symptoms)

- `evidence/test-config.md` — test parameters and high-level results
- `evidence/log-summary.txt` — parsed log statistics
- `evidence/metrics-snapshot.txt` — raw Prometheus metrics post-test
- `evidence/test-output.txt` — full load test terminal output
- `evidence/agent-service.log` — structured JSON logs with trace correlation

### Screenshots (view for visual patterns)

- `evidence/jaeger-trace-success.png` — successful trace timeline
- `evidence/jaeger-trace-failure.png` — failed trace timeline
- `evidence/jaeger-trace-tools.png` — tool execution span layout
- `evidence/jaeger-trace-llm-retries.png` — retry behavior in spans
- `evidence/jaeger-long-time-before-execution.png` — timeout scenario
- `evidence/jaeger-service-overview.png` — aggregate service view
- `evidence/prometheus-request-rate-by-status.png` — throughput by outcome
- `evidence/prometheus-error-rate.png` — failure rate over time
- `evidence/prometheus-task-latency-percentiles.png` — P50/P95/P99
- `evidence/prometheus-stage-duration.png` — per-stage latency
- `evidence/prometheus-cache-hit-rate.png` — cache effectiveness
- `evidence/prometheus-active-tasks.png` — concurrency utilization
- `evidence/prometheus-llm-retry-rate.png` — retry frequency by reason
- `evidence/prometheus-token-usage.png` — token consumption patterns
- `evidence/prometheus-rate-limiter-wait.png` — rate limiter backpressure
- `evidence/grafana-dashboard-overview.png` — full dashboard view

### JSON trace exports (parse for detailed span data)

- `evidence/jaeger-trace-success.json`
- `evidence/jaeger-trace-failure.json`
- `evidence/jaeger-trace-llm-retries.json`
- `evidence/jaeger-long-time-before-execution.json`
- `evidence/jaeger-all-results.json` — **very large file** containing all tracing data from the load test. Too big to read directly, but contains comprehensive span-level information. Write small Python analysis scripts to parse this JSON and extract specific insights (e.g., latency distributions per stage, retry counts per trace, sequential tool timing gaps, correlation between tenant and failure rate).

---

## Process

### Phase 1: Gather symptoms from evidence

1. Read `evidence/test-config.md` and `evidence/log-summary.txt` for high-level numbers
2. View all Jaeger screenshots — note timing patterns, sequential vs parallel execution, retry cascades, timeout behavior
3. View all Prometheus screenshots — note error rates, latency distributions, cache behavior, rate limiter pressure
4. Read `evidence/metrics-snapshot.txt` for exact counter values
5. Skim `evidence/test-output.txt` for patterns (cached tasks with 0 tokens, failures at 30.01s, latency variance)
6. Optionally parse JSON traces for precise span durations

### Phase 2: Trace symptoms to root causes in code

For each observed symptom (e.g., "tools always execute sequentially", "P99 = P95 = 30.01s", "cached tasks report 0 tokens"):

1. Identify which source file contains the relevant logic
2. Read the specific code path
3. Determine the root cause
4. Assess the impact (performance, reliability, cost, correctness)

### Phase 3: Write DIAGNOSIS.md

Create `DIAGNOSIS.md` at project root with the following structure for each issue:

```markdown
# Diagnosis Report

## Summary

- Number of issues found
- Overall system health assessment
- Key metrics from the load test

## Issue N: [Descriptive Title]

### Symptom

What was observed in the telemetry/evidence (with specific values, screenshots referenced)

### Evidence

- Which artifact(s) show this (file name + what to look at)
- Relevant metric values or log excerpts

### Root Cause

- Which file/function/line causes this
- Why the current implementation behaves this way

### Impact

- Performance / reliability / cost / correctness impact
- Severity: Critical / High / Medium / Low

### Proposed Fix

- What to change (conceptual, not a full implementation)
- Expected improvement
```

---

## Constraints

- Do NOT pre-assume which issues exist — discover them from the evidence
- Each issue MUST be backed by at least one concrete evidence artifact (screenshot, metric value, log line, or trace span)
- Reference evidence files by name so a reader can verify
- Keep root-cause analysis tied to specific code locations (file:line)
- Propose fixes but do NOT implement them (that's Step 4)
- Focus on issues that have measurable impact visible in the telemetry — not theoretical concerns

---

## Output

Single file: `DIAGNOSIS.md` at project root (`/Users/ivan.melnikov/Documents/Personal/ai-platform/DIAGNOSIS.md`)

---

## Verification

After writing DIAGNOSIS.md:

1. Every referenced evidence file should exist in `evidence/`
2. Every referenced source file:line should be verifiable by reading the code
3. The document should be self-contained — a reader unfamiliar with the project should understand each issue from the description alone
