# Production Readiness: AI Agent Platform

## 1. Service Overview

The AI Agent Platform is a multi-tenant FastAPI service that accepts task requests from multiple tenants, routes each one through a four-stage LLM pipeline (plan → execute tools → summarise → validate), and returns a structured result. The current deployment runs as a single Docker container alongside a mock LLM server, using an in-process token-bucket rate limiter, an in-memory task store, and an in-memory response cache. All state is ephemeral and scoped to the process lifetime. The service exposes `POST /tasks`, `GET /tasks/{task_id}`, and `GET /metrics` (Prometheus text format).

---

## 2. SLIs / SLOs

All targets are justified against the after-fix load-test numbers (250 requests, 15 concurrent clients, 3 tenants; full results in `DIAGNOSIS.md`).

### SLI 1 — Availability

| Field      | Value                                                                                                                                                                                                                                           |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Metric** | `task_requests_total`                                                                                                                                                                                                                           |
| **PromQL** | `1 - (rate(task_requests_total{status="failed"}[5m]) / rate(task_requests_total[5m]))`                                                                                                                                                          |
| **SLO**    | ≥ 99.5% over any 30-day rolling window                                                                                                                                                                                                          |
| **Why**    | After fixing the concurrency and timeout bugs, the load test achieved 100% success rate (250/250). Pre-fix it was 88.4%. 99.5% gives headroom for genuine transient LLM errors (~10% mock error rate) while catching architectural regressions. |

### SLI 2 — P95 End-to-End Latency

| Field      | Value                                                                                                                                                                                             |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Metric** | `task_duration_seconds`                                                                                                                                                                           |
| **PromQL** | `histogram_quantile(0.95, sum(rate(task_duration_seconds_bucket[5m])) by (le))`                                                                                                                   |
| **SLO**    | ≤ 20s over any 1-hour window                                                                                                                                                                      |
| **Why**    | After-fix P95 is 16.64s. The 20s budget gives a ~3s buffer above observed values while staying well below the 30s task timeout. A breach here indicates either queue buildup or LLM retry storms. |

### SLI 3 — P99 End-to-End Latency

| Field      | Value                                                                                                                                                                                                    |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Metric** | `task_duration_seconds`                                                                                                                                                                                  |
| **PromQL** | `histogram_quantile(0.99, sum(rate(task_duration_seconds_bucket[5m])) by (le))`                                                                                                                          |
| **SLO**    | ≤ 28s over any 1-hour window                                                                                                                                                                             |
| **Why**    | After-fix P99 is 22.27s. Pre-fix it was pinned at the 30s timeout ceiling. Setting the SLO at 28s (below the hard timeout) means any alert fires before tasks are actually timing out rather than after. |

### SLI 4 — Cache Hit Rate

| Field      | Value                                                                                                                                                                                                                                                                                                          |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Metric** | `cache_hits_total`                                                                                                                                                                                                                                                                                             |
| **PromQL** | `rate(cache_hits_total[5m]) / rate(task_requests_total[5m])`                                                                                                                                                                                                                                                   |
| **SLO**    | ≥ 15% over any 1-hour window (informational; no SLO breach action)                                                                                                                                                                                                                                             |
| **Why**    | The load test (3 tenants, 5 unique tasks each) produced a ~20.8% hit rate (52/250 requests served from cache). A sustained drop below 15% signals that the cache key space has grown unexpectedly (e.g., task descriptions are being generated dynamically and never repeating), which warrants investigation. |

### SLI 5 — Semaphore Utilisation

| Field      | Value                                                                                                                                                                                                                                                                            |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Metric** | `active_tasks`                                                                                                                                                                                                                                                                   |
| **PromQL** | `active_tasks / 5` (where 5 = `MAX_CONCURRENT_TASKS`)                                                                                                                                                                                                                            |
| **SLO**    | Sustained utilisation ≤ 80% (i.e., `active_tasks` ≤ 4 on average) over any 5-minute window                                                                                                                                                                                       |
| **Why**    | The load test briefly peaked at 4–5 active tasks. Sustained saturation at `MAX_CONCURRENT_TASKS` means the semaphore has become the bottleneck and new requests are queuing rather than executing. This is the leading indicator for the timeout failures documented in Issue 2. |

