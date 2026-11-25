# Production readiness review

Date: 2026-11-25. Scope: everything on the stacked branches up to `feature/integration-testing`
(PRs #1–#11 on the fork). Evidence is CI on GitHub Actions unless stated otherwise.

**Verdict: not ready for production yet.** The application and its delivery pipeline are built
and tested; the blockers below are mostly things that can only be done with an AWS account and
real provider credentials, plus a few engineering gaps. Nothing has been deployed.

## Blockers (must close before production)

| # | Blocker | Why | Owner action |
|---|---|---|---|
| B1 | No AWS environment: Terraform never planned or applied; deploy workflow never run | Infra, IAM, networking, Cognito, WAF are unverified against AWS | Approve a staging account; `terraform plan/apply`; first deploy |
| B2 | Never run with real models/tools | All runs used mock mode; live Groq/Tavily/AviationStack/OpenWeather paths are only unit-tested | Provide keys in staging Secrets Manager; run e2e + load in live mode |
| B3 | Leaked `VERCEL_OIDC_TOKEN` in the public `backend` branch history (F1) | Credential exposure | Rotate the token; decide on history rewrite |
| B4 | Live Vercel site (`frontend` branch) runs Next.js 14 with critical advisories (F14) | Exploitable today | Ship `apps/web` (Next.js 15) or patch the old branch |
| B5 | Prompt-injection defences are partial (F5): deterministic rails catch 2/15 held-out attacks | Model can still be steered; structural controls limit the damage | Enable a model-based classifier (Bedrock Guardrails prompt-attack filter) |
| B6 | Uploads need a malware scanner (`RAG_REQUIRE_SCAN=true` refuses uploads without one) | Document upload is disabled in AWS until then | Deploy a scanner (e.g. ClamAV service) and set `MALWARE_SCAN_URL` |

## Verified (with evidence)

| Area | What | Evidence |
|---|---|---|
| Functional | Plan → review → approve/revise, guardrail block, injection refusal | 226 API tests; Playwright e2e (8 tests) on real web builds |
| Auth | PKCE sign-in, bearer tokens, ownership (owner/tenant admin), 401/404 semantics | 26 auth tests (locally signed RS256 tokens); browser run against `tools/mock-oidc` |
| Accessibility | WCAG 2.1 A/AA (axe) on home, review, final views: 0 violations | e2e `a11y.spec.ts` |
| Data | Migrations, ownership, memory retention, RAG schema with pgvector | Postgres integration tests in CI |
| Model gateway | Fallback, alias routing, virtual-key scoping, per-user spend + budget enforcement | CI `gateway` job with the pinned LiteLLM image |
| RAG | Tenant isolation, versioning, dedupe, hybrid retrieval quality gate | `rag-eval` job (bge-small); `docs/RAG_EVALUATION.md` |
| Observability | Cross-service trace in Tempo, metrics in Prometheus, logs in Loki, Grafana provisioning, 9 alert rules | CI `observability` job |
| Containers | Images build, pass Trivy (fixable HIGH/CRITICAL), start, and serve a plan | CI `images` job smoke test |
| Infra code | `terraform validate`; checkov 359 passed / 0 failed / 17 justified skips; shellcheck; actionlint | CI `infra` job |
| Load (mock mode) | See below | CI `resilience` job (k6) |
| Resilience | See below | CI `resilience` job (`infra/resilience/drill.py`) |

### Load test (k6, mock mode, one api + one orchestrator container on a GitHub runner)

`MOCK_DELAY=0.1 s` per model/tool call stands in for provider latency; rate limits disabled.
Reads at a constant 50 rps for 1 min, plus 1→25 concurrent users doing plan + approve for 80 s:

| Metric | Result |
|---|---|
| Plan (to review) p50 / p95 / max | 920 ms / 1.16–1.30 s / 1.3–2.1 s (two runs) |
| Approve p95 | 506–721 ms |
| Read (health, list plans) p95 | 5–7 ms |
| Plan error rate | 0 % |
| Total throughput | ~96 requests/s |

What this means: the platform overhead (auth, limits, ownership, orchestrator hop, checkpointing,
streaming) is small next to model latency, and one orchestrator handled 25 concurrent plans with
no errors. What it doesn't mean: real capacity is bounded by provider latency and rate limits,
`MAX_CONCURRENT_RUNS` (32 per orchestrator) and gateway budgets; it must be re-measured in
staging with live models before setting autoscaling targets and SLOs.

### Resilience drill

| Failure injected | Expected | Result |
|---|---|---|
| Orchestrator killed mid-plan | Client stream ends promptly with an error event; API stays healthy (degraded); new plans 502; after restart, earlier plans are served and can be approved | Pass (13/13 drill checks) after fixes: health returned 500 and new plans 500 before; a cut stream had no error event |
| Redis stopped | Plans still complete; health OK | Pass (after making run metadata fail-soft) |
| Postgres restarted | No failed requests after recovery | Pass, 0/5 failed (pooled connections validated on checkout) |
| Backup → restore | pg_dump/pg_restore into a fresh DB; ownership rows intact; a paused plan resumes to completion on the restored copy | Pass |

## Service objectives (proposed, to confirm in staging)

| SLO | Target |
|---|---|
| API availability (non-5xx, excluding provider outages) | 99.5 % monthly |
| Read endpoints p95 | < 300 ms |
| Time to first streamed event | < 2 s p95 |
| Plan completion (to review) | 95 % succeed (incl. demo-data fallback); p95 < 60 s with live models |

Alerts for these exist (`infra/observability/prometheus/alerts.yml`); routing is not set up.

## Risk register (open)

| Risk | Impact | Mitigation in place | Next step |
|---|---|---|---|
| Prompt injection via user text, documents or tool output | Wrong/unsafe plan content | Input rails, output canary, delimited data, least-privilege tools, no secrets reachable | Model-based classifier (B5) |
| Provider outage / rate limits | Plans degrade to demo data | Gateway fallbacks + retries, per-run fallback with UI notice | Second provider (Bedrock) in the fallback chain |
| Cost runaway | Spend | Gateway virtual-key budget, per-user budget, per-run LLM-call cap, rate limits | Spend alerts per tenant |
| Tokens in `sessionStorage` (F16) | Token theft if XSS | No third-party scripts, React escaping | BFF with httpOnly cookies; CSP |
| Single-tenant claims until the Cognito trigger exists | Each user is their own tenant; org admin features limited | Ownership checks are correct either way | Pre-token-generation Lambda |
| App uses the RDS master user | Larger blast radius if the app is compromised | Network isolation, TLS, secrets in Secrets Manager | Dedicated app role with least privilege |
| Retrieval eval set is small | Quality regressions in real documents may go unnoticed | CI quality gate | Larger golden set + faithfulness judge |

## Operational readiness

- Runbooks: deploy/rollback (`infra/deploy/deploy.sh`, `docs/DEPLOYMENT.md`), migrations
  (`migrate.py`), retention (`maintenance.py`), backup/restore (drill above; RDS automated
  backups in Terraform). Incident runbooks per alert: to write.
- On-call, alert routing, status page: not set up.
- Data protection: retention defaults (plans 365 d, memory 180 d), user deletion endpoints,
  PII redaction before models/logs. A DPIA / privacy review is still needed for real users.
