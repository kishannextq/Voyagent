# API Contracts

## Current contract (implemented, pinned by `services/api/tests/test_api_mock.py`)

Base URL: `NEXT_PUBLIC_API_URL`. No authentication yet (see SECURITY.md F2).

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/health` | — | `{status, mock_mode, modes}` |
| GET | `/api/agents` | — | static agent list |
| POST | `/api/plan` | `PlanRequest` | `PlanResponse` (blocking) |
| POST | `/api/plan/stream` | `PlanRequest` | `text/event-stream` |
| GET | `/api/plan/{thread_id}` | — | `PlanResponse` · 404 if unknown |
| POST | `/api/plan/{thread_id}/approve` | `ApprovalRequest` | `PlanResponse` · 409 if not awaiting approval |
| POST | `/api/plan/{thread_id}/approve/stream` | `ApprovalRequest` | `text/event-stream` · 409 |

```jsonc
// PlanRequest
{ "query": "string (3–2000)", "user_id": "string", "thread_id": "string|null" }
// ApprovalRequest
{ "approved": true, "feedback": "string (≤2000)" }
// PlanResponse
{ "thread_id": "…", "status": "awaiting_approval|completed|blocked",
  "state": { /* public TravelState fields */ }, "interrupt": {…}|null,
  "modes": {"llm":"live|mock", …}, "fallback": {"service","message"}|null, "dummy_nodes": {…} }
```

Request bodies reject unknown fields (`422`). Provider keys are server-side only (ARCHITECTURE.md D10);
422 responses list `type`, `loc`, `msg` and never echo submitted values.

### SSE events (order)

| Event | Data | When |
|---|---|---|
| `start` | `{thread_id, modes}` | first frame |
| `node` | `{node, data, dummy}` | each completed graph node |
| `fallback` | `{service, message}` | a live call failed; run continues on dummy data (once) |
| `interrupt` | `{node:"human_approval", data:{question, draft_itinerary, approval_request}}` | paused for review |
| `done` | `PlanResponse` | last frame on success |
| `error` | `{message}` (secrets redacted) | last frame on failure |

## v1 target (planned, Phase 2)

- Prefix `/api/v1`; `Authorization: Bearer <Cognito access token>` (or httpOnly session cookie via web BFF).
- `user_id` and `tenant_id` come from the token, never the body.
- `thread_id` server-generated (UUIDv7), stored with owner; access checked on every read/resume.
- Errors as `application/problem+json`: `{type, title, status, detail, trace_id}`.
- SSE frames gain `id:` for resumption and a periodic `: keepalive` comment (≤15 s) for proxies.
- `GET /api/v1/plans` (paginated, own plans only), `DELETE /api/v1/plans/{id}`.
- Response header `traceparent` echoed for client-side correlation.
