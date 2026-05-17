# AI Platform

A multi-tenant agent execution service that accepts task descriptions, runs them through a plan-execute-summarise pipeline backed by an LLM, and returns structured results.

## Services

| Service                 | Port | Description                                |
| ----------------------- | ---- | ------------------------------------------ |
| Agent Execution Service | 8080 | HTTP API for submitting and querying tasks |
| Mock LLM Server         | 8081 | Simulated inference endpoint               |

## Quickstart

```bash
docker compose up --build
```

Then submit a task:

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_description": "Summarise Q3 revenue", "tenant_id": "acme", "priority": "normal"}'
```

Retrieve it by ID:

```bash
curl http://localhost:8080/tasks/<task_id>
```

## API

### `POST /tasks`

| Field              | Type   | Required | Description                            |
| ------------------ | ------ | -------- | -------------------------------------- |
| `task_description` | string | yes      | Natural language task                  |
| `tenant_id`        | string | yes      | Tenant identifier                      |
| `priority`         | string | no       | `urgent`, `normal` (default), or `low` |

### `GET /tasks/{task_id}`

Returns the task result. `status` is one of `pending`, `running`, `completed`, or `failed`.

### `GET /health`

Returns `{"status": "ok"}`.

## Environment Variables

| Variable         | Default                | Description                 |
| ---------------- | ---------------------- | --------------------------- |
| `LLM_SERVER_URL` | `http://mock-llm:8081` | Inference endpoint base URL |
