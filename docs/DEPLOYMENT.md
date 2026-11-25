# Deployment

## Local development [done]

Prereqs: Docker, Node 22 LTS, [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # MOCK_MODE=true needs no provider keys
docker compose up --build         # web http://localhost:3000 · api http://localhost:8000/docs
docker compose --profile tools up pgadmin   # http://localhost:5050
```

Without Docker:
```bash
cd services/api && uv sync && MOCK_MODE=true uv run uvicorn server:api --reload --port 8000
cd apps/web && npm ci && npm run dev
```

Tests:
```bash
cd services/api && uv run ruff check . && uv run pytest -q
cd apps/web && npx tsc --noEmit && npm run build
```

## Environments

| Env | Purpose | Infra | Data |
|---|---|---|---|
| local | dev | docker compose | throwaway volumes |
| test | CI | GitHub Actions, compose services | ephemeral |
| staging | pre-prod validation | AWS account `staging` (Terraform) | synthetic |
| production | customers | AWS account `prod` (Terraform) | real, backed up |

Orchestrator: api uses `ORCHESTRATOR_URL` + `ORCHESTRATOR_TOKEN` (unset URL = in-process). Orchestrator
service: `uvicorn orchestrator_app:app --port 8001` with `ORCHESTRATOR_TOKEN`, provider keys, `DATABASE_URL`,
`REDIS_URL`; agent budgets `RUN_MAX_LLM_CALLS` (20); memory `MEMORY_ENABLED`, `MEMORY_RETENTION_DAYS` (180);
tool sandbox `SANDBOX_MAX_MEMORY_BYTES`, `SANDBOX_MAX_OPEN_FILES`, `SANDBOX_MAX_FILE_BYTES`, `SANDBOX_MAX_PROCESSES`.
Scheduled task (daily): `python maintenance.py purge` (`PLAN_RETENTION_DAYS`, default 365).

RAG: `RAG_ENABLED`, `RAG_EMBEDDER` (`gateway` in deployed envs; must match across api, orchestrator, ingest-worker),
`RAG_EMBED_MODEL`, `RAG_EMBED_DIM` (fixed at migration time), `RAG_RERANKER`, `RAG_MAX_UPLOAD_BYTES`,
`RAG_MAX_PDF_PAGES`, `MALWARE_SCAN_URL`, `RAG_REQUIRE_SCAN=true`. Worker: `python ingest_worker.py`.

Telemetry: `OTEL_EXPORTER_OTLP_ENDPOINT` (traces; unset = off), `OTEL_SERVICE_NAME`, `METRICS_PORT`
(9464; never expose publicly — scrape from inside the VPC).

Model gateway: `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` (API side; virtual key from
`services/model-gateway/bootstrap.py`), `LLM_MODEL_FAST`/`LLM_MODEL_SMART` (aliases),
`LLM_TIMEOUT_SECONDS`. Gateway side: `LITELLM_MASTER_KEY`, `LITELLM_DATABASE_URL`, `GROQ_API_KEY`,
`REDIS_HOST`/`REDIS_PORT`.

API runtime settings: `AUTH_MODE` (`cognito`, required in staging/production) with `COGNITO_REGION`,
`COGNITO_USER_POOL_ID` (or `COGNITO_ISSUER`), `COGNITO_CLIENT_IDS`, `AUTH_TENANT_CLAIM`; `APP_ENV` (`staging`/`production` require `DATABASE_URL` and `REDIS_URL`),
`FRONTEND_ORIGINS` (comma-separated, required in deployed envs), optional `FRONTEND_ORIGIN_REGEX`,
`CHECKPOINTER_SETUP=false` on services (run setup as a migration task), `PG_POOL_MAX`, `LOG_LEVEL`,
`MAX_CONCURRENT_RUNS` (per process, default 32; 503 beyond), `RUN_TIMEOUT_SECONDS` (default 300),
`MCP_CONNECT_TIMEOUT` / `MCP_CALL_TIMEOUT` (default 30 s), `MAX_BODY_BYTES` (default 64 KiB),
`RATE_LIMIT_REQUESTS_PER_MINUTE` (120), `RATE_LIMIT_RUNS_PER_MINUTE` (10), `RATE_LIMIT_ENABLED`,
`FORWARDED_ALLOW_IPS` (proxies whose `X-Forwarded-For` is trusted; image default `127.0.0.1`, set to the
ALB subnets in AWS, never `*`). Rate limits fail open if Redis errors at runtime (logged as
`rate_limit_unavailable`). Health check path: `/api/v1/health`.

The API is async (one event loop per process). psycopg's async driver does not support Windows'
default ProactorEventLoop, so run the API with `DATABASE_URL` on Linux/containers; on Windows,
leave `DATABASE_URL` unset (in-memory checkpointer) or use Docker/WSL.

Separate AWS accounts per environment (AWS Organizations). Config via env vars; secrets from
Secrets Manager; no `.env` files beyond local.

## AWS target (Terraform in `infra/terraform`, deploy workflow in `.github/workflows/deploy.yml`: written and checked in CI, never applied or run — see `infra/terraform/README.md`)

- **Images**: GitHub Actions (OIDC role, no long-lived AWS keys) → build → Trivy → push to ECR (immutable tags = git SHA).
- **Compute**: ECS Fargate service per deployable, private subnets, autoscaling on CPU + request count (+ queue depth for workers).
- **Ingress**: Route 53 → CloudFront (+WAF, ACM cert in us-east-1) → ALB (ACM regional cert) → target groups. ALB idle timeout raised for SSE.
- **Migrations**: `python migrate.py` as a one-off ECS task with the new image before the service update.
- **Rollout**: ECS rolling deploy with deployment circuit breaker + automatic rollback; health-check gated; migrations as a pre-deploy one-off task.
- **Rollback**: redeploy previous task definition revision (`aws ecs update-service --task-definition <prev>`); DB migrations are backward-compatible (expand/contract).
- **State**: Terraform state in S3 (versioned, KMS) with native S3 lockfile; one state per env.
- **DNS/domain**: domain purchase, certificates and Cloudflare (if used) require owner action.

## Legacy hosting

The original deployment (Vercel for `frontend`, Render for `backend`) is documented in
`apps/web/README.md` and continues to run from the untouched original branches.
