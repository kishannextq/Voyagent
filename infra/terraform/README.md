# Voyagent infrastructure (Terraform)

**Status: written, validated and security-scanned in CI; never applied.** Applying creates
billable AWS resources and needs explicit approval, an AWS account, a domain and an ACM certificate.

```
envs/main.tf           one root for staging and production (-var-file / -backend-config)
modules/network        VPC, public/app/data subnets, NAT, VPC endpoints, flow logs, app security group
modules/secrets        KMS key (with key policy), Secrets Manager: generated service secrets + empty provider keys
modules/data           RDS PostgreSQL 17 (pgvector, TLS-only, managed master password, backups,
                       enhanced monitoring) and ElastiCache Valkey (TLS, auth token)
modules/auth           Cognito user pool, Hosted UI, PKCE web client, admin/member groups
modules/edge           ALB (HTTPS, TLS 1.3 policy, 330 s idle timeout for SSE) + WAF (managed rules,
                       per-IP rate limit, logging)
modules/services       ECR (immutable, scan on push), ECS Fargate: web, api, orchestrator, gateway,
                       ingest worker; Service Connect; per-service task roles; circuit-breaker
                       rollback; CPU autoscaling; migrate task; daily maintenance schedule
modules/ci_oidc        GitHub OIDC deploy role scoped to one repo + environment
```

## First-time setup (operator)

1. Create the state bucket (versioned, KMS, public access blocked, access limited to ops/deploy).
2. `cp staging.tfvars.example staging.tfvars`, `cp staging.backend.hcl.example staging.backend.hcl`, fill in.
3. ```
   cd envs
   terraform init -backend-config=staging.backend.hcl
   terraform plan -var-file=staging.tfvars -out=plan.tfplan     # review
   terraform apply plan.tfplan                                    # needs approval
   ```
4. Put real provider keys into the `voyagent-staging/<NAME>-*` secrets (they start as `SET-ME`,
   which the app treats as "not configured").
5. Bootstrap the gateway (`services/model-gateway/bootstrap.py`) and store the virtual key in
   `LLM_GATEWAY_KEY`.
6. Configure the GitHub environment (`staging`) with required reviewers and the variables listed
   in `.github/workflows/deploy.yml`; deploy with the **Deploy** workflow.

## Checks (CI `infra` job)

`terraform fmt -check`, `terraform validate`, checkov (0 failed; justified skips are inline as
`#checkov:skip=...:reason`), shellcheck on `infra/deploy/deploy.sh`, actionlint on workflows.

## Known gaps

- Pre-token-generation Lambda for the tenant claim (until then each user is their own tenant).
- Separate least-privilege database role for the app (it uses the RDS master user today).
- Secret rotation for generated secrets; cache auth token is in Terraform state (encrypted bucket).
- Managed Prometheus/Grafana (or the self-hosted stack) and alert routing in AWS.
- CloudFront in front of the ALB; S3 for document originals; SQS for the ingest queue.
- Malware-scanner service for uploads (`RAG_REQUIRE_SCAN=true` is set, so uploads are refused
  until a scanner URL is configured).
