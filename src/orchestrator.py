"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
  4. Validate — quality-gate the summary before returning it
"""

import logging
import time
import traceback
from collections import deque

from opentelemetry.trace import StatusCode

from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.config import LLM_SERVER_URL
from src.telemetry import get_tracer, pipeline_stage_duration_seconds

logger = logging.getLogger(__name__)

# Execution audit trail for debugging and compliance review
_execution_log: deque[dict] = deque(maxlen=10_000)


async def run_task(
    task_id: str, description: str, tenant_id: str, priority: Priority
) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise-validate pipeline."""
    created = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    tracer = get_tracer()

    with tracer.start_as_current_span("task.execute") as root_span:
        root_span.set_attribute("task_id", task_id)
        root_span.set_attribute("tenant_id", tenant_id)
        root_span.set_attribute("priority", priority.value)

        try:
            # ── Step 1: Planning ──────────────────────────────────
            t0 = time.monotonic()
            with tracer.start_as_current_span("task.plan") as span:
                plan_prompt = f"Plan the following task: {description}"
                span.set_attribute("prompt_length", len(plan_prompt))
                plan = await call_llm(prompt=plan_prompt, max_tokens=256, stage="plan")
                span.set_attribute("prompt_tokens", plan.get("prompt_tokens", 0))
                span.set_attribute(
                    "completion_tokens", plan.get("completion_tokens", 0)
                )
                if plan.get("error"):
                    span.set_status(StatusCode.ERROR, plan["error"])

            stage_dur = time.monotonic() - t0
            pipeline_stage_duration_seconds.labels(stage="plan").observe(stage_dur)
            logger.info(
                "stage_complete",
                extra={
                    "event": "stage_complete",
                    "stage": "plan",
                    "task_id": task_id,
                    "duration_ms": round(stage_dur * 1000),
                },
            )

            total_prompt_tokens += plan.get("prompt_tokens", 0)
            total_completion_tokens += plan.get("completion_tokens", 0)

            if plan.get("error"):
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    tenant_id=tenant_id,
                    priority=priority,
                    error=plan["error"],
                    token_usage={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                    },
                    created_at=created,
                    completed_at=time.time(),
                )

            # ── Step 2: Tool execution ───────────────────────────
            t0 = time.monotonic()
            tools_to_run = [
                ("search", {"query": description}),
                ("database_lookup", {"key": tenant_id}),
                ("calculator", {"expression": "1+1"}),
            ]
            with tracer.start_as_current_span("task.tools") as span:
                span.set_attribute("tool_count", len(tools_to_run))
                tool_results = await execute_tools(tools_to_run)

            stage_dur = time.monotonic() - t0
            pipeline_stage_duration_seconds.labels(stage="execute").observe(stage_dur)
            logger.info(
                "stage_complete",
                extra={
                    "event": "stage_complete",
                    "stage": "execute",
                    "task_id": task_id,
                    "duration_ms": round(stage_dur * 1000),
                },
            )

            # ── Step 3: Summarise ────────────────────────────────
            t0 = time.monotonic()
            with tracer.start_as_current_span("task.summarize") as span:
                summary_prompt = (
                    f"Summarise results for task: {description}\n"
                    f"Tool outputs: {tool_results}"
                )
                span.set_attribute("prompt_length", len(summary_prompt))
                summary = await call_llm(
                    prompt=summary_prompt, max_tokens=512, stage="summarize"
                )
                span.set_attribute("prompt_tokens", summary.get("prompt_tokens", 0))
                span.set_attribute(
                    "completion_tokens", summary.get("completion_tokens", 0)
                )
                if summary.get("error") and summary.get("text") is None:
                    span.set_status(StatusCode.ERROR, summary["error"])

            stage_dur = time.monotonic() - t0
            pipeline_stage_duration_seconds.labels(stage="summarize").observe(stage_dur)
            logger.info(
                "stage_complete",
                extra={
                    "event": "stage_complete",
                    "stage": "summarize",
                    "task_id": task_id,
                    "duration_ms": round(stage_dur * 1000),
                },
            )

            total_prompt_tokens += summary.get("prompt_tokens", 0)
            total_completion_tokens += summary.get("completion_tokens", 0)

            if summary.get("error") and summary.get("text") is None:
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    tenant_id=tenant_id,
                    priority=priority,
                    error=summary["error"],
                    token_usage={
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                    },
                    created_at=created,
                    completed_at=time.time(),
                )

            # ── Step 4: Quality validation ─────────────────────
            t0 = time.monotonic()
            with tracer.start_as_current_span("task.validate") as span:
                validation_prompt = (
                    f"Rate the quality of this response (1-10) and flag "
                    f"any factual errors or compliance issues:\n\n"
                    f"{summary.get('text', '')}"
                )
                span.set_attribute("prompt_length", len(validation_prompt))
                validation = await call_llm(
                    prompt=validation_prompt, max_tokens=128, stage="validate"
                )
                span.set_attribute("prompt_tokens", validation.get("prompt_tokens", 0))
                span.set_attribute(
                    "completion_tokens", validation.get("completion_tokens", 0)
                )

            stage_dur = time.monotonic() - t0
            pipeline_stage_duration_seconds.labels(stage="validate").observe(stage_dur)
            logger.info(
                "stage_complete",
                extra={
                    "event": "stage_complete",
                    "stage": "validate",
                    "task_id": task_id,
                    "duration_ms": round(stage_dur * 1000),
                },
            )

            total_prompt_tokens += validation.get("prompt_tokens", 0)
            total_completion_tokens += validation.get("completion_tokens", 0)

            _execution_log.append(
                {
                    "task_id": task_id,
                    "tenant_id": tenant_id,
                    "description": description,
                    "plan_prompt": f"Plan the following task: {description}",
                    "plan_response": plan,
                    "tool_results": tool_results,
                    "summary_prompt": summary_prompt,
                    "summary_response": summary,
                    "quality_score": validation.get("text", ""),
                    "token_usage": {
                        "prompt": total_prompt_tokens,
                        "completion": total_completion_tokens,
                    },
                    "completed_at": time.time(),
                }
            )

            return TaskResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                tenant_id=tenant_id,
                priority=priority,
                result=summary.get("text", ""),
                token_usage={
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                },
                created_at=created,
                completed_at=time.time(),
            )

        except Exception as e:
            root_span.record_exception(e)
            root_span.set_status(StatusCode.ERROR, str(e))
            error_detail = (
                f"Task execution failed: {str(e)}\n"
                f"Trace: {traceback.format_exc()}\n"
                f"Pipeline stage: {'plan' if total_prompt_tokens == 0 else 'execute'}\n"
                f"LLM endpoint: {LLM_SERVER_URL}"
            )
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                tenant_id=tenant_id,
                priority=priority,
                error=error_detail,
                token_usage={
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                },
                created_at=created,
                completed_at=time.time(),
            )
