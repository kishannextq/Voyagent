# Security

## Findings register

Found during the Phase 1 audit. Status is updated as fixes land.

| ID | Severity | Finding | Location | Status |
|---|---|---|---|---|
| F1 | Critical | `VERCEL_OIDC_TOKEN` committed in `.env.local`. **The GitHub repository is public** (README wrongly says private), so treat it as disclosed | `origin/backend` commit `3475a8d` | Excluded from monorepo. **Owner must rotate.** History rewrite needs force-push to `backend` (not authorized). |
| F2 | High | No authentication; `user_id` client-supplied; thread IDs `{user_id}_{8 hex}` are guessable → any plan readable via `GET /api/plan/{id}` | `server.py` | **Partly fixed**: server IDs are 128-bit random with no user ID; client IDs validated and can't target an existing thread (409). Authn/ownership checks remain → `feature/auth-cognito` |
| F3 | High | User-supplied provider keys sent from browser, optionally kept in `localStorage` | `apps/web/lib/keys.ts`, `ApiKeysDialog.tsx` | **Fixed** on `feature/backend-fastapi`: dialog and storage removed; stale browser keys wiped on load; API rejects `api_keys` (422, value not echoed) |
| F4 | High | Raw LLM output, guardrail output and user content printed to stdout | `agents.py` | **Fixed**: all `print()` removed; JSON logs carry metadata only (agent, routing, outcome), redaction filter, request IDs; test asserts no user text/model output in logs |
| F5 | Medium | Guardrail is a single LLM prompt with user text interpolated (injectable). Fails closed on parse error | `agents.py` `supervisor_agent` | Open → Phase 6 |
| F6 | Medium | Unbounded module-level dicts per thread (memory growth; breaks with >1 replica) | `server.py` | **Fixed**: `run_store.py`, Redis with TTL (shared across replicas); bounded LRU+TTL in memory for local/test only; required in staging/prod |
| F7 | Medium | Sync graph on threadpool; MCP subprocess spawned per tool call (DoS amplification) | `server.py`, `mcp_client.py` | Open → Phase 2 |
| F8 | Medium | CORS `allow_credentials=True` with `*` methods/headers | `server.py` | Open → Phase 2 |
| F9 | Low | 110 pinned deps incl. unused Streamlit/pandas/GitPython | `requirements.txt` | **Fixed** (13 direct deps, `uv.lock`) |
| F10 | Medium | Single psycopg connection shared across threads; silent fallback to in-memory if DB down | `graph.py` | **Fixed**: `psycopg_pool` pool; a configured but unreachable DB fails startup; memory only when `DATABASE_URL` unset in local/test. Postgres integration test runs in CI |
| F11 | Medium | stdio MCP servers inherit the full process environment (all secrets) | `mcp_client.py` `_server_env` | Open → Phase 2 |
| F12 | Low | Tavily key passed in URL query string (may be logged by proxies) | `mcp_client.py` | Open → Phase 2 |
| F13 | Low | Outbound HTTP calls without timeout | `weather_mcp_server.py` | **Fixed** |

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

**Supply chain** — lockfiles (npm, uv); `npm audit` + Trivy image scan in CI [done]; tfsec/checkov
on Terraform (Phase 8); Dependabot (planned).

**Network** — private subnets for services and data; security groups allow only required paths;
VPC endpoints for S3/Secrets Manager/ECR/Bedrock.

**Audit** — `audit_log` table + CloudTrail; logs exclude prompts/responses by default.

## Reporting
Report vulnerabilities privately to the repository owner; do not open public issues.
