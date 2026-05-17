# Coding Rules

Rules derived from the conventions already established in this codebase.

---

## Module structure

- Every `.py` file begins with a one-line module docstring that names the module's responsibility.
- Multi-sentence docstrings use a blank line after the first sentence to introduce a bullet list of sub-responsibilities (see `mock_llm_server.py`, `orchestrator.py`).
- `src/__init__.py` is kept empty; it exists only to make `src` a package.

## Imports

- Standard-library imports first, then third-party, then local `src.*` — each group separated by a blank line.
- Import individual names from `src.config` explicitly (`from src.config import X, Y`) rather than importing the module.
- Use `from __future__ import annotations` in model files to allow forward references in type hints.

## Configuration

- All tunables live in `src/config.py` as module-level constants.
- `os.getenv()` is used only in `config.py`; no other file reads environment variables directly.
- Provide a sensible default in every `os.getenv()` call.
- Accompany each numeric constant with an inline comment that states its unit or meaning (e.g. `# seconds`, `# max LLM calls per second`).

## Data models (`src/models.py`)

- Use `@dataclass` for plain data containers; use `pydantic.BaseModel` only at the HTTP boundary (`main.py`).
- `str`-based `Enum`s for any field that is also a string value in the API (`Priority`, `TaskStatus`).
- Use `field(default_factory=...)` for mutable or computed defaults (e.g. `created_at`).
- Optional fields are typed `Optional[T] = None`; never use `T | None` in model files.

## Async patterns

- Every I/O-bound operation is `async`; no blocking calls inside coroutines.
- Use `asyncio.Semaphore` for concurrency caps, `asyncio.Lock` for mutual exclusion, and `asyncio.wait_for` for deadlines — all sourced from `src.config` constants.
- Prefer `asyncio.sleep` over `time.sleep` inside coroutines.
- Per-tenant locks are stored in a plain `dict` and created lazily with `dict.setdefault`.

## HTTP client

- One shared `httpx.AsyncClient` per process, created lazily via a module-level `_get_client()` factory.
- Set `timeout` and `limits` explicitly when constructing the client; never rely on defaults.
- Reuse the client across requests (connection pooling); do not create a new client per request.

## Retry and resilience

- Retry only on transient errors: HTTP 500, 429, and `httpx.TimeoutException`.
- Apply exponential backoff with additive jitter: `delay = BASE * FACTOR**attempt + uniform(0, delay * 0.3)`.
- Skip the sleep after the final attempt (`if attempt < MAX_ATTEMPTS - 1`).
- On exhaustion, return a structured error dict (with an `"error"` key) rather than raising an exception.

## Rate limiting

- Implement rate limiting as a token-bucket (`_TokenBucket`) protected by an `asyncio.Lock`.
- The global rate-limiter instance is module-level and shared across all calls.
- Rate and burst capacity come from `src.config` constants.

## Error handling

- Functions that can fail return a result dict with an `"error"` key rather than raising.
- Callers check `result.get("error")` before consuming other fields.
- Catch broad `Exception` only at the top of a pipeline stage; re-raise nothing — convert to a structured `TaskResult(status=FAILED, error=...)`.
- Include traceback text in error details returned to callers for debuggability.

## API layer (`src/main.py`)

- Use `pydantic.BaseModel` for request/response bodies — never raw dicts at the HTTP boundary.
- Separate the internal dataclass (`TaskResult`) from the HTTP response model (`TaskResponse`); convert via a dedicated `_to_response()` helper.
- Prefix module-level state with `_` to signal it is internal: `_task_semaphore`, `_response_cache`, `_tenant_locks`.
- Return `HTTPException(status_code=404)` for missing resources; no custom exception classes.

## Caching

- Cache keyed on `"{tenant_id}:{task_description}"` — document why excluded fields (e.g. priority) are omitted.
- Store only the minimal payload needed for cache hits (not the full `TaskResult`).
- Cache only successful (`COMPLETED`) results; never cache failures.

## In-memory state

- Module-level dicts and lists are acceptable for in-memory state (`task_store`, `_response_cache`, `_execution_log`).
- Always document that this state resets on restart — either in a docstring or an inline comment.

## Naming

- `snake_case` for everything: variables, functions, modules.
- Private module-level names start with `_` (e.g. `_http_client`, `_rate_limiter`).
- Constants are `UPPER_SNAKE_CASE`.
- Async functions that are not public endpoints are prefixed with `_` when defined as closures (e.g. `_guarded_execute`).

## Comments

- Write a comment only when the *why* is non-obvious: a design tradeoff, a known quirk, a deliberate constraint.
- Use `# ── Section header ──` style to visually separate pipeline stages inside a long function.
- Do not restate what the code already says; remove comments that merely describe the next line.

## Testing

- Load/integration tests live in `tests/` and are run as scripts (`python -m tests.test_load`).
- Tests target the running service on `localhost:8080`; no mocking of internal components in load tests.

## Tooling

- Use `uv` for all dependency and environment management; do not use `pip` directly.
- Pin exact versions in `pyproject.toml` dependencies (e.g. `fastapi==0.115.0`).
- Minimum Python version is declared in `pyproject.toml` as `requires-python`.
