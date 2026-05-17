# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
# Install dependencies
uv sync

# Run the full platform (agent service + mock LLM)
docker compose up --build

# Run just the agent service locally (requires LLM_SERVER_URL env var)
LLM_SERVER_URL=http://localhost:8081 uvicorn src.main:app --port 8080

# Run just the mock LLM server locally
python src/mock_llm_server.py

# Run load test (platform must be running on localhost:8080)
python -m tests.test_load
```

## Architecture

The platform is a multi-tenant agent execution service. Two FastAPI apps run side-by-side:

- **`src/main.py`** — the Agent Execution Service (port 8080): accepts task submissions via `POST /tasks`, enforces concurrency limits, caches identical `(tenant_id, task_description)` pairs to skip redundant LLM calls, and applies a per-tenant `asyncio.Lock` to guarantee sequential execution within a tenant.
- **`src/mock_llm_server.py`** — a Mock LLM Server (port 8081): simulates a real inference endpoint with intentional ~10% 500 errors, ~5% 429 rate-limits, and ~5% extreme latency. This unreliability is by design.

### Task execution pipeline (`src/orchestrator.py`)

Each task runs through four sequential LLM calls:

1. **Plan** — ask the LLM to create an execution plan
2. **Execute** — run three hardcoded tools (search, database_lookup, calculator) via `src/tool_executor.py`
3. **Summarise** — synthesise a final answer from tool outputs
4. **Validate** — quality-gate the summary before returning it

Token counts are accumulated across all four steps and returned in `token_usage`.

### LLM client (`src/llm_client.py`)

`call_llm()` applies two layers of protection before hitting the inference endpoint:

- **Token bucket rate limiter** — caps calls at `LLM_RATE_LIMIT_RPS` (10 RPS, burst 20)
- **Exponential backoff with jitter** — up to `RETRY_MAX_ATTEMPTS` (5) retries on 500/429/timeout

A single shared `httpx.AsyncClient` is reused across calls for connection pooling.

### Configuration (`src/config.py`)

All tunables live here. `LLM_SERVER_URL` is the only env-var override; everything else is hardcoded constants (timeout, concurrency cap, retry policy, rate limits, token costs).

## Keeping This File Up-to-Date

After any meaningful code modification, verify that this file accurately reflects the current codebase. Update it **only if there is an actual inconsistency** — if the file is already consistent with the code, make no edits.

Check for drift in: commands, architecture descriptions, file/module names, configuration constants, and key design constraints.

### Key design constraints

- `MAX_CONCURRENT_TASKS = 5` — semaphore in `main.py` limits simultaneous pipeline executions
- `TASK_TIMEOUT_SECONDS = 30` — applies both to the overall task (`asyncio.wait_for` in `main.py`) and to individual HTTP requests in `llm_client.py`
- Tool execution in `tool_executor.py` is currently sequential (no `asyncio.gather`), even though tools are independent
- The `_execution_log` in `orchestrator.py` and `task_store`/`_response_cache` in `main.py` are in-memory only — they reset on restart
