# Step 4: Fix Issues & Show Before/After

## Context

Steps 1–3 are complete. We have a fully instrumented service, load-test evidence from a 250-request run across 3 tenants, and a `DIAGNOSIS.md` that identifies 5 issues. Step 4 implements fixes for the highest-impact issues and demonstrates measurable improvement with before/after telemetry.

Current branch: `feat/apply-improvements`

---

## Issues to Fix

Priority order (impact × implementation risk):

| #   | Issue                                                                                | Severity | Fix complexity |
| --- | ------------------------------------------------------------------------------------ | -------- | -------------- |
| 1   | Per-tenant lock acquired outside semaphore → max 3 concurrent tasks despite cap of 5 | Critical | Low            |
| 2   | Timeout covers queue wait time → tasks fail with 0 work done                         | Critical | Low            |
| 3   | Sequential tool execution → unnecessary latency per task                             | Medium   | Very low       |
| 4   | Unbounded `_response_cache` and `task_store` → OOM over time                         | Medium   | Low            |

Issue 4 (single-tenant failure concentration) resolves automatically when Issues 1 and 2 are fixed.

---

## Fix 1: Invert Lock / Semaphore Nesting

**File:** `src/main.py`

### Current code (around line 126-138)

```python
async def _guarded_execute():
    lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())
    async with lock:                    # lock acquired FIRST
        async with _task_semaphore:     # semaphore acquired SECOND (inside lock)
            active_tasks.inc()
            try:
                result = await run_task(...)
            finally:
                active_tasks.dec()
    return result
```

The lock is held for the full execution, so only one task per tenant can enter the semaphore at a time. With 3 tenants, `active_tasks` is capped at 3 regardless of `MAX_CONCURRENT_TASKS = 5`.

### Fix

Acquire the semaphore first, then the lock — or remove the per-tenant lock entirely if strict sequential ordering per tenant is not a stated requirement.

**Option A (recommended): remove the per-tenant lock.** The lock was likely added to prevent cache races, but the cache lookup and update can be guarded more narrowly (or the cache can tolerate harmless duplicate inflight calls). Removing it restores full semaphore-governed concurrency.

**Option B: narrow the critical section.** If ordering is required, move the lock so it only guards the cache read/write, not the entire pipeline execution:

```python
async def _guarded_execute():
    async with _task_semaphore:          # semaphore acquired FIRST
        active_tasks.inc()
        try:
            lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())
            async with lock:             # lock guards only cache lookup+update
                cached = _response_cache.get(cache_key)
                if cached:
                    return cached
            result = await run_task(...)  # pipeline runs WITHOUT holding lock
            async with lock:
                _response_cache[cache_key] = result
            return result
        finally:
            active_tasks.dec()
```

This allows up to `MAX_CONCURRENT_TASKS` tasks to run simultaneously, while still preventing concurrent cache writes for the same tenant.

**Chosen approach:** Option A (remove per-tenant lock entirely). The cache is already keyed by `(tenant_id, task_description)` — a duplicate inflight request will simply compute the same result twice and overwrite the cache harmlessly. The lock adds significant latency cost for no correctness benefit.

---

## Fix 2: Apply Timeout Only to Pipeline Execution

**File:** `src/main.py`

### Current code (around line 143-148)

```python
result = await asyncio.wait_for(
    _guarded_execute(),          # includes lock wait + semaphore wait + execution
    timeout=TASK_TIMEOUT_SECONDS,
)
```

The 30-second budget is consumed by queue wait time. A task that waits 29s for its tenant lock has only 1s to run the full 4-stage pipeline.

### Fix

Split into two explicit phases — waiting and executing — and apply the timeout only to execution:

```python
# Phase 1: acquire semaphore (no timeout — just queue)
async with _task_semaphore:
    active_tasks.inc()
    try:
        # Phase 2: run pipeline with timeout
        result = await asyncio.wait_for(
            run_task(...),
            timeout=TASK_TIMEOUT_SECONDS,
        )
    finally:
        active_tasks.dec()
```

If a queue timeout is desired (to reject tasks that wait too long rather than queuing indefinitely), add a separate shorter timeout around the semaphore acquisition only.

**Note:** After removing the per-tenant lock (Fix 1), `_guarded_execute()` can be inlined. The structure becomes:

```python
async with _task_semaphore:
    active_tasks.inc()
    try:
        result = await asyncio.wait_for(run_task(...), timeout=TASK_TIMEOUT_SECONDS)
    finally:
        active_tasks.dec()
```

This guarantees the full 30s is available for actual pipeline work.

---

## Fix 3: Parallelize Tool Execution

**File:** `src/tool_executor.py`

### Current code (line 49-53)

```python
async def execute_tools(tools: list[tuple[str, dict]]) -> list[dict]:
    results = []
    for tool_name, args in tools:
        result = await execute_tool(tool_name, args)
        results.append(result)
    return results
```

Tools are independent — no data flows from one to the next. Sequential execution means total duration = sum of all tool times (~474ms avg). Parallel execution would drop it to the slowest tool time (~316ms avg, the `search` tool).

### Fix

```python
async def execute_tools(tools: list[tuple[str, dict]]) -> list[dict]:
    return list(await asyncio.gather(*(execute_tool(name, args) for name, args in tools)))
```

