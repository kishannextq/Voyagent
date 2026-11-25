# API Contracts

## Current contract (implemented, pinned by `services/api/tests/test_api_mock.py`)

Base URL: `NEXT_PUBLIC_API_URL`. No authentication yet (see SECURITY.md F2).

All routes live under `/api/v1` (the web app uses these). The same handlers are still served at the
unversioned `/api/...` paths for older web builds; those responses carry `Deprecation: true` and
`Link: </api/v1/...>; rel="successor-version"` and are left out of the OpenAPI schema.

| Method | Path | Body | Response | Limits |
|---|---|---|---|---|
| GET | `/api/v1/health` | — | `{status, mock_mode, modes}` | none |
| GET | `/api/v1/agents` | — | static agent list | requests |
| POST | `/api/v1/plan` | `PlanRequest` | `PlanResponse` (waits for the result) | requests + runs |
| POST | `/api/v1/plan/stream` | `PlanRequest` | `text/event-stream` | requests + runs |
| GET | `/api/v1/plan/{thread_id}` | — | `PlanResponse` · 404 if unknown | requests |
| POST | `/api/v1/plan/{thread_id}/approve` | `ApprovalRequest` | `PlanResponse` · 409 if not awaiting approval | requests + runs |
| POST | `/api/v1/plan/{thread_id}/approve/stream` | `ApprovalRequest` | `text/event-stream` · 409 | requests + runs |

Common errors:

| Status | When |
|---|---|
| 413 | body larger than `MAX_BODY_BYTES` (default 64 KiB), by `Content-Length` or streamed |
| 422 | validation failed, including unknown fields (values never echoed) |
| 429 + `Retry-After` | per-client limit hit: `RATE_LIMIT_REQUESTS_PER_MINUTE` (default 120) or `RATE_LIMIT_RUNS_PER_MINUTE` (default 10) |
| 503 + `Retry-After` | this process is already running `MAX_CONCURRENT_RUNS` plans |

Clients are identified by IP until authentication lands. `Retry-After` and `X-Request-ID` are
exposed to browsers via CORS.

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

- Prefix `/api/v1` [done]; `Authorization: Bearer <Cognito access token>` (or httpOnly session cookie via web BFF).
- `user_id` and `tenant_id` come from the token, never the body.
- `thread_id` server-generated (UUIDv7), stored with owner; access checked on every read/resume.
- Errors as `application/problem+json`: `{type, title, status, detail, trace_id}`.
- SSE frames gain `id:` for resumption and a periodic `: keepalive` comment (≤15 s) for proxies.
- `GET /api/v1/plans` (paginated, own plans only), `DELETE /api/v1/plans/{id}`.
- Response header `traceparent` echoed for client-side correlation.
