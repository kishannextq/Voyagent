# Security

## Findings register

Found during the Phase 1 audit. Status is updated as fixes land.

| ID | Severity | Finding | Location | Status |
|---|---|---|---|---|
| F1 | Critical | `VERCEL_OIDC_TOKEN` committed in `.env.local`. **The GitHub repository is public** (README wrongly says private), so treat it as disclosed | `origin/backend` commit `3475a8d` | Excluded from monorepo. **Owner must rotate.** History rewrite needs force-push to `backend` (not authorized). |
| F2 | High | No authentication; `user_id` client-supplied; thread IDs `{user_id}_{8 hex}` are guessable → any plan readable via `GET /api/plan/{id}` | `server.py` | **Fixed** (verified with locally signed tokens and a mock OIDC provider; not yet against a real Cognito pool): Cognito access tokens verified against JWKS (RS256, issuer, expiry, `token_use`, `client_id`); identity only from the token; ownership in `app.threads`, owner or same-tenant admin only, else 404; auth cannot be disabled in staging/production; 128-bit IDs |
| F3 | High | User-supplied provider keys sent from browser, optionally kept in `localStorage` | `apps/web/lib/keys.ts`, `ApiKeysDialog.tsx` | **Fixed** on `feature/backend-fastapi`: dialog and storage removed; stale browser keys wiped on load; API rejects `api_keys` (422, value not echoed) |
| F4 | High | Raw LLM output, guardrail output and user content printed to stdout | `agents.py` | **Fixed**: all `print()` removed; JSON logs carry metadata only (agent, routing, outcome), redaction filter, request IDs; test asserts no user text/model output in logs |
| F5 | Medium | Guardrail is a single LLM prompt with user text interpolated (injectable). Fails closed on parse error | `agents.py` `supervisor_agent` | **Partly fixed**: deterministic input rails run first (400, rule id logged), output rails + system-prompt canary on every model reply, tool/document content delimited as data. Measured: 40/40 known attacks blocked, 0/30 false positives; **held-out phrasings only 2/15** — patterns don't generalise. Structural controls (no secrets reachable by the model, per-agent tool allow-lists, ownership checks in code) don't depend on wording. Remaining: a model-based classifier (Bedrock Guardrails prompt-attack filter) |
| F6 | Medium | Unbounded module-level dicts per thread (memory growth; breaks with >1 replica) | `server.py` | **Fixed**: `run_store.py`, Redis with TTL (shared across replicas); bounded LRU+TTL in memory for local/test only; required in staging/prod |
| F7 | Medium | Sync graph on threadpool; MCP subprocess spawned per tool call (DoS amplification) | `server.py`, `mcp_client.py` | **Fixed**: graph runs async on the event loop; one persistent MCP session per service (reconnects if it dies or a call times out; `MCP_CONNECT_TIMEOUT`/`MCP_CALL_TIMEOUT`); per-process run cap (503 + `Retry-After`), per-run deadline, recursion limit; per-client rate limits in Redis (429 + `Retry-After`) and a 64 KiB body limit (413). Limits key on IP until auth lands |
| F8 | Medium | CORS `allow_credentials=True` with `*` methods/headers | `server.py` | **Fixed**: no credentials, explicit methods/headers, env-only origins (no hardcoded prod domains), `*` ignored |
| F9 | Low | 110 pinned deps incl. unused Streamlit/pandas/GitPython | `requirements.txt` | **Fixed** (13 direct deps, `uv.lock`) |
| F10 | Medium | Single psycopg connection shared across threads; silent fallback to in-memory if DB down | `graph.py` | **Fixed**: `psycopg_pool` pool; a configured but unreachable DB fails startup; memory only when `DATABASE_URL` unset in local/test. Postgres integration test runs in CI |
| F11 | Medium | stdio MCP servers inherit the full process environment (all secrets) | `mcp_client.py` `_server_env` | **Fixed**: allow-listed process basics + that server's own key only; weather server no longer loads `.env`; verified by spawning the real server |
| F12 | Low | Tavily key passed in URL query string (may be logged by proxies) | `mcp_client.py` | **Fixed** (unverified against live Tavily: no key available): `Authorization: Bearer` header; URL configurable via `TAVILY_MCP_URL` |
| F13 | Low | Outbound HTTP calls without timeout | `weather_mcp_server.py` | **Fixed** |
| F15 | High | API image ran uvicorn with `--forwarded-allow-ips='*'`: any client could set `X-Forwarded-For`, spoofing its IP in logs and evading per-IP limits | `services/api/Dockerfile` | **Fixed**: trusted proxies from `FORWARDED_ALLOW_IPS` (image default `127.0.0.1`; deployments set the load balancer subnets); test rotates a spoofed header through uvicorn's proxy middleware |
| F16 | Low | Web keeps Cognito tokens in `sessionStorage`, readable by page scripts if an XSS ever lands | `apps/web/lib/auth.ts` | Open: accepted for v1 (no third-party scripts, React escaping, CSP planned); move to a BFF with httpOnly cookies |
| F14 | Critical | `apps/web` (and the live `frontend` deployment) on Next.js 14 with critical/high advisories (incl. unauthenticated RCE in image optimization on some configurations; PostCSS); `npm audit` lists no fix short of a major upgrade | `apps/web/package.json` | **Fixed** on `feature/frontend-nextjs`: Next.js 15.5.29, React 19; Next's nested PostCSS forced to 8.5.32 via npm `overrides`; `npm audit --omit=dev` reports 0. The live `frontend` branch deployment is still on 14.2.35 until the owner ships this |

