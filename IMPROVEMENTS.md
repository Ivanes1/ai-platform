# Performance Improvements

Four fixes were applied to the agent execution service. This document shows the before/after results from a 250-request load test run across three tenants (tenant-alpha, tenant-beta, tenant-gamma).

---

## Summary

| Metric                     | Before   | After   | Change    |
| -------------------------- | -------- | ------- | --------- |
| Failed requests            | 29 / 250 | 0 / 250 | **-100%** |
| Error rate                 | 11.6%    | 0%      | **-100%** |
| P50 latency                | 13.96s   | 8.88s   | **-36%**  |
| P95 latency                | 30.01s   | 16.64s  | **-45%**  |
| P99 latency                | 30.01s   | 22.27s  | **-26%**  |
| Tool execution stage (avg) | 473ms    | 293ms   | **-38%**  |

---

## Fix 1 — Remove per-tenant lock (`src/main.py`)

**Problem:** The per-tenant `asyncio.Lock` was acquired _outside_ the semaphore, so only one task per tenant could enter the semaphore at a time. With three tenants, `active_tasks` was capped at 3 despite `MAX_CONCURRENT_TASKS = 5`.

**Fix:** Removed `_tenant_locks` entirely. The semaphore now governs all concurrency directly.

### active_tasks peak

| Before         | After       |
| -------------- | ----------- |
| Plateaued at 3 | Reaches 4–5 |

![active tasks before/after](improvements/prometheus-active-tasks.png)

---

## Fix 2 — Move timeout inside semaphore (`src/main.py`)

**Problem:** `asyncio.wait_for` wrapped the entire execution including semaphore wait time. A task queuing for 29s consumed its full 30s budget before doing any work, producing failures with 0 tokens.

**Fix:** `asyncio.wait_for` now wraps only `run_task(...)`, inside the semaphore block.

### Task latency percentiles

|     | Before | After  |
| --- | ------ | ------ |
| P50 | 13.96s | 8.88s  |
| P95 | 30.01s | 16.64s |
| P99 | 30.01s | 22.27s |

Before, P95 and P99 were pinned at exactly 30.01s — the timeout ceiling. After, tail latency reflects real LLM retry backoff rather than queue starvation.

![latency percentiles](improvements/prometheus-task-latency-percentiles.png)

### Error rate

All 29 pre-fix failures had `token_usage = {prompt: 0, completion: 0}` — they timed out before reaching the LLM. 27 of 29 were from tenant-gamma, which was consistently last to acquire the lock.

![error rate](improvements/prometheus-error-rate.png)

---

## Fix 3 — Parallel tool execution (`src/tool_executor.py`)

**Problem:** The three tools (`search`, `database_lookup`, `calculator`) ran sequentially despite being fully independent. Total duration was the sum of all three (~473ms average).

**Fix:** Replaced the sequential loop with `asyncio.gather`.

```python
# before
for tool_name, args in tools:
    result = await execute_tool(tool_name, args)

# after
return list(await asyncio.gather(*(execute_tool(name, args) for name, args in tools)))
```

### Execute stage duration

|           | Before | After    |
| --------- | ------ | -------- |
| Average   | 473ms  | 293ms    |
| Reduction |        | **−38%** |

Duration drops from the sum of all tools to the slowest tool (`search`, max 500ms). The Jaeger trace below shows the three tool spans now overlapping instead of stacking end-to-end.

![stage duration](improvements/prometheus-stage-duration.png)

![jaeger parallel tools](improvements/jaeger-trace-tools.png)

---

## Fix 4 — Bounded in-memory caches (`src/main.py`, `src/orchestrator.py`)

**Problem:** `task_store`, `_response_cache`, and `_execution_log` were unbounded structures that grew without limit for the lifetime of the process.

**Fix:**

- `task_store` → `TTLCache(maxsize=10_000, ttl=7200)`
- `_response_cache` → `TTLCache(maxsize=1_000, ttl=3_600)`
- `_execution_log` → `deque(maxlen=10_000)`

No load test metric directly measures this fix (it prevents an OOM condition rather than improving throughput), but the service log confirms no memory-related warnings during the after run.
