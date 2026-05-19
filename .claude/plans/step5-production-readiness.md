# Step 5: Write Production Readiness Doc

## Goal

Write `PRODUCTION.md` at the project root — a document for an SRE or infrastructure engineer covering SLOs, alerting, and what needs to change before this service can run on GCP/Kubernetes.

Do NOT modify any source files in this step.

---

## Inputs

- `DIAGNOSIS.md` — after-fix numbers (P50/P95/P99 latency, error rate, tool stage duration)
- `src/config.py` — configuration constants (timeouts, rate limits, cost per token)
- `src/main.py`, `src/orchestrator.py`, `src/llm_client.py` — current in-memory state and concurrency model
- `src/telemetry.py` — which Prometheus metrics are already instrumented

---

## Document Sections

1. **Service Overview** — one paragraph: what this service does and its current deployment model

2. **SLIs / SLOs** — at least 5 SLIs (availability, P95 latency, P99 latency, cache hit rate, semaphore utilisation). Each entry: metric name, PromQL expression, SLO target, and one-line justification grounded in the load-test numbers from `DIAGNOSIS.md`

3. **Alerting Rules** — 5 Prometheus alert rules in copy-pasteable YAML:
   - High error rate
   - P95 latency breach
   - Semaphore saturation
   - LLM retry storm
   - High memory usage

   Each rule has `summary` and `description` annotations, plus a note on what to investigate first.

4. **GCP / Kubernetes Changes** — what needs to change and why, covering:
   - Persistent task store (replace in-memory `TTLCache` → Redis / Cloud Spanner)
   - Distributed response cache (same issue, different dict)
   - Distributed rate limiting (per-process token bucket breaks under multiple replicas)
   - Health probes (`/health` liveness, add `/ready` readiness)
   - Secrets management (Secret Manager instead of plain env vars)
   - Horizontal scaling (HPA config, implication of per-process semaphore)
   - PodDisruptionBudgets
   - Resource requests/limits (based on observed container stats)

5. **Known Remaining Gaps** — issues diagnosed but not yet fixed: priority scheduling, per-tenant rate limiting, cache key excludes priority, token cost not surfaced in responses

---

## Acceptance Criteria

- [ ] `PRODUCTION.md` exists at the project root
- [ ] All 5 SLIs have PromQL expressions and targets justified by load-test data
- [ ] All 5 alert rules are valid YAML with annotations
- [ ] All 8 GCP/K8s items are covered
- [ ] No source files modified
