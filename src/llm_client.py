"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.
"""

import asyncio
import logging
import random
import time

import httpx
from opentelemetry.trace import StatusCode

from src.config import (
    LLM_SERVER_URL,
    TASK_TIMEOUT_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
)
from src.telemetry import (
    get_tracer,
    llm_call_duration_seconds,
    llm_calls_total,
    llm_retries_total,
    rate_limiter_wait_seconds,
)

logger = logging.getLogger(__name__)

# Shared HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=TASK_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


class _TokenBucket:
    """Rate limiter to protect the downstream LLM service from overload
    and prevent runaway inference costs during traffic spikes."""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            while self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last_refill = now
            self._tokens -= 1


# Global rate limiter for LLM calls
_rate_limiter = _TokenBucket(rate=LLM_RATE_LIMIT_RPS, capacity=LLM_RATE_LIMIT_BURST)


async def call_llm(prompt: str, max_tokens: int = 512, stage: str = "") -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    client = _get_client()
    tracer = get_tracer()
    last_error = None
    last_status = None
    accumulated_tokens = 0

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("llm.attempt", attempt)
            span.set_attribute("llm.max_tokens", max_tokens)
            span.set_attribute("llm.prompt_length", len(prompt))
            if stage:
                span.set_attribute("llm.stage", stage)

            t0 = time.monotonic()
            try:
                wait_start = time.monotonic()
                await _rate_limiter.acquire()
                rate_limiter_wait_seconds.observe(time.monotonic() - wait_start)

                response = await client.post(
                    f"{LLM_SERVER_URL}/v1/inference",
                    json={"prompt": prompt, "max_tokens": max_tokens},
                )

                duration = time.monotonic() - t0
                llm_call_duration_seconds.labels(stage=stage).observe(duration)
                span.set_attribute("llm.http_status", response.status_code)

                if response.status_code == 200:
                    data = response.json()
                    data["prompt_tokens"] = (
                        data.get("prompt_tokens", 0) + accumulated_tokens
                    )
                    llm_calls_total.labels(stage=stage, http_status="200").inc()
                    return data

                last_status = response.status_code
                last_error = f"LLM returned {response.status_code}"
                span.set_status(StatusCode.ERROR, last_error)

                if response.status_code == 500:
                    accumulated_tokens += max(1, len(prompt.split()))
                    llm_retries_total.labels(reason="server_error").inc()
                    retry_reason = "server_error"
                elif response.status_code == 429:
                    llm_retries_total.labels(reason="rate_limited").inc()
                    retry_reason = "rate_limited"
                else:
                    retry_reason = "server_error"

            except httpx.TimeoutException:
                duration = time.monotonic() - t0
                llm_call_duration_seconds.labels(stage=stage).observe(duration)
                last_error = "LLM request timed out"
                last_status = 408
                span.set_attribute("llm.http_status", 408)
                span.set_status(StatusCode.ERROR, last_error)
                llm_retries_total.labels(reason="timeout").inc()
                retry_reason = "timeout"

            except Exception as e:
                duration = time.monotonic() - t0
                llm_call_duration_seconds.labels(stage=stage).observe(duration)
                last_error = str(e)
                last_status = 0
                span.set_attribute("llm.http_status", 0)
                span.set_status(StatusCode.ERROR, last_error)
                retry_reason = "server_error"

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR**attempt)
            jitter = random.uniform(0, delay * 0.3)
            total_delay = delay + jitter
            logger.warning(
                "llm_retry",
                extra={
                    "event": "llm_retry",
                    "attempt": attempt,
                    "reason": retry_reason,
                    "delay_ms": round(total_delay * 1000),
                },
            )
            await asyncio.sleep(total_delay)

    llm_calls_total.labels(stage=stage, http_status=str(last_status)).inc()
    return {
        "error": last_error,
        "text": "",
        "prompt_tokens": accumulated_tokens,
        "completion_tokens": 0,
        "status_code": last_status,
    }