---

## 3. Alerting Rules

```yaml
groups:
  - name: agent-platform
    rules:
      # ── 1. High error rate ────────────────────────────────────────────────
      - alert: HighTaskErrorRate
        expr: |
          (
            rate(task_requests_total{status="failed"}[5m])
            /
            rate(task_requests_total[5m])
          ) > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Task error rate exceeds 1% for 5 minutes"
          description: >
            {{ $value | humanizePercentage }} of tasks are failing (threshold: 1%).
            First check active_tasks and semaphore saturation — the dominant failure
            mode seen in load testing was tasks timing out while queued, not downstream
            LLM errors. If active_tasks < MAX_CONCURRENT_TASKS, check LLM retry logs.

      # ── 2. P95 latency breach ─────────────────────────────────────────────
      - alert: HighP95Latency
        expr: |
          histogram_quantile(
            0.95,
            sum(rate(task_duration_seconds_bucket[5m])) by (le)
          ) > 20
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "P95 task latency exceeds 20s"
          description: >
            P95 latency is {{ $value | humanizeDuration }} (SLO: 20s).
            Investigate in order: (1) semaphore saturation alert firing? (2)
            llm_retries_total rate spiking? (3) pipeline_stage_duration_seconds
            — which stage is slow? After-fix baseline is 16.64s; a breach here
            means something has regressed.

      # ── 3. Semaphore saturation ───────────────────────────────────────────
      - alert: SemaphoreSaturation
        expr: active_tasks >= 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "All {{ $value }} concurrency slots occupied for 5+ minutes"
          description: >
            active_tasks has been at MAX_CONCURRENT_TASKS (5) for 5 minutes.
            New requests are queuing rather than executing. Check rate(task_requests_total[5m])
            for a traffic surge, or check for a slow LLM causing tasks to hold slots longer
            than usual (llm_call_duration_seconds histogram). Consider scaling out or
            temporarily raising MAX_CONCURRENT_TASKS.

      # ── 4. LLM retry storm ────────────────────────────────────────────────
      - alert: LLMRetryStorm
        expr: rate(llm_retries_total[5m]) > 3
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "LLM retry rate exceeds 3/s for 5 minutes"
          description: >
            LLM retry rate is {{ $value | humanize }}/s (threshold: 3/s).
            Load test baseline was ~0.18/s (127 retries over ~12 min).
            Check llm_retries_total labels — split by reason ("server_error" vs
            "rate_limited"). A spike in "rate_limited" means the upstream LLM
            quota is exhausted; "server_error" means the LLM endpoint is degraded.
            Both cause exponential backoff that quickly cascades into task timeouts.

      # ── 5. High memory usage ──────────────────────────────────────────────
      - alert: HighMemoryUsage
        expr: process_resident_memory_bytes > 512 * 1024 * 1024
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "Agent service RSS exceeds 512 MiB"
          description: >
            Resident memory is {{ $value | humanize1024 }}B.
            Load test baseline with TTLCache eviction was ~87 MB for 250 requests.
            A sustained climb toward 512 MiB indicates the TTLCache bounds are
            too large, the execution_log deque is holding large payloads, or a
            new unbounded structure was introduced. Profile with py-spy or check
            /metrics for task_store and cache size gauges (add these if not yet
            present).
```

---

## 4. GCP / Kubernetes Changes

### 4.1 Persistent Task Store

**Current state:** `task_store` is a `TTLCache(maxsize=10_000, ttl=7200)` in-process dict.

**Problem:** Any pod restart, rolling update, or crash loses all in-flight and recently completed task results. Callers polling `GET /tasks/{task_id}` get 404 after a restart. Under multiple replicas, a task submitted to pod A is invisible to pod B.

**Fix:** Replace with Redis (Cloud Memorystore for Redis on GCP). Task results are serialised to JSON and stored with a TTL matching the current 2-hour window. The key `task:{task_id}` is readable by any replica. If durability across Redis restarts is required, use `appendonly yes` or switch to Cloud Spanner for a fully managed, strongly consistent store (at higher latency and cost).

---

### 4.2 Distributed Response Cache

**Current state:** `_response_cache` is a `TTLCache(maxsize=1_000, ttl=3_600)` in-process dict.

