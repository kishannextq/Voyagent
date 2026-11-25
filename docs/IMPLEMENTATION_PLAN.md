# Implementation Plan & Checklist

Updated continuously. `[x]` = implemented and verified (how is noted); `[ ]` = not done.

## Phase 1 — Audit, architecture, foundation  (`integration/platform`)
- [x] Repository and branch audit; existing branches left untouched
- [x] Monorepo: `apps/web` (from `frontend`), `services/api` (from `origin/backend`, secrets excluded)
- [x] Line endings normalized (separate, content-neutral commit)
- [x] API deps trimmed 110 pins → 13 direct deps, locked with `uv.lock`
- [x] API characterization tests, mock mode (5 tests: health, validation, full SSE plan→approve, guardrail block, 404)
- [x] Ruff lint clean; fixed HTTP calls without timeouts in weather MCP server
- [x] Dockerfiles (non-root, healthchecks) for api and web; Next.js standalone output + security headers
- [x] docker-compose: postgres(pgvector), redis, api, web, pgAdmin (profile) — config validated; api + standalone web smoke-tested outside Docker
- [x] Images build in CI and pass Trivy (api: clean of fixable HIGH/CRITICAL; web: Next.js 14 findings fixed by the upgrade, F14). OS upgrades applied, pip/npm removed from runtime images, Node 22 LTS
- [ ] Run `docker compose up` end to end (blocked locally: Docker Desktop/WSL unresponsive on dev machine)
- [x] GitHub Actions CI: typecheck/build, lint/test, gitleaks, image build + Trivy — running on the fork `kishannextq/Voyagent` (owner repo is read-only for this account). First run: api green; gitleaks flagged fake test fixtures (fixed); `npm audit` failed on Next.js 14 advisories (F14, fixed on `feature/frontend-nextjs`)
- [x] Design docs (this folder)

## Phase 2 — API hardening  (`feature/backend-fastapi`, `feature/auth-cognito`)
- [x] Remove "bring your own keys" end to end: web dialog/storage removed, stale keys wiped; API forbids unknown fields; `/api/health` no longer reveals key presence; 422s don't echo input; query/feedback length caps (10 API tests)
- [x] Structured JSON logging with redaction + request IDs; all `print()` removed (F4) — 9 logging tests
- [x] Run metadata in Redis with TTL; bounded in-memory fallback local/test only; `APP_ENV` policy (F6)
- [x] 128-bit server thread IDs without user ID; client IDs validated, 409 on existing thread (F2, partial)
- [x] Pooled Postgres checkpointer; unreachable DB fails startup; memory only for unset URL in local/test (F10) — Postgres integration test (6 concurrent runs + resume from a second instance) passed in CI
- [x] Async graph execution on the event loop (async nodes, `astream`, `AsyncPostgresSaver`, async Redis); one persistent MCP session per service instead of a subprocess per call; execution bounds: `MAX_CONCURRENT_RUNS` (503 when full), `RUN_TIMEOUT_SECONDS`, recursion limit (F7) — 7 tests incl. real-subprocess reuse proof; live uvicorn plan→approve smoke test
- [x] MCP subprocess gets a minimal env (F11); Tavily key in Authorization header (F12, live call unverified)
- [x] `/api/v1` versioned routes; web migrated; unversioned aliases kept with `Deprecation`/`Link` headers, hidden from OpenAPI
- [x] Cognito access-token validation (JWKS, RS256), roles from groups (admin/member), tenant from claim; plan ownership in Postgres; owner/tenant-admin access, 404 otherwise; `GET /plans`; limits key on user (F2) — 26 auth tests; web sign-in (Hosted UI, PKCE) verified end to end against `tools/mock-oidc` in headless Edge. **Not yet verified against a real Cognito pool** (needs AWS; Terraform in Phase 8)
- [x] Strict CORS (F8)
- [x] Request body size limit (ASGI, 413, incl. chunked bodies); per-client rate limits in Redis with in-memory fallback local/test (429 + `Retry-After`); shared Redis client; trusted-proxy fix for `X-Forwarded-For` (F15) — 14 tests
- [ ] Edge limits in AWS: WAF rate-based rules + ALB/CloudFront body limits (Phase 8)
- [x] Alembic migrations (`app.threads`), `migrate.py` one-off task (schema + checkpointer), compose `migrate` service; services fail fast on an unmigrated DB — Postgres tests in CI
- [x] `apps/web` upgraded to Next.js 15.5.29 + React 19 (F14) on `feature/frontend-nextjs`: `npm audit --omit=dev` clean; tsc + build; headless-Edge UI run plan→approve against the mock API with no console errors
- [ ] Next.js 16 upgrade (15.x is on security backports; plan before 15 support ends)

## Phase 3 — Model gateway  (`feature/model-gateway`)
- [ ] LiteLLM proxy in compose + config (Groq, Bedrock); orchestrator calls go through it
- [ ] Routing policy (task, latency, context, cost), fallback chain, timeouts, retries with caps
- [ ] Virtual keys per tenant, budgets, Redis cache, spend logs to Postgres

## Phase 4 — Orchestration & agents  (`feature/ai-orchestration`)
- [ ] Extract `orchestrator` service; api relays its stream
- [ ] Agent config: `agent.md` / `soul.md`, capabilities, tool allow-lists, step/time/token budgets
- [ ] Sandboxed tool execution (no host code execution); Step Functions for long workflows
- [ ] Long-term memory with retention/deletion policy

## Phase 5 — RAG  (`feature/rag-engine`, `feature/document-processing`)
- [ ] Ingestion: S3 upload, validation, malware scan hook, extraction/OCR, dedupe, versioning, status
- [ ] Chunking strategies benchmarked; parent-child retrieval
- [ ] Embedding abstraction (Bedrock Titan/Cohere, HF) with versioning
- [ ] Hybrid retrieval (pgvector + full-text), tenant filters, reranking, citations
- [ ] Evaluation harness: Recall@K, Precision@K, MRR, NDCG, faithfulness; golden dataset

## Phase 6 — Guardrails  (`feature/guardrails`)
- [ ] Input/output rails library; Bedrock Guardrails; dialogue rails config
- [ ] Jailbreak/prompt-injection regression suite in CI

## Phase 7 — Observability  (`feature/observability`)
- [ ] OTel traces/metrics/logs across web → api → orchestrator → gateway
- [ ] Alloy, Prometheus, Loki, Tempo, Grafana dashboards + alerts; Langfuse

## Phase 8 — Infrastructure & delivery  (`feature/terraform-infrastructure`, `feature/cicd-deployment`)
- [ ] Terraform modules; `validate`/`plan` for staging (no apply without authorization)
- [ ] ECR/ECS deploy pipeline with health-gated rollout and rollback

## Phase 9 — Integration & readiness  (`feature/integration-testing`, `release/v1`)
- [ ] Playwright E2E, accessibility checks
- [ ] k6 load tests; documented measured capacity and bottlenecks
- [ ] Failure injection, backup/restore drill, production-readiness review

## Dependency order

```
P1 ─► P2 ─► P3 ─► P4 ─┬─► P6 ─► P9
                      └─► P5 ─┘
P7 can start after P2.  P8 can start after P1 (plan-only) and gates P9.
```
