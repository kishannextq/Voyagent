# Branch Strategy

## Protected, untouched branches

| Branch | Content | Policy |
|---|---|---|
| `frontend` | Original Next.js app (default branch on origin) | Read-only. No commits, merges, rebases or force-pushes. |
| `backend` (`origin/backend`) | Original FastAPI/LangGraph app | Read-only. Contains a committed `.env.local` (see SECURITY.md F1); history rewrite only with explicit owner authorization. |

## New branches

```
frontend (untouched)        origin/backend (untouched)
   │                              │  (imported via git read-tree, minus secrets)
   └──────────┬───────────────────┘
              ▼
     integration/platform  ◄──── merge (PR, CI green, review) ──── feature/*
              │
              ▼
          release/v1  (production validation; tagged releases)
```

| Branch | Cut from | Scope |
|---|---|---|
| `integration/platform` | `frontend` @ ee5dba5 | Monorepo base, docs, local dev, CI. All features integrate here. |
| `feature/backend-fastapi` | integration | API hardening: remove BYO keys, structured logging, async graph, pooled checkpointer, API v1 |
| `feature/auth-cognito` | integration | Cognito, JWT validation, RBAC, tenant model |
| `feature/model-gateway` | integration | LiteLLM proxy, routing/fallback, virtual keys, Redis cache |
| `feature/ai-orchestration` | integration | Orchestrator service, agent config (agent.md/soul.md), tool permissions, sandbox |
| `feature/rag-engine` | integration | Retrieval service, pgvector, hybrid search, reranking, evaluation |
| `feature/document-processing` | integration | Upload, validation, scanning, extraction, chunking, embedding worker |
| `feature/guardrails` | integration | Input/output rails library, Bedrock Guardrails, jailbreak suite |
| `feature/observability` | integration | OTel, Alloy, Prometheus, Loki, Tempo, Grafana, Langfuse |
| `feature/terraform-infrastructure` | integration | Terraform modules + envs (validate/plan only until authorized) |
| `feature/cicd-deployment` | integration | ECR push, ECS deploy, environments, rollback |
| `feature/integration-testing` | integration | E2E (Playwright), load tests (k6), failure injection |
| `release/v1` | integration | Release candidate stabilisation |

Rules:
- Branches are created only when work on them starts (dependency order in IMPLEMENTATION_PLAN.md).
- Rebase a feature branch onto `integration/platform` only if it has not been pushed/shared; otherwise merge.
- Every merge into `integration/platform` requires green CI. No direct pushes to `integration/platform` once it has a remote.
- Nothing is pushed to `origin` without explicit owner approval.
- Commits follow Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`...).