**Problem:** Each replica maintains its own cache. With N replicas, the effective cache hit rate degrades from the single-process rate to approximately `1/N` of it — every replica must warm up independently, multiplying LLM calls and costs.

**Fix:** Move the cache to the same Redis instance as the task store, using a key of `cache:{sha256(tenant_id + description)}`. The TTL is already present in the current design, so the migration is straightforward. Add a cache-miss counter per-replica to monitor warm-up behaviour after deployments.

---

### 4.3 Distributed Rate Limiting

**Current state:** A per-process token bucket at `LLM_RATE_LIMIT_RPS = 10 RPS` (burst 20) in `llm_client.py`.

**Problem:** With N replicas, the effective rate limit is `N × 10 RPS`. If the upstream LLM quota is truly 10 RPS, this means 3 replicas can collectively fire 30 RPS, causing the upstream to rate-limit and triggering the retry storm pattern documented in Issue 4.

**Fix:** Replace the in-process `asyncio`-based token bucket with an atomic Redis token bucket (using `EVAL` with Lua, or a library like `redis-py-ratelimit`). All replicas share a single bucket keyed by the LLM endpoint. Alternatively, use a sidecar proxy (Envoy, Istio) to enforce egress rate limits at the mesh layer, which avoids adding Redis as a dependency for the rate-limiting path.

---

### 4.4 Health Probes

**Current state:** No `/health` or `/ready` endpoint. Kubernetes has no way to distinguish a live process from one that is stuck.

**Fix:** Add two endpoints:

- `GET /health` (liveness): returns `200 OK` if the process is running. A 5xx here causes Kubernetes to restart the pod. Implementation: a simple `{"status": "ok"}` response — never perform external checks in liveness probes (it causes restart cascades if the LLM is down).
- `GET /ready` (readiness): returns `200 OK` only when the service is ready to accept traffic — i.e., after `setup_telemetry()` has completed and the OTLP exporter has connected. Returns `503` during startup or when `active_tasks == MAX_CONCURRENT_TASKS` (optional load-shedding). A non-200 readiness response removes the pod from the load balancer without restarting it.

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 2
```

---

### 4.5 Secrets Management

**Current state:** `LLM_SERVER_URL` and `OTEL_EXPORTER_OTLP_ENDPOINT` are passed as plain environment variables. Any API keys or database credentials added later would follow the same pattern, making them visible in pod specs, CI logs, and `kubectl describe pod`.

**Fix:** Store secrets in GCP Secret Manager. Mount them into the container using the [Secret Manager CSI driver](https://cloud.google.com/secret-manager/docs/using-secret-manager-with-container-registry) or Workload Identity. Reference them in the pod spec as a volume or environment variable sourced from the mounted secret file — not hardcoded in the `Deployment` manifest. Rotate secrets without redeploying by bumping the secret version and triggering a rolling restart.

---

### 4.6 Horizontal Scaling

**Current state:** A single container. `MAX_CONCURRENT_TASKS = 5` is a per-process semaphore.

**Problem:** Scaling to N replicas multiplies total concurrency to `N × 5`, but the per-process semaphore means each replica still caps at 5 in-flight tasks. The global rate limit also multiplies (see §4.3). Additionally, each replica has its own `_tenant_locks` — inter-replica fairness is not enforced.

**Fix:**

- Define a Kubernetes `HorizontalPodAutoscaler` targeting CPU or a custom metric (`active_tasks / MAX_CONCURRENT_TASKS`):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-service-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agent-service
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Pods
      pods:
        metric:
          name: active_tasks_utilisation # custom metric via Prometheus adapter
        target:
          type: AverageValue
          averageValue: "4" # scale out when avg active_tasks > 4 (80% of 5)
```

