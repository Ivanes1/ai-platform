"""FastAPI application — Agent Execution Service.

Provides the HTTP API for submitting and querying agent tasks.
"""

import logging
import time
import uuid
import asyncio
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from typing import Optional
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task
from src.config import MAX_CONCURRENT_TASKS, TASK_TIMEOUT_SECONDS
from src.telemetry import (
    active_tasks,
    cache_hits_total,
    get_tracer,
    setup_telemetry,
    shutdown_telemetry,
    task_duration_seconds,
    task_requests_total,
    token_usage_total,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Execution Service")

# Task storage
task_store: dict[str, TaskResult] = {}

# Response cache for repeated queries — avoids redundant LLM calls
_response_cache: dict[str, dict] = {}

# Limit concurrent task executions to protect downstream LLM service
_task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Ensure tasks for the same tenant execute in submission order
# to prevent race conditions on downstream tenant state
_tenant_locks: dict[str, asyncio.Lock] = {}


class CreateTaskBody(BaseModel):
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


@app.on_event("startup")
async def startup():
    setup_telemetry(app)


@app.on_event("shutdown")
async def shutdown():
    shutdown_telemetry()


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody):
    task_id = str(uuid.uuid4())
    tracer = get_tracer()

    with tracer.start_as_current_span("http.create_task") as span:
        span.set_attribute("task.id", task_id)
        span.set_attribute("task.tenant_id", body.tenant_id)
        span.set_attribute("task.priority", body.priority.value)

        cache_key = f"{body.tenant_id}:{body.task_description}"
        if cache_key in _response_cache:
            cache_hits_total.labels(tenant_id=body.tenant_id).inc()
            cached = _response_cache[cache_key]
            result = TaskResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                tenant_id=body.tenant_id,
                priority=body.priority,
                result=cached.get("result"),
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(),
                completed_at=time.time(),
            )
            task_store[task_id] = result
            task_requests_total.labels(
                tenant_id=body.tenant_id,
                priority=body.priority.value,
                status="completed_cached",
            ).inc()
            logger.info(
                "task completed from cache",
                extra={
                    "task_id": task_id,
                    "tenant_id": body.tenant_id,
                },
            )
            return _to_response(result)

        task_store[task_id] = TaskResult(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tenant_id=body.tenant_id,
            priority=body.priority,
        )

        async def _guarded_execute():
            lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())
            async with lock:
                async with _task_semaphore:
                    active_tasks.inc()
                    try:
                        return await run_task(
                            task_id=task_id,
                            description=body.task_description,
                            tenant_id=body.tenant_id,
                            priority=body.priority,
                        )
                    finally:
                        active_tasks.dec()

        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _guarded_execute(),
                timeout=TASK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            result = TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                tenant_id=body.tenant_id,
                priority=body.priority,
                error="Task execution exceeded time limit",
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(),
                completed_at=time.time(),
            )

        duration = time.monotonic() - t0
        task_store[task_id] = result

        status_label = result.status.value
        task_requests_total.labels(
            tenant_id=body.tenant_id,
            priority=body.priority.value,
            status=status_label,
        ).inc()
        task_duration_seconds.labels(
            tenant_id=body.tenant_id,
            priority=body.priority.value,
        ).observe(duration)

        usage = result.token_usage or {}
        if usage.get("prompt_tokens"):
            token_usage_total.labels(tenant_id=body.tenant_id, token_type="prompt").inc(
                usage["prompt_tokens"]
            )
        if usage.get("completion_tokens"):
            token_usage_total.labels(
                tenant_id=body.tenant_id, token_type="completion"
            ).inc(usage["completion_tokens"])

        if result.status == TaskStatus.COMPLETED:
            _response_cache[cache_key] = {"result": result.result}
            logger.info(
                "task completed",
                extra={
                    "task_id": task_id,
                    "tenant_id": body.tenant_id,
                    "duration_ms": round(duration * 1000),
                },
            )
        else:
            logger.error(
                "task failed",
                extra={
                    "task_id": task_id,
                    "tenant_id": body.tenant_id,
                    "error": result.error,
                    "duration_ms": round(duration * 1000),
                },
            )

        return _to_response(result)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    if task_id not in task_store:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task_store[task_id])


@app.get("/health")
async def health():
    return {"status": "ok"}


def _to_response(r: TaskResult) -> TaskResponse:
    return TaskResponse(
        task_id=r.task_id,
        status=r.status,
        tenant_id=r.tenant_id,
        priority=r.priority,
        result=r.result,
        error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at,
        completed_at=r.completed_at,
    )