## Controls (target)

**Identity & access** — Cognito user pools (OAuth 2.0 code + PKCE), JWT validated at `api` (JWKS
cached, `aud`/`iss`/`exp`/`token_use` checked); roles `viewer|member|admin|platform_admin`;
tenant scoping on every query + Postgres RLS; service-to-service short-lived JWTs.

**Edge** — CloudFront + WAF managed rules (Core, KnownBadInputs, IP reputation, rate-based);
TLS 1.2+; security headers (web: set in `next.config.js` [done]; CSP planned).

**Secrets** — AWS Secrets Manager, injected as ECS task secrets; KMS CMKs per environment;
nothing in images or git; gitleaks in CI [done].

**AI guardrails** — layered: (1) deterministic input rules (injection/jailbreak phrases, PII
detectors, size limits); (2) Bedrock Guardrails (denied topics, PII, prompt attack filter);
(3) retrieved documents and tool outputs are always wrapped as untrusted data, never as
instructions; (4) tool allow-lists per agent + argument validation; (5) output rails: secret
patterns, PII redaction, cross-tenant ID checks; (6) bounded agent loops (steps, time, tokens).
Regression suite covers e.g. "ignore your instructions", "reveal your system prompt",
"return all API keys", "show another user's documents".

**Agents and tools** — per-agent tool allow-lists, per-node timeouts and LLM-call budgets, per-run LLM-call budget (`agents/*.md`, enforced in code) [done]; stdio tool servers start under rlimits (memory, files, file size, processes, no core dumps, umask 077) with only their own key [done]; network/filesystem isolation of tools [planned, Phase 8]. Remembered preferences are allow-listed structured values, sanitised, passed to the model as quoted data, expire after 180 days, and are user-deletable [done].

**Documents (RAG)** — type from content not filename; size/page limits; optional malware-scan hook (required via `RAG_REQUIRE_SCAN` in deployed envs); uploads/deletes for tenant admins only; every query filtered by tenant (tested, incl. pgvector); retrieved text delimited and neutralised (`</documents>` look-alikes), framed as data, cited [n]; retrieval failures never break a plan [done]. OCR/macro-bearing formats not accepted.

**Infrastructure** (Terraform, not applied) — private app subnets, isolated data subnets without internet routes, VPC endpoints, flow logs; KMS everywhere (RDS, cache, secrets, logs, ECR, scheduler); RDS TLS-only with Secrets-Manager-managed password; cache TLS + auth; ALB TLS 1.3 policy + WAF (managed rules, rate limit, logging); per-service task roles (only the gateway may call Bedrock, scoped ARNs); GitHub OIDC deploy role limited to one repo/environment; immutable ECR tags with scan on push; checkov clean with justified skips.

**Service-to-service** — api → orchestrator with a shared token (`ORCHESTRATOR_TOKEN`, Secrets Manager; constant-time check; required outside local/test) plus network isolation (security groups) [done in code; SGs in Phase 8].

**Model gateway** — master key and virtual keys from Secrets Manager; API holds only a scoped virtual key; gateway not public; image pinned by digest (see MICROSERVICES.md for the version/advisory analysis) [done].

**Supply chain** — `uv` `exclude-newer` cooldown (only releases ≥2 weeks old) [done]; lockfiles (npm, uv); `npm audit` + Trivy image scan in CI [done]; tfsec/checkov
on Terraform (Phase 8); Dependabot (planned).

**Network** — private subnets for services and data; security groups allow only required paths;
VPC endpoints for S3/Secrets Manager/ECR/Bedrock.

**Audit** — `audit_log` table + CloudTrail; logs exclude prompts/responses by default.

## Reporting
Report vulnerabilities privately to the repository owner; do not open public issues.
