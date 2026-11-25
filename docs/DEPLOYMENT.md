# Deployment

## Local development [done]

Prereqs: Docker, Node 20+, [uv](https://docs.astral.sh/uv/).

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

API runtime settings: `APP_ENV` (`staging`/`production` require `DATABASE_URL` and `REDIS_URL`),
`FRONTEND_ORIGINS` (comma-separated, required in deployed envs), optional `FRONTEND_ORIGIN_REGEX`,
`CHECKPOINTER_SETUP=false` on services (run setup as a migration task), `PG_POOL_MAX`, `LOG_LEVEL`.

Separate AWS accounts per environment (AWS Organizations). Config via env vars; secrets from
Secrets Manager; no `.env` files beyond local.

## AWS target (planned — nothing is provisioned without explicit authorization)

- **Images**: GitHub Actions (OIDC role, no long-lived AWS keys) → build → Trivy → push to ECR (immutable tags = git SHA).
- **Compute**: ECS Fargate service per deployable, private subnets, autoscaling on CPU + request count (+ queue depth for workers).
- **Ingress**: Route 53 → CloudFront (+WAF, ACM cert in us-east-1) → ALB (ACM regional cert) → target groups. ALB idle timeout raised for SSE.
- **Rollout**: ECS rolling deploy with deployment circuit breaker + automatic rollback; health-check gated; migrations as a pre-deploy one-off task.
- **Rollback**: redeploy previous task definition revision (`aws ecs update-service --task-definition <prev>`); DB migrations are backward-compatible (expand/contract).
- **State**: Terraform state in S3 (versioned, KMS) with native S3 lockfile; one state per env.
- **DNS/domain**: domain purchase, certificates and Cloudflare (if used) require owner action.

## Legacy hosting

The original deployment (Vercel for `frontend`, Render for `backend`) is documented in
`apps/web/README.md` and continues to run from the untouched original branches.
