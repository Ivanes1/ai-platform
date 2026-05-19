
# Load Test Configuration

**Date**: 2026-05-19
**Test duration**: ~12 minutes (estimated from timestamp range)
**Concurrent clients**: 15
**Total requests**: 250
**Request pattern**:
- 3 tenants (tenant-alpha, tenant-beta, tenant-gamma) — random distribution
- 3 priorities (urgent, normal, low) — random distribution
- 10 task templates with 70% unique, 30% duplicate (cache testing)

**Platform**:
- Docker Compose
- Python 3.12.13
- 5 containers: agent-service, mock-llm, jaeger, prometheus, grafana
- MAX_CONCURRENT_TASKS: 5
- TASK_TIMEOUT_SECONDS: 30
- RETRY_MAX_ATTEMPTS: 5
- LLM_RATE_LIMIT_RPS: 10, burst 20

**Results**:
- Total requests: 250
- Completed: 221 (88.4%)
- Failed: 29 (11.6%)
- Cache hits: 38
- P50 latency: 13.96s
- P95 latency: 30.01s
- P99 latency: 30.01s
- Max latency: 30.01s

**Token usage by tenant**:
- tenant-alpha: 87 tasks, 12,231 prompt tokens, 39,170 completion tokens
- tenant-beta: 83 tasks, 10,299 prompt tokens, 32,949 completion tokens
- tenant-gamma: 80 tasks, 8,323 prompt tokens, 26,169 completion tokens