One line change. `asyncio.gather` preserves order so downstream code that indexes into results by position remains correct.

---

## Fix 4: Cap Unbounded In-Memory Caches

**Files:** `src/main.py`, `src/orchestrator.py`

### Changes

1. **`_response_cache`** — replace bare `dict` with a TTL-capped LRU cache. Add `cachetools` to dependencies (already available if not present, or use `functools.lru_cache` pattern manually):

```python
from cachetools import TTLCache

_response_cache: TTLCache[str, dict] = TTLCache(maxsize=1000, ttl=3600)  # 1h TTL, 1000-entry cap
```

2. **`task_store`** — replace bare `dict` with a bounded deque or TTL cache. Tasks older than N hours are unlikely to be polled:

```python
from cachetools import TTLCache

task_store: TTLCache[str, TaskResult] = TTLCache(maxsize=10_000, ttl=7200)  # 2h TTL
```

3. **`_execution_log`** in `src/orchestrator.py:25` — cap using a `collections.deque` with `maxlen`:

```python
from collections import deque

_execution_log: deque[dict] = deque(maxlen=10_000)
```

Add `cachetools` to `requirements.txt` and `pyproject.toml` if not already present.

---

## Implementation Order

1. Read the current `src/main.py` to confirm exact line numbers for the lock/semaphore/timeout code
2. Apply Fix 1 (remove per-tenant lock) to `src/main.py`
3. Apply Fix 2 (move timeout inside semaphore) to `src/main.py`
4. Apply Fix 3 (parallelize tools) to `src/tool_executor.py`
5. Apply Fix 4 (cap caches) to `src/main.py` and `src/orchestrator.py`; add `cachetools` to `requirements.txt` / `pyproject.toml`
6. Run pre-commit checks: `uv run ruff format src/ && uv run ruff check --fix src/ && uv run pyright src/`
7. Rebuild and run the load test; capture before/after evidence

---

## Before/After Evidence Collection

### Before baseline (already captured in `evidence/`)

- `evidence/prometheus-active-tasks.png` — `active_tasks` plateau at 3
- `evidence/prometheus-error-rate.png` — 11.6% error rate
- `evidence/prometheus-task-latency-percentiles.png` — P95 = 30.01s (timeout ceiling)
- `evidence/metrics-snapshot.txt` — raw counter values
- `evidence/test-output.txt` — 29 failures, all with 0 tokens

### After: run load test and capture

```bash
docker compose up --build -d
sleep 30
python -m tests.test_load
```

Capture to `evidence/after/`:

| File                                            | What to show                                                          |
| ----------------------------------------------- | --------------------------------------------------------------------- |
| `after/prometheus-active-tasks.png`             | `active_tasks` should now reach 4–5                                   |
| `after/prometheus-error-rate.png`               | Error rate should drop significantly (target < 2%)                    |
| `after/prometheus-task-latency-percentiles.png` | P95 should drop below 30s                                             |
| `after/prometheus-stage-duration.png`           | `execute` stage duration should drop from ~473ms to ~316ms            |
| `after/jaeger-trace-tools.png`                  | Tool spans should overlap (parallel) instead of stacking (sequential) |
| `after/metrics-snapshot.txt`                    | Raw counter snapshot for exact comparison                             |

```bash
curl -s http://localhost:8080/metrics > evidence/after/metrics-snapshot.txt
docker compose logs agent-service > evidence/after/agent-service.log
```

### Key before/after numbers to document in DIAGNOSIS.md

| Metric                     | Before  | Expected After |
| -------------------------- | ------- | -------------- |
| `active_tasks` peak        | 3       | 4–5            |
| Error rate                 | 11.6%   | < 2%           |
| P95 task latency           | 30.01s  | < 15s          |
| P99 task latency           | 30.01s  | < 25s          |
| Tool execute stage P95     | ~473ms  | ~316ms         |
| Failures from tenant-gamma | 27 / 29 | ~0             |

---

## Code Quality Constraints

- Do not change any business logic beyond the four fixes
- No new abstractions — the fixes are minimal and surgical
- After each fix, run: `.git/hooks/pre-commit`
- All type annotations must remain valid (pyright must pass)
- Do not touch `src/mock_llm_server.py` or `tests/test_load.py`

---

## DIAGNOSIS.md Updates

After collecting after-evidence, add a `### Results` subsection to each issue in `DIAGNOSIS.md` with the actual before/after numbers and relevant screenshots. Add an overall `## Before/After Summary` table at the end of the file.

---

## Acceptance Criteria

- [ ] Per-tenant lock removed from `src/main.py`; `_tenant_locks` dict and all references deleted
- [ ] `asyncio.wait_for` timeout applies only to `run_task(...)`, not to semaphore acquisition
- [ ] `execute_tools` uses `asyncio.gather`; all type annotations still valid
- [ ] `_response_cache` and `task_store` are TTL-capped; `_execution_log` is a bounded deque
- [ ] Pre-commit hook passes (ruff format + ruff check + pyright)
- [ ] Load test runs to completion after fixes
- [ ] `evidence/after/` directory contains all required screenshots and metric snapshots
- [ ] `DIAGNOSIS.md` has a `### Results` subsection for each fixed issue and a `## Before/After Summary` table at the end
