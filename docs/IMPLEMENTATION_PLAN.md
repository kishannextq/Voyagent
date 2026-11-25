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
- [x] `docker compose up` end to end: verified in CI (observability job runs postgres, redis, migrate, orchestrator, api, ingest-worker and the telemetry stack with real traffic); the web container is smoke-tested separately. Still not run on the dev machine (Docker Desktop/WSL unresponsive)
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
- [x] LiteLLM v1.108.5 (digest-pinned) in compose (`--profile gateway`) + config; API calls models only through it when `LLM_GATEWAY_URL` is set (Bedrock configured but not enabled: needs AWS)
- [x] Routing by task (`fast`/`smart` aliases), fallback chain, timeouts, capped retries (gateway only)
- [x] Virtual key for the API scoped to the aliases with budget + RPM; per-end-user (tenant:sub) spend and budgets; Redis response cache; spend in the gateway's Postgres DB — CI `gateway` job with the real image
- [ ] Per-tenant virtual keys (one key per tenant) once tenants are provisioned objects
- [x] Supply-chain cooldown for Python deps: `uv` `exclude-newer` (releases ≥2 weeks old)

## Phase 4 — Orchestration & agents  (`feature/ai-orchestration`)
- [x] `orchestrator` service (`orchestrator_app.py`, same image, internal only, service token); api relays its SSE stream and holds no provider keys; in-process mode for local/tests — 9 tests + live two-process browser run with request IDs shared across services
- [x] Agent config: `agents/soul.md` + `agents/<node>.md` (model, tool allow-list, per-node LLM-call budget and timeout), validated at startup and enforced at runtime; per-run LLM-call budget — 9 tests
- [ ] Token budgets per run (needs real usage metadata; gateway enforces spend budgets meanwhile)
- [x] Tool processes under OS resource limits (`sandbox_exec.py`) + minimal env; no tool executes host code — real servers spawned in the sandbox in CI
- [ ] Stronger tool isolation (separate task/microVM with egress allow-list) — Phase 8
- [ ] Step Functions for long workflows: not needed yet (runs are bounded at minutes and stream synchronously); revisit if plans need durable waits beyond the SSE window
- [x] Long-term memory: allow-listed preferences from approved plans, 180-day expiry, `GET/DELETE /api/v1/me/memory`, opt-out flag; `DELETE /api/v1/plans/{id}`; `maintenance.py purge` (expired memory + plans older than `PLAN_RETENTION_DAYS`) — 7 tests incl. Postgres

## Phase 5 — RAG  (`feature/rag-engine`, `feature/document-processing`)
- [x] Ingestion: upload API (tenant admins), content-based type detection (PDF/Markdown/text), size/page limits, malware-scan hook (`MALWARE_SCAN_URL`, `RAG_REQUIRE_SCAN`), extraction, sha256 dedupe, title versioning (older versions superseded), status pipeline; Redis queue + `ingest_worker.py` (in-process locally)
- [ ] S3 object storage for originals (bytes in Postgres ≤5 MiB for now); OCR for scanned PDFs; SQS instead of Redis in AWS
- [x] Chunking strategies benchmarked (fixed-400, fixed-1000, parent-2000/child-400); parent-child retrieval — `docs/RAG_EVALUATION.md`
- [x] Embedding abstraction with model id stored per vector: `gateway` (deployed; Bedrock/Cohere alias in LiteLLM, **not verified**), `fastembed` bge-small (local/CI eval), `hash` (tests)
- [x] Hybrid retrieval (pgvector HNSW + Postgres full-text, RRF), mandatory tenant filter, optional cross-encoder rerank, parent expansion, citations [n]; budget + itinerary agents use it via allow-listed `rag.search`
- [x] Evaluation harness: Recall@K, Precision@K, MRR, NDCG on a golden set; citation-support check; CI `rag-eval` job with the semantic embedder and a quality gate
- [ ] Model-graded faithfulness judge; larger, harder golden set (current one saturates)

## Phase 6 — Guardrails  (`feature/guardrails`)
- [x] Rails library + policy file (`guardrails/policy.yaml`, validated at startup): input rails before any model call (injection/jailbreak/prompt-leak/exfiltration/code-exec/role-tag/delimiter patterns on normalised text incl. FR/ES/PT/IT/DE; PII redaction email/card(Luhn)/IBAN/passport/phone); output rails on every model reply (system-prompt canary → withheld, PII, unsafe link schemes, length); tool outputs delimited as data
- [x] Jailbreak/prompt-injection regression suite in CI: 40 known attacks blocked, 0/30 benign false positives (tuned set); held-out set 2/15 blocked, 0/15 false positives (generalisation floor)
- [ ] Model-based injection classifier (e.g. Bedrock Guardrails prompt-attack filter via the gateway, config stubbed) — the deterministic rails generalise poorly (held-out 13%)
- [ ] Bedrock Guardrails enabled (needs AWS)

## Phase 7 — Observability  (`feature/observability`)
- [x] OTel traces api → orchestrator (spans for runs, agents, model/tool calls, document search; traceparent injected); Prometheus metrics on an internal port; trace ids in logs — 4 tests
- [x] Collector, Prometheus, Loki, Tempo, Grafana in compose (`--profile observability`), provisioned datasources + dashboard, 9 alert rules (promtool-checked) — CI `observability` job verifies a cross-service trace in Tempo, metrics in Prometheus, logs in Loki and Grafana provisioning
- [ ] Browser and gateway spans; Langfuse (needs a data-retention decision); token/cost metrics; alert routing

## Phase 8 — Infrastructure & delivery  (`feature/terraform-infrastructure`, `feature/cicd-deployment`)
- [x] Terraform modules (network, secrets/KMS, data: RDS pgvector + Valkey, Cognito, ALB + WAF, ECS services, GitHub OIDC) with staging/production via tfvars — `terraform validate` + checkov (359 passed, 0 failed, 17 justified skips) in CI. **Never applied; `plan` not run (no AWS account)**
- [x] Deploy pipeline: manual workflow per protected environment (OIDC, no stored keys), build → Trivy → push immutable SHA tags → migrate task → rolling ECS deploy with circuit breaker → smoke test → automatic rollback (`infra/deploy/deploy.sh`) — shellcheck + actionlint in CI. **Not executed**
- [ ] Known infra gaps: tenant-claim Lambda, app DB role, secret rotation, CloudFront, S3/SQS for documents, upload scanner, alert routing (`infra/terraform/README.md`)

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
