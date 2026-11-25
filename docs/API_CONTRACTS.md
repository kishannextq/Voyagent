# API Contracts

## Current contract (implemented, pinned by `services/api/tests/test_api_mock.py`)

Base URL: `NEXT_PUBLIC_API_URL`.

**Authentication** (`AUTH_MODE=cognito`): every route except `health` and `agents` needs
`Authorization: Bearer <Cognito access token>`, otherwise `401` with `WWW-Authenticate: Bearer`
(no reason given). Identity (`sub`, tenant, roles from Cognito groups) comes only from the token;
a `user_id` in the body is ignored. Plans are visible to their owner and to `admin`s of the same
tenant; anything else returns `404`, the same as an unknown ID. `AUTH_MODE=disabled` (local/test
only) uses a fixed local user.

All routes live under `/api/v1` (the web app uses these). The same handlers are still served at the
unversioned `/api/...` paths for older web builds; those responses carry `Deprecation: true` and
`Link: </api/v1/...>; rel="successor-version"` and are left out of the OpenAPI schema.

| Method | Path | Body | Response | Limits |
|---|---|---|---|---|
| GET | `/api/v1/health` | — | `{status, mock_mode, modes}` | none |
| GET | `/api/v1/agents` | — | static agent list | requests |
| POST | `/api/v1/plan` | `PlanRequest` | `PlanResponse` (waits for the result) | requests + runs |
| POST | `/api/v1/plan/stream` | `PlanRequest` | `text/event-stream` | requests + runs |
| GET | `/api/v1/plans?limit=20&before=<ts>&all_users=false` | — | `{items:[{thread_id, created_at, owner}], next_before}` newest first; `all_users` needs `admin` (403) | requests |
| GET | `/api/v1/plan/{thread_id}` | — | `PlanResponse` · 404 if unknown or not yours | requests |
| DELETE | `/api/v1/plans/{thread_id}` | — | 204; deletes checkpoints, run metadata, ownership · 404 if not yours | requests |
| GET | `/api/v1/me/memory` | — | `{enabled, preferences:{home_city?,travel_style?,budget_level?,last_destination?}, retention_days}` | requests |
| DELETE | `/api/v1/me/memory` | — | 204; forgets all preferences | requests |
| POST | `/api/v1/plan/{thread_id}/approve` | `ApprovalRequest` | `PlanResponse` · 409 if not awaiting approval | requests + runs |
| POST | `/api/v1/plan/{thread_id}/approve/stream` | `ApprovalRequest` | `text/event-stream` · 409 | requests + runs |

Common errors:

| Status | When |
|---|---|
| 413 | body larger than `MAX_BODY_BYTES` (default 64 KiB), by `Content-Length` or streamed |
| 422 | validation failed, including unknown fields (values never echoed) |
| 429 + `Retry-After` | per-client limit hit: `RATE_LIMIT_REQUESTS_PER_MINUTE` (default 120) or `RATE_LIMIT_RUNS_PER_MINUTE` (default 10) |
| 503 + `Retry-After` | this process is already running `MAX_CONCURRENT_RUNS` plans |

Limits key on the authenticated user (tenant + sub); unauthenticated routes key on the client IP. `Retry-After` and `X-Request-ID` are
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

- Prefix `/api/v1` [done]; bearer Cognito access token [done]; httpOnly session cookie via a web BFF [planned].
- `user_id` and `tenant_id` come from the token, never the body [done].
- `thread_id` server-generated, stored with owner; access checked on every read/resume [done].
- Errors as `application/problem+json`: `{type, title, status, detail, trace_id}`.
- SSE frames gain `id:` for resumption and a periodic `: keepalive` comment (≤15 s) for proxies.
- `GET /api/v1/plans`, `DELETE /api/v1/plans/{id}` [done].
- Response header `traceparent` echoed for client-side correlation.