- Move the global semaphore to a Redis-backed distributed semaphore (e.g., `redis-py`'s `Semaphore`) so the concurrency cap is enforced across all replicas, not per-process.

---

### 4.7 PodDisruptionBudgets

**Current state:** No PDB. A cluster upgrade or node drain can terminate all pods simultaneously, causing all in-flight tasks to fail.

**Fix:**

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: agent-service-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: agent-service
```

This ensures at least one pod remains available during voluntary disruptions. For a 2-replica deployment, combine with `maxUnavailable: 1` on the `Deployment`'s rolling update strategy to guarantee zero-downtime deploys. Add graceful shutdown handling (`SIGTERM` → drain in-flight tasks before exit) so the terminating pod finishes its current pipeline executions before Kubernetes removes it from the load balancer.

---

### 4.8 Resource Requests and Limits

**Current state:** No resource requests or limits defined. The container can consume unbounded CPU and memory, which can starve other pods on the same node and prevent the Kubernetes scheduler from making good placement decisions.

**Observed baseline** (from load test, 250 requests, 3 tenants, 15 concurrent clients):

- RSS: ~87 MB at steady state
- CPU: not directly measured, but the workload is I/O-bound (LLM calls dominate)

**Recommended starting values:**

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi" # matches the HighMemoryUsage alert threshold
    cpu: "1000m" # cap to prevent runaway CPU in retry storms
```

The memory limit of 512 MiB aligns with the alerting threshold from §3 — if the process approaches this, the alert fires before the OOM kill. Tune after observing production P99 resource usage; the TTLCache bounds (`maxsize=10_000` for task_store, `maxsize=1_000` for response_cache) should also be reduced if the limits prove too tight.

---

## 5. Known Remaining Gaps

The following issues were identified during diagnosis (see `DIAGNOSIS.md`) but not yet fixed. They are noted here so they can be prioritised for the next engineering cycle.

### Priority Scheduling Not Enforced

**Issue:** Tasks carry a `priority` field (`low`, `normal`, `urgent`) that is stored in the task result and emitted as a metric label, but has no effect on execution order. Under the current FIFO semaphore, a `low`-priority task submitted before an `urgent` task will always execute first.

**Impact:** SLA violations for high-priority tenants at high load. Urgent tasks offer no delivery guarantee beyond normal tasks.

**Recommended fix:** Replace the single `asyncio.Semaphore` with a priority queue (`asyncio.PriorityQueue`). Weight entries by priority level and submit tasks with their priority at enqueue time.

---

### No Per-Tenant Rate Limiting

**Issue:** The LLM rate limiter is global (single token bucket shared across all tenants). A single high-volume tenant can exhaust the entire request budget, causing `429` responses and retry storms that degrade performance for all other tenants.

**Impact:** Noisy-neighbour problem in multi-tenant deployments. Tenant-gamma's higher volume was the direct cause of its 93% share of failures in the pre-fix load test.

**Recommended fix:** Maintain one token bucket per `tenant_id`, each capped at a fraction of the global limit (e.g., `LLM_RATE_LIMIT_RPS / num_active_tenants`, with a per-tenant hard cap). Move to Redis-backed buckets when running multiple replicas (see §4.3).

---

### Cache Key Excludes Priority

**Issue:** The response cache key is `sha256(tenant_id + task_description)`. Priority is not part of the key, so a `low`-priority cached result can be returned for an `urgent` request with the same description.

**Impact:** If the pipeline applies different behaviour based on priority (or if tenants expect priority to affect the response), stale cross-priority cache hits silently return wrong results. Even without behavioural differences today, this is a latent correctness bug: adding priority-aware logic later will require a cache invalidation strategy.

**Recommended fix:** Include `priority` in the cache key. This reduces the hit rate slightly (separate cache entries per priority level) but eliminates the correctness risk.

---

### Token Cost Not Surfaced in API Responses

**Issue:** `TOKEN_COST_PER_1K_INPUT` and `TOKEN_COST_PER_1K_OUTPUT` are defined in `config.py` but unused. The `TaskResult` response includes raw token counts but no cost estimate. Cached tasks report 0 tokens (they bypass the LLM entirely), making aggregate cost accounting inaccurate.

**Impact:** Operators cannot attribute LLM costs to tenants. Billing, showback, and quota enforcement are not possible without an external reconstruction step.

**Recommended fix:** Compute `estimated_cost = (prompt_tokens / 1000 * TOKEN_COST_PER_1K_INPUT) + (completion_tokens / 1000 * TOKEN_COST_PER_1K_OUTPUT)` and include it in `TaskResult`. For cached responses, report the tokens and cost of the original execution (or zero with a `cache_hit: true` flag so callers can distinguish). Emit `token_cost_total` as a Prometheus counter labelled by `tenant_id`.